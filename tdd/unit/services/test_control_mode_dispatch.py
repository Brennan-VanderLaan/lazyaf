"""
Unit tests for the 12.3 control-mode dispatch bridge (wave2-123-wiring):

- Mode is decided AT DISPATCH TIME from the image's capability label and
  stamped EXPLICITLY into exec_context["control_mode"] - the consumer never
  guesses (R6: declared, not inferred).
- Selecting control mode creates the StepExecution row the /api/steps/*
  router authenticates against (PREPARING, timeout_at = timeout + hard
  grace) and a per-step-execution JWT scoped to that row's id.
- `config.control: false` is the stdout-mode escape hatch on a labeled
  image; there is NO promotion of unlabeled images.
- In control mode _consume_local_events DROPS executor log/status events
  (the router is the sole writer of StepRun.logs / step_log / intermediate
  step_update - R3), while the executor `result` event still drives
  terminal state through _finish_local_step in BOTH modes.

12.3 adversarial-review hardening covered here:
- _finish_local_step reconciles the StepExecution row: a row that never
  left PREPARING fails the step loudly even on exit 0; exit 124 reads as a
  timeout; a runtime-reported error surfaces as a WARNING log line +
  StepRun.error; the row is driven terminal.
- Fix 1 regression: log lines the /api/steps router wrote from OTHER
  sessions survive the normal (locked) finish path - the exit-code marker
  is appended with a targeted SQL expression, never a stale
  read-modify-write.
- Fix 5 forensics: the executor's bounded stdout tail is persisted as a
  '[container] ...' block when the step fails or the router wrote nothing.

Fakes honor the LocalExecutor interface contract (execute_step,
cancel_step, image_supports_control_layer, find_missing_images) and
simulate the control runtime's reports by writing the StepExecution /
StepRun rows from their OWN sessions - exactly like POST /api/steps/*
does. The WS manager is NEVER mocked on broadcast paths (R6): a capturing
transport attaches to the real ConnectionManager singleton.
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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories.pipelines import make_repo_and_graph_pipeline  # noqa: E402

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import RunStatus, StepExecution, StepExecutionStatus
from app.services.control_layer.auth import decode_step_token, validate_step_token
from app.services.pipeline_executor import (
    LOCAL_STEP_HARD_TIMEOUT_GRACE,
    STEP_TOKEN_TTL_SLACK,
    PipelineExecutor,
)
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name


LABELED_IMAGE = "lazyaf-base:dev"
STOCK_IMAGE = "python:3.12"


# -----------------------------------------------------------------------------
# Contract fakes
# -----------------------------------------------------------------------------

class Decision:
    def __init__(self, mode: str, reason: str):
        self.mode = mode
        self.reason = reason


class ContractRouter:
    def decide(self, step_type: str, step_config: dict) -> Decision:
        if step_type in ("script", "docker"):
            return Decision("local", f"{step_type}-runs-local")
        return Decision("legacy", "agent-legacy-until-12.5")


class FakeWorkspaceService:
    def __init__(self):
        self.workspaces: dict[str, SimpleNamespace] = {}

    async def get_or_create(
        self, db, pipeline_run_id, repo_id, branch, commit_sha, worker_key=None
    ):
        # worker_key is the workspace LANE (M13-1). Keyed by (run, lane)
        # like the real service, so a fan-out gets distinct workspaces
        # instead of silently sharing one checkout.
        lane = (pipeline_run_id, worker_key or "default")
        ws = self.workspaces.get(lane)
        if ws is None:
            ws = SimpleNamespace(
                id=f"ws-{pipeline_run_id[:8]}",
                pipeline_run_id=pipeline_run_id,
                worker_key=lane[1],
                volume_name=generate_volume_name(pipeline_run_id, lane[1]),
                status="ready",
            )
            self.workspaces[lane] = ws
        return ws

    async def acquire(self, db, workspace_id):
        pass

    async def release(self, db, workspace_id):
        pass

    async def cleanup(self, db, pipeline_run_id):
        pass


class LabelAwareExecutor:
    """LocalExecutor-interface fake: labeled images declare the control
    capability; yields a scripted event stream and records everything.

    Control-runtime simulation: before yielding the terminal result event
    it applies `mark_execution` to the StepExecution row and appends
    `router_log_lines` to StepRun.logs - both from its OWN sessions, the
    way POST /api/steps/* really writes them. `mark_execution=None` models
    an image whose /control runtime never reported at all.
    """

    def __init__(self, labeled_images=(LABELED_IMAGE,), events=None, factory=None):
        self.labeled_images = set(labeled_images)
        self.events = events
        self.factory = factory
        self.calls: list[tuple[dict, dict]] = []
        self.label_queries: list[str] = []
        # Fields the simulated runtime reports onto the StepExecution row.
        self.mark_execution: dict | None = {"status": "running"}
        # Lines the simulated router lands in StepRun.logs mid-run.
        self.router_log_lines: list[str] = []

    async def image_supports_control_layer(self, image: str) -> bool:
        self.label_queries.append(image)
        return image in self.labeled_images

    async def find_missing_images(self, images):
        return []

    async def _simulate_runtime_reports(self, execution_context) -> None:
        if self.factory is None or not execution_context.get("control_mode"):
            return
        async with self.factory() as db:
            if self.mark_execution:
                execution = await db.get(
                    StepExecution, execution_context["step_execution_id"]
                )
                for key, value in self.mark_execution.items():
                    setattr(execution, key, value)
            if self.router_log_lines:
                chunk = "".join(f"{line}\n" for line in self.router_log_lines)
                await db.execute(
                    update(StepRun)
                    .where(StepRun.id == execution_context["step_run_id"])
                    .values(logs=func.coalesce(StepRun.logs, "") + chunk)
                    .execution_options(synchronize_session=False)
                )
            await db.commit()

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        events = self.events
        if events is None:
            events = [
                {"type": "status", "status": "preparing"},
                {"type": "status", "status": "running"},
                {"type": "log", "line": "container-stdout-echo"},
                {"type": "result", "status": "completed", "exit_code": 0},
            ]
        for event in events:
            if event.get("type") == "result":
                await self._simulate_runtime_reports(execution_context)
            await asyncio.sleep(0)
            yield event

    async def cancel_step(self, execution_key):
        return False


class CapturingSocket:
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
    db_path = (tmp_path / "control_dispatch.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    executor = PipelineExecutor()
    local = LabelAwareExecutor(factory=factory)
    executor._router = ContractRouter()
    executor._workspace_service = FakeWorkspaceService()
    executor._local_executor = local

    socket = CapturingSocket()
    manager.active_connections.append(socket)

    yield SimpleNamespace(
        factory=factory, executor=executor, local=local, socket=socket
    )

    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    await engine.dispose()


async def make_repo_and_pipeline(factory, steps: list[dict]):
    """A repo and a pipeline whose definition is the LINEAR GRAPH `steps` describes.

    12.8: the argument shape is unchanged - the same `list[dict]` every call
    site below already passes - and so is the persisted node ORDER, whose ids
    are `step_0..step_N`. What changed is the column: `steps_graph`, not
    `steps`.

    This was one of seven byte-identical copies. It is now one line onto
    `tdd/shared/factories/pipelines`, so the next change to how a test
    pipeline is persisted happens once (R3).
    """
    return await make_repo_and_graph_pipeline(
        factory, steps, name="control-dispatch-pipeline", repo_name="control-dispatch-repo",
    )


async def run_pipeline(env, steps: list[dict]) -> str:
    repo, pipeline = await make_repo_and_pipeline(env.factory, steps)
    async with env.factory() as db:
        run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
        run_id = run.id
    await env.executor.wait_for_run(run_id)
    return run_id


async def fetch_run(env, run_id: str) -> PipelineRun:
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


async def fetch_executions(env) -> list[StepExecution]:
    async with env.factory() as db:
        result = await db.execute(select(StepExecution))
        return list(result.scalars().all())


def labeled_step(name="Control", command="echo hi", **config_extra) -> dict:
    config = {"command": command, "image": LABELED_IMAGE}
    config.update(config_extra)
    return {"name": name, "type": "script", "timeout": 60, "config": config}


# -----------------------------------------------------------------------------
# Mode selection at dispatch
# -----------------------------------------------------------------------------

class TestModeSelection:
    async def test_labeled_image_selects_control_mode_explicitly(self, env):
        run_id = await run_pipeline(env, [labeled_step()])

        assert env.local.label_queries == [LABELED_IMAGE]
        (_, exec_context), = env.local.calls
        assert exec_context["control_mode"] is True
        assert exec_context["step_execution_id"]
        assert exec_context["step_auth_token"]

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value

    async def test_control_mode_creates_step_execution_row(self, env):
        before = datetime.utcnow()
        run_id = await run_pipeline(env, [labeled_step()])

        run = await fetch_run(env, run_id)
        step_run = run.step_runs[0]
        executions = await fetch_executions(env)
        assert len(executions) == 1
        execution = executions[0]

        (_, exec_context), = env.local.calls
        assert execution.id == exec_context["step_execution_id"]
        assert execution.step_run_id == step_run.id
        assert execution.execution_key == exec_context["execution_key"]
        # Created PREPARING at dispatch; the simulated runtime reported
        # `running` and _finish_local_step reconciled the row TERMINAL
        # (12.3 fix 2d) - a non-terminal row after run completion is a bug.
        assert execution.status == StepExecutionStatus.COMPLETED.value
        assert execution.completed_at is not None
        # timeout_at = now + timeout + hard grace
        expected_floor = before + timedelta(seconds=60 + LOCAL_STEP_HARD_TIMEOUT_GRACE)
        assert execution.timeout_at >= expected_floor - timedelta(seconds=1)

    async def test_token_is_scoped_to_the_step_execution(self, env):
        await run_pipeline(env, [labeled_step()])

        (_, exec_context), = env.local.calls
        token = exec_context["step_auth_token"]
        execution_id = exec_context["step_execution_id"]

        assert validate_step_token(token, step_id=execution_id) is True
        assert validate_step_token(token, step_id="some-other-step") is False

        payload = decode_step_token(token)
        assert payload["execution_key"] == exec_context["execution_key"]
        # Lifetime = timeout + grace + a tight 300s slack (12.3 hardening:
        # NOT the 24h default and no longer a full hour - terminal
        # reconciliation 409s zombie posts, so the token only needs to
        # outlive a legitimately late final report)
        lifetime = payload["exp"] - payload["iat"]
        assert lifetime == 60 + LOCAL_STEP_HARD_TIMEOUT_GRACE + STEP_TOKEN_TTL_SLACK

    async def test_control_false_forces_stdout_mode_on_labeled_image(self, env):
        run_id = await run_pipeline(env, [labeled_step(control=False)])

        (_, exec_context), = env.local.calls
        assert exec_context["control_mode"] is False
        assert "step_execution_id" not in exec_context
        assert "step_auth_token" not in exec_context
        assert await fetch_executions(env) == []
        # The image label is never even consulted - the override wins first
        assert env.local.label_queries == []

        # stdout path fully live: logs land via _consume_local_events
        run = await fetch_run(env, run_id)
        assert "container-stdout-echo" in run.step_runs[0].logs

    async def test_unlabeled_image_stays_stdout_with_no_row(self, env):
        run_id = await run_pipeline(
            env,
            [{"name": "Stock", "type": "script", "timeout": 60,
              "config": {"command": "echo hi", "image": STOCK_IMAGE}}],
        )

        (_, exec_context), = env.local.calls
        assert exec_context["control_mode"] is False
        assert await fetch_executions(env) == []
        assert env.local.label_queries == [STOCK_IMAGE]

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value
        assert "container-stdout-echo" in run.step_runs[0].logs

    async def test_exec_form_list_command_stays_stdout(self, env):
        """List commands are the explicit shell-less opt-out; the config
        file contract carries a STRING command, so they keep stdout mode
        even on a labeled image."""
        await run_pipeline(
            env,
            [{"name": "ExecForm", "type": "script", "timeout": 60,
              "config": {"command": ["python", "-c", "print('x')"],
                          "image": LABELED_IMAGE}}],
        )

        (_, exec_context), = env.local.calls
        assert exec_context["control_mode"] is False
        assert await fetch_executions(env) == []


# -----------------------------------------------------------------------------
# One reporting path per datum (R3): consumer drops, router owns
# -----------------------------------------------------------------------------

class TestControlModeConsumer:
    async def test_consumer_drops_log_and_status_events_in_control_mode(self, env):
        """The stdout stream is consumed ONLY for liveness + the result
        event: no StepRun.logs writes, no step_log frames, no intermediate
        step_update frames from the executor stream."""
        run_id = await run_pipeline(env, [labeled_step()])

        run = await fetch_run(env, run_id)
        step_run = run.step_runs[0]

        # The echoed container stdout line must NOT be double-written
        assert "container-stdout-echo" not in (step_run.logs or "")
        assert env.socket.of_type("step_log") == []

        # Executor preparing/running status events were dropped; the ONLY
        # step_update frame is the terminal one from _finish_local_step
        statuses = [p["status"] for p in env.socket.of_type("step_update")]
        assert "preparing" not in statuses
        assert "running" not in statuses
        assert statuses == [RunStatus.PASSED.value]

    async def test_result_event_still_owns_terminal_state(self, env):
        """Terminal StepRun state keeps exactly one owner in both modes:
        the executor result event through _finish_local_step (exit code is
        ground truth even when the control runtime reported over HTTP)."""
        env.local.events = [
            {"type": "status", "status": "preparing"},
            {"type": "status", "status": "running"},
            {"type": "result", "status": "failed", "exit_code": 3},
        ]
        run_id = await run_pipeline(env, [labeled_step()])

        run = await fetch_run(env, run_id)
        step_run = run.step_runs[0]
        assert run.status == RunStatus.FAILED.value
        assert step_run.status == RunStatus.FAILED.value
        assert "exit code: 3" in step_run.logs
        assert step_run.completed_at is not None

        step_statuses = env.socket.of_type("step_run_status")
        assert any(s["status"] == RunStatus.FAILED.value for s in step_statuses)

    async def test_stdout_mode_flow_unchanged(self, env):
        """Stock images keep the ENTIRE 12.2-INT behavior: stream-consumer
        writes logs, broadcasts preparing/running and step_log frames."""
        run_id = await run_pipeline(
            env,
            [{"name": "Stock", "type": "script", "timeout": 60,
              "config": {"command": "echo hi", "image": STOCK_IMAGE}}],
        )

        run = await fetch_run(env, run_id)
        assert "container-stdout-echo" in run.step_runs[0].logs
        log_lines = [p["line"] for p in env.socket.of_type("step_log")]
        assert "container-stdout-echo" in log_lines
        statuses = [p["status"] for p in env.socket.of_type("step_update")]
        assert "preparing" in statuses
        assert "running" in statuses
        assert statuses[-1] == RunStatus.PASSED.value


# -----------------------------------------------------------------------------
# Terminal reconciliation (12.3 adversarial fix 2) + log integrity (fix 1)
# + forensics tail (fix 5), all through the NORMAL locked finish path
# -----------------------------------------------------------------------------

class TestFinishReconciliation:
    async def test_never_reported_fails_step_even_on_exit_zero(self, env):
        """(2a) A StepExecution that never left PREPARING means the image
        has no working /control runtime: exit code 0 must NOT read green."""
        env.local.mark_execution = None  # runtime never reports

        run_id = await run_pipeline(env, [labeled_step()])

        run = await fetch_run(env, run_id)
        step_run = run.step_runs[0]
        assert run.status == RunStatus.FAILED.value
        assert step_run.status == RunStatus.FAILED.value
        assert "control runtime never reported" in step_run.error
        assert "/control runtime" in step_run.error

        (execution,) = await fetch_executions(env)
        assert execution.status == StepExecutionStatus.FAILED.value
        assert execution.completed_at is not None

    async def test_exit_124_surfaces_as_timeout(self, env):
        """(2b) The in-container timeout convention (exit 124) reads as a
        timeout in StepRun.error, not a generic exit-code failure."""
        env.local.events = [
            {"type": "status", "status": "running"},
            {"type": "result", "status": "failed", "exit_code": 124},
        ]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert step_run.status == RunStatus.FAILED.value
        assert "timed out after 60s" in step_run.error

        (execution,) = await fetch_executions(env)
        assert execution.status == StepExecutionStatus.TIMEOUT.value

    async def test_runtime_reported_timeout_status_wins_over_generic(self, env):
        env.local.mark_execution = {"status": "timeout"}
        env.local.events = [
            {"type": "status", "status": "running"},
            {"type": "result", "status": "failed", "exit_code": 137},
        ]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert step_run.status == RunStatus.FAILED.value
        assert "timed out" in step_run.error
        # Already terminal from the runtime's own report - left untouched
        (execution,) = await fetch_executions(env)
        assert execution.status == StepExecutionStatus.TIMEOUT.value

    async def test_execution_error_surfaces_without_flipping_status(self, env):
        """(2c) A runtime-reported error (dropped log lines) lands as a loud
        WARNING log line + StepRun.error while the step keeps its REAL exit
        status."""
        env.local.mark_execution = {
            "status": "completed",
            "error": "3 log lines failed to reach backend",
        }

        run_id = await run_pipeline(env, [labeled_step()])

        run = await fetch_run(env, run_id)
        step_run = run.step_runs[0]
        assert run.status == RunStatus.PASSED.value
        assert step_run.status == RunStatus.PASSED.value  # real exit status
        assert step_run.error == "3 log lines failed to reach backend"
        assert (
            "[lazyaf] WARNING: 3 log lines failed to reach backend"
            in step_run.logs
        )

    async def test_router_written_logs_survive_finish(self, env):
        """(fix 1 regression) Lines the /api/steps router landed from OTHER
        sessions survive the normal locked finish path: the exit-code marker
        is appended with a targeted SQL expression, never by writing back a
        stale session-cached blob."""
        env.local.router_log_lines = ["router-a", "router-b"]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert step_run.status == RunStatus.PASSED.value
        assert step_run.logs == "router-a\nrouter-b\n[lazyaf] exit code: 0\n"

    async def test_failed_step_persists_container_tail(self, env):
        """(fix 5) On failure the executor's bounded stdout tail lands as a
        '[container] ...' block BEFORE the exit-code marker - even when the
        router also wrote lines."""
        env.local.router_log_lines = ["router-line"]
        env.local.events = [
            {"type": "status", "status": "running"},
            {"type": "result", "status": "failed", "exit_code": 2,
             "log_tail": ["boom-stdout"]},
        ]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert step_run.logs == (
            "router-line\n"
            "[container] boom-stdout\n"
            "[lazyaf] exit code: 2\n"
        )

    async def test_zero_router_bytes_persist_tail_on_success(self, env):
        """(fix 5) A PASSING step whose router landed zero log bytes still
        keeps the container stdout for forensics."""
        env.local.events = [
            {"type": "status", "status": "running"},
            {"type": "result", "status": "completed", "exit_code": 0,
             "log_tail": ["only-stdout"]},
        ]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert step_run.status == RunStatus.PASSED.value
        assert step_run.logs == "[container] only-stdout\n[lazyaf] exit code: 0\n"

    async def test_successful_step_with_router_logs_skips_tail(self, env):
        """(fix 5) A passing step whose logs already arrived via the router
        does NOT get the stdout tail duplicated in."""
        env.local.router_log_lines = ["the-real-lines"]
        env.local.events = [
            {"type": "status", "status": "running"},
            {"type": "result", "status": "completed", "exit_code": 0,
             "log_tail": ["the-real-lines"]},
        ]

        run_id = await run_pipeline(env, [labeled_step()])

        step_run = (await fetch_run(env, run_id)).step_runs[0]
        assert "[container]" not in step_run.logs
        assert step_run.logs == "the-real-lines\n[lazyaf] exit code: 0\n"
