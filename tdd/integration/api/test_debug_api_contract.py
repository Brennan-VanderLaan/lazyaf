"""Debug re-run HTTP contract - Phase 12.7 (contracts C2, C14, C16, C18).

Drives the REAL FastAPI app against the REAL global `pipeline_executor`
(made Docker-free for T1 by tdd/conftest.py's `_t1_docker_free_local_execution`
fixture, which swaps only the two collaborators that would reach for Docker
and keeps the production routing decision).

ROUTER REGISTRATION: `backend/app/main.py` is integrator-owned this wave, so
`app.include_router(debug.router)` is not there yet. The `debug_router`
fixture below mounts it if it is absent and no-ops once the integrator has
applied the line - so these tests pin the contract now and keep pinning it
after registration, without either state being a skip.
"""
import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.main import app
from app.models.debug import DebugSession
from app.models.pipeline import Pipeline, PipelineRun, RunStatus
from app.models.repo import Repo
from app.routers import debug as debug_router_module
from app.services.execution import debug_session_service as service_module
from app.services.execution.debug_session_service import debug_session_service


#: A two-step LINEAR GRAPH. 12.8 retires the v1 array, so the wire vocabulary
#: for `breakpoints` is graph step IDS - these tests used index keys and every
#: one of them is now a 400 naming the keys that do exist.
STEPS_GRAPH = {
    "version": 2,
    "entry_points": ["first"],
    "steps": {
        "first": {"name": "first", "type": "script", "config": {"command": "echo one"}},
        "second": {"name": "second", "type": "script", "config": {"command": "echo two"}},
    },
    "edges": [
        {
            "id": "edge_0_success",
            "from_step": "first",
            "to_step": "second",
            "condition": "success",
        }
    ],
}

STEP_IDS = list(STEPS_GRAPH["steps"])


@pytest.fixture(autouse=True)
def debug_router(monkeypatch):
    """Mount the debug router if main.py has not registered it yet."""
    monkeypatch.setattr(service_module, "GATE_POLL_SECONDS", 0.05)
    paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/debug/{session_id}" not in paths:
        app.include_router(debug_router_module.router)
    yield


@pytest_asyncio.fixture
async def failed_run(db_session):
    """A repo + pipeline + a FAILED run to re-run."""
    repo = Repo(id=str(uuid4()), name="dbg", default_branch="main", is_ingested=True)
    db_session.add(repo)
    await db_session.commit()
    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name="dbg-pipeline",
        steps_graph=json.dumps(STEPS_GRAPH),
    )
    db_session.add(pipeline)
    await db_session.commit()
    run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status=RunStatus.FAILED.value,
        trigger_type="manual",
        trigger_context=json.dumps({"branch": "main", "commit_sha": "abc1234"}),
        current_step=0,
        steps_completed=0,
        steps_total=len(STEP_IDS),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def wait_for_status(client, session_id, status, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        response = await client.get(f"/api/debug/{session_id}")
        if response.status_code == 200 and response.json()["status"] == status:
            return response.json()
        await asyncio.sleep(0.02)
    raise AssertionError(f"session never reached {status}")


async def drain(client, session_id):
    """Leave no paused gate behind (and no 2s reset stall for the next test)."""
    await client.post(
        f"/api/debug/{session_id}/resume", json={"clear_remaining": True}
    )
    from app.services.pipeline_executor import pipeline_executor

    await pipeline_executor.wait_for_run(
        (await client.get(f"/api/debug/{session_id}")).json()["pipeline_run_id"]
    )


class TestCreateDebugRerun:
    async def test_create_starts_a_run_and_returns_no_token(self, client, failed_run):
        """Contract C14: the create response carries a join COMMAND, never a
        secret. failure_01 returned a long-lived token here and from the GET
        the UI polls."""
        response = await client.post(
            f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
            json={"breakpoints": ["first"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"run_id", "debug_session_id", "join_command"}
        assert "token" not in json.dumps(body)
        assert body["run_id"] != failed_run.id
        assert body["join_command"] == f"lazyaf debug attach {body['debug_session_id']}"

        await wait_for_status(client, body["debug_session_id"], "waiting_at_bp")
        await drain(client, body["debug_session_id"])

    async def test_an_unknown_breakpoint_key_is_a_400_naming_it(
        self, client, failed_run, db_session
    ):
        """Contract C2: an unknown key would otherwise be a breakpoint that
        silently never fires."""
        response = await client.post(
            f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
            json={"breakpoints": ["first", "build"]},
        )
        assert response.status_code == 400
        assert "build" in response.json()["detail"]
        rows = (
            await db_session.execute(
                select(PipelineRun).where(PipelineRun.trigger_type == "debug_rerun")
            )
        ).scalars().all()
        assert rows == [], "a refused create must not have started a run"

    async def test_an_index_style_breakpoint_is_a_400_naming_the_real_keys(
        self, client, failed_run
    ):
        """v1 addressed a step by its POSITION, so `"0"` is what a stored
        breakpoint or a habit looks like after 12.8. It is a key this
        pipeline does not define: the API refuses it and names the keys that
        exist, rather than accepting a breakpoint that can never fire."""
        response = await client.post(
            f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
            json={"breakpoints": ["0"]},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "0" in detail
        for step_id in STEP_IDS:
            assert step_id in detail

    async def test_a_pipeline_with_no_graph_is_a_400_not_a_silent_session(
        self, client, db_session
    ):
        """R1: the graph is the only execution definition. Debugging a
        pipeline that has none is refused by name - note the EMPTY
        breakpoints, which the unknown-key check could never have caught."""
        repo = Repo(id=str(uuid4()), name="dbg2", default_branch="main", is_ingested=True)
        db_session.add(repo)
        await db_session.commit()
        graphless = Pipeline(
            id=str(uuid4()), repo_id=repo.id, name="graphless-pipeline"
        )
        db_session.add(graphless)
        await db_session.commit()
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=graphless.id,
            status=RunStatus.FAILED.value,
            trigger_type="manual",
            trigger_context=json.dumps({"branch": "main", "commit_sha": "abc1234"}),
            current_step=0,
            steps_completed=0,
            steps_total=0,
        )
        db_session.add(run)
        await db_session.commit()

        response = await client.post(
            f"/api/pipeline-runs/{run.id}/debug-rerun", json={"breakpoints": []}
        )
        assert response.status_code == 400
        assert "graphless-pipeline" in response.json()["detail"]
        assert "no graph definition" in response.json()["detail"]
        started = (
            await db_session.execute(
                select(PipelineRun).where(PipelineRun.trigger_type == "debug_rerun")
            )
        ).scalars().all()
        assert started == []

    async def test_unknown_run_is_404(self, client):
        response = await client.post(
            f"/api/pipeline-runs/{uuid4()}/debug-rerun", json={"breakpoints": []}
        )
        assert response.status_code == 404

    async def test_the_rerun_is_stamped_debug_rerun(
        self, client, failed_run, db_session
    ):
        response = await client.post(
            f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
            json={"breakpoints": ["first"]},
        )
        body = response.json()
        await wait_for_status(client, body["debug_session_id"], "waiting_at_bp")
        run = await db_session.get(PipelineRun, body["run_id"])
        assert run.trigger_type == "debug_rerun"
        assert run.trigger_ref == failed_run.id
        # Contract C10: only branch + commit_sha carry over.
        assert json.loads(run.trigger_context) == {
            "branch": "main",
            "commit_sha": "abc1234",
        }
        await drain(client, body["debug_session_id"])


class TestGetSession:
    async def test_get_reports_the_pause_without_a_token(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first", "second"]},
            )
        ).json()
        body = await wait_for_status(
            client, created["debug_session_id"], "waiting_at_bp"
        )
        assert "token" not in body
        assert body["current_step"]["key"] == "first"
        assert body["current_step"]["name"] == "first"
        assert body["breakpoints"] == ["first", "second"]
        assert body["breakpoints_hit"] == ["first"]
        assert body["breakpoints_pending"] == ["second"]
        assert body["attach_available"] is True
        assert body["attach_unavailable_reason"] is None
        assert body["commit"]["sha"] == "abc1234"
        assert "[debug] paused before step" in body["logs"]
        await drain(client, created["debug_session_id"])

    async def test_unknown_session_is_404(self, client):
        assert (await client.get(f"/api/debug/{uuid4()}")).status_code == 404

    async def test_list_shows_only_non_terminal_sessions(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        await wait_for_status(client, created["debug_session_id"], "waiting_at_bp")
        listed = (await client.get("/api/debug")).json()
        assert [s["id"] for s in listed] == [created["debug_session_id"]]

        await client.post(f"/api/debug/{created['debug_session_id']}/abort")
        assert (await client.get("/api/debug")).json() == []


class TestJoinToken:
    async def test_join_token_is_minted_on_demand_and_binds_the_session(
        self, client, failed_run
    ):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")

        response = await client.post(f"/api/debug/{session_id}/join-token")
        assert response.status_code == 200
        body = response.json()
        assert debug_router_module.read_join_token(body["token"]) == session_id
        assert body["join_command"].endswith(body["token"])
        assert body["expires_at"]

        # Re-mintable, deliberately: single-use is incompatible with
        # reconnecting a dropped terminal.
        second = (await client.post(f"/api/debug/{session_id}/join-token")).json()
        assert debug_router_module.read_join_token(second["token"]) == session_id
        await drain(client, session_id)

    async def test_a_forged_token_proves_nothing(self):
        assert debug_router_module.read_join_token("not-a-jwt") is None
        assert debug_router_module.read_join_token(None) is None

    async def test_join_token_on_an_ended_session_is_409(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")
        await client.post(f"/api/debug/{session_id}/abort")
        response = await client.post(f"/api/debug/{session_id}/join-token")
        assert response.status_code == 409
        assert "aborted by user" in response.json()["detail"]

    async def test_unknown_session_join_token_is_404(self, client):
        assert (await client.post(f"/api/debug/{uuid4()}/join-token")).status_code == 404


class TestResumeAbortExtend:
    async def test_resume_returns_pending_and_the_next_breakpoint(
        self, client, failed_run
    ):
        """Contract C5 over the wire: resume is not an ending."""
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first", "second"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")
        response = await client.post(
            f"/api/debug/{session_id}/resume", json={"clear_remaining": False}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "pending", "next_breakpoint": "second"}

        second = await wait_for_status(client, session_id, "waiting_at_bp")
        assert second["current_step"]["key"] == "second"
        await drain(client, session_id)

    async def test_resume_when_not_paused_is_409(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": []},
            )
        ).json()
        response = await client.post(f"/api/debug/{created['debug_session_id']}/resume")
        assert response.status_code == 409
        from app.services.pipeline_executor import pipeline_executor

        await pipeline_executor.wait_for_run(created["run_id"])

    async def test_abort_ends_the_session_and_says_why(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")
        response = await client.post(f"/api/debug/{session_id}/abort")
        assert response.status_code == 200
        assert response.json() == {"status": "ended", "end_reason": "aborted by user"}

        body = (await client.get(f"/api/debug/{session_id}")).json()
        assert body["attach_available"] is False
        assert "ended" in body["attach_unavailable_reason"]

    async def test_extend_moves_the_deadline(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        before = await wait_for_status(client, session_id, "waiting_at_bp")
        response = await client.post(
            f"/api/debug/{session_id}/extend", json={"additional_minutes": 45}
        )
        assert response.status_code == 200
        assert response.json()["expires_at"] > before["expires_at"]
        assert response.json()["clamped"] is False
        await drain(client, session_id)

    async def test_extend_rejects_an_out_of_range_request(self, client, failed_run):
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")
        response = await client.post(
            f"/api/debug/{session_id}/extend", json={"additional_minutes": 100000}
        )
        assert response.status_code == 422
        await drain(client, session_id)

    @pytest.mark.parametrize("verb", ["resume", "abort", "extend"])
    async def test_unknown_session_is_404_on_every_verb(self, client, verb):
        response = await client.post(f"/api/debug/{uuid4()}/{verb}", json={})
        assert response.status_code == 404


class TestSessionRowIsTheOnlyDebugState:
    async def test_the_run_keeps_an_ordinary_run_status(
        self, client, failed_run, db_session
    ):
        """Contract C18: no `debug_*` RunStatus member exists, so a paused
        run reads as RUNNING and the pause is visible on the session row and
        the WS frame instead."""
        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        await wait_for_status(client, created["debug_session_id"], "waiting_at_bp")
        run = await db_session.get(PipelineRun, created["run_id"])
        await db_session.refresh(run)
        assert run.status == RunStatus.RUNNING.value

        session_row = (
            await db_session.execute(
                select(DebugSession).where(
                    DebugSession.id == created["debug_session_id"]
                )
            )
        ).scalar_one()
        await db_session.refresh(session_row)
        assert session_row.status == "waiting_at_bp"
        assert session_row.state_history and "waiting_at_bp" in session_row.state_history
        await drain(client, created["debug_session_id"])

    async def test_one_session_per_run_is_enforced_by_the_database(
        self, client, failed_run, db_session
    ):
        """`pipeline_run_id` is UNIQUE: two sessions for one run would make
        "which one pauses this step?" unanswerable."""
        from sqlalchemy.exc import IntegrityError

        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        await wait_for_status(client, created["debug_session_id"], "waiting_at_bp")
        duplicate = DebugSession(
            id=str(uuid4()),
            pipeline_run_id=created["run_id"],
            status="pending",
            breakpoints="[]",
            hit_breakpoints="[]",
            timeout_seconds=3600,
            max_timeout_seconds=14400,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()
        await drain(client, created["debug_session_id"])


class _FakeWebSocket:
    """Enough WebSocket for the UPGRADE path.

    The refusals below all happen before `accept()`, which is the point: a
    socket that fails the check is never accepted, so the failure is visible
    in the handshake rather than one frame later (the `ws_runners.py`
    property this endpoint mirrors).
    """

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.query_params = {}
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def send_text(self, text):
        self.sent.append(text)


class TestTerminalUpgradeRefusals:
    """Contracts C14, C16, C17 - every refusal before accept(), with a reason."""

    async def _factory(self, db_session):
        def factory():
            return _NonClosingSession(db_session)

        return factory

    async def test_a_missing_token_is_refused_4401(self, client, db_session):
        from app.routers.debug import debug_terminal_socket
        from app.services.execution import debug_terminal as protocol

        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, str(uuid4()), mode="sidecar", token=None,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_BAD_TOKEN
        assert "join token" in ws.closed[1]

    async def test_a_token_for_another_session_is_refused(self, client, db_session):
        from app.routers.debug import debug_terminal_socket, mint_join_token
        from app.services.execution import debug_terminal as protocol

        token, _exp = mint_join_token(str(uuid4()), None)
        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, str(uuid4()), mode="sidecar", token=token,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_BAD_TOKEN

    async def test_shell_mode_is_refused_with_the_reason_not_downgraded(
        self, client, db_session
    ):
        """Contract C17: `--shell` is an error naming why, never a silent
        fallback to the sidecar."""
        from app.routers.debug import debug_terminal_socket, mint_join_token
        from app.services.execution import debug_terminal as protocol

        session_id = str(uuid4())
        token, _exp = mint_join_token(session_id, None)
        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, session_id, mode="shell", token=token,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_NOT_ATTACHABLE
        assert ws.closed[1] == protocol.SHELL_REFUSED_REASON

    async def test_an_unknown_session_is_refused_4404(self, client, db_session):
        from app.routers.debug import debug_terminal_socket, mint_join_token
        from app.services.execution import debug_terminal as protocol

        session_id = str(uuid4())
        token, _exp = mint_join_token(session_id, None)
        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, session_id, mode="sidecar", token=token,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_UNKNOWN_SESSION

    async def test_an_ended_session_is_refused_even_with_a_valid_token(
        self, client, db_session, failed_run
    ):
        """Contract C14: revocation is free because the upgrade re-reads the
        row - the JWT's opinion does not survive the session ending."""
        from app.routers.debug import debug_terminal_socket, mint_join_token
        from app.services.execution import debug_terminal as protocol

        created = (
            await client.post(
                f"/api/pipeline-runs/{failed_run.id}/debug-rerun",
                json={"breakpoints": ["first"]},
            )
        ).json()
        session_id = created["debug_session_id"]
        await wait_for_status(client, session_id, "waiting_at_bp")
        token, _exp = mint_join_token(session_id, None)
        await client.post(f"/api/debug/{session_id}/abort")

        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, session_id, mode="sidecar", token=token,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_NOT_ATTACHABLE
        assert "ended" in ws.closed[1]

    async def test_a_remote_step_pause_refuses_attach_with_its_reason(
        self, client, db_session, failed_run
    ):
        """Contract C16: a remote pause is real; attaching to it is refused
        with the sentence, never a sidecar over the wrong volume."""
        from app.routers.debug import debug_terminal_socket, mint_join_token
        from app.services.execution import debug_terminal as protocol

        session = DebugSession(
            id=str(uuid4()),
            pipeline_run_id=failed_run.id,
            status="waiting_at_bp",
            breakpoints=json.dumps(["first"]),
            hit_breakpoints=json.dumps(["first"]),
            timeout_seconds=3600,
            max_timeout_seconds=14400,
            current_step_key="first",
            current_step_executor="remote",
        )
        db_session.add(session)
        await db_session.commit()

        token, _exp = mint_join_token(session.id, None)
        ws = _FakeWebSocket()
        await debug_terminal_socket(
            ws, session.id, mode="sidecar", token=token,
            session_factory=await self._factory(db_session),
        )
        assert ws.accepted is False
        assert ws.closed[0] == protocol.CLOSE_NOT_ATTACHABLE
        assert ws.closed[1] == protocol.REMOTE_ATTACH_REASON[:120]


class _NonClosingSession:
    """Hand the endpoint the test's own session, ignoring its close().

    The endpoint opens and closes a session per message by design (a terminal
    connection can last hours and must never pin one). In-test we want it to
    read the SAME transaction the fixtures wrote, so close() is a no-op here
    and the real fixture teardown owns the session.
    """

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def close(self):
        return None
