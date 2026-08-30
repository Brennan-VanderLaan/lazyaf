"""Unit tests for the DB-backed runner registry (Phase 12.6).

The registry is where failure_01's central defect lived: the state machine
existed, the DB column existed, and the two never met - so `runners.status`
never left "idle" while the machine walked a completely different path.

The design's answer is a single funnel: machine first (an illegal move raises
before anything durable happens), DB second, UI third. These tests drive that
funnel through a REAL session and the REAL ConnectionManager with a capturing
transport (R6) - never an AsyncMock, because a mock cannot tell you that a
broadcast was serializable or that exactly one frame was emitted.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.runner import Runner
from app.services.execution.runner_protocol import RegisterMessage
from app.services.execution.runner_registry import (
    DuplicateRunnerConnection,
    RunnerRegistry,
)
from app.services.execution.runner_state import (
    InvalidRunnerTransitionError,
    RunnerState,
)
from app.services.websocket import manager


class CapturingSocket:
    """A real transport that records what was actually put on the wire.

    Not an AsyncMock (R6): this asserts the frame was JSON-serializable and
    lets a test count frames, which is the whole point of "exactly one
    runner_status per transition".
    """

    def __init__(self, fail_on_send: bool = False):
        self.frames: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self._fail_on_send = fail_on_send

    async def send_text(self, text: str) -> None:
        if self._fail_on_send:
            raise ConnectionResetError("socket is gone")
        if self.closed:
            raise RuntimeError("send on a closed socket")
        self.frames.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code

    def of_type(self, message_type: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == message_type]


def register(runner_id="runner-1", **kwargs) -> RegisterMessage:
    payload = dict(
        runner_id=runner_id,
        name=kwargs.pop("name", "Test Runner"),
        runner_type=kwargs.pop("runner_type", "generic"),
        labels=kwargs.pop("labels", {}),
    )
    payload.update(kwargs)
    return RegisterMessage(**payload)


@pytest_asyncio.fixture
async def registry():
    reg = RunnerRegistry()
    yield reg
    await reg.reset()


@pytest_asyncio.fixture
async def ui_socket():
    """A UI client attached to the REAL ConnectionManager."""
    socket = CapturingSocket()
    manager.active_connections.append(socket)
    yield socket
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)


async def _row(db, runner_id: str) -> Runner | None:
    result = await db.execute(select(Runner).where(Runner.id == runner_id))
    return result.scalar_one_or_none()


class TestConnect:
    async def test_connect_creates_the_row(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())

        row = await _row(db_session, "runner-1")
        assert row is not None
        assert row.status == RunnerState.IDLE.value
        assert row.runner_type == "generic"
        assert row.name == "Test Runner"
        assert row.websocket_id is not None
        assert row.connected_at is not None

    async def test_connect_walks_disconnected_connecting_idle(self, db_session, registry):
        """NEVER construct the machine at IDLE. failure_01 did, so the
        machine's own history was a lie and reconnect could never observe
        in-flight work."""
        await registry.connect(db_session, CapturingSocket(), register())

        machine = registry.machine("runner-1")
        moves = [(t.from_state, t.to_state) for t in machine.history]
        assert moves == [
            (RunnerState.DISCONNECTED, RunnerState.CONNECTING),
            (RunnerState.CONNECTING, RunnerState.IDLE),
        ]

    async def test_connect_stores_protocol_and_agent_forensics(self, db_session, registry):
        await registry.connect(
            db_session,
            CapturingSocket(),
            register(protocol_version=1, agent_version="0.1.0"),
        )
        row = await _row(db_session, "runner-1")
        assert row.protocol_version == 1
        assert row.agent_version == "0.1.0"

    async def test_connect_normalizes_labels(self, db_session, registry):
        await registry.connect(
            db_session,
            CapturingSocket(),
            register(labels={"arch": "x86_64", "has": "docker"}),
        )
        row = await _row(db_session, "runner-1")
        assert row.get_labels() == {"arch": "amd64", "has": ["docker"]}

    async def test_reconnect_reanchors_a_stale_status(self, db_session, registry):
        """A row left 'busy' by a crashed process is a lie about a socket
        that no longer exists; the fresh machine starts at DISCONNECTED and
        the row must agree with it."""
        db_session.add(
            Runner(
                id="runner-1",
                status=RunnerState.BUSY.value,
                runner_type="generic",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        await registry.connect(db_session, CapturingSocket(), register())

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.IDLE.value
        assert registry.machine("runner-1").history[0].from_state is (
            RunnerState.DISCONNECTED
        )

    async def test_reconnect_preserves_the_claimed_step(self, db_session, registry):
        """on_runner_reconnect must be able to SEE the step this runner still
        believes it holds, so connect() must not clear the pointer."""
        db_session.add(
            Runner(
                id="runner-1",
                status=RunnerState.BUSY.value,
                runner_type="generic",
                current_step_execution_id="se-1",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        await registry.connect(db_session, CapturingSocket(), register())

        row = await _row(db_session, "runner-1")
        assert row.current_step_execution_id == "se-1"

    async def test_connect_generates_an_id_when_absent(self, db_session, registry):
        runner = await registry.connect(
            db_session, CapturingSocket(), RegisterMessage(runner_id="")
        )
        assert runner.id
        assert registry.is_connected(runner.id)

    async def test_connect_wakes_the_dispatcher(self, db_session, registry):
        wakes = []
        registry.set_dispatch_waker(lambda: wakes.append(1))

        await registry.connect(db_session, CapturingSocket(), register())

        assert wakes, "a newly idle runner must wake the dispatcher"

    async def test_a_broken_waker_never_breaks_a_connection(self, db_session, registry):
        def boom():
            raise RuntimeError("dispatcher is down")

        registry.set_dispatch_waker(boom)
        await registry.connect(db_session, CapturingSocket(), register())
        assert registry.is_connected("runner-1")


class TestDuplicateConnection:
    async def test_duplicate_runner_id_raises(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())

        with pytest.raises(DuplicateRunnerConnection):
            await registry.connect(db_session, CapturingSocket(), register())

    async def test_incumbent_is_left_completely_untouched(self, db_session, registry):
        incumbent = CapturingSocket()
        await registry.connect(db_session, incumbent, register())
        before = await _row(db_session, "runner-1")
        websocket_id = before.websocket_id

        impostor = CapturingSocket()
        with pytest.raises(DuplicateRunnerConnection):
            await registry.connect(db_session, impostor, register())

        after = await _row(db_session, "runner-1")
        assert after.status == RunnerState.IDLE.value
        assert after.websocket_id == websocket_id
        assert registry.websocket_id("runner-1") == websocket_id
        # The impostor never displaced the live socket.
        assert await registry.send("runner-1", _pong()) is True
        assert incumbent.of_type("pong")
        assert impostor.frames == []


def _pong():
    from app.services.execution.runner_protocol import PongMessage

    return PongMessage()


class TestTransition:
    async def test_legal_transition_updates_the_row(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())

        await registry.transition(
            db_session, "runner-1", RunnerState.ASSIGNED, reason="dispatch"
        )

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.ASSIGNED.value
        assert registry.machine("runner-1").state is RunnerState.ASSIGNED

    async def test_illegal_transition_raises_and_writes_nothing(self, db_session, registry):
        """Machine first. A silent DB write the machine never sanctioned is
        exactly how the two diverge."""
        await registry.connect(db_session, CapturingSocket(), register())

        with pytest.raises(InvalidRunnerTransitionError):
            await registry.transition(db_session, "runner-1", RunnerState.BUSY)

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.IDLE.value
        assert registry.machine("runner-1").state is RunnerState.IDLE

    async def test_unknown_runner_raises_key_error(self, db_session, registry):
        with pytest.raises(KeyError):
            await registry.transition(db_session, "nobody", RunnerState.IDLE)

    async def test_full_assignment_cycle(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())
        await registry.transition(db_session, "runner-1", RunnerState.ASSIGNED)
        await registry.transition(db_session, "runner-1", RunnerState.BUSY)
        await registry.transition(db_session, "runner-1", RunnerState.IDLE)

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.IDLE.value

    async def test_idle_can_go_straight_to_dead(self, db_session, registry):
        """The death monitor iterates EVERY machine, IDLE included: a runner
        that connects and then silently vanishes must not sit idle forever
        collecting assignments."""
        await registry.connect(db_session, CapturingSocket(), register())

        await registry.transition(
            db_session, "runner-1", RunnerState.DEAD, reason="heartbeat timeout"
        )

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.DEAD.value

    async def test_transition_to_idle_wakes_the_dispatcher(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())
        wakes = []
        registry.set_dispatch_waker(lambda: wakes.append(1))

        await registry.transition(db_session, "runner-1", RunnerState.ASSIGNED)
        assert wakes == []
        await registry.transition(db_session, "runner-1", RunnerState.BUSY)
        await registry.transition(db_session, "runner-1", RunnerState.IDLE)
        assert len(wakes) == 1


class TestBroadcast:
    async def test_connect_emits_exactly_one_frame_per_transition(
        self, db_session, registry, ui_socket
    ):
        await registry.connect(db_session, CapturingSocket(), register())

        frames = ui_socket.of_type("runner_status")
        assert len(frames) == 2  # CONNECTING, IDLE
        assert [f["payload"]["status"] for f in frames] == ["connecting", "idle"]

    async def test_each_transition_emits_exactly_one_frame(
        self, db_session, registry, ui_socket
    ):
        await registry.connect(db_session, CapturingSocket(), register())
        ui_socket.frames.clear()

        await registry.transition(db_session, "runner-1", RunnerState.ASSIGNED)

        assert len(ui_socket.of_type("runner_status")) == 1

    async def test_illegal_transition_emits_no_frame(
        self, db_session, registry, ui_socket
    ):
        await registry.connect(db_session, CapturingSocket(), register())
        ui_socket.frames.clear()

        with pytest.raises(InvalidRunnerTransitionError):
            await registry.transition(db_session, "runner-1", RunnerState.BUSY)

        assert ui_socket.of_type("runner_status") == []

    async def test_frame_payload_is_the_read_model(self, db_session, registry, ui_socket):
        await registry.connect(
            db_session, CapturingSocket(), register(labels={"arch": "arm64"})
        )

        payload = ui_socket.of_type("runner_status")[-1]["payload"]
        assert payload["id"] == "runner-1"
        assert payload["labels"] == {"arch": "arm64"}
        assert payload["connection"] == "websocket"
        assert payload["runner_type"] == "generic"


class TestHeartbeat:
    async def test_heartbeat_is_stamped_backend_side(self, db_session, registry):
        """No timestamp from a runner is ever compared to a backend deadline:
        a clock hours off must not make a dead runner immortal."""
        await registry.connect(db_session, CapturingSocket(), register())
        stale = datetime.utcnow() - timedelta(hours=2)
        row = await _row(db_session, "runner-1")
        row.last_heartbeat = stale
        await db_session.commit()

        await registry.heartbeat(db_session, "runner-1")

        await db_session.refresh(row)
        assert row.last_heartbeat > stale
        assert (datetime.utcnow() - row.last_heartbeat).total_seconds() < 5

    async def test_heartbeat_updates_the_machine(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())
        machine = registry.machine("runner-1")
        machine._last_heartbeat = datetime.utcnow() - timedelta(minutes=5)
        assert machine.is_alive(30) is False

        await registry.heartbeat(db_session, "runner-1")

        assert machine.is_alive(30) is True

    async def test_heartbeat_for_an_unknown_runner_is_inert(self, db_session, registry):
        await registry.heartbeat(db_session, "nobody")  # must not raise


class TestFindAvailable:
    async def _connect(self, db, registry, runner_id, **kwargs):
        return await registry.connect(
            db, CapturingSocket(), register(runner_id=runner_id, **kwargs)
        )

    async def test_returns_every_match_not_one_winner(self, db_session, registry):
        """failure_01's find_idle_runner returned a single runner and was a
        TOCTOU by construction. The dispatcher picks; the CAS arbitrates."""
        await self._connect(db_session, registry, "a")
        await self._connect(db_session, registry, "b")

        available = await registry.find_available(db_session, {})

        assert {r.id for r in available} == {"a", "b"}

    async def test_excludes_a_runner_this_process_cannot_reach(
        self, db_session, registry
    ):
        """A row can say 'idle' while this process holds no socket for it."""
        db_session.add(
            Runner(
                id="ghost",
                status=RunnerState.IDLE.value,
                runner_type="generic",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        assert await registry.find_available(db_session, {}) == []

    async def test_excludes_a_busy_runner(self, db_session, registry):
        await self._connect(db_session, registry, "a")
        await registry.transition(db_session, "a", RunnerState.ASSIGNED)

        assert await registry.find_available(db_session, {}) == []

    async def test_filters_by_runner_id_pin(self, db_session, registry):
        await self._connect(db_session, registry, "a")
        await self._connect(db_session, registry, "b")

        available = await registry.find_available(db_session, {"runner_id": "b"})

        assert [r.id for r in available] == ["b"]

    async def test_filters_by_labels(self, db_session, registry):
        await self._connect(db_session, registry, "pi", labels={"arch": "aarch64"})
        await self._connect(db_session, registry, "box", labels={"arch": "x86_64"})

        available = await registry.find_available(db_session, {"arch": "arm64"})

        assert [r.id for r in available] == ["pi"]

    async def test_filters_by_has_subset(self, db_session, registry):
        await self._connect(db_session, registry, "docked", labels={"has": ["docker"]})
        await self._connect(db_session, registry, "bare", labels={"has": []})

        available = await registry.find_available(db_session, {"has": ["docker"]})

        assert [r.id for r in available] == ["docked"]

    async def test_unsatisfiable_pin_matches_nothing(self, db_session, registry):
        await self._connect(db_session, registry, "a", labels={"arch": "amd64"})

        assert await registry.find_available(db_session, {"gpu": "a100"}) == []


class TestSend:
    async def test_send_delivers_the_frame(self, db_session, registry):
        socket = CapturingSocket()
        await registry.connect(db_session, socket, register())

        assert await registry.send("runner-1", _pong()) is True
        assert socket.of_type("pong")

    async def test_send_to_an_unknown_runner_returns_false(self, registry):
        assert await registry.send("nobody", _pong()) is False

    async def test_a_failed_send_drops_the_connection(self, db_session, registry):
        socket = CapturingSocket()
        await registry.connect(db_session, socket, register())
        socket._fail_on_send = True

        assert await registry.send("runner-1", _pong()) is False
        assert registry.is_connected("runner-1") is False

    async def test_drain_reaches_every_runner(self, db_session, registry):
        first, second = CapturingSocket(), CapturingSocket()
        await registry.connect(db_session, first, register(runner_id="a"))
        await registry.connect(db_session, second, register(runner_id="b"))

        await registry.drain("backend shutting down")

        assert first.of_type("drain")[0]["reason"] == "backend shutting down"
        assert second.of_type("drain")

    async def test_drain_survives_a_dead_socket(self, db_session, registry):
        dead = CapturingSocket(fail_on_send=True)
        alive = CapturingSocket()
        await registry.connect(db_session, dead, register(runner_id="a"))
        await registry.connect(db_session, alive, register(runner_id="b"))

        await registry.drain()

        assert alive.of_type("drain")


class TestDisconnect:
    async def test_disconnect_clears_the_socket_and_the_row(self, db_session, registry):
        socket = CapturingSocket()
        await registry.connect(db_session, socket, register())
        websocket_id = registry.websocket_id("runner-1")

        await registry.disconnect(db_session, "runner-1", websocket_id)

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.DISCONNECTED.value
        assert row.websocket_id is None
        assert registry.is_connected("runner-1") is False

    async def test_dead_to_disconnected_does_not_raise(self, db_session, registry):
        """THE failure_01 finally-block bug: it threw
        InvalidRunnerTransitionError out of the close handler, skipping both
        the DB update and the requeue."""
        socket = CapturingSocket()
        await registry.connect(db_session, socket, register())
        websocket_id = registry.websocket_id("runner-1")
        await registry.transition(db_session, "runner-1", RunnerState.DEAD)

        await registry.disconnect(db_session, "runner-1", websocket_id)

        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.DISCONNECTED.value

    async def test_disconnect_of_an_unknown_runner_is_inert(self, db_session, registry):
        await registry.disconnect(db_session, "nobody", "ws-1")  # must not raise

    async def test_a_superseded_socket_cannot_tear_down_the_incumbent(
        self, db_session, registry
    ):
        """A ghost's close must not disconnect the connection that replaced
        it - the reconnect-vs-reassign race the audit names."""
        await registry.connect(db_session, CapturingSocket(), register())

        await registry.disconnect(db_session, "runner-1", "some-older-websocket-id")

        assert registry.is_connected("runner-1") is True
        row = await _row(db_session, "runner-1")
        assert row.status == RunnerState.IDLE.value

    async def test_disconnect_broadcasts(self, db_session, registry, ui_socket):
        await registry.connect(db_session, CapturingSocket(), register())
        websocket_id = registry.websocket_id("runner-1")
        ui_socket.frames.clear()

        await registry.disconnect(db_session, "runner-1", websocket_id)

        frames = ui_socket.of_type("runner_status")
        assert len(frames) == 1, "one disconnect is one frame, not a two-step reveal"
        assert frames[0]["payload"]["status"] == "disconnected"
        assert frames[0]["payload"]["connection"] == "none"

    async def test_a_superseded_socket_broadcasts_nothing(
        self, db_session, registry, ui_socket
    ):
        await registry.connect(db_session, CapturingSocket(), register())
        ui_socket.frames.clear()

        await registry.disconnect(db_session, "runner-1", "stale-websocket-id")

        assert ui_socket.of_type("runner_status") == []


class TestBootstrapAndReset:
    async def test_bootstrap_marks_every_row_disconnected(self, db_session, registry):
        """No connection survives a restart. Pretending one did is how a
        fresh backend hands work to a ghost."""
        db_session.add_all(
            [
                Runner(
                    id="a",
                    status=RunnerState.BUSY.value,
                    runner_type="generic",
                    websocket_id="ws-a",
                    last_heartbeat=datetime.utcnow(),
                ),
                Runner(
                    id="b",
                    status=RunnerState.IDLE.value,
                    runner_type="generic",
                    websocket_id="ws-b",
                    last_heartbeat=datetime.utcnow(),
                ),
            ]
        )
        await db_session.commit()

        await registry.bootstrap(db_session)

        for runner_id in ("a", "b"):
            row = await _row(db_session, runner_id)
            assert row.status == RunnerState.DISCONNECTED.value
            assert row.websocket_id is None

    async def test_bootstrap_leaves_the_claimed_step_for_the_orphan_sweep(
        self, db_session, registry
    ):
        db_session.add(
            Runner(
                id="a",
                status=RunnerState.BUSY.value,
                runner_type="generic",
                current_step_execution_id="se-1",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        await registry.bootstrap(db_session)

        row = await _row(db_session, "a")
        assert row.current_step_execution_id == "se-1"

    async def test_bootstrap_clears_in_memory_state(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())

        await registry.bootstrap(db_session)

        assert registry.connection_count == 0
        assert registry.machine("runner-1") is None

    async def test_reset_closes_and_forgets_connections(self, db_session, registry):
        socket = CapturingSocket()
        await registry.connect(db_session, socket, register())

        await registry.reset()

        assert registry.connection_count == 0
        assert socket.closed is True


class TestSnapshot:
    async def test_snapshot_reports_the_connection_channel(self, db_session, registry):
        """A vacuous 'runners exist' pass is exactly what gate assertion 9
        must not accept - the DB row alone cannot tell a live runner from an
        'idle' row left behind by a crashed process."""
        await registry.connect(db_session, CapturingSocket(), register(runner_id="live"))
        db_session.add(
            Runner(
                id="ghost",
                status=RunnerState.IDLE.value,
                runner_type="generic",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        rows = {row["id"]: row for row in await registry.snapshot(db_session)}

        assert rows["live"]["connection"] == "websocket"
        assert rows["ghost"]["connection"] == "none"

    async def test_snapshot_is_json_serializable(self, db_session, registry):
        await registry.connect(db_session, CapturingSocket(), register())
        json.dumps(await registry.snapshot(db_session))  # must not raise
