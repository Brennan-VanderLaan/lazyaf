"""The breakpoint gate inside the executor - contracts C1, C3, C4, C6, C7, C8.

Phase 12.7. These tests drive the REAL `PipelineExecutor` with contract fakes
for the seams it owns (router / workspace / local executor), the REAL
`ConnectionManager` with a capturing transport (R6 - never mock the manager on
a broadcast path; failure_01's breakpoint tests AsyncMocked it and hid a
guaranteed `broadcast()` arity TypeError), and the REAL DebugSessionService
against a file-backed SQLite engine.

Nothing here asserts about a mock's call list. Every assertion is a row, a
broadcast frame, or a step that actually ran.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.debug import DebugSession
from app.models.pipeline import RunStatus, StepExecution
from app.services.execution import debug_session_service as service_module
from app.services.execution.debug_session_service import (
    DebugGateOutcome,
    debug_session_service,
)
from app.services.execution.debug_state import DebugState
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name


# -----------------------------------------------------------------------------
# Contract fakes (same shapes tdd/unit/services/test_pipeline_local_dispatch.py
# uses - the pre-agreed 12.2-INT interfaces, not ad-hoc mocks)
# -----------------------------------------------------------------------------


class Decision:
    def __init__(self, mode: str, reason: str):
        self.mode = mode
        self.reason = reason


class LocalRouter:
    def decide(self, step_type: str, step_config: dict) -> Decision:
        return Decision("local", f"{step_type}-runs-local")


class FakeWorkspaceService:
    """Records the workspace lifecycle, and models the refcount for C8."""

    def __init__(self):
        self.ops: list[tuple] = []
        self.workspaces: dict[str, SimpleNamespace] = {}

    async def get_or_create(
        self, db, pipeline_run_id, repo_id, branch, commit_sha, worker_key=None
    ):
        # worker_key is the workspace LANE (M13-1). Keyed by (run, lane)
        # like the real service, so a fan-out gets distinct workspaces
        # instead of silently sharing one checkout.
        lane = (pipeline_run_id, worker_key or "default")
        self.ops.append(("get_or_create", pipeline_run_id))
        ws = self.workspaces.get(lane)
        if ws is None:
            ws = SimpleNamespace(
                id=f"ws-{pipeline_run_id[:8]}",
                pipeline_run_id=pipeline_run_id,
                worker_key=lane[1],
                volume_name=generate_volume_name(pipeline_run_id, lane[1]),
                status="ready",
                use_count=0,
            )
            self.workspaces[lane] = ws
        return ws

    async def acquire(self, db, workspace_id):
        self.ops.append(("acquire", workspace_id))
        for ws in self.workspaces.values():
            if ws.id == workspace_id:
                ws.use_count += 1

    async def release(self, db, workspace_id):
        self.ops.append(("release", workspace_id))
        for ws in self.workspaces.values():
            if ws.id == workspace_id:
                ws.use_count -= 1

    async def cleanup(self, db, pipeline_run_id):
        self.ops.append(("cleanup", pipeline_run_id))

    def op_names(self) -> list[str]:
        return [op[0] for op in self.ops]

    def use_count(self, run_id: str, worker_key: str = "default") -> int:
        # Workspaces are keyed by (run, LANE) since M13-1. The default lane is
        # what every pre-M13 step - and the debug gate - uses.
        ws = self.workspaces.get((run_id, worker_key))
        return ws.use_count if ws else 0


class FakeLocalExecutor:
    """Yields LocalExecutor's event stream shape and records what ran."""

    def __init__(self):
        self.ran: list[str] = []
        self.missing_images: list[str] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        return image.startswith("lazyaf-")

    async def find_missing_images(self, images) -> list[str]:
        return [image for image in images if image in self.missing_images]

    async def execute_step(self, step_config, execution_context):
        self.ran.append(execution_context["execution_key"])
        for event in (
            {"type": "status", "status": "running"},
            {"type": "log", "line": "ok"},
            {"type": "result", "status": "completed", "exit_code": 0},
        ):
            await asyncio.sleep(0)
            yield event

    async def cancel_step(self, execution_key):
        return False

    def reset(self):
        self.ran.clear()


class CapturingSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    """Executor + debug service + real WS manager, on a file-backed engine."""
    # The pause loop polls; 5s would make every test in this file slow. The
    # constant is read from the module at call time, so this is the real code
    # path with a shorter clock - not a different code path.
    monkeypatch.setattr(service_module, "GATE_POLL_SECONDS", 0.05)

    db_path = (tmp_path / "debug_gate.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", echo=False, connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    executor = PipelineExecutor()
    executor._router = LocalRouter()
    workspace = FakeWorkspaceService()
    executor._workspace_service = workspace
    local = FakeLocalExecutor()
    executor._local_executor = local

    await debug_session_service.reset()
    debug_session_service._executor = executor

    socket = CapturingSocket()
    manager.active_connections.append(socket)

    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        executor=executor,
        workspace=workspace,
        local=local,
        socket=socket,
    )

    # Reset the executor FIRST (it wakes every parked gate through the debug
    # service), and only then drop the seam - a gate that woke after the seam
    # was cleared would tear down against the process-wide executor.
    try:
        await executor.reset()
    except Exception:
        pass
    debug_session_service._executor = None
    await debug_session_service.reset()
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    await engine.dispose()


async def seed(factory, steps=None, graph=None):
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()), name="debug-repo", default_branch="main", is_ingested=True
        )
        db.add(repo)
        await db.commit()
        await db.refresh(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="debug-pipeline",
            steps=json.dumps(steps) if steps is not None else None,
            steps_graph=json.dumps(graph) if graph is not None else None,
        )
        db.add(pipeline)
        await db.commit()
        await db.refresh(pipeline)
        original = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.FAILED.value,
            trigger_type="manual",
            trigger_context=json.dumps({"branch": "main", "commit_sha": "abc1234"}),
            current_step=0,
            steps_completed=0,
            steps_total=len(steps or []),
        )
        db.add(original)
        await db.commit()
        await db.refresh(original)
        return repo, pipeline, original


SCRIPT_STEPS = [
    {"name": "first", "type": "script", "config": {"command": "echo one"}},
    {"name": "second", "type": "script", "config": {"command": "echo two"}},
]


async def wait_until(predicate, timeout=20.0, interval=0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def read_session(factory, session_id) -> DebugSession:
    async with factory() as db:
        return (
            await db.execute(select(DebugSession).where(DebugSession.id == session_id))
        ).scalar_one()


# -----------------------------------------------------------------------------
# Zero-cost path
# -----------------------------------------------------------------------------


class TestGateIsInertForOrdinaryRuns:
    async def test_no_session_means_resume_without_pausing(self, env):
        repo, pipeline, _original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            run = await env.executor.start_pipeline(db, pipeline, repo)
        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            refreshed = await db.get(PipelineRun, run.id)
            assert refreshed.status == RunStatus.PASSED.value
        assert len(env.local.ran) == 2

    async def test_gate_result_reports_it_never_paused(self, env):
        repo, pipeline, _original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            run = await env.executor.start_pipeline(db, pipeline, repo)
        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            step_run = (
                await db.execute(
                    select(StepRun).where(StepRun.pipeline_run_id == run.id)
                )
            ).scalars().first()
        result = await debug_session_service.gate(
            env.factory, run.id, step_run.id, SimpleNamespace(value="local")
        )
        assert result.outcome is DebugGateOutcome.RESUME
        assert result.paused is False

    async def test_a_step_not_named_by_a_breakpoint_is_not_paused(self, env):
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["1"],
            )
        # Step 0 is not breakpointed: it runs, and the run reaches step 1.
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        assert env.local.ran, "step 0 should have executed without pausing"
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)


async def _session_status(factory, session_id, state) -> bool:
    session = await read_session(factory, session_id)
    return session.status == state.value


# -----------------------------------------------------------------------------
# Pausing
# -----------------------------------------------------------------------------


class TestGatePausesAndResumes:
    async def test_breakpoint_pauses_before_the_step_executes(self, env):
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        assert env.local.ran == [], "the step must NOT have executed at the gate"

        row = await read_session(env.factory, session.id)
        assert row.current_step_key == "0"
        assert row.current_step_name == "first"
        assert row.current_step_executor == "local"
        assert json.loads(row.hit_breakpoints) == ["0"]
        assert row.expires_at is not None

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)
        assert len(env.local.ran) == 2

    async def test_the_session_row_exists_before_any_step_run_does(self, env):
        """The create-ordering fix: failure_01 never started the run at all,
        and starting it before the row lands is a race the entry step wins."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        observed: list[bool] = []

        original_gate = debug_session_service.gate

        async def spying_gate(factory, run_id, step_run_id, mode):
            async with env.factory() as db:
                found = (
                    await db.execute(
                        select(DebugSession).where(
                            DebugSession.pipeline_run_id == run_id
                        )
                    )
                ).scalar_one_or_none()
            observed.append(found is not None)
            return await original_gate(factory, run_id, step_run_id, mode)

        debug_session_service.gate = spying_gate
        try:
            async with env.factory() as db:
                session, run = await debug_session_service.create(
                    db,
                    original_run=original,
                    pipeline=pipeline,
                    repo=repo,
                    breakpoints=["0"],
                )
            await wait_until(
                lambda: _session_status(
                    env.factory, session.id, DebugState.WAITING_AT_BP
                )
            )
            async with env.factory() as db:
                await debug_session_service.resume(db, session.id)
            await env.executor.wait_for_run(run.id)
        finally:
            debug_session_service.gate = original_gate

        assert observed and all(observed), (
            "every gate call saw the DebugSession row - the entry step never "
            "raced past a breakpoint"
        )

    async def test_a_paused_gate_broadcasts_debug_session_status(self, env):
        """R6: the REAL ConnectionManager, so an arity bug is a real failure."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        frames = env.socket.of_type("debug_session_status")
        assert frames, "the pause must be visible on the WS channel"
        waiting = [f for f in frames if f["status"] == DebugState.WAITING_AT_BP.value]
        assert waiting
        assert waiting[-1]["current_step"]["key"] == "0"
        assert waiting[-1]["attach_available"] is True

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)

    async def test_the_paused_step_says_so_in_its_own_logs(self, env):
        """C11: one writer, and the plain log view still explains the pause."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )

        async def notice_landed():
            async with env.factory() as db:
                step_run = (
                    await db.execute(
                        select(StepRun).where(StepRun.pipeline_run_id == run.id)
                    )
                ).scalars().first()
            return step_run is not None and "[debug] paused before step" in (
                step_run.logs or ""
            )

        await wait_until(notice_landed)
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)


# -----------------------------------------------------------------------------
# C1: the placement
# -----------------------------------------------------------------------------


class TestGatePlacementDoesNotHoldTheRunLock:
    async def test_a_parallel_sibling_finishes_while_its_peer_is_paused(self, env):
        """Contract C1. A gate in `_dispatch_step_run` would hold
        `self._run_lock` for the whole pause, and this sibling could never
        finish - the gate would deadlock the run it exists to debug."""
        graph = {
            "entry_points": ["paused", "sibling"],
            "steps": {
                "paused": {
                    "name": "paused",
                    "type": "script",
                    "config": {"command": "echo p"},
                },
                "sibling": {
                    "name": "sibling",
                    "type": "script",
                    "config": {"command": "echo s"},
                },
            },
            "edges": [],
        }
        repo, pipeline, original = await seed(env.factory, graph=graph)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["paused"],
            )

        async def sibling_done():
            async with env.factory() as db:
                rows = (
                    await db.execute(
                        select(StepRun).where(StepRun.pipeline_run_id == run.id)
                    )
                ).scalars().all()
            return any(
                r.step_id == "sibling" and r.status == RunStatus.PASSED.value
                for r in rows
            )

        # Order matters: establish the PAUSE first, then watch the sibling
        # finish underneath it. Waiting only for the sibling would let a run
        # whose gate had not armed yet pass this test for the wrong reason.
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        await wait_until(sibling_done)
        row = await read_session(env.factory, session.id)
        assert row.status == DebugState.WAITING_AT_BP.value, (
            "the sibling finished, so the run lock was free - but the peer "
            "must still be held at its breakpoint"
        )
        assert row.current_step_key == "paused"

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)

    async def test_the_database_is_writable_while_a_step_is_paused(self, env):
        """Contract C4: the gate holds no session across the pause. A pinned
        connection or an aging transaction snapshot would show up here."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            probe = Repo(
                id=str(uuid4()), name="written-during-pause", default_branch="main"
            )
            db.add(probe)
            await asyncio.wait_for(db.commit(), timeout=5.0)
            assert await db.get(Repo, probe.id) is not None

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)


# -----------------------------------------------------------------------------
# C3: nothing can reap a paused step
# -----------------------------------------------------------------------------


class TestPausedStepIsNotReapable:
    async def test_a_paused_step_has_no_step_execution_row(self, env):
        """Contract C3, the structural half. No row means no `timeout_at`,
        no `last_heartbeat`, and nothing for the orphan recovery to find -
        heartbeat suspension by PLACEMENT, with no suspension flag."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            step_runs = (
                await db.execute(
                    select(StepRun).where(StepRun.pipeline_run_id == run.id)
                )
            ).scalars().all()
            executions = (
                await db.execute(
                    select(StepExecution).where(
                        StepExecution.step_run_id.in_([s.id for s in step_runs])
                    )
                )
            ).scalars().all()
        assert executions == []
        assert [s.status for s in step_runs] == [RunStatus.RUNNING.value]

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)

    async def test_orphan_recovery_finds_nothing_to_reap(self, env):
        """Contract C3, the behavioural half: run the real recovery scan
        against a paused run and assert it returns nothing."""
        from app.services.execution.recovery import recover_orphaned_executions

        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            reaped = await recover_orphaned_executions(db)
        assert reaped == [] or reaped == 0 or not reaped

        async with env.factory() as db:
            refreshed = (
                await db.execute(
                    select(StepRun).where(StepRun.pipeline_run_id == run.id)
                )
            ).scalars().all()
        assert [s.status for s in refreshed] == [RunStatus.RUNNING.value]

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)


# -----------------------------------------------------------------------------
# C6 / C7: the row is the truth, the gate is the timeout owner
# -----------------------------------------------------------------------------


class TestRowIsTheTruth:
    async def test_a_direct_row_write_releases_the_gate_without_any_signal(self, env):
        """Contract C6. Nothing pokes the event here: the row is edited from
        an unrelated session, and the gate's next poll must notice. A design
        that trusted the in-memory event would hang forever."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            row = await db.get(DebugSession, session.id)
            row.status = DebugState.PENDING.value
            row.current_step_key = None
            row.expires_at = None
            await db.commit()

        await wait_until(lambda: _ran(env))
        await env.executor.wait_for_run(run.id)
        assert len(env.local.ran) == 2

    async def test_no_background_timeout_task_exists(self, env):
        """Contract C7: the paused gate IS the timeout owner. failure_01's
        monitor task was never started; a task here would be a new thing to
        leak and a new thing for reset() to strand."""
        assert not any(
            isinstance(value, asyncio.Task)
            for value in vars(debug_session_service).values()
        )
        assert not any(
            isinstance(value, (list, dict))
            and any(isinstance(v, asyncio.Task) for v in _values(value))
            for value in vars(debug_session_service).values()
        )

    async def test_an_expired_pause_fails_the_step_through_the_normal_path(self, env):
        """Contract C7 + the reuse property: a timed-out gate finishes the
        step through `_finish_local_step`, so there is no second terminal
        path for debug runs to drift from."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        from datetime import datetime, timedelta

        async with env.factory() as db:
            row = await db.get(DebugSession, session.id)
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            await db.commit()

        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            refreshed = await db.get(PipelineRun, run.id)
            step_rows = (
                await db.execute(
                    select(StepRun).where(StepRun.pipeline_run_id == run.id)
                )
            ).scalars().all()
            ended = await db.get(DebugSession, session.id)
        assert refreshed.status == RunStatus.FAILED.value
        assert step_rows[0].status == RunStatus.FAILED.value
        assert "timed out" in (step_rows[0].error or "")
        assert ended.status == DebugState.TIMEOUT.value
        assert ended.end_reason == "timed out at breakpoint"
        assert env.local.ran == []


def _values(container):
    return container.values() if isinstance(container, dict) else container


async def _ran(env) -> bool:
    return bool(env.local.ran)


# -----------------------------------------------------------------------------
# C8: the workspace pin
# -----------------------------------------------------------------------------


class TestWorkspacePin:
    async def test_the_volume_is_pinned_while_paused_and_released_after(self, env):
        """Contract C8. Without the pin, a breakpoint on step 0 would attach a
        sidecar to a volume that does not exist yet."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        assert env.workspace.use_count(run.id) == 1
        assert env.workspace.op_names()[:2] == ["get_or_create", "acquire"]

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        await env.executor.wait_for_run(run.id)
        # Every acquire is paired with a release: the pause's, plus the two
        # the steps themselves take.
        assert env.workspace.op_names().count("acquire") == env.workspace.op_names().count(
            "release"
        )

    async def test_a_remote_step_is_paused_without_a_local_pin(self, env):
        """C16/§5: a remote step's volume lives on the runner host, so the
        backend must not create one - the pause is still real."""
        repo, pipeline, original = await seed(env.factory, steps=SCRIPT_STEPS)
        async with env.factory() as db:
            run = PipelineRun(
                id=str(uuid4()),
                pipeline_id=pipeline.id,
                status=RunStatus.RUNNING.value,
                trigger_type="debug_rerun",
                current_step=0,
                steps_completed=0,
                steps_total=2,
            )
            db.add(run)
            await db.commit()
            step_run = StepRun(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                step_index=0,
                step_name="first",
                status=RunStatus.RUNNING.value,
                executor="remote",
            )
            db.add(step_run)
            session = DebugSession(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                status=DebugState.PENDING.value,
                breakpoints=json.dumps(["0"]),
                hit_breakpoints=json.dumps([]),
                timeout_seconds=3600,
                max_timeout_seconds=14400,
            )
            db.add(session)
            await db.commit()

        gate_task = asyncio.create_task(
            debug_session_service.gate(
                env.factory, run.id, step_run.id, SimpleNamespace(value="remote")
            )
        )
        await wait_until(
            lambda: _session_status(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        assert env.workspace.op_names() == [], (
            "the backend must not provision a workspace for a remote step"
        )
        row = await read_session(env.factory, session.id)
        assert row.current_step_executor == "remote"
        available, reason = debug_session_service.attachability(row)
        assert available is False
        assert "remote runner" in reason

        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)
        result = await asyncio.wait_for(gate_task, timeout=5.0)
        assert result.outcome is DebugGateOutcome.RESUME
        assert result.paused is True
