"""
Unit tests for the 12.2-INT pipeline executor rewire: router dispatch,
async-first local execution, state-machine-driven lifecycle, and
observability (R1).

The seams contracted to other agents (ExecutionRouter, WorkspaceService,
LocalExecutor) are replaced with in-process fakes that honour the
pre-agreed interface contracts verbatim. The WebSocket manager is NEVER
mocked on broadcast paths (R6): tests attach a capturing transport to the
real ConnectionManager singleton.

Includes the R1 spy test: locally-routed steps must never enqueue to
job_queue.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo, StepRun, Card, Job
from app.models.pipeline import ExecutorMode, RunStatus
from app.services.pipeline_executor import (
    PipelineExecutor,
    build_verification_step,
)
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name


# -----------------------------------------------------------------------------
# Contract fakes (shapes per the 12.2-INT pre-agreed interface contracts)
# -----------------------------------------------------------------------------

class Decision:
    """RoutingDecision shape per contract: mode + reason."""

    def __init__(self, mode: str, reason: str):
        self.mode = mode
        self.reason = reason


class ContractRouter:
    """Implements the contracted routing rules: script/docker/agent -> local,
    explicit executor=legacy override -> legacy.

    12.5 fallout: agent steps route LOCAL by default ("agent-default-local");
    `executor: legacy` on an agent step is the LAST legacy escape hatch and
    stays honored, loudly.

    12.4 fallout: `executor: legacy` on a script/docker step RAISES, exactly
    as the real ExecutionRouter does. Phase 12.4 deleted script/docker
    execution from the runners, so honoring that override would enqueue a
    job guaranteed to be rejected on pickup. Mirrored here so this fake
    cannot drift into contradicting the router it stands in for.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def decide(self, step_type: str, step_config: dict) -> Decision:
        self.calls.append((step_type, dict(step_config or {})))
        if (step_config or {}).get("executor") == "legacy":
            if step_type in ("script", "docker"):
                raise ValueError(
                    f"Unsupported combination: step_type={step_type!r} with "
                    "executor='legacy' (the legacy path for script/docker was "
                    "removed in Phase 12.4)"
                )
            return Decision("legacy", "explicit-override")
        if step_type in ("script", "docker"):
            return Decision("local", f"{step_type}-runs-local")
        if step_type == "agent":
            return Decision("local", "agent-default-local")
        return Decision("legacy", f"unknown-step-type:{step_type}")


class ExplodingRouter:
    def decide(self, step_type: str, step_config: dict):
        raise RuntimeError("router exploded")


class FakeWorkspaceService:
    """Records the workspace lifecycle calls the executor makes."""

    def __init__(self):
        self.ops: list[tuple] = []
        self.workspaces: dict[str, SimpleNamespace] = {}

    async def get_or_create(self, db, pipeline_run_id, repo_id, branch, commit_sha):
        self.ops.append(("get_or_create", pipeline_run_id, repo_id, branch, commit_sha))
        ws = self.workspaces.get(pipeline_run_id)
        if ws is None:
            ws = SimpleNamespace(
                id=f"ws-{pipeline_run_id[:8]}",
                pipeline_run_id=pipeline_run_id,
                volume_name=generate_volume_name(pipeline_run_id),
                status="ready",
                use_count=0,
            )
            self.workspaces[pipeline_run_id] = ws
        return ws

    async def acquire(self, db, workspace_id):
        self.ops.append(("acquire", workspace_id))

    async def release(self, db, workspace_id):
        self.ops.append(("release", workspace_id))

    async def cleanup(self, db, pipeline_run_id):
        self.ops.append(("cleanup", pipeline_run_id))

    def op_names(self) -> list[str]:
        return [op[0] for op in self.ops]


class FakeLocalExecutor:
    """Yields a scripted event stream shaped like LocalExecutor's."""

    def __init__(self, events=None, gate: asyncio.Event | None = None):
        self.events = events  # None -> default success script
        self.gate = gate
        self.calls: list[tuple[dict, dict]] = []
        self.label_queries: list[str] = []
        # Image preflight contract (12.3): tags in `missing_images` are
        # reported unresolvable; every preflight query is recorded.
        self.missing_images: list[str] = []
        self.preflight_queries: list[list[str]] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        """Contract method (12.3): these fakes model STOCK images - no
        control-layer capability label, so every step takes stdout mode.

        The lazyaf-* agent images are the exception (12.5): they DO declare
        the label in reality, and an agent step that cannot get control mode
        fails at dispatch by design - modelling them as unlabeled here would
        assert the wrong thing.
        """
        self.label_queries.append(image)
        return image.startswith("lazyaf-")

    async def find_missing_images(self, images) -> list[str]:
        """Contract method (12.3 image preflight)."""
        self.preflight_queries.append(list(images))
        return [image for image in images if image in self.missing_images]

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        if self.gate is not None:
            await self.gate.wait()
        events = self.events
        if events is None:
            events = [
                {"type": "status", "status": "preparing"},
                {"type": "status", "status": "running"},
                {"type": "log", "line": f"ran: {step_config.get('command', '')}"},
                {"type": "result", "status": "completed", "exit_code": 0},
            ]
        for event in events:
            await asyncio.sleep(0)
            yield event

    async def cancel_step(self, execution_key):
        return False


class EnqueueSpy:
    """Recording spy for job_queue (R1 spy test)."""

    def __init__(self):
        self.calls = []

    async def enqueue(self, job):
        self.calls.append(job)
        return job.id


class CapturingSocket:
    """Capturing transport attached to the REAL ConnectionManager (R6)."""

    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def env(tmp_path):
    """File-backed engine + executor wired with contract fakes + real WS
    manager with a capturing transport."""
    db_path = (tmp_path / "local_dispatch.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    executor = PipelineExecutor()
    router = ContractRouter()
    workspace = FakeWorkspaceService()
    local = FakeLocalExecutor()
    executor._router = router
    executor._workspace_service = workspace
    executor._local_executor = local

    socket = CapturingSocket()
    manager.active_connections.append(socket)

    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        executor=executor,
        router=router,
        workspace=workspace,
        local=local,
        socket=socket,
    )

    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    await engine.dispose()


@pytest.fixture
def enqueue_spy(monkeypatch):
    """Patch the executor module's job_queue with a recording spy."""
    spy = EnqueueSpy()
    import app.services.pipeline_executor as pe

    monkeypatch.setattr(pe, "job_queue", spy)
    return spy


async def make_repo(factory) -> Repo:
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="local-dispatch-repo",
            default_branch="main",
            is_ingested=True,
        )
        db.add(repo)
        await db.commit()
        await db.refresh(repo)
        return repo


async def make_linear_pipeline(factory, repo: Repo, steps: list[dict]) -> Pipeline:
    async with factory() as db:
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="local-dispatch-pipeline",
            steps=json.dumps(steps),
        )
        db.add(pipeline)
        await db.commit()
        await db.refresh(pipeline)
        return pipeline


async def make_graph_pipeline(factory, repo: Repo, graph: dict) -> Pipeline:
    async with factory() as db:
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="local-dispatch-graph-pipeline",
            steps="[]",
            steps_graph=json.dumps(graph),
        )
        db.add(pipeline)
        await db.commit()
        await db.refresh(pipeline)
        return pipeline


async def start_and_wait(env, pipeline, repo, **kwargs) -> PipelineRun:
    async with env.factory() as db:
        run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo, **kwargs)
        run_id = run.id
    await env.executor.wait_for_run(run_id)
    return await fetch_run(env, run_id)


async def fetch_run(env, run_id: str) -> PipelineRun:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


async def fetch_all(env, model):
    from sqlalchemy import select

    async with env.factory() as db:
        result = await db.execute(select(model))
        return list(result.scalars().all())


def script_step(name="Script", command="echo hi", **extra) -> dict:
    step = {"name": name, "type": "script", "config": {"command": command}}
    step.update(extra)
    return step


# -----------------------------------------------------------------------------
# Routing wiring
# -----------------------------------------------------------------------------

class TestRoutingDispatch:
    async def test_script_step_routes_local_and_records_executor(self, env, enqueue_spy):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert len(run.step_runs) == 1
        assert run.step_runs[0].executor == "local"
        # Router was consulted with the contract signature
        assert env.router.calls == [("script", {"command": "echo hi"})]

    async def test_spy_local_steps_never_enqueue_to_job_queue(self, env, enqueue_spy):
        """R1 SPY TEST: a locally-routed script pipeline makes ZERO calls to
        job_queue.enqueue and creates no Card/Job rows."""
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [script_step("One", "echo one"), script_step("Two", "echo two")],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert enqueue_spy.calls == []
        assert await fetch_all(env, Card) == []
        assert await fetch_all(env, Job) == []
        assert all(sr.executor == "local" for sr in run.step_runs)

    async def test_agent_step_routes_local_and_enqueues_nothing(
        self, env, enqueue_spy
    ):
        """12.5: an agent step runs on the local executor like any other.

        No Card, no Job, no queue entry - a silent fallback to legacy is
        indistinguishable from success (R1), so the absence is asserted.
        """
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{
                "name": "Agent",
                "type": "agent",
                "config": {"agent": "mock", "title": "Do it"},
            }],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert enqueue_spy.calls == []
        assert run.step_runs[0].executor == "local"
        cards = await fetch_all(env, Card)
        jobs = await fetch_all(env, Job)
        assert cards == [] and jobs == []
        assert len(env.local.calls) == 1

    async def test_agent_step_without_agent_key_fails_that_step_loudly(
        self, env, enqueue_spy
    ):
        """No default agent: a step that names none fails at dispatch with
        the valid vocabulary in the message - it never silently picks one."""
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "Agent", "type": "agent", "config": {"title": "Do it"}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        assert enqueue_spy.calls == []
        error = run.step_runs[0].error or ""
        assert "agent" in error and "claude-code" in error

    async def test_explicit_legacy_override_on_agent_step_logged_at_warning(
        self, env, enqueue_spy, caplog
    ):
        """`executor: legacy` on an AGENT step is still honored, loudly.

        After 12.5 this is the LAST remaining legacy escape hatch (R2
        requires it to stay callable until the 12.6 deletion commit), so the
        override still names a path that exists. It stays a WARNING - an
        override is never silent (R1).
        """
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "A", "type": "agent", "config": {"title": "Do it", "executor": "legacy"}}],
        )

        with caplog.at_level(logging.WARNING, logger="app.services.pipeline_executor"):
            run = await start_and_wait(env, pipeline, repo)

        assert run.step_runs[0].executor == "legacy"
        assert len(enqueue_spy.calls) == 1
        assert any("explicit-override" in r.message for r in caplog.records)

    async def test_explicit_legacy_override_on_script_step_fails_loudly(
        self, env, enqueue_spy
    ):
        """`executor: legacy` on a SCRIPT step fails the step at dispatch.

        Updated for Phase 12.4: this used to assert the override routed the
        step legacy and enqueued it. Runners now REJECT script/docker jobs,
        so honoring the override would enqueue a job guaranteed to be failed
        on pickup - the silent in_progress -> failed loop. The router raises
        instead, and the dispatch error path fails the step with a message
        naming the unsupported combination. Nothing is enqueued and nothing
        runs locally: no silent fallback in EITHER direction.
        """
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script", "config": {"command": "echo x", "executor": "legacy"}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "execution routing failed" in step.error
        assert "executor='legacy'" in step.error
        assert step.executor is None
        assert enqueue_spy.calls == []
        assert env.local.calls == []
        assert await fetch_all(env, Job) == []

    async def test_routing_failure_fails_step_and_run_loudly(self, env, enqueue_spy):
        env.executor._router = ExplodingRouter()
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "execution routing failed" in step.error
        assert step.executor is None
        # No silent fallback: nothing enqueued, nothing executed locally
        assert enqueue_spy.calls == []
        assert env.local.calls == []

    async def test_remote_mode_rejected_until_12_6(self, env, enqueue_spy):
        """A router returning 'remote' (valid enum, no execution path yet)
        must fail the step loudly - never fall through to a queue nothing
        dequeues (failure_01 landmine 5)."""

        class RemoteRouter:
            def decide(self, step_type, step_config):
                return Decision(ExecutorMode.REMOTE.value, "testing-remote")

        env.executor._router = RemoteRouter()
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "12.6" in step.error
        assert enqueue_spy.calls == []
        assert env.local.calls == []


# -----------------------------------------------------------------------------
# Async-first (R5)
# -----------------------------------------------------------------------------

class TestAsyncFirst:
    async def test_start_pipeline_returns_before_local_execution_completes(self, env):
        gate = asyncio.Event()
        env.executor._local_executor = FakeLocalExecutor(gate=gate)
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
            # Returned while the (gated) container "runs": run not finished
            assert run.status == RunStatus.RUNNING.value

        fetched = await fetch_run(env, run_id)
        assert fetched.status == RunStatus.RUNNING.value
        assert any(run_id in key for key in env.executor._tasks)

        gate.set()
        await env.executor.wait_for_run(run_id)
        fetched = await fetch_run(env, run_id)
        assert fetched.status == RunStatus.PASSED.value

    async def test_no_leaked_tasks_after_completion(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("A", "echo a"), script_step("B", "echo b")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert not any(run.id in key for key in env.executor._tasks)
        assert run.id not in env.executor._state_machines
        assert run.id not in env.executor._session_factories


# -----------------------------------------------------------------------------
# Local lifecycle: incremental persistence, state machine, workspace
# -----------------------------------------------------------------------------

class TestLocalLifecycle:
    async def test_success_lifecycle_persists_and_broadcasts(self, env):
        env.executor._local_executor = FakeLocalExecutor(events=[
            {"type": "status", "status": "preparing"},
            {"type": "status", "status": "running"},
            {"type": "log", "line": "hello"},
            {"type": "log", "line": "world"},
            {"type": "result", "status": "completed", "exit_code": 0},
        ])
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert run.steps_completed == 1
        step = run.step_runs[0]
        assert step.status == RunStatus.PASSED.value
        assert step.logs == "hello\nworld\n[lazyaf] exit code: 0\n"
        assert step.started_at is not None
        assert step.completed_at is not None
        assert step.error is None

        # Typed WS publishes through the REAL manager (R6)
        log_lines = [p["line"] for p in env.socket.of_type("step_log")]
        assert log_lines == ["hello", "world"]
        statuses = [p["status"] for p in env.socket.of_type("step_update")]
        assert statuses[:2] == ["preparing", "running"]
        assert statuses[-1] == RunStatus.PASSED.value
        # Legacy broadcasts still flow for existing UI consumers
        assert env.socket.of_type("step_run_status")
        assert env.socket.of_type("pipeline_run_status")

    async def test_workspace_acquire_release_cleanup_ordering(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert env.workspace.op_names() == [
            "get_or_create", "acquire", "release", "cleanup",
        ]
        assert env.workspace.ops[0][1] == run.id  # pipeline_run_id
        assert env.workspace.ops[0][2] == repo.id
        assert env.workspace.ops[-1][1] == run.id

    async def test_two_local_steps_share_one_workspace(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("A", "echo a"), script_step("B", "echo b")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        names = env.workspace.op_names()
        assert names.count("get_or_create") == 2  # idempotent per contract
        assert names.count("acquire") == 2
        assert names.count("release") == 2
        assert names.count("cleanup") == 1
        assert len(env.workspace.workspaces) == 1  # one workspace per run

    async def test_branch_and_commit_come_from_trigger_context(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(
            env, pipeline, repo,
            trigger_type="push",
            trigger_context={"branch": "feature-x", "commit_sha": "abc123"},
        )

        assert run.status == RunStatus.PASSED.value
        op = env.workspace.ops[0]
        assert op == ("get_or_create", run.id, repo.id, "feature-x", "abc123")

    async def test_local_execution_config_honours_contract(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script", "timeout": 42,
              "config": {"command": "echo cfg", "environment": {"FOO": "bar"}}}],
        )

        run = await start_and_wait(env, pipeline, repo, params={"PARAM_ONE": "1"})

        assert run.status == RunStatus.PASSED.value
        assert len(env.local.calls) == 1
        exec_config, exec_context = env.local.calls[0]
        assert exec_config["command"] == "echo cfg"
        assert exec_config["timeout"] == 42
        assert exec_config["environment"]["FOO"] == "bar"
        assert exec_config["environment"]["PARAM_ONE"] == "1"
        # Defaults are single-sourced in the EXECUTOR (fix 11): the builder
        # passes only explicit overrides - no image/working_dir/HOME/network
        # defaults injected here, and no dead context keys.
        assert "image" not in exec_config
        assert "working_dir" not in exec_config
        assert "network" not in exec_config
        assert "HOME" not in exec_config["environment"]
        assert exec_context["pipeline_run_id"] == run.id
        assert exec_context["workspace_volume"] == generate_volume_name(run.id)
        for dead_key in ("workspace_addressing", "repo_url", "branch"):
            assert dead_key not in exec_context
        assert run.id in exec_context["execution_key"]

    async def test_step_image_override_wins_over_default(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "docker",
              "config": {"command": "true", "image": "alpine:3.20"}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        exec_config, _ = env.local.calls[0]
        assert exec_config["image"] == "alpine:3.20"

    async def test_continue_in_context_ignored_with_one_time_info(self, env, caplog):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [
                script_step("A", "echo a", continue_in_context=True),
                script_step("B", "echo b", continue_in_context=True),
            ],
        )

        with caplog.at_level(logging.INFO, logger="app.services.pipeline_executor"):
            run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        obsolete_logs = [
            r for r in caplog.records if "continue_in_context is obsolete" in r.message
        ]
        assert len(obsolete_logs) == 1  # one-time INFO, not per-step spam


# -----------------------------------------------------------------------------
# Failure paths
# -----------------------------------------------------------------------------

class TestLocalFailurePaths:
    async def test_failed_result_fails_step_and_run_and_cleans_workspace(self, env):
        env.executor._local_executor = FakeLocalExecutor(events=[
            {"type": "status", "status": "running"},
            {"type": "log", "line": "boom"},
            {"type": "result", "status": "failed", "exit_code": 2},
        ])
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "exit code 2" in step.error
        assert "boom" in step.logs
        # Workspace released AND cleaned even on failure
        names = env.workspace.op_names()
        assert "release" in names and "cleanup" in names
        # No leaked tasks; machine retired
        assert not any(run.id in key for key in env.executor._tasks)
        assert run.id not in env.executor._state_machines

    async def test_timeout_result_fails_step_with_timeout_error(self, env):
        env.executor._local_executor = FakeLocalExecutor(events=[
            {"type": "status", "status": "running"},
            {"type": "status", "status": "timeout"},
            {"type": "result", "status": "timeout", "exit_code": None,
             "timeout_seconds": 300},
        ])
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "timed out" in step.error
        assert "cleanup" in env.workspace.op_names()

    async def test_stream_without_result_event_fails_loudly(self, env):
        env.executor._local_executor = FakeLocalExecutor(events=[
            {"type": "status", "status": "running"},
            {"type": "log", "line": "then silence"},
        ])
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        assert "without a result event" in run.step_runs[0].error

    async def test_workspace_creation_failure_fails_step_and_run(self, env):
        class BrokenWorkspaceService(FakeWorkspaceService):
            async def get_or_create(self, db, pipeline_run_id, repo_id, branch, commit_sha):
                raise RuntimeError("volume creation refused")

        env.executor._workspace_service = BrokenWorkspaceService()
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "volume creation refused" in step.error
        # Container never launched
        assert env.local.calls == []

    async def test_linear_on_failure_next_continues_past_failed_local_step(self, env):
        class PerCommandExecutor(FakeLocalExecutor):
            async def execute_step(self, step_config, execution_context):
                self.calls.append((dict(step_config), dict(execution_context)))
                if "fail" in step_config.get("command", ""):
                    yield {"type": "status", "status": "running"}
                    yield {"type": "result", "status": "failed", "exit_code": 1}
                else:
                    yield {"type": "status", "status": "running"}
                    yield {"type": "log", "line": "ok"}
                    yield {"type": "result", "status": "completed", "exit_code": 0}

        env.executor._local_executor = PerCommandExecutor()
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [
                {"name": "Fails", "type": "script", "on_failure": "next",
                 "config": {"command": "fail now"}},
                script_step("After", "echo after"),
            ],
        )

        run = await start_and_wait(env, pipeline, repo)

        # Step 1 failed but on_failure=next carried on; run completes.
        assert len(run.step_runs) == 2
        statuses = {sr.step_index: sr.status for sr in run.step_runs}
        assert statuses[0] == RunStatus.FAILED.value
        assert statuses[1] == RunStatus.PASSED.value
        # Behavior-compat with main's legacy linear semantics: running past
        # the last step completes the run as passed even if an earlier step
        # failed under on_failure=next (graph pipelines DO check all steps).
        assert run.status == RunStatus.PASSED.value


# -----------------------------------------------------------------------------
# Graph dispatch
# -----------------------------------------------------------------------------

class TestGraphLocalDispatch:
    def _two_entry_graph(self):
        return {
            "version": 2,
            "steps": {
                "a": {"id": "a", "name": "A", "type": "script",
                      "config": {"command": "echo a"}},
                "b": {"id": "b", "name": "B", "type": "script",
                      "config": {"command": "echo b"}},
            },
            "edges": [],
            "entry_points": ["a", "b"],
        }

    async def test_graph_local_steps_fan_out_and_never_enqueue(self, env, enqueue_spy):
        repo = await make_repo(env.factory)
        pipeline = await make_graph_pipeline(env.factory, repo, self._two_entry_graph())

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert enqueue_spy.calls == []
        assert sorted(json.loads(run.completed_step_ids)) == ["a", "b"]
        assert all(sr.executor == "local" for sr in run.step_runs)
        assert len(env.local.calls) == 2

    async def test_graph_mixed_routing_agent_runs_local(self, env, enqueue_spy):
        graph = {
            "version": 2,
            "steps": {
                "build": {"id": "build", "name": "Build", "type": "script",
                          "config": {"command": "echo build"}},
                "agent": {"id": "agent", "name": "Agent", "type": "agent",
                          "config": {"agent": "mock", "title": "fix"}},
            },
            "edges": [
                {"id": "e1", "from_step": "build", "to_step": "agent",
                 "condition": "success"},
            ],
            "entry_points": ["build"],
        }
        repo = await make_repo(env.factory)
        pipeline = await make_graph_pipeline(env.factory, repo, graph)

        run = await start_and_wait(env, pipeline, repo)

        # 12.5: BOTH steps run locally, and nothing reaches the queue.
        # (The agent step's TERMINAL state is owned by the in-container
        # control runtime, which this routing fake does not simulate - that
        # reconciliation is covered by test_control_mode_dispatch.py and the
        # T2 round trip. What this test pins is the routing decision.)
        by_id = {sr.step_id: sr for sr in run.step_runs}
        assert by_id["build"].executor == "local"
        assert by_id["build"].status == RunStatus.PASSED.value
        assert by_id["agent"].executor == "local"
        assert enqueue_spy.calls == []


# -----------------------------------------------------------------------------
# needs: [docker] sugar (fix 10) - config builder translates to the socket bind
# -----------------------------------------------------------------------------

class TestNeedsSugar:
    async def test_needs_docker_adds_socket_bind_mount(self, env):
        from app.services.execution.local_executor import DOCKER_SOCKET_SOURCE

        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "DinD", "type": "script",
              "config": {"command": "docker ps", "needs": ["docker"]}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        exec_config, _ = env.local.calls[0]
        assert exec_config["mounts"] == [{
            "addressing": "bind",
            "source": DOCKER_SOCKET_SOURCE,
            "target": DOCKER_SOCKET_SOURCE,
            "mode": "rw",
        }]

    async def test_unknown_needs_capability_fails_step_loudly(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script",
              "config": {"command": "echo x", "needs": ["gpu"]}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "unknown step 'needs' capability" in step.error
        # The container was never launched with an unexpected mount set
        assert env.local.calls == []


# -----------------------------------------------------------------------------
# Merge action branch resolution (fix 1): local steps have no job -
# the branch comes from the run's trigger context; unresolvable = FAIL loudly
# -----------------------------------------------------------------------------

class FakeGitManager:
    def __init__(self, merge_success=True):
        self.merges: list[tuple] = []
        self.merge_success = merge_success

    def merge_branch(self, repo_id, source_branch, target_branch):
        self.merges.append((repo_id, source_branch, target_branch))
        if self.merge_success:
            return {"success": True, "message": "merged"}
        return {"success": False, "error": "conflict"}

    def delete_directory_from_branch(self, repo_id, branch, directory):
        return {"success": True}


class TestMergeActionBranchResolution:
    @pytest.fixture
    def fake_git(self, monkeypatch):
        import app.services.pipeline_executor as pe

        git = FakeGitManager()
        monkeypatch.setattr(pe, "git_repo_manager", git)
        return git

    async def test_merge_resolves_branch_from_trigger_context_for_local_steps(
        self, env, fake_git
    ):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script", "on_success": "merge:main",
              "config": {"command": "echo x"}}],
        )

        run = await start_and_wait(
            env, pipeline, repo,
            trigger_type="push",
            trigger_context={"branch": "feature-y", "commit_sha": "abc"},
        )

        assert run.status == RunStatus.PASSED.value
        assert fake_git.merges == [(repo.id, "feature-y", "main")]

    async def test_merge_with_unresolvable_branch_fails_run_loudly(
        self, env, fake_git
    ):
        """No job (local step) and no trigger-context branch: the run FAILS -
        never warn-and-continue-green (fix 1)."""
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script", "on_success": "merge:main",
              "config": {"command": "echo x"}}],
        )

        run = await start_and_wait(env, pipeline, repo)  # no trigger_context

        assert run.status == RunStatus.FAILED.value
        assert fake_git.merges == []  # merge never attempted
        step = run.step_runs[0]
        assert "could not resolve the source branch" in (step.error or "")

    async def test_merge_source_equals_target_skips_merge_and_continues(
        self, env, fake_git
    ):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "S", "type": "script", "on_success": "merge:main",
              "config": {"command": "echo x"}}],
        )

        run = await start_and_wait(
            env, pipeline, repo, trigger_context={"branch": "main"}
        )

        assert run.status == RunStatus.PASSED.value
        assert fake_git.merges == []


# -----------------------------------------------------------------------------
# Wedge paths (fix 2): no early return may leave a RUNNING StepRun unowned
# -----------------------------------------------------------------------------

class TestWedgePaths:
    async def _make_running_row_pair(self, env, pipeline_id, step_index=0):
        from datetime import datetime

        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline_id,
            status=RunStatus.RUNNING.value,
            steps_total=1,
            started_at=datetime.utcnow(),
        )
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=step_index,
            step_name="Ghost",
            status=RunStatus.RUNNING.value,
            executor=ExecutorMode.LOCAL.value,
            started_at=datetime.utcnow(),
        )
        async with env.factory() as db:
            db.add(run)
            db.add(step_run)
            await db.commit()
        return run.id, step_run.id

    async def test_missing_step_definition_fails_step_and_run(self, env):
        """StepRun whose step definition vanished: the task must fail the
        step and drive normal completion - never return leaving it RUNNING."""
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])
        run_id, step_run_id = await self._make_running_row_pair(
            env, pipeline.id, step_index=7  # out of range: definition missing
        )

        await env.executor._run_local_step(env.factory, run_id, step_run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "step definition not found" in step.error
        # Container never launched
        assert env.local.calls == []

    async def test_missing_pipeline_fails_step_and_run(self, env):
        """Pipeline row gone mid-run: step FAILED, run FAILED - no orphan."""
        run_id, step_run_id = await self._make_running_row_pair(
            env, "ghost-pipeline-id"
        )

        await env.executor._run_local_step(env.factory, run_id, step_run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "not found" in step.error


# -----------------------------------------------------------------------------
# Deadline poisoning (fix 3): outer deadline kills the container first, gives
# the consumer a bounded natural-exit grace, and NEVER hard-cancels it
# mid-commit; a stuck consumer is abandoned and the step fails from a FRESH
# session - it ALWAYS reaches FAILED.
# -----------------------------------------------------------------------------

class StuckExecutor:
    """Stream blocks on a gate; cancel_step optionally opens it."""

    def __init__(self, kill_opens_gate: bool):
        self.gate = asyncio.Event()
        self.kill_opens_gate = kill_opens_gate
        self.cancel_calls: list[str] = []
        self.calls: list[tuple[dict, dict]] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        return False  # stock image: stdout mode (12.3 contract method)

    async def find_missing_images(self, images) -> list[str]:
        return []  # every image resolves (12.3 contract method)

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "running"}
        await self.gate.wait()
        # Stream ends with NO result event (the container was killed).

    async def cancel_step(self, execution_key):
        self.cancel_calls.append(execution_key)
        if self.kill_opens_gate:
            self.gate.set()
        return True


class TestDeadlineDiscipline:
    @pytest.fixture
    def short_deadlines(self, monkeypatch):
        import app.services.pipeline_executor as pe

        monkeypatch.setattr(pe, "LOCAL_STEP_HARD_TIMEOUT_GRACE", 0)
        monkeypatch.setattr(pe, "LOCAL_STEP_CONSUMER_GRACE", 0.3)

    async def test_deadline_kills_container_and_step_fails(
        self, env, short_deadlines
    ):
        """Happy hardening path: the kill ends the stream within grace; the
        step reaches FAILED with the hard-deadline error on the SAME session."""
        stuck = StuckExecutor(kill_opens_gate=True)
        env.executor._local_executor = stuck
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "Hang", "type": "script", "timeout": 1,
              "config": {"command": "sleep 999"}}],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        assert "hard deadline" in step.error
        assert len(stuck.cancel_calls) == 1  # container kill came FIRST
        assert run.id in stuck.cancel_calls[0]

    async def test_stuck_consumer_abandoned_and_step_failed_in_fresh_session(
        self, env, short_deadlines
    ):
        """Poison path: the kill does NOT end the stream. The consumer is
        abandoned (never hard-cancelled) and the step still ALWAYS reaches
        FAILED via a fresh session."""
        stuck = StuckExecutor(kill_opens_gate=False)
        env.executor._local_executor = stuck
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [{"name": "Wedge", "type": "script", "timeout": 1,
              "config": {"command": "sleep 999"}}],
        )

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id

        # The step must reach FAILED even while the consumer is still stuck.
        deadline = asyncio.get_running_loop().time() + 10
        step = None
        while asyncio.get_running_loop().time() < deadline:
            fetched = await fetch_run(env, run_id)
            if fetched.step_runs and fetched.step_runs[0].status == RunStatus.FAILED.value:
                step = fetched.step_runs[0]
                break
            await asyncio.sleep(0.05)
        assert step is not None, "step never reached FAILED while consumer stuck"
        assert "hard deadline" in step.error
        assert stuck.cancel_calls, "container kill was never requested"

        # The run's own FAILED status lands via an async continuation after
        # the step flips - poll for it too (a single immediate read flaked
        # under the slower dogfood container in run d2f583d9).
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            fetched = await fetch_run(env, run_id)
            if fetched.status == RunStatus.FAILED.value:
                break
            await asyncio.sleep(0.05)
        assert fetched.status == RunStatus.FAILED.value

        # The abandoned consumer was NOT cancelled - it is still parked on
        # the gate. Release it so the reaper can close the session and the
        # run's tasks drain cleanly.
        assert not stuck.gate.is_set()
        stuck.gate.set()
        await env.executor.wait_for_run(run_id)
        assert not any(run_id in key for key in env.executor._tasks)


# -----------------------------------------------------------------------------
# Run-lock lifecycle (fix 4): never popped while held - a straggler finishing
# after completion still serializes on the SAME lock object
# -----------------------------------------------------------------------------

class TestRunLockLifecycle:
    async def test_straggler_after_completion_serializes_on_same_lock(self, env):
        executor = env.executor
        run_id = "run-lock-lifecycle"
        lock = executor._run_lock(run_id)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def straggler():
            async with lock:
                holding.set()
                await release.wait()

        task = asyncio.create_task(straggler())
        await holding.wait()

        # Completion runs WHILE the straggler holds the lock (the old code
        # popped the dict entry here, handing the next caller a DIFFERENT
        # lock object and losing mutual exclusion).
        executor._schedule_run_lock_eviction(run_id)
        await asyncio.sleep(0.05)  # let the evict task reach the lock
        assert executor._run_lock(run_id) is lock, (
            "run lock was evicted while HELD - stragglers no longer serialize"
        )

        release.set()
        await task
        await executor.wait_for_run(run_id)  # evict task key contains run_id
        assert run_id not in executor._run_locks  # evicted once idle

    async def test_eviction_waits_for_pending_step_tasks(self, env):
        executor = env.executor
        run_id = "run-lock-pending"
        lock = executor._run_lock(run_id)
        gate = asyncio.Event()

        async def slow_step():
            await gate.wait()

        executor._spawn_task(f"step:{run_id}:straggler", slow_step())
        executor._schedule_run_lock_eviction(run_id)
        await asyncio.sleep(0.05)
        # Straggler task still pending: the lock survives.
        assert executor._run_lock(run_id) is lock

        gate.set()
        await executor.wait_for_run(run_id)
        assert run_id not in executor._run_locks


# -----------------------------------------------------------------------------
# Single LocalExecutor (fix 5): concurrent first-calls build exactly one
# -----------------------------------------------------------------------------

class TestSingleLocalExecutor:
    async def test_concurrent_first_calls_build_exactly_one_executor(
        self, monkeypatch
    ):
        import time as time_mod

        import app.services.execution.local_executor as le_mod

        calls: list[int] = []

        class FakeClient:
            pass

        def slow_make_client():
            calls.append(1)
            time_mod.sleep(0.05)  # widen the race window (runs in threadpool)
            return FakeClient()

        monkeypatch.setattr(le_mod, "make_docker_client", slow_make_client)

        executor = PipelineExecutor()
        results = await asyncio.gather(
            *(executor._get_local_executor() for _ in range(8))
        )

        assert len(calls) == 1, "docker client constructed more than once"
        assert len({id(r) for r in results}) == 1
        assert all(r is executor._local_executor for r in results)


# -----------------------------------------------------------------------------
# T1 isolation guard (fix 13): this file has no local_exec marker, so the
# conftest guard must be ACTIVE here - proving a plain T1 run cannot
# construct a real docker client.
# -----------------------------------------------------------------------------

class TestT1DockerGuard:
    def test_docker_client_construction_blocked_without_local_exec_marker(self):
        import docker as docker_sdk

        with pytest.raises(AssertionError, match="local_exec"):
            docker_sdk.from_env()
        with pytest.raises(AssertionError, match="local_exec"):
            docker_sdk.DockerClient(base_url="tcp://nowhere:1")

    def test_global_executor_keeps_production_routing(self):
        """The GLOBAL executor routes with the REAL ExecutionRouter in T1.

        Updated for Phase 12.4: the conftest used to force every step legacy
        here. That now encodes an impossible production state - the runners
        REJECT script/docker jobs, so a legacy-routed script step is a
        routing bug, and the executor's enqueue guard raises on one. T1 stays
        Docker-free by stubbing the executor's COLLABORATORS (LocalExecutor,
        WorkspaceService), not by rewriting its routing decision, so what
        API-tier tests exercise is the production route.
        """
        from app.services.pipeline_executor import pipeline_executor

        decision = pipeline_executor._get_router().decide(
            "script", {"command": "echo hi"}
        )
        assert decision.mode == ExecutorMode.LOCAL.value
        assert decision.reason == "script-default-local"

        # 12.5: agent steps route local too.
        agent = pipeline_executor._get_router().decide("agent", {"title": "x"})
        assert agent.mode == ExecutorMode.LOCAL.value
        assert agent.reason == "agent-default-local"

    async def test_global_local_path_is_docker_free_in_t1(self):
        """The stubbed collaborators - not a rerouted decision - are what
        keep T1 off Docker: exercising the local path must not construct a
        docker client (which the guard above would turn into an
        AssertionError)."""
        from app.services.pipeline_executor import pipeline_executor

        executor = await pipeline_executor._get_local_executor()
        assert type(executor).__name__ == "_T1StubLocalExecutor"

        events = [
            event
            async for event in executor.execute_step({"command": "echo hi"}, {})
        ]
        assert events[-1] == {
            "type": "result", "status": "completed", "exit_code": 0,
        }


# -----------------------------------------------------------------------------
# Typed WS publish API (R6: real manager, capturing transport)
# -----------------------------------------------------------------------------

class TestTypedPublishApi:
    async def test_publish_step_update_shape(self):
        socket = CapturingSocket()
        manager.active_connections.append(socket)
        try:
            await manager.publish_step_update("run-1", 3, "running")
        finally:
            manager.active_connections.remove(socket)

        assert socket.messages == [{
            "type": "step_update",
            "payload": {"pipeline_run_id": "run-1", "step_index": 3,
                        "status": "running"},
        }]

    async def test_publish_step_log_shape(self):
        socket = CapturingSocket()
        manager.active_connections.append(socket)
        try:
            await manager.publish_step_log("run-1", 0, "a log line")
        finally:
            manager.active_connections.remove(socket)

        assert socket.messages == [{
            "type": "step_log",
            "payload": {"pipeline_run_id": "run-1", "step_index": 0,
                        "line": "a log line"},
        }]

    async def test_publish_step_log_batch_shape(self):
        """ONE step_log_batch frame per call (12.3: the control-mode /logs
        router ships whole batches, matching the frontend's appendLines
        consumer); empty batches produce no frame."""
        socket = CapturingSocket()
        manager.active_connections.append(socket)
        try:
            await manager.publish_step_log_batch("run-1", 2, ["one", "two"])
            await manager.publish_step_log_batch("run-1", 2, [])
        finally:
            manager.active_connections.remove(socket)

        assert socket.messages == [{
            "type": "step_log_batch",
            "payload": {"pipeline_run_id": "run-1", "step_index": 2,
                        "lines": ["one", "two"]},
        }]


# -----------------------------------------------------------------------------
# Image preflight (12.3 hardening): resolve all step images before step 0
# -----------------------------------------------------------------------------

class TestImagePreflight:
    async def test_missing_images_fail_run_before_any_dispatch(
        self, env, enqueue_spy, caplog
    ):
        """A run naming unresolvable images fails with ONE message listing
        every missing tag - no StepRun is created, nothing dispatches."""
        repo = await make_repo(env.factory)
        env.local.missing_images = ["ghost:one", "ghost:two"]
        pipeline = await make_linear_pipeline(env.factory, repo, [
            script_step("A", "echo a", config={"command": "echo a", "image": "ghost:one"}),
            script_step("B", "echo b", config={"command": "echo b", "image": "ghost:two"}),
            script_step("C", "echo c"),
        ])

        with caplog.at_level(logging.ERROR, logger="app.services.pipeline_executor"):
            run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        assert run.step_runs == []  # failed BEFORE dispatching step 0
        assert env.local.calls == []
        assert enqueue_spy.calls == []
        # Distinct images resolved ONCE, in one query
        assert env.local.preflight_queries == [["ghost:one", "ghost:two"]]
        # ONE message naming every missing tag
        preflight_errors = [
            r.message for r in caplog.records
            if "missing step image(s)" in r.message
        ]
        assert len(preflight_errors) == 1
        assert "ghost:one" in preflight_errors[0]
        assert "ghost:two" in preflight_errors[0]

    async def test_graph_run_preflights_all_step_images(self, env):
        repo = await make_repo(env.factory)
        env.local.missing_images = ["ghost:entry"]
        pipeline = await make_graph_pipeline(env.factory, repo, {
            "steps": {
                "a": {"name": "A", "type": "script",
                      "config": {"command": "echo a", "image": "ghost:entry"}},
                "b": {"name": "B", "type": "script",
                      "config": {"command": "echo b"}},
            },
            "edges": [{"from_step": "a", "to_step": "b", "condition": "success"}],
            "entry_points": ["a"],
        })

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        assert run.step_runs == []
        assert env.local.calls == []
        assert env.local.preflight_queries == [["ghost:entry"]]

    async def test_resolvable_images_run_normally(self, env):
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [
            script_step("A", "echo a", config={"command": "echo a", "image": "python:3.12"}),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert env.local.preflight_queries == [["python:3.12"]]

    async def test_runs_without_explicit_images_skip_preflight(self, env):
        """Steps on the default image never touch the resolver: the default
        is pre-pulled at app startup, and reaching for a docker client here
        would drag one into legacy-only runs."""
        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("A", "echo a")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert env.local.preflight_queries == []


# -----------------------------------------------------------------------------
# The test-result gate (the demotion the legacy path had and local lost)
# -----------------------------------------------------------------------------

class TestReportingExecutor(FakeLocalExecutor):
    """Ingests test results for the step it is running, then exits 0.

    Models the real shape: the control runtime POSTs results to
    /api/steps/{id}/test-results DURING the step, so by the time the step
    finishes the TestRun rows are already there. `statuses` is what the
    suite reported.
    """
    __test__ = False  # a helper, not a pytest class

    def __init__(self, factory, repo_id, statuses, exit_code=0):
        super().__init__()
        self.factory = factory
        self.repo_id = repo_id
        self.statuses = list(statuses)
        self.exit_code = exit_code

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        await self._write_results(execution_context)
        yield {"type": "status", "status": "running"}
        yield {
            "type": "result",
            "status": "completed" if self.exit_code == 0 else "failed",
            "exit_code": self.exit_code,
        }

    async def _write_results(self, execution_context):
        from app.models import TestRef, TestRun

        async with self.factory() as db:
            for i, status in enumerate(self.statuses):
                ref = TestRef(
                    id=str(uuid4()),
                    lazyaf_test_id=f"LZ-{i}",
                    repo_id=self.repo_id,
                )
                db.add(ref)
                db.add(
                    TestRun(
                        id=str(uuid4()),
                        test_ref_id=ref.id,
                        pipeline_run_id=execution_context["pipeline_run_id"],
                        step_run_id=execution_context["step_run_id"],
                        status=status,
                    )
                )
            await db.commit()


class TestTestResultGate:
    async def test_exit_zero_with_a_failing_suite_fails_the_step(self, env):
        """THE LOST GATE. A test command that reports failures and still
        exits 0 (a wrapper script, a `|| true`, a runner that swallows the
        code) used to read as a PASSING step - which on an ad-hoc card run
        means the card is offered for merge red."""
        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, ["passed", "failed", "passed"]
        )
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("Tests", "pytest || true")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step_run = run.step_runs[0]
        assert step_run.status == RunStatus.FAILED.value
        assert "1 test(s) reported FAILED" in (step_run.error or "")
        assert "reported FAILED" in (step_run.logs or "")

    async def test_a_green_suite_leaves_the_step_green(self, env):
        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, ["passed", "skipped"]
        )
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("Tests", "pytest")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert run.step_runs[0].status == RunStatus.PASSED.value

    async def test_a_step_that_ingests_nothing_is_untouched(self, env):
        """No results is not a pass and not a failure - the gate only ever
        DEMOTES, so a step that reports none keeps its exit code's verdict."""
        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, []
        )
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("No tests", "echo hi")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value

    async def test_an_already_failed_step_keeps_its_own_error(self, env):
        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, ["failed"], exit_code=1
        )
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("Tests", "pytest")]
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.step_runs[0].status == RunStatus.FAILED.value

    async def test_results_of_ANOTHER_step_do_not_bleed(self, env):
        """The gate is scoped to the step's own step_run_id."""
        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, ["failed"]
        )
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [script_step("A", "pytest"), script_step("B", "echo b")],
        )

        run = await start_and_wait(env, pipeline, repo)

        by_index = {sr.step_index: sr for sr in run.step_runs}
        assert by_index[0].status == RunStatus.FAILED.value
        # Step 1 never runs (on_failure defaults to stop), so the only proof
        # available is that the run stopped at the red step, not that step 1
        # inherited its results.
        assert 1 not in by_index


class TestVerificationStepSeam:
    """The seam the ad-hoc card run wires a post-agent test step through."""

    def test_verification_step_stops_the_run_when_red(self):
        step = build_verification_step("pytest -q")
        assert step["type"] == "script"
        assert step["config"]["command"] == "pytest -q"
        assert step["on_failure"] == "stop", (
            "a red verification step must fail the RUN - that is what "
            "demotes the card"
        )
        assert step["on_success"] == "next"

    def test_verification_step_accepts_an_image_and_timeout(self):
        step = build_verification_step(
            "pytest", image="lazyaf-test-runner:dev", timeout=900,
            name="Repo suite", step_id="repo-suite",
        )
        assert step["config"]["image"] == "lazyaf-test-runner:dev"
        assert step["timeout"] == 900
        assert step["name"] == "Repo suite"
        assert step["id"] == "repo-suite"

    async def test_a_verification_step_after_an_agent_demotes_the_run(self, env):
        """End to end on the pipeline side: step 0 goes green, step 1 (the
        verification step) comes back red, and the RUN fails - which is the
        signal agent_run.on_run_complete routes the card outcome off."""
        repo = await make_repo(env.factory)
        env.executor._local_executor = FakeLocalExecutor(
            events=[
                {"type": "status", "status": "running"},
                {"type": "result", "status": "failed", "exit_code": 1},
            ]
        )
        pipeline = await make_linear_pipeline(
            env.factory,
            repo,
            [
                script_step("Work", "echo worked"),
                build_verification_step("pytest -q"),
            ],
        )

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value

    async def test_the_runs_results_are_readable_off_the_run(self, env):
        """The other half of the seam: the caller that decides a card's
        outcome reads TEST RESULTS off the RUN, not an exit code.

        The reader is agent_run.run_test_summary (one owner - the module
        that puts the numbers on the card). This asserts the pipeline side
        actually feeds it: results ingested during a step are tied to the
        run and come back keyed by status.
        """
        from app.services.agent_run import run_test_summary

        repo = await make_repo(env.factory)
        env.executor._local_executor = TestReportingExecutor(
            env.factory, repo.id, ["passed", "passed", "failed", "skipped"]
        )
        pipeline = await make_linear_pipeline(
            env.factory, repo, [script_step("Tests", "pytest")]
        )
        run = await start_and_wait(env, pipeline, repo)

        async with env.factory() as db:
            summary = await run_test_summary(db, run.id)

        assert summary.tests_run is True
        assert summary.tests_passed is False
        assert (summary.pass_count, summary.fail_count, summary.skip_count) == (
            2, 1, 1,
        )

    async def test_a_run_with_no_results_claims_nothing(self, env):
        """"No manifest" is not "green" - the summary must say it has no
        evidence rather than report a passing suite."""
        from app.services.agent_run import run_test_summary

        repo = await make_repo(env.factory)
        pipeline = await make_linear_pipeline(env.factory, repo, [script_step()])
        run = await start_and_wait(env, pipeline, repo)

        async with env.factory() as db:
            summary = await run_test_summary(db, run.id)

        assert summary.tests_run is False
        assert summary.tests_passed is None
