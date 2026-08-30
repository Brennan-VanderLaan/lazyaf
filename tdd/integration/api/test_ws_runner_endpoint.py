"""
The runner WebSocket endpoint - Phase 12.6 (Agent C).

Driven against a REAL `TestClient` WebSocket through the REAL app, the REAL
registry singleton and a REAL sqlite engine (R6): the handshake is the thing
under test, so faking the transport would test nothing.

Three contracts:

1. **The handshake matrix.** no token -> 4003; bad token -> 4003; no register
   inside REGISTRATION_TIMEOUT -> error + 4000; malformed register -> error +
   4001; an unsupported `protocol_version` -> error + 4002; a duplicate
   `runner_id` -> 4004 with the INCUMBENT untouched. And the one that is
   easy to get wrong in the other direction: a bad MID-SESSION frame gets an
   error frame and the connection STAYS OPEN, because one malformed frame
   must never kill a live step.

2. **Per-message DB sessions.** A runner connection is a multi-HOUR object.
   A session held across it pins a pooled connection for hours and ages a
   transaction snapshot into nonsense. Asserted structurally: the injected
   session factory counts opens and closes, and between messages the live
   count is zero while the open count keeps rising.

3. **The step gate** (cross-agent contract #7). Every step-scoped inbound
   frame requires BOTH `step_execution.runner_id == connection.runner_id`
   AND `runner.websocket_id == connection.websocket_id`. A frame that fails
   either half changes NO row.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Pipeline, PipelineRun, Repo, StepRun  # noqa: E402
from app.models.pipeline import (  # noqa: E402
    RunStatus,
    StepExecution,
    StepExecutionStatus,
)
from app.models.runner import Runner  # noqa: E402
from app.routers import ws_runners  # noqa: E402
from app.services.execution import runner_protocol as protocol  # noqa: E402
from app.services.execution.runner_dispatcher import runner_dispatcher  # noqa: E402
from app.services.execution.runner_registry import runner_registry  # noqa: E402
from app.services.execution.runner_state import RunnerState  # noqa: E402

GOOD_TOKEN = get_settings().runner_auth_secret


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------

class TrackingSessionFactory:
    """The injected session factory, counting what the endpoint opens.

    This is the instrument for contract 2. It is not a fake session - it
    hands out real AsyncSessions on a real engine; it only records the
    lifecycle so a session that outlives a message is visible.
    """

    def __init__(self, engine):
        self._factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        self.opened = 0
        self.closed = 0
        self.live = 0
        self.max_live = 0

    def __call__(self):
        return _TrackedSession(self)


class _TrackedSession:
    def __init__(self, tracker: TrackingSessionFactory):
        self._tracker = tracker
        self._session = tracker._factory()

    async def __aenter__(self):
        self._tracker.opened += 1
        self._tracker.live += 1
        self._tracker.max_live = max(self._tracker.max_live, self._tracker.live)
        return await self._session.__aenter__()

    async def __aexit__(self, *exc):
        self._tracker.live -= 1
        self._tracker.closed += 1
        return await self._session.__aexit__(*exc)


def _run(url: str, coro_factory):
    """Run one DB coroutine on its own short-lived engine.

    The endpoint runs in the TestClient's portal thread with its own event
    loop; a pooled connection created there cannot be reused here. NullPool
    plus a fresh engine per call keeps the two loops from ever sharing a
    DBAPI connection, which is the whole cross-loop hazard.
    """

    async def _main():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        try:
            async with factory() as db:
                return await coro_factory(db)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@pytest.fixture
def ws_env(tmp_path):
    db_path = (tmp_path / "ws_runner.db").as_posix()
    url = f"sqlite+aiosqlite:///{db_path}"

    async def _create():
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())
    asyncio.run(runner_registry.reset())
    asyncio.run(runner_dispatcher.reset())

    engine = create_async_engine(url, poolclass=NullPool)
    sessions = TrackingSessionFactory(engine)
    app.dependency_overrides[ws_runners.get_runner_session_factory] = lambda: sessions

    yield SimpleNamespace(url=url, sessions=sessions)

    app.dependency_overrides.pop(ws_runners.get_runner_session_factory, None)
    asyncio.run(runner_registry.reset())
    asyncio.run(runner_dispatcher.reset())
    asyncio.run(engine.dispose())


@pytest.fixture
def client(ws_env):
    # No `with`: entering the TestClient context runs the app's LIFESPAN,
    # which starts the dispatcher, the orphan audit and an image pre-pull.
    # websocket_connect builds its own portal, so the socket works without
    # any of that.
    return TestClient(app)


def register_frame(runner_id="pi-1", **overrides):
    frame = {
        "type": "register",
        "runner_id": runner_id,
        "name": runner_id,
        "runner_type": "generic",
        "labels": {"arch": "x86_64", "has": ["docker"]},
    }
    frame.update(overrides)
    return frame


def connect(client, token=GOOD_TOKEN, query_token=None):
    url = "/ws/runner"
    if query_token is not None:
        url += f"?token={query_token}"
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.websocket_connect(url, headers=headers)


def registered(ws, frame=None):
    ws.send_json(frame or register_frame())
    message = ws.receive_json()
    assert message["type"] == "registered", message
    return message


def roundtrip(ws):
    """Force the server to drain everything sent before this call.

    The receive loop is sequential, so a `pong` for a ping sent AFTER frame X
    proves frame X has been fully handled. Cheaper and far more reliable than
    polling the database on a timer.
    """
    ws.send_json({"type": "ping"})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        message = ws.receive_json()
        if message["type"] == "pong":
            return
    raise AssertionError("no pong within 20s")


# -----------------------------------------------------------------------------
# Contract 1: the handshake matrix
# -----------------------------------------------------------------------------

class TestHandshakeAuth:
    def test_bad_header_token_is_refused_before_accept(self, client):
        """A socket that PRESENTS a wrong secret is never accepted - the
        failure is visible in the HANDSHAKE, not one frame later.

        The tradeoff, stated: a WebSocket cannot carry an application frame
        before it is accepted, so the `error{code:"auth"}` body travels in
        the close REASON here. Making it a frame would mean accepting an
        unauthenticated socket first, which section 1.3 forbids outright.
        """
        with pytest.raises(WebSocketDisconnect) as exc:
            with connect(client, token="not-the-secret"):
                pass
        assert exc.value.code == protocol.CLOSE_AUTH_FAILED

    def test_no_token_anywhere_is_refused_at_register(self, client):
        """No header, no query, no `register.token`: every channel is empty,
        so the socket is closed 4003 the moment the first frame arrives.

        It is accepted first only because the last-resort channel LIVES in
        the register frame - a socket that never presents a secret at the
        upgrade has to be read before it can be judged.
        """
        with client.websocket_connect("/ws/runner") as ws:
            ws.send_json(register_frame())
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "auth"
            assert error["fatal"] is True
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_AUTH_FAILED

    def test_a_non_bearer_authorization_header_is_ignored(self, client):
        """Only `Bearer <secret>` counts; a Basic header falls through to the
        register channel rather than being half-honored."""
        with client.websocket_connect(
            "/ws/runner", headers={"Authorization": "Basic abc"}
        ) as ws:
            registered(ws, register_frame(token=GOOD_TOKEN))

    def test_query_parameter_is_the_header_fallback(self, client):
        with client.websocket_connect(f"/ws/runner?token={GOOD_TOKEN}") as ws:
            registered(ws)

    def test_bad_query_token_is_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/runner?token=wrong"):
                pass
        assert exc.value.code == protocol.CLOSE_AUTH_FAILED

    def test_register_token_is_the_last_resort_channel(self, client):
        """Accepted only because neither the header nor the query carried a
        secret. It necessarily happens after accept(): a WebSocket cannot
        carry an application frame before it is accepted."""
        with client.websocket_connect("/ws/runner") as ws:
            registered(ws, register_frame(token=GOOD_TOKEN))

    def test_register_token_wrong_closes_4003(self, client):
        with client.websocket_connect("/ws/runner") as ws:
            ws.send_json(register_frame(token="wrong"))
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "auth"
            assert error["fatal"] is True
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_AUTH_FAILED


class TestHandshakeRegistration:
    def test_no_register_within_the_timeout_closes_4000(self, client, monkeypatch):
        monkeypatch.setattr(protocol, "REGISTRATION_TIMEOUT", 0.25)
        with connect(client) as ws:
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "registration_timeout"
            assert error["fatal"] is True
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_REGISTRATION_TIMEOUT

    def test_malformed_register_closes_4001(self, client):
        with connect(client) as ws:
            # `runner_type` is required by validate_runner_message; `name`
            # and `labels` deliberately are not.
            ws.send_json({"type": "register", "runner_id": "pi-1"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_registration"
            assert "runner_type" in error["message"]
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_INVALID_REGISTRATION

    def test_non_register_first_frame_closes_4001(self, client):
        with connect(client) as ws:
            ws.send_json({"type": "heartbeat"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "register" in error["message"]
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_INVALID_REGISTRATION

    def test_unsupported_protocol_version_closes_4002(self, client):
        with connect(client) as ws:
            ws.send_json(register_frame(protocol_version=2))
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "protocol_version"
            assert error["fatal"] is True
            assert "offered 2" in error["message"]
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == protocol.CLOSE_UNSUPPORTED_VERSION

    def test_absent_protocol_version_is_a_pre_version_agent(self, client):
        """Absent reads as 1 - the contract suite forbids a required field."""
        frame = register_frame()
        frame.pop("protocol_version", None)
        with connect(client) as ws:
            reply = registered(ws, frame)
            assert reply["protocol_version"] == protocol.PROTOCOL_VERSION

    def test_registered_carries_the_servers_timing(self, client):
        """The runner learns heartbeat/death timing FROM the server, so the
        three-timeout drift failure_01 shipped cannot recur."""
        with connect(client) as ws:
            reply = registered(ws)
            assert reply["heartbeat_interval"] == protocol.HEARTBEAT_INTERVAL
            assert reply["death_timeout"] == protocol.DEATH_TIMEOUT
            assert reply["resume_action"] == protocol.RESUME_IDLE
            assert reply["resume_step_id"] is None

    def test_registration_walks_the_state_machine_to_idle(self, client, ws_env):
        with connect(client) as ws:
            registered(ws)
            row = _run(ws_env.url, _load_runner("pi-1"))
            assert row.status == RunnerState.IDLE.value
            assert row.websocket_id
            assert row.get_labels()["arch"] == "amd64"  # normalized backend-side

    def test_duplicate_runner_id_closes_4004_and_leaves_the_incumbent(
        self, client, ws_env
    ):
        with connect(client) as first:
            registered(first)
            incumbent = _run(ws_env.url, _load_runner("pi-1")).websocket_id

            with connect(client) as second:
                second.send_json(register_frame())
                error = second.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "duplicate_connection"
                with pytest.raises(WebSocketDisconnect) as exc:
                    second.receive_json()
            assert exc.value.code == protocol.CLOSE_DUPLICATE_CONNECTION

            # The FIRST connection wins and is completely untouched.
            roundtrip(first)
            after = _run(ws_env.url, _load_runner("pi-1"))
            assert after.websocket_id == incumbent
            assert after.status == RunnerState.IDLE.value


class TestMidSessionErrors:
    def test_a_bad_frame_does_not_close_the_connection(self, client):
        """The asymmetry that matters: a bad REGISTER is fatal, a bad
        mid-session frame is not. One malformed frame must never kill a live
        step."""
        with connect(client) as ws:
            registered(ws)
            ws.send_json({"type": "ack"})  # missing step_id
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["fatal"] is False
            assert "step_id" in error["message"]
            # Still alive.
            roundtrip(ws)

    def test_unknown_message_type_is_not_fatal(self, client):
        with connect(client) as ws:
            registered(ws)
            ws.send_json({"type": "teleport"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "Unknown message type: teleport" in error["message"]
            roundtrip(ws)

    def test_non_json_frame_is_not_fatal(self, client):
        with connect(client) as ws:
            registered(ws)
            ws.send_text("{not json")
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["fatal"] is False
            roundtrip(ws)

    def test_oversized_frame_is_dropped_not_closed(self, client):
        """A single huge line must not kill a live step."""
        with connect(client) as ws:
            registered(ws)
            huge = "x" * (protocol.MAX_MESSAGE_BYTES + 64)
            ws.send_json({"type": "log", "step_id": "s", "lines": [huge]})
            error = ws.receive_json()
            assert error["code"] == "too_large"
            assert error["fatal"] is False
            roundtrip(ws)

    def test_heartbeat_is_answered_with_pong(self, client):
        with connect(client) as ws:
            registered(ws)
            ws.send_json({"type": "heartbeat"})
            assert ws.receive_json()["type"] == "pong"


# -----------------------------------------------------------------------------
# Contract 2: per-message DB sessions
# -----------------------------------------------------------------------------

class TestPerMessageSessions:
    def test_no_session_is_held_across_the_connection(self, client, ws_env):
        with connect(client) as ws:
            registered(ws)
            roundtrip(ws)
            after_register = ws_env.sessions.opened
            assert ws_env.sessions.live == 0

            for _ in range(5):
                ws.send_json({"type": "heartbeat"})
                assert ws.receive_json()["type"] == "pong"

            roundtrip(ws)
            # Every heartbeat opened AND closed its own session.
            assert ws_env.sessions.opened >= after_register + 5
            assert ws_env.sessions.live == 0
            assert ws_env.sessions.closed == ws_env.sessions.opened

    def test_the_endpoint_never_holds_two_sessions_at_once(self, client, ws_env):
        with connect(client) as ws:
            registered(ws)
            for _ in range(10):
                ws.send_json({"type": "heartbeat"})
                ws.receive_json()
            roundtrip(ws)
        assert ws_env.sessions.max_live <= 1

    def test_heartbeat_is_stamped_backend_side(self, client, ws_env):
        """No timestamp from a runner is ever compared to a backend
        deadline - a clock hours off must not make a dead runner immortal."""
        with connect(client) as ws:
            registered(ws)
            roundtrip(ws)
            before = _run(ws_env.url, _load_runner("pi-1")).last_heartbeat
            ws.send_json({"type": "heartbeat", "timestamp": "2099-01-01T00:00:00"})
            assert ws.receive_json()["type"] == "pong"
            roundtrip(ws)
            after = _run(ws_env.url, _load_runner("pi-1")).last_heartbeat
        assert after >= before
        assert after.year < 2099


# -----------------------------------------------------------------------------
# Contract 3: the step gate
# -----------------------------------------------------------------------------

class TestStepGate:
    def test_log_from_the_owning_runner_is_written_with_the_runner_prefix(
        self, client, ws_env
    ):
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            ws.send_json(
                {
                    "type": "log",
                    "step_id": ids["execution_id"],
                    "lines": ["pulling lazyaf-test-runner:dev"],
                }
            )
            roundtrip(ws)
            logs = _run(ws_env.url, _load_logs(ids["step_run_id"]))
        assert logs == "[runner] pulling lazyaf-test-runner:dev\n"

    def test_log_for_a_step_owned_by_another_runner_changes_no_row(
        self, client, ws_env
    ):
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "some-other-runner"))
            ws.send_json(
                {
                    "type": "log",
                    "step_id": ids["execution_id"],
                    "lines": ["I should not be written"],
                }
            )
            roundtrip(ws)
            logs = _run(ws_env.url, _load_logs(ids["step_run_id"]))
        assert logs in (None, "")

    def test_log_for_an_unassigned_step_changes_no_row(self, client, ws_env):
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            ws.send_json(
                {
                    "type": "log",
                    "step_id": ids["execution_id"],
                    "lines": ["nobody owns me"],
                }
            )
            roundtrip(ws)
            logs = _run(ws_env.url, _load_logs(ids["step_run_id"]))
        assert logs in (None, "")

    def test_log_for_an_unknown_step_changes_no_row(self, client, ws_env):
        with connect(client) as ws:
            registered(ws)
            ws.send_json(
                {"type": "log", "step_id": str(uuid4()), "lines": ["ghost"]}
            )
            roundtrip(ws)  # dropped with a WARN, connection alive

    def test_a_superseded_websocket_id_fails_the_gate(self, client, ws_env):
        """The second half of the fence. The step IS assigned to this runner
        and the frame still does not count, because the runner row's
        websocket_id has moved on - exactly the reconnect-vs-reassign race."""
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            _run(ws_env.url, _rotate_websocket_id("pi-1"))
            ws.send_json(
                {
                    "type": "log",
                    "step_id": ids["execution_id"],
                    "lines": ["from a ghost socket"],
                }
            )
            roundtrip(ws)
            logs = _run(ws_env.url, _load_logs(ids["step_run_id"]))
        assert logs in (None, "")

    def test_ack_for_another_runners_step_never_reaches_the_dispatcher(
        self, client, ws_env, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            runner_dispatcher,
            "notify_ack",
            lambda step_id, runner_id: seen.append((step_id, runner_id)) or True,
        )
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "someone-else"))
            ws.send_json({"type": "ack", "step_id": ids["execution_id"]})
            roundtrip(ws)
        assert seen == []

    def test_ack_from_the_owner_reaches_the_dispatcher(
        self, client, ws_env, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            runner_dispatcher,
            "notify_ack",
            lambda step_id, runner_id: seen.append((step_id, runner_id)) or True,
        )
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            ws.send_json({"type": "ack", "step_id": ids["execution_id"]})
            roundtrip(ws)
        assert seen == [(ids["execution_id"], "pi-1")]

    def test_step_complete_for_another_runners_step_is_inert(
        self, client, ws_env, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            runner_dispatcher,
            "notify_complete",
            lambda *args, **kwargs: seen.append(args) or True,
        )
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "someone-else"))
            ws.send_json(
                {
                    "type": "step_complete",
                    "step_id": ids["execution_id"],
                    "exit_code": 0,
                }
            )
            roundtrip(ws)
        assert seen == []

    def test_step_complete_from_the_owner_reaches_the_dispatcher(
        self, client, ws_env, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            runner_dispatcher,
            "notify_complete",
            lambda *args, **kwargs: seen.append(args) or True,
        )
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            ws.send_json(
                {
                    "type": "step_complete",
                    "step_id": ids["execution_id"],
                    "exit_code": 137,
                    "error": "killed",
                }
            )
            roundtrip(ws)
        assert seen == [(ids["execution_id"], "pi-1", 137, "killed")]

    def test_exit_code_zero_is_a_present_field(self, client, ws_env, monkeypatch):
        """Membership, not truthiness: a falsiness check in the validator
        would reject every SUCCESSFUL step completion."""
        seen = []
        monkeypatch.setattr(
            runner_dispatcher,
            "notify_complete",
            lambda *args, **kwargs: seen.append(args) or True,
        )
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            ws.send_json(
                {
                    "type": "step_complete",
                    "step_id": ids["execution_id"],
                    "exit_code": 0,
                }
            )
            roundtrip(ws)
        assert seen == [(ids["execution_id"], "pi-1", 0, None)]


# -----------------------------------------------------------------------------
# Teardown: the disconnect requeues, and never raises
# -----------------------------------------------------------------------------

class TestTeardown:
    def test_close_marks_the_runner_disconnected_and_clears_the_fence(
        self, client, ws_env
    ):
        with connect(client) as ws:
            registered(ws)
            roundtrip(ws)
            # Close INSIDE the context: TestClient's __exit__ cancels the
            # task running the app, which would abort the teardown a real
            # disconnect always gets to finish.
            ws.close(protocol.CLOSE_NORMAL)
            row = _settle(ws_env.url, "pi-1", RunnerState.DISCONNECTED.value)
        assert row.status == RunnerState.DISCONNECTED.value
        assert row.websocket_id is None

    def test_disconnect_mid_step_requeues_the_step(self, client, ws_env):
        """The socket dies holding an assignment: the step goes back to
        `pending` for the dispatcher, unassigned."""
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            _run(ws_env.url, _hold(ids["execution_id"], "pi-1"))
            roundtrip(ws)
            ws.close(protocol.CLOSE_NORMAL)
            _settle(ws_env.url, "pi-1", RunnerState.DISCONNECTED.value)
            execution = _run(ws_env.url, _load_execution(ids["execution_id"]))
        assert execution.status == StepExecutionStatus.PENDING.value
        assert execution.runner_id is None

    def test_reconnect_after_reassignment_is_told_to_abort(self, client, ws_env):
        """`runner_id is None` maps to ABORT, not continue - the step was
        requeued and the dispatcher may already be handing it elsewhere, so
        the returning runner's container must die rather than race."""
        ids = _run(ws_env.url, _seed_step())
        _run(ws_env.url, _seed_runner("pi-1", holding=ids["execution_id"]))
        _run(ws_env.url, _assign(ids["execution_id"], None))

        with connect(client) as ws:
            reply = registered(ws)
        assert reply["resume_action"] == protocol.RESUME_ABORT
        assert reply["resume_step_id"] == ids["execution_id"]

    def test_reconnect_still_owning_its_step_is_told_to_continue(
        self, client, ws_env
    ):
        ids = _run(ws_env.url, _seed_step())
        _run(ws_env.url, _seed_runner("pi-1", holding=ids["execution_id"]))
        _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))

        with connect(client) as ws:
            reply = registered(ws)
            assert reply["resume_action"] == protocol.RESUME_CONTINUE
            assert reply["resume_step_id"] == ids["execution_id"]
            # And the row says BUSY, not IDLE: `connect()` always walks a
            # fresh machine to IDLE, which is right for the machine's
            # history and wrong for what this runner is doing.
            roundtrip(ws)
            assert (
                _run(ws_env.url, _load_runner("pi-1")).status
                == RunnerState.BUSY.value
            )

    def test_abort_is_followed_by_a_cancel_step_frame(self, client, ws_env):
        ids = _run(ws_env.url, _seed_step())
        _run(ws_env.url, _seed_runner("pi-1", holding=ids["execution_id"]))
        _run(ws_env.url, _assign(ids["execution_id"], None))

        with connect(client) as ws:
            registered(ws)
            cancel = ws.receive_json()
        assert cancel["type"] == "cancel_step"
        assert cancel["step_id"] == ids["execution_id"]
        assert cancel["reason"] == "reassigned"


# -----------------------------------------------------------------------------
# The death watchdog (section 2.7)
# -----------------------------------------------------------------------------

class TestDeathWatchdog:
    """A runner that stops speaking is marked DEAD and its step requeued.

    The watchdog runs for IDLE connections too - that is exactly why
    `runner_state.py` carries IDLE -> DEAD. A runner that connects and then
    silently vanishes must not sit `idle` forever collecting assignments;
    failure_01 death-checked only ASSIGNED/BUSY.
    """

    @pytest.fixture(autouse=True)
    def _fast_death(self, monkeypatch):
        monkeypatch.setattr(protocol, "DEATH_MONITOR_INTERVAL", 0.05)
        monkeypatch.setattr(protocol, "DEATH_TIMEOUT", 0)

    def test_a_silent_runner_is_closed_and_its_step_requeued(self, client, ws_env):
        ids = _run(ws_env.url, _seed_step())
        with connect(client) as ws:
            registered(ws)
            _run(ws_env.url, _assign(ids["execution_id"], "pi-1"))
            _run(ws_env.url, _hold(ids["execution_id"], "pi-1"))
            with pytest.raises(WebSocketDisconnect):
                for _ in range(50):
                    ws.receive_json()
            execution = _settle_execution(
                ws_env.url, ids["execution_id"], StepExecutionStatus.PENDING.value
            )
        assert execution.runner_id is None

    def test_an_idle_runner_is_death_checked_too(self, client, ws_env):
        with connect(client) as ws:
            registered(ws)
            with pytest.raises(WebSocketDisconnect):
                for _ in range(50):
                    ws.receive_json()


# -----------------------------------------------------------------------------
# DB helpers (each returns a coroutine factory for _run)
# -----------------------------------------------------------------------------

def _load_runner(runner_id):
    async def _op(db):
        row = (
            await db.execute(select(Runner).where(Runner.id == runner_id))
        ).scalar_one()
        db.expunge(row)
        return row

    return _op


def _load_execution(execution_id):
    async def _op(db):
        row = (
            await db.execute(
                select(StepExecution).where(StepExecution.id == execution_id)
            )
        ).scalar_one()
        db.expunge(row)
        return row

    return _op


def _load_logs(step_run_id):
    async def _op(db):
        return (
            await db.execute(select(StepRun.logs).where(StepRun.id == step_run_id))
        ).scalar_one()

    return _op


def _seed_step():
    async def _op(db):
        repo = Repo(id=str(uuid4()), name="ws-repo", default_branch="main")
        pipeline = Pipeline(
            id=str(uuid4()), repo_id=repo.id, name="ws-pipeline", steps="[]"
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            steps_total=1,
        )
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=0,
            step_name="remote-probe",
            status=RunStatus.RUNNING.value,
        )
        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{run.id}:0:{step_run.id}",
            status=StepExecutionStatus.ASSIGNED.value,
        )
        db.add_all([repo, pipeline, run, step_run, execution])
        await db.commit()
        return {"step_run_id": step_run.id, "execution_id": execution.id}

    return _op


def _seed_runner(runner_id, holding=None):
    async def _op(db):
        runner = Runner(
            id=runner_id,
            name=runner_id,
            runner_type="generic",
            status=RunnerState.DISCONNECTED.value,
            current_step_execution_id=holding,
        )
        db.add(runner)
        await db.commit()

    return _op


def _assign(execution_id, runner_id):
    async def _op(db):
        await db.execute(
            update(StepExecution)
            .where(StepExecution.id == execution_id)
            .values(runner_id=runner_id)
        )
        await db.commit()

    return _op


def _hold(execution_id, runner_id):
    async def _op(db):
        await db.execute(
            update(Runner)
            .where(Runner.id == runner_id)
            .values(current_step_execution_id=execution_id)
        )
        await db.commit()

    return _op


def _rotate_websocket_id(runner_id):
    async def _op(db):
        await db.execute(
            update(Runner)
            .where(Runner.id == runner_id)
            .values(websocket_id=str(uuid4()))
        )
        await db.commit()

    return _op


def _settle(url, runner_id, status, timeout=20.0):
    """Wait for the endpoint's teardown, which runs after the socket closes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _run(url, _load_runner(runner_id))
        if row.status == status:
            return row
        time.sleep(0.05)
    raise AssertionError(f"runner {runner_id} never reached {status}")


def _settle_execution(url, execution_id, status, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _run(url, _load_execution(execution_id))
        if row.status == status:
            return row
        time.sleep(0.05)
    raise AssertionError(f"step execution {execution_id} never reached {status}")
