"""Debug session lifecycle - contracts C5, C9, C10, C14, C16, C18, C20.

Phase 12.7. These pin the three things failure_01's service got wrong (its
`create` never started the run, its `resume` ended the session, its timeout
monitor was never started) plus the scope decisions that came out of the
rebuild.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
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
from app.models import Card, Pipeline, PipelineRun, Repo, StepRun
from app.models.card import CardStatus
from app.models.debug import DebugSession
from app.models.pipeline import RunStatus
from app.services.execution import debug_session_service as service_module
from app.services.execution.debug_session_service import (
    DebugSessionError,
    debug_session_service,
)
from app.services.execution.debug_state import DebugState
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name


class Decision:
    def __init__(self, mode: str, reason: str):
        self.mode = mode
        self.reason = reason


class LocalRouter:
    def decide(self, step_type: str, step_config: dict) -> Decision:
        return Decision("local", "local")


class FakeWorkspaceService:
    def __init__(self):
        self.ops: list[str] = []
        self.workspaces: dict[str, SimpleNamespace] = {}

    async def get_or_create(self, db, pipeline_run_id, repo_id, branch, commit_sha):
        self.ops.append("get_or_create")
        ws = self.workspaces.setdefault(
            pipeline_run_id,
            SimpleNamespace(
                id=f"ws-{pipeline_run_id[:8]}",
                volume_name=generate_volume_name(pipeline_run_id),
                use_count=0,
            ),
        )
        return ws

    async def acquire(self, db, workspace_id):
        self.ops.append("acquire")

    async def release(self, db, workspace_id):
        self.ops.append("release")

    async def cleanup(self, db, pipeline_run_id):
        self.ops.append("cleanup")


class FakeLocalExecutor:
    def __init__(self):
        self.ran: list[str] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        return False

    async def find_missing_images(self, images) -> list[str]:
        return []

    async def execute_step(self, step_config, execution_context):
        self.ran.append(execution_context["execution_key"])
        for event in (
            {"type": "status", "status": "running"},
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


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "GATE_POLL_SECONDS", 0.05)
    db_path = (tmp_path / "debug_service.db").as_posix()
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

    trigger_actions: list[tuple] = []
    merges: list[tuple] = []

    async def spy_trigger_action(db, pipeline_run, context, action, success):
        trigger_actions.append((pipeline_run.id, action, success))

    async def spy_merge(*args, **kwargs):
        merges.append(args)
        return True

    executor._execute_trigger_action = spy_trigger_action
    executor._merge_branch = spy_merge

    await debug_session_service.reset()
    debug_session_service._executor = executor

    socket = CapturingSocket()
    manager.active_connections.append(socket)

    yield SimpleNamespace(
        factory=factory,
        executor=executor,
        workspace=workspace,
        local=local,
        socket=socket,
        trigger_actions=trigger_actions,
        merges=merges,
    )

    try:
        await executor.reset()
    except Exception:
        pass
    debug_session_service._executor = None
    await debug_session_service.reset()
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    await engine.dispose()


STEPS = [
    {"name": "first", "type": "script", "config": {"command": "echo one"}},
    {"name": "second", "type": "script", "config": {"command": "echo two"}},
    {"name": "third", "type": "script", "config": {"command": "echo three"}},
]


async def seed(factory, *, trigger_type="manual", context=None, card_id=None):
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()), name="svc-repo", default_branch="main", is_ingested=True
        )
        db.add(repo)
        await db.commit()
        await db.refresh(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="svc-pipeline",
            steps=json.dumps(STEPS),
        )
        db.add(pipeline)
        if card_id is not None:
            db.add(
                Card(
                    id=card_id,
                    repo_id=repo.id,
                    title="a card",
                    status=CardStatus.IN_PROGRESS.value,
                )
            )
        await db.commit()
        await db.refresh(pipeline)
        original = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.FAILED.value,
            trigger_type=trigger_type,
            trigger_ref=card_id,
            trigger_context=json.dumps(context) if context else None,
            current_step=0,
            steps_completed=0,
            steps_total=len(STEPS),
        )
        db.add(original)
        await db.commit()
        await db.refresh(original)
        return repo, pipeline, original


async def wait_until(predicate, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


async def status_is(factory, session_id, state) -> bool:
    async with factory() as db:
        row = await db.get(DebugSession, session_id)
    return row is not None and row.status == state.value


# -----------------------------------------------------------------------------
# C5: resume does not end the session
# -----------------------------------------------------------------------------


class TestMultipleBreakpointsWork:
    async def test_two_breakpoints_in_one_run_both_fire(self, env):
        """Contract C5, the whole reason `resume` goes to PENDING.

        failure_01 ended the session on resume, so the SECOND breakpoint had
        no live session to pause into and silently never fired. This is the
        regression that proves it does now.
        """
        repo, pipeline, original = await seed(
            env.factory, context={"branch": "main", "commit_sha": "deadbee"}
        )
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0", "2"],
            )

        # First breakpoint
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            row = await db.get(DebugSession, session.id)
            assert row.current_step_key == "0"
            _resumed, next_bp = await debug_session_service.resume(db, session.id)
            assert _resumed.status == DebugState.PENDING.value, (
                "resume must return the session to PENDING, never ENDED"
            )
            assert next_bp == "2"

        # Second breakpoint - only reachable because the session stayed alive
        await wait_until(
            lambda: _paused_at(env.factory, session.id, "2")
        )
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id)

        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            final = await db.get(PipelineRun, run.id)
            row = await db.get(DebugSession, session.id)
        assert final.status == RunStatus.PASSED.value
        assert len(env.local.ran) == 3
        assert json.loads(row.hit_breakpoints) == ["0", "2"]
        assert row.status == DebugState.ENDED.value
        assert row.end_reason == "pipeline completed"

    async def test_clear_remaining_runs_to_completion(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0", "1", "2"],
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            row, next_bp = await debug_session_service.resume(
                db, session.id, clear_remaining=True
            )
        assert next_bp is None
        assert json.loads(row.breakpoints) == []
        await env.executor.wait_for_run(run.id)
        assert len(env.local.ran) == 3

    async def test_resume_on_a_running_session_is_refused(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=[]
            )
            with pytest.raises(DebugSessionError) as exc:
                await debug_session_service.resume(db, session.id)
            assert "not paused" in str(exc.value)
        await env.executor.wait_for_run(run.id)


async def _paused_at(factory, session_id, key) -> bool:
    async with factory() as db:
        row = await db.get(DebugSession, session_id)
    return (
        row is not None
        and row.status == DebugState.WAITING_AT_BP.value
        and row.current_step_key == key
    )


# -----------------------------------------------------------------------------
# C10: the re-run's trigger_context is rebuilt, not copied
# -----------------------------------------------------------------------------


class TestTriggerContextIsRebuilt:
    async def test_a_debug_rerun_can_never_merge_or_move_a_card(self, env):
        """Contract C10. Copying `trigger_context` would let a debug re-run
        of a card_work pipeline merge a branch and walk a card to in_review -
        side effects nobody asked for by pressing "debug"."""
        card_id = str(uuid4())
        repo, pipeline, original = await seed(
            env.factory,
            trigger_type="card_work",
            context={
                "branch": "feature/x",
                "commit_sha": "cafe123",
                "card_id": card_id,
                "on_pass": "merge",
                "on_fail": "card",
            },
            card_id=card_id,
        )

        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=[]
            )
        await env.executor.wait_for_run(run.id)

        async with env.factory() as db:
            new_run = await db.get(PipelineRun, run.id)
            card_after = await db.get(Card, card_id)
        context = json.loads(new_run.trigger_context)
        assert context == {"branch": "feature/x", "commit_sha": "cafe123"}
        assert "on_pass" not in context and "card_id" not in context
        assert new_run.trigger_type == "debug_rerun"
        assert new_run.trigger_ref == original.id
        assert env.trigger_actions == [], "no on_pass/on_fail action may fire"
        assert env.merges == [], "a debug re-run must never merge"
        assert card_after.status == CardStatus.IN_PROGRESS.value

    async def test_an_explicit_commit_overrides_the_original(self, env):
        repo, pipeline, original = await seed(
            env.factory, context={"branch": "main", "commit_sha": "old"}
        )
        async with env.factory() as db:
            _session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=[],
                use_original_commit=False,
                commit_sha="new123",
                branch="hotfix",
            )
        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            new_run = await db.get(PipelineRun, run.id)
        assert json.loads(new_run.trigger_context) == {
            "branch": "hotfix",
            "commit_sha": "new123",
        }


# -----------------------------------------------------------------------------
# Validation, abort, extend
# -----------------------------------------------------------------------------


class TestBreakpointValidation:
    async def test_an_unknown_key_is_refused_before_the_run_starts(self, env):
        """Contract C2: an unknown key would otherwise be a breakpoint that
        silently never fires."""
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            with pytest.raises(DebugSessionError) as exc:
                await debug_session_service.create(
                    db,
                    original_run=original,
                    pipeline=pipeline,
                    repo=repo,
                    breakpoints=["0", "nope"],
                )
            assert "nope" in str(exc.value)
            runs = (
                await db.execute(
                    select(PipelineRun).where(
                        PipelineRun.trigger_type == "debug_rerun"
                    )
                )
            ).scalars().all()
        assert runs == [], "a refused create must not have started a run"


class TestAbort:
    async def test_abort_ends_the_session_and_cancels_the_run(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=["0"]
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            row = await debug_session_service.abort(db, session.id)
        assert row.status == DebugState.ENDED.value
        assert row.end_reason == "aborted by user"

        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            final = await db.get(PipelineRun, run.id)
        assert final.status == RunStatus.CANCELLED.value
        assert env.local.ran == [], "the paused step must never have executed"

    async def test_aborting_twice_is_refused_with_the_reason(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=["0"]
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            await debug_session_service.abort(db, session.id)
            with pytest.raises(DebugSessionError) as exc:
                await debug_session_service.abort(db, session.id)
            assert "aborted by user" in str(exc.value)
        await env.executor.wait_for_run(run.id)


class TestExtend:
    async def test_extend_moves_the_deadline(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=["0"]
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            before = (await db.get(DebugSession, session.id)).expires_at
            row, clamped = await debug_session_service.extend(db, session.id, 30)
        assert row.expires_at > before
        assert clamped is False
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id, clear_remaining=True)
        await env.executor.wait_for_run(run.id)

    async def test_extend_is_clamped_to_max_timeout_and_says_so(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=["0"]
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            row, clamped = await debug_session_service.extend(db, session.id, 180)
            for _ in range(5):
                row, clamped = await debug_session_service.extend(db, session.id, 180)
            anchor = row.breakpoint_hit_at
        assert clamped is True
        assert row.expires_at <= anchor + timedelta(seconds=row.max_timeout_seconds)
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id, clear_remaining=True)
        await env.executor.wait_for_run(run.id)

    async def test_extend_on_an_unpaused_session_is_refused(self, env):
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db, original_run=original, pipeline=pipeline, repo=repo, breakpoints=[]
            )
            with pytest.raises(DebugSessionError) as exc:
                await debug_session_service.extend(db, session.id, 30)
            assert "no deadline" in str(exc.value)
        await env.executor.wait_for_run(run.id)


# -----------------------------------------------------------------------------
# Run completion / restart
# -----------------------------------------------------------------------------


class TestSessionNeverOutlivesItsRun:
    async def test_completion_reports_breakpoints_that_never_fired(self, env):
        """An unreachable breakpoint is a visible fact, not silence."""
        repo, pipeline, original = await seed(env.factory)
        async with env.factory() as db:
            session, run = await debug_session_service.create(
                db,
                original_run=original,
                pipeline=pipeline,
                repo=repo,
                breakpoints=["0"],
            )
        await wait_until(
            lambda: status_is(env.factory, session.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            await debug_session_service.resume(db, session.id, clear_remaining=True)
        await env.executor.wait_for_run(run.id)
        async with env.factory() as db:
            row = await db.get(DebugSession, session.id)
        assert row.status == DebugState.ENDED.value
        assert row.end_reason == "pipeline completed"

        # And the unreachable case: a breakpoint whose step never ran.
        repo2, pipeline2, original2 = await seed(env.factory)
        async with env.factory() as db:
            session2, run2 = await debug_session_service.create(
                db,
                original_run=original2,
                pipeline=pipeline2,
                repo=repo2,
                breakpoints=["1"],
            )
        await wait_until(
            lambda: status_is(env.factory, session2.id, DebugState.WAITING_AT_BP)
        )
        async with env.factory() as db:
            await debug_session_service.resume(db, session2.id, clear_remaining=False)
        await env.executor.wait_for_run(run2.id)
        async with env.factory() as db:
            row2 = await db.get(DebugSession, session2.id)
        assert row2.end_reason == "pipeline completed"

    async def test_startup_sweep_ends_a_session_stranded_by_a_restart(self, env, monkeypatch):
        """Contract C20: a paused gate is an in-process task, so a restart
        kills it. Honest handling beats a run that is neither running nor
        finished."""
        swept: list[set] = []

        async def fake_sweep(live_ids):
            swept.append(live_ids)
            return 0

        from app.services.execution import debug_terminal

        monkeypatch.setattr(
            debug_terminal.debug_terminal_service, "sweep_orphan_sidecars", fake_sweep
        )

        async with env.factory() as db:
            repo = Repo(id=str(uuid4()), name="r", default_branch="main")
            db.add(repo)
            pipeline = Pipeline(
                id=str(uuid4()), repo_id=repo.id, name="p", steps=json.dumps(STEPS)
            )
            db.add(pipeline)
            run = PipelineRun(
                id=str(uuid4()),
                pipeline_id=pipeline.id,
                status=RunStatus.RUNNING.value,
                trigger_type="debug_rerun",
                current_step=0,
                steps_completed=0,
                steps_total=3,
            )
            db.add(run)
            stranded_step = StepRun(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                step_index=0,
                step_name="first",
                status=RunStatus.RUNNING.value,
                executor="local",
            )
            db.add(stranded_step)
            db.add(
                DebugSession(
                    id=str(uuid4()),
                    pipeline_run_id=run.id,
                    status=DebugState.WAITING_AT_BP.value,
                    breakpoints=json.dumps(["0"]),
                    hit_breakpoints=json.dumps(["0"]),
                    timeout_seconds=3600,
                    max_timeout_seconds=14400,
                    current_step_key="0",
                )
            )
            await db.commit()

            count = await debug_session_service.sweep_paused_sessions(db)
            assert count == 1
            row = (
                await db.execute(select(DebugSession))
            ).scalars().one()
            refreshed = await db.get(PipelineRun, run.id)
            stranded_after = await db.get(StepRun, stranded_step.id)
        assert row.status == DebugState.ENDED.value
        assert row.end_reason == "backend restarted while paused"
        assert refreshed.status == RunStatus.FAILED.value
        # No half-alive step either: a RUNNING StepRun under a FAILED run is
        # precisely the state this sweep exists to prevent.
        assert stranded_after.status == RunStatus.FAILED.value
        assert stranded_after.error == "backend restarted while paused"
        assert swept == [set()]


# -----------------------------------------------------------------------------
# Projection: what the API and the WS frame agree on
# -----------------------------------------------------------------------------


class TestProjection:
    def test_the_projection_never_carries_a_token(self):
        """Contract C14: failure_01 put a long-lived secret in a polled GET."""
        session = DebugSession(
            id=str(uuid4()),
            pipeline_run_id=str(uuid4()),
            status=DebugState.WAITING_AT_BP.value,
            breakpoints=json.dumps(["0", "1"]),
            hit_breakpoints=json.dumps(["0"]),
            timeout_seconds=3600,
            max_timeout_seconds=14400,
            current_step_key="0",
            current_step_name="first",
            current_step_index=0,
            current_step_executor="local",
        )
        payload = debug_session_service.to_dict(session)
        assert "token" not in payload
        assert not any("token" in key for key in payload if key != "join_command")
        assert payload["breakpoints_pending"] == ["1"]
        assert payload["breakpoints_hit"] == ["0"]

    def test_attach_unavailable_always_states_a_reason(self):
        """R1: never a silent degrade. Every False carries a sentence."""
        base = dict(
            id=str(uuid4()),
            pipeline_run_id=str(uuid4()),
            breakpoints="[]",
            hit_breakpoints="[]",
            timeout_seconds=3600,
            max_timeout_seconds=14400,
        )
        for status, key, executor in (
            (DebugState.PENDING.value, None, None),
            (DebugState.ENDED.value, None, None),
            (DebugState.WAITING_AT_BP.value, "0", "remote"),
        ):
            session = DebugSession(
                status=status, current_step_key=key, current_step_executor=executor, **base
            )
            available, reason = debug_session_service.attachability(session)
            assert available is False
            assert reason, f"{status} must state why attach is unavailable"

        local = DebugSession(
            status=DebugState.WAITING_AT_BP.value,
            current_step_key="0",
            current_step_executor="local",
            **base,
        )
        available, reason = debug_session_service.attachability(local)
        assert available is True and reason is None

    def test_no_new_run_status_members_were_added(self):
        """Contract C18: debug state lives on the session row only.

        RunStatus has five members pinned by dozens of tests and every UI
        colour map; PLAN's proposed debug_* members are deliberately dropped.
        """
        assert {s.value for s in RunStatus} == {
            "pending",
            "running",
            "passed",
            "failed",
            "cancelled",
        }
