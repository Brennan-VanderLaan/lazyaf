"""
Pipeline execution service.

Orchestrates multi-step pipeline workflows by:
1. Creating pipeline runs and step runs
2. Routing each step through the ExecutionRouter (Phase 12.2-INT / 12.6):
   - mode=local:  execute in a Docker container via LocalExecutor, in an
     asyncio task with its OWN session scope, streaming status/log events
     incrementally into the StepRun row and over the typed WS publish API
   - mode=remote: dispatch the same step to a runner agent over the runner
     WebSocket via RemoteExecutor, which reproduces LocalExecutor's event
     contract exactly - so everything downstream of dispatch is shared code
     and nothing here has to know what "remote" means
3. Handling local/remote task continuations
4. Graph-based parallel execution with fan-out/fan-in
5. Broadcasting status via WebSocket

Async model (R5 / failure_01 landmine 4): request and git-push handlers never
await container execution. start_pipeline creates the run row and dispatches
the entry steps; dispatching a step spawns an asyncio task (registered in a
task registry with a done-callback that logs exceptions, so nothing leaks or
dies silently). All container execution, log streaming, and continuation
logic run inside those tasks using a session factory derived from the
caller's engine - never the request's session.

Observability (R1): every StepRun records which executor ran it in
StepRun.executor ("local" | "remote"), set at dispatch time. Routing
failures fail the step and the run loudly; since 12.6 there is nothing left
to silently fall back TO, which is the point of the deletion. Run lifecycle
is driven through main's PipelineStateMachine.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import (
    Card,
    Job,
    Pipeline,
    PipelineRun,
    Repo,
    RunStatus,
    StepRun,
    TestRun,
    TestRunStatus,
)
from app.models.pipeline import ExecutorMode, StepExecution, StepExecutionStatus
from app.services.websocket import manager
from app.services.git_server import git_repo_manager
from app.services.workspace.pipeline_state_machine import (
    PipelineStateMachine,
    PipelineStatus,
)
from app.services.workspace.state_machine import generate_volume_name
# 12.7 debug re-run. Imported at module scope rather than lazily for two
# reasons: the gate runs on EVERY executor step (a lazy import per step is
# noise), and this import is what registers `app.models.debug` - and with it
# the `debug_sessions` table - on Base.metadata wherever the executor is
# imported, which is everywhere the app is. The service imports this module
# only from inside function bodies, so there is no cycle.
from app.services.execution.debug_session_service import (
    DebugGateOutcome,
    debug_session_service,
)

logger = logging.getLogger(__name__)


# Grace added on top of a step's own timeout before the outer hard deadline
# fires (the in-container timeout should always fire first).
LOCAL_STEP_HARD_TIMEOUT_GRACE = 120

# After the outer deadline kills the container, how long the event-stream
# consumer gets to end NATURALLY before it is abandoned and the step is
# failed from a fresh session (fix 3: never hard-cancel the consumer
# mid-commit).
LOCAL_STEP_CONSUMER_GRACE = 15.0

# reset(): how long in-flight tasks get to drain on their own (after their
# containers are killed) before being cancelled as a last resort.
RESET_DRAIN_GRACE = 2.0

# Log persistence/publish cadence (fix 7): buffered log lines are flushed to
# the StepRun row (one commit) and published over WS whenever either bound is
# hit - never one commit per line.
LOG_FLUSH_MAX_LINES = 200
LOG_FLUSH_INTERVAL_SECONDS = 0.5

# Extra slack a per-step-execution token lives beyond the step's own hard
# deadline (12.3 hardening: was a full hour - far wider than any legitimate
# late report needs).
STEP_TOKEN_TTL_SLACK = 300

# 12.6: control mode is MANDATORY on the remote path, for two independent
# reasons, so the stdout escape hatches RAISE instead of downgrading (the
# same shape agent steps have carried since 12.5):
#
# 1. The StepExecution row IS the assignment unit. The dispatcher's
#    compare-and-swap claims it, `StepExecution.runner_id` records who holds
#    it, and the step gate on the WS endpoint checks it on every inbound
#    ack/log/step_complete. A stdout-mode remote step has no such row, so
#    there is nothing to assign and nothing to fence.
# 2. Its logs could never arrive. On the local path stdout is read off a
#    container the backend owns; on the remote path that container is on
#    another host and only the runner's own `[runner]` lines cross the
#    socket. A stdout-mode remote step would run and report nothing.
_REMOTE_NEEDS_CONTROL = (
    "remote step {step!r} cannot run in stdout mode ({why}): the remote "
    "assignment protocol is keyed on the StepExecution row that only control "
    "mode creates, and a step container on another host can only report "
    "through the control layer. Remove the stdout opt-out, or drop the "
    "`requires:` block to run this step locally."
)

# StepExecution statuses that count as terminal for reconciliation (mirrors
# app.routers.steps.TERMINAL_EXECUTION_STATUSES - both derive from the enum).
TERMINAL_STEP_EXECUTION_STATUSES = frozenset({
    StepExecutionStatus.COMPLETED.value,
    StepExecutionStatus.FAILED.value,
    StepExecutionStatus.CANCELLED.value,
    StepExecutionStatus.TIMEOUT.value,
})

# StepExecution statuses proving the control runtime NEVER reported: the row
# was created/prepared at dispatch and no /api/steps POST ever moved it.
NEVER_REPORTED_STEP_EXECUTION_STATUSES = frozenset({
    StepExecutionStatus.PENDING.value,
    StepExecutionStatus.ASSIGNED.value,
    StepExecutionStatus.PREPARING.value,
})


# -----------------------------------------------------------------------------
# Agent steps on the control layer (Phase 12.5)
# -----------------------------------------------------------------------------

# The fixed command an agent step runs. Users NEVER write it: the wrapper is
# a module of the tested runner-common package installed in the agent images,
# and run.py executes it exactly as it executes a script - same watchdog, same
# log pump, same exit code semantics.
AGENT_WRAPPER_COMMAND = "python3 -m runner_common.agent_wrapper"

# Agent vocabulary -> default image (cross-agent contract #5). `mock` resolves
# to agent-base because the mock executor needs python + runner-common and
# that is precisely what agent-base is; a fourth image would be rebuild cost
# with no payload.
DEFAULT_AGENT_IMAGE = {
    "claude-code": "lazyaf-claude:dev",
    "gemini": "lazyaf-gemini:dev",
    "mock": "lazyaf-agent-base:dev",
}

# Agent vocabulary -> the settings key holding its API key, and the env var
# name the CLI reads. `mock` needs neither.
AGENT_SECRET_ENV = {
    "claude-code": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "gemini": ("GEMINI_API_KEY", "gemini_api_key"),
    "mock": None,
}

# Agent vocabulary -> UsageManifest.provider, so even a step that produces no
# usage manifest (killed before the wrapper wrote one) is attributed to the
# right provider by run.py's fallback record.
AGENT_USAGE_PROVIDER = {
    "claude-code": "anthropic",
    "gemini": "google",
    "mock": "self-hosted",
}

# The image label an agent image DECLARES (baked by images/agent-base). Used
# by ONE preflight assertion - never by mode selection - so a user pointing
# an agent step at lazyaf-test-runner:dev gets one clear message instead of
# `ModuleNotFoundError: runner_common` thirty seconds into the container.
AGENT_RUNTIME_LABEL = "lazyaf.agent-runtime"

# A capability label is DECLARED only when its value is exactly "1". Presence
# alone is not a declaration - `LABEL lazyaf.agent-runtime=0` is an image
# author saying NO, and a presence-only check reads it as yes.
LABEL_DECLARED_VALUE = "1"

# Prefix of the ISOLATED branch an agent step gets when nothing declared one.
# See resolve_agent_work_branch: an agent step that names no branch must never
# inherit the branch the run was triggered on, because committing and pushing
# there is what re-fires the push trigger that started the run.
AGENT_WORK_BRANCH_PREFIX = "lazyaf/agent-"

# Default timeouts. 300s is a rounding error for an agent; 1800s is the
# agent-step default, and the in-container watchdog remains the ONE timeout
# owner (the executor's backstop is timeout + grace on top of that).
DEFAULT_STEP_TIMEOUT = 300
DEFAULT_AGENT_STEP_TIMEOUT = 1800


def default_timeout_for(step_type: str) -> int:
    """The timeout a step of this type gets when it declares none."""
    if step_type == "agent":
        return DEFAULT_AGENT_STEP_TIMEOUT
    return DEFAULT_STEP_TIMEOUT


def resolve_agent_work_branch(
    step_config: dict,
    context: dict,
    base_branch: str,
    fallback_id: str,
) -> tuple[str, bool]:
    """Which branch an agent step commits and pushes to, and whether the
    step CONFIG declared it.

    THE LOOP THIS EXISTS TO STOP. Before this, an agent step that named no
    branch fell through to the run's base branch - which for a push-triggered
    run is the branch that was just pushed. The step then committed and
    PUSHED there, the push fired the same push trigger, and the pipeline
    re-ran itself with a real provider bill attached to every lap. Nothing
    in the loop was rate-limited or depth-capped: it stopped when the budget
    did.

    The rule, in one line: **only an explicit `branch:` in the step config
    may resolve to the run's trigger/base branch.**

    Resolution order:

    1. ``step_config["branch"]`` - the EXPLICIT declaration. It may name the
       trigger branch: a pipeline author who writes ``branch: main`` on an
       agent step has said so out loud, and that is the one way to get a
       push to the branch the run was triggered on.
    2. ``context["work_branch"]`` - set by the internal ad-hoc path
       (``agent_run.start_adhoc_agent_run``) for card work and the
       playground, which know their own throwaway branch. It is honored
       ONLY while it differs from the base branch; if an ad-hoc caller ever
       passes the base branch, it is dropped rather than pushed to.
    3. Otherwise an ISOLATED branch derived from ``fallback_id`` (the
       StepRun id): ``lazyaf/agent-<8 hex>``. Unique per StepRun, so a
       re-run gets a fresh branch, and never equal to a real branch name a
       trigger watches.

    Returns:
        (work_branch, declared_in_step_config)
    """
    declared = (step_config.get("branch") or "").strip()
    if declared:
        return declared, True

    inherited = (context.get("work_branch") or "").strip()
    if inherited and inherited != base_branch:
        return inherited, False
    if inherited:
        logger.warning(
            "Agent step inherited work_branch %r, which IS the run's base "
            "branch; using an isolated branch instead - pushing to the "
            "trigger branch requires an explicit `branch:` in the step config",
            inherited,
        )

    return f"{AGENT_WORK_BRANCH_PREFIX}{(fallback_id or uuid4().hex)[:8]}", False


def build_verification_step(
    command: str,
    *,
    name: str = "Verify",
    step_id: str = "verify",
    image: str | None = None,
    timeout: int = DEFAULT_STEP_TIMEOUT,
    working_dir: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The POST-AGENT VERIFICATION STEP of an ad-hoc agent run (12.5 seam).

    The legacy card path ran the repo's test suite after the agent and
    demoted the card when it came back red. The control-layer path lost that
    gate: the agent's own exit code became the card's verdict, so a card
    whose agent exited 0 over a broken tree was offered for merge green.

    This is the pipeline-side half of putting the gate back. The ad-hoc
    pipeline gets a SECOND step - an ordinary script step running the repo's
    test command - and everything else already works:

    - it runs in the SAME workspace volume (keyed by pipeline_run_id), so it
      sees the tree the agent just committed, on the agent's work branch;
    - ``on_failure: "stop"`` makes a red suite fail the RUN, and the run's
      success is what ``agent_run.on_run_complete`` routes on, so the card
      lands in ``failed`` instead of ``in_review``;
    - in control mode its ingested test results also gate the step itself
      (see ``_apply_test_result_gate``), so a test command that swallows a
      failing suite and exits 0 still fails.

    The results themselves are read back off the RUN by
    ``agent_run.run_test_summary`` - one reader, owned by the module that
    puts the numbers on the card, so there is no second summary here.

    Args:
        command: the repo's test command, as a shell string.
        name/step_id: how the step shows up in the run's step list.
        image: pin the test image; omit for the platform default.
        timeout: seconds; the in-container watchdog owns it.
        working_dir: defaults to the image's repo checkout.
        environment: extra non-secret env for the test command.
    """
    config: dict[str, Any] = {"command": command}
    if image:
        config["image"] = image
    if working_dir:
        config["working_dir"] = working_dir
    if environment:
        config["environment"] = dict(environment)
    return {
        "id": step_id,
        "name": name,
        "type": "script",
        "config": config,
        "timeout": timeout,
        "on_success": "next",
        "on_failure": "stop",
    }


def resolve_agent_type(step_config: dict) -> str:
    """The agent an agent step selects, validated against the vocabulary.

    Accepts the historical `runner_type` spelling as well as `agent` (the
    legacy queue keyed on runner_type; pipelines in the wild carry it).
    Raises ValueError on anything unknown - there is NO default agent, and
    guessing one is how a step silently bills the wrong provider.
    """
    agent = step_config.get("agent") or step_config.get("runner_type")
    if not agent or agent == "any":
        raise ValueError(
            "agent step is missing an `agent:` key - valid agents are "
            f"{', '.join(sorted(DEFAULT_AGENT_IMAGE))} (there is no default)"
        )
    if agent not in DEFAULT_AGENT_IMAGE:
        raise ValueError(
            f"unknown agent {agent!r}: valid agents are "
            f"{', '.join(sorted(DEFAULT_AGENT_IMAGE))}"
        )
    return agent


def agent_secret_environment(agent: str, step_name: str = "") -> dict[str, str]:
    """API keys for one agent, read from settings at DISPATCH time.

    Returned as `secret_environment`, which the LocalExecutor delivers ONLY
    through the step config file - never through inspectable container env.

    A missing key fails the step HERE rather than thirty seconds later
    inside an opaque CLI auth error, and the message names the variable
    without ever putting its value in the logs.
    """
    mapping = AGENT_SECRET_ENV.get(agent)
    if mapping is None:
        return {}
    env_var, settings_key = mapping
    value = getattr(get_settings(), settings_key, None)
    if not value:
        raise ValueError(
            f"agent step {step_name!r} needs {env_var} to run the {agent!r} "
            f"CLI, but no key is configured - set {env_var} in the backend's "
            "environment"
        )
    return {env_var: value}


class LocalStepContextError(RuntimeError):
    """A local step task could not load its execution context (fix 2).

    Carries whatever rows DID load so the failure handler can still drive
    the step to FAILED and the run through its normal completion flow - no
    early return may leave a RUNNING StepRun with no owner.
    """

    def __init__(
        self,
        message: str,
        *,
        pipeline_run: "PipelineRun | None" = None,
        step_run: "StepRun | None" = None,
        pipeline: "Pipeline | None" = None,
        repo: "Repo | None" = None,
        graph: dict | None = None,
        steps: list | None = None,
        is_graph: bool = False,
        can_continue: bool = False,
    ):
        super().__init__(message)
        self.pipeline_run = pipeline_run
        self.step_run = step_run
        self.pipeline = pipeline
        self.repo = repo
        self.graph = graph
        self.steps = steps or []
        self.is_graph = is_graph
        # True when enough context loaded to run the NORMAL continuation
        # (graph fan-out / linear on_failure) instead of failing the run
        # outright.
        self.can_continue = can_continue


def parse_steps(steps_str: str | None) -> list[dict]:
    """Parse steps from JSON string to list."""
    if not steps_str:
        return []
    try:
        return json.loads(steps_str)
    except (json.JSONDecodeError, TypeError):
        return []


def parse_steps_graph(steps_graph_str: str | None) -> dict | None:
    """Parse steps_graph from JSON string to dict."""
    if not steps_graph_str:
        return None
    try:
        return json.loads(steps_graph_str)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_json_list(json_str: str | None) -> list:
    """Parse a JSON list string, returning empty list on failure."""
    if not json_str:
        return []
    try:
        result = json.loads(json_str)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def get_upstream_step_ids(graph: dict, step_id: str) -> list[str]:
    """Get all step IDs that have edges pointing TO this step."""
    edges = graph.get("edges", [])
    return [e["from_step"] for e in edges if e.get("to_step") == step_id]


def get_downstream_edges(graph: dict, step_id: str, condition: str) -> list[dict]:
    """Get all edges FROM this step matching the given condition (success/failure/always)."""
    edges = graph.get("edges", [])
    result = []
    for edge in edges:
        if edge.get("from_step") == step_id:
            edge_condition = edge.get("condition", "success")
            # Match condition: success matches success, failure matches failure, always matches both
            if edge_condition == condition or edge_condition == "always":
                result.append(edge)
    return result


def count_total_steps(graph: dict) -> int:
    """Count total steps in a graph."""
    return len(graph.get("steps", {}))


def pipeline_run_to_ws_dict(run: PipelineRun) -> dict:
    """Convert a PipelineRun model to a dict for websocket broadcast."""
    return {
        "id": run.id,
        "pipeline_id": run.pipeline_id,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "current_step": run.current_step,
        "steps_completed": run.steps_completed,
        "steps_total": run.steps_total,
        "active_step_ids": parse_json_list(run.active_step_ids),
        "completed_step_ids": parse_json_list(run.completed_step_ids),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def step_run_to_ws_dict(step_run: StepRun) -> dict:
    """Convert a StepRun model to a dict for websocket broadcast."""
    return {
        "id": step_run.id,
        "pipeline_run_id": step_run.pipeline_run_id,
        "step_index": step_run.step_index,
        "step_id": step_run.step_id,
        "step_name": step_run.step_name,
        "status": step_run.status,
        "job_id": step_run.job_id,
        "executor": step_run.executor,
        "error": step_run.error,
        "started_at": step_run.started_at.isoformat() if step_run.started_at else None,
        "completed_at": step_run.completed_at.isoformat() if step_run.completed_at else None,
    }


class PipelineExecutor:
    """Orchestrates pipeline execution."""

    def __init__(self):
        # asyncio task registry: "run:{run_id}" / "step:{run_id}:{step_run_id}"
        # -> Task. Done-callbacks log exceptions and remove entries, so a
        # crashed task is loud and nothing leaks (R1).
        self._tasks: dict[str, asyncio.Task] = {}
        # run_id -> PipelineStateMachine driving the run lifecycle.
        self._state_machines: dict[str, PipelineStateMachine] = {}
        # run_id -> async session factory bound to the engine the run was
        # started on (so local step tasks hit the same database as the caller,
        # in production AND under the test harness).
        self._session_factories: dict[str, Any] = {}
        # run_id -> asyncio.Lock serializing step-completion/dispatch sections
        # (parallel graph steps read-modify-write active/completed_step_ids;
        # without this, concurrent finishers clobber each other's updates).
        # Never popped while held - eviction runs as its own task after the
        # run's tasks drain (fix 4), so a straggler always serializes on the
        # SAME lock object.
        self._run_locks: dict[str, asyncio.Lock] = {}
        # Lazily-created seams (patchable in tests).
        self._router = None
        self._workspace_service = None
        self._local_executor = None
        # RemoteExecutor (12.6). No docker client, no construction race - it
        # talks to the runner registry - so it needs no init lock.
        self._remote_executor = None
        # Serializes LocalExecutor construction so exactly one docker client
        # ever exists (fix 5).
        self._local_executor_init_lock = asyncio.Lock()
        self._continue_in_context_logged = False
        # (resolved image ID, label) -> declared?  Mirrors LocalExecutor's
        # control-layer cache: keyed by ID so a rebuilt tag is re-evaluated.
        self._image_label_cache: dict[tuple[str, str], bool] = {}

    # -------------------------------------------------------------------------
    # Seams (lazy imports against the 12.2-INT contracts; failures are loud)
    # -------------------------------------------------------------------------

    def _get_router(self):
        """ExecutionRouter per the 12.2-INT contract:
        decide(step_type, step_config) -> RoutingDecision(mode, reason).

        No arity probing, no interim shim: a missing or contract-broken
        router raises (ImportError/TypeError) and fails the step loudly at
        dispatch - the failure IS the signal (fix 11).
        """
        if self._router is None:
            from app.services.workspace.execution_router import ExecutionRouter

            self._router = ExecutionRouter()
        return self._router

    def _get_workspace_service(self):
        """WorkspaceService module singleton per the 12.2-INT contract."""
        if self._workspace_service is None:
            from app.services.workspace_service import workspace_service

            self._workspace_service = workspace_service
        return self._workspace_service

    async def _get_local_executor(self):
        """LocalExecutor over a real docker client (client built off-loop, R5).

        Guarded by an asyncio.Lock (fix 5): concurrent first-callers - two
        parallel entry steps of the same run - must never race two docker
        clients into existence; exactly one LocalExecutor is ever built.
        The client comes from make_docker_client (cross-file contract #1:
        honors settings.docker_host, shared with workspace population).
        """
        if self._local_executor is None:
            async with self._local_executor_init_lock:
                if self._local_executor is None:
                    from starlette.concurrency import run_in_threadpool

                    from app.services.execution.local_executor import (
                        LocalExecutor,
                        make_docker_client,
                    )

                    client = await run_in_threadpool(make_docker_client)
                    self._local_executor = LocalExecutor(client)
        return self._local_executor

    async def _get_remote_executor(self):
        """RemoteExecutor (12.6) over the runner registry.

        Same lazy-import discipline as the router (fix 11): a missing or
        contract-broken RemoteExecutor raises here and fails the step loudly
        at dispatch. There is no local fallback - a step whose `requires:`
        block cannot be honored must NOT silently run on the backend host,
        which is exactly the 12.4-12.6 interim behavior this phase removes.
        """
        if self._remote_executor is None:
            from app.services.execution.remote_executor import RemoteExecutor

            self._remote_executor = RemoteExecutor()
        return self._remote_executor

    async def _get_executor(self, mode: ExecutorMode):
        """The ONE place a mode becomes an executor instance.

        Everything downstream - the event consumer, the deadline discipline,
        the completion path - is mode-blind by construction because this is
        the only branch on mode in the execution path.
        """
        if mode is ExecutorMode.REMOTE:
            return await self._get_remote_executor()
        return await self._get_local_executor()

    # -------------------------------------------------------------------------
    # Task registry
    # -------------------------------------------------------------------------

    def _spawn_task(self, key: str, coro) -> asyncio.Task:
        """Create, register, and supervise an asyncio task (R1: no dark tasks)."""
        task = asyncio.create_task(coro)
        self._tasks[key] = task

        def _on_done(t: asyncio.Task, _key: str = key) -> None:
            self._tasks.pop(_key, None)
            if t.cancelled():
                logger.info(f"Pipeline task {_key} cancelled")
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    f"Pipeline task {_key} crashed: {exc!r}",
                    exc_info=exc,
                )

        task.add_done_callback(_on_done)
        return task

    async def reset(self) -> None:
        """Test-mode reset hook (see routers/test_api.py registry).

        Drains every in-flight run/step task and drops ALL per-run in-memory
        state, which points at DB rows the reset endpoint is about to delete
        (the failure_01 decay mode: DB-only resets leave stale memory).

        Safe teardown (fix 3/13 - never hard-cancel a consumer mid-commit as
        the FIRST move):
        1. Kill in-flight containers so event streams end naturally.
        2. Give tasks a bounded grace to drain on their own.
        3. Only then cancel stragglers as a last resort.

        The cached LocalExecutor keeps its docker client but clears its
        idempotency/running caches.
        """
        if self._local_executor is not None:
            cancel_all = getattr(self._local_executor, "cancel_all", None)
            if cancel_all is not None:
                try:
                    await cancel_all()
                except Exception:
                    logger.exception("reset: killing in-flight containers failed")
        # 12.7: wake every paused breakpoint gate BEFORE draining tasks. A
        # gate parked on its 5s poll would otherwise burn the whole drain
        # grace and be cancelled as a straggler; woken, it re-reads a row the
        # reset endpoint is about to delete and returns on its own.
        try:
            await debug_session_service.reset()
        except Exception:
            logger.exception("reset: waking paused debug gates failed")
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=RESET_DRAIN_GRACE)
            if pending:
                logger.warning(
                    "reset: cancelling %d task(s) that did not drain within "
                    "%.1fs grace",
                    len(pending),
                    RESET_DRAIN_GRACE,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._state_machines.clear()
        self._session_factories.clear()
        self._run_locks.clear()
        # Images may be rebuilt between test runs (same tag, new ID).
        self._image_label_cache.clear()
        # Recreate the init lock: asyncio primitives bind to the loop that
        # first awaits them, and reset() is the boundary where the test
        # harness may hand us a fresh loop.
        self._local_executor_init_lock = asyncio.Lock()
        if self._local_executor is not None:
            self._local_executor.reset()

    async def wait_for_run(self, run_id: str) -> None:
        """Await every in-flight asyncio task belonging to a run.

        Continuations may spawn new tasks while we wait, so loop until the
        registry has none left for this run. Used by tests and shutdown.
        """
        while True:
            pending = [
                t
                for key, t in list(self._tasks.items())
                if run_id in key and not t.done()
            ]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        """Per-run lock for completion/dispatch critical sections.

        Locking discipline: acquired ONLY at the outermost entry points
        (start_pipeline's entry dispatch, on_step_complete, and
        _finish_local_step). Dispatch/continuation helpers never acquire it
        themselves - they run under their caller's hold (asyncio.Lock is not
        reentrant).

        Lifecycle (fix 4): the dict entry is NEVER popped while the lock is
        held. Run completion schedules _evict_run_lock, which waits for the
        run's tasks (stragglers included) to drain and the lock to fall idle
        before evicting - so a step finishing after completion still
        serializes on the SAME lock object.
        """
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    def _schedule_run_lock_eviction(self, run_id: str) -> None:
        """Schedule eviction of a finished run's lock (fix 4: never pop a
        lock while any holder or straggler may still reference the dict)."""
        if run_id not in self._run_locks:
            return
        self._spawn_task(
            f"evict:{run_id}:{uuid4().hex[:8]}", self._evict_run_lock(run_id)
        )

    async def _evict_run_lock(self, run_id: str) -> None:
        """Evict a run's lock only after every run/step task has drained and
        the lock is idle (no holder, no waiters). Until then, stragglers keep
        serializing on the same object."""
        while True:
            pending = [
                task
                for key, task in list(self._tasks.items())
                if (
                    key.startswith(f"run:{run_id}")
                    or key.startswith(f"step:{run_id}:")
                    or key.startswith(f"step-reap:{run_id}:")
                )
                and not task.done()
            ]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        lock = self._run_locks.get(run_id)
        if lock is None:
            return
        while True:
            async with lock:
                pass
            # No holder and no queued waiters (checked without awaiting in
            # between, so nothing can interleave): safe to evict.
            if not lock.locked() and not getattr(lock, "_waiters", None):
                break
        if self._run_locks.get(run_id) is lock:
            self._run_locks.pop(run_id, None)

    def _session_factory_for(self, run_id: str, db: AsyncSession):
        """Session factory bound to the caller's engine (own session scope for
        local step tasks; falls back to the app-global factory)."""
        factory = self._session_factories.get(run_id)
        if factory is None:
            bind = getattr(db, "bind", None)
            if bind is not None:
                factory = async_sessionmaker(
                    bind, class_=AsyncSession, expire_on_commit=False
                )
            else:
                from app.database import async_session as factory  # noqa: F811
            self._session_factories[run_id] = factory
        return factory

    # -------------------------------------------------------------------------
    # State machine helpers
    # -------------------------------------------------------------------------

    def _machine_for(self, run_id: str, total_steps: int) -> PipelineStateMachine:
        """Get (or recreate after a restart) the run's state machine."""
        machine = self._state_machines.get(run_id)
        if machine is None:
            machine = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=total_steps)
            self._state_machines[run_id] = machine
        return machine

    def _log_local_continue_in_context(self) -> None:
        """continue_in_context is obsolete on the local path (one-time INFO)."""
        if not self._continue_in_context_logged:
            logger.info(
                "continue_in_context is obsolete for locally-executed steps: "
                "the persistent workspace volume already carries state between "
                "steps. The flag is accepted and ignored (12.2-INT)."
            )
            self._continue_in_context_logged = True

    # -------------------------------------------------------------------------
    # Routing (R1: observable, never silent)
    # -------------------------------------------------------------------------

    def _decide_route(
        self, step_type: str, step_config: dict, step_name: str
    ) -> tuple[ExecutorMode, str, dict]:
        """Route a step via the ExecutionRouter contract.

        Returns (ExecutorMode, reason, requirements). Raises on any router
        failure and on an unknown mode - a routing error must fail the step
        loudly, never quietly fall back to legacy. Every compare/write site
        uses the ExecutorMode enum (cross-file contract #3).

        Phase 12.6: ExecutorMode.REMOTE is ACCEPTED and LOCAL and REMOTE
        are the only modes left. REMOTE used to raise ("...which has no
        execution path until Phase 12.6"); RemoteExecutor is that path, and
        `requirements` is the parsed `requires:` block the dispatcher matches
        against the runner registry. It is empty for a local route and may
        legitimately be empty for a remote one ("any connected runner will
        do").
        """
        router = self._get_router()
        decision = router.decide(step_type, step_config)
        reason = decision.reason
        requirements = dict(getattr(decision, "requirements", {}) or {})
        try:
            mode = ExecutorMode(decision.mode)
        except ValueError:
            raise RuntimeError(
                f"ExecutionRouter returned unknown mode {decision.mode!r} "
                f"(reason={reason!r}) for step '{step_name}'"
            ) from None
        if mode not in (ExecutorMode.LOCAL, ExecutorMode.REMOTE):
            raise RuntimeError(
                f"ExecutionRouter returned mode {mode.value!r} "
                f"(reason={reason!r}) for step '{step_name}', which has no "
                "execution path"
            )
        log = logger.warning if reason == "explicit-override" else logger.info
        detail = f" requirements={requirements}" if mode is ExecutorMode.REMOTE else ""
        log(
            f"[ROUTE] step '{step_name}' (type={step_type}) -> "
            f"{mode.value} ({reason}){detail}"
        )
        return mode, reason, requirements

    # -------------------------------------------------------------------------
    # Completion / trigger actions
    # -------------------------------------------------------------------------

    async def _complete_pipeline(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        success: bool,
    ) -> None:
        """
        Complete a pipeline run and execute trigger actions.

        This handles:
        1. Driving the PipelineStateMachine to its terminal state
        2. Setting the final status (passed/failed)
        3. Cleaning up the run's workspace (completion AND failure paths)
        4. Executing on_pass/on_fail actions from trigger_context
        5. Broadcasting the status update
        """
        run_id = pipeline_run.id

        # Drive the state machine to terminal (created in start_pipeline; may
        # be absent for runs predating a backend restart).
        machine = self._state_machines.pop(run_id, None)
        if machine is not None and not machine.is_terminal():
            try:
                if success:
                    if machine.current_status == PipelineStatus.RUNNING:
                        machine.transition_to(PipelineStatus.COMPLETING)
                    machine.transition_to(PipelineStatus.COMPLETED)
                else:
                    machine.mark_step_failed(
                        pipeline_run.current_step or 0, "pipeline failed"
                    )
            except ValueError as e:
                logger.error(
                    f"Pipeline state machine error completing run {run_id[:8]}: {e}"
                )
        if machine is not None:
            logger.info(
                f"Pipeline run {run_id[:8]} state machine terminal: "
                f"{machine.current_status.value}"
            )

        pipeline_run.status = RunStatus.PASSED.value if success else RunStatus.FAILED.value
        pipeline_run.completed_at = datetime.utcnow()
        await db.commit()
        await db.refresh(pipeline_run)

        # 12.7: a run must never leave a live debug session behind. BEFORE
        # _cleanup_workspace, because ending the session tears down its
        # sidecar and docker refuses to remove a volume a running container
        # still mounts (contract C9).
        await self._end_debug_session(db, run_id, "pipeline completed")

        # Workspace cleanup MUST happen on completion AND failure, before
        # trigger actions (salvage audit: hook placement).
        await self._cleanup_workspace(db, run_id)
        self._session_factories.pop(run_id, None)
        # Lock eviction is deferred until the run's tasks drain and the lock
        # is idle (fix 4: this method often runs UNDER the run lock).
        self._schedule_run_lock_eviction(run_id)

        # Execute trigger actions if present in trigger_context
        if pipeline_run.trigger_context:
            try:
                context = json.loads(pipeline_run.trigger_context)
                action = context.get("on_pass") if success else context.get("on_fail")

                if action and action != "nothing":
                    await self._execute_trigger_action(db, pipeline_run, context, action, success)
            except Exception as e:
                logger.error(f"Failed to execute trigger action: {e}")

        # Ad-hoc agent runs (12.5, cross-agent contract #7): card work and
        # playground sessions ARE pipeline runs now, so their completion
        # bookkeeping hangs off the one place every run ends. Durable by
        # construction - it routes on the persisted trigger_type/trigger_ref
        # columns, never an in-memory registry a restart would lose - and a
        # no-op for every other trigger type.
        await self._notify_agent_run_complete(db, pipeline_run, success)

        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        logger.info(f"Pipeline run {pipeline_run.id[:8]} completed with status {pipeline_run.status}")

    async def _notify_agent_run_complete(
        self, db: AsyncSession, pipeline_run: PipelineRun, success: bool
    ) -> None:
        """Call agent_run.on_run_complete, loudly but never fatally.

        Imported lazily so pipeline_executor keeps no import-time dependency
        on the ad-hoc run module. A failure here is logged with a traceback:
        the pipeline run itself is already terminal and committed, and losing
        that fact to a card-status bug would be worse than the bug.
        """
        try:
            from app.services import agent_run
        except ImportError:  # pragma: no cover - module lands with 12.5
            return
        hook = getattr(agent_run, "on_run_complete", None)
        if hook is None:  # pragma: no cover - defensive
            return
        try:
            await hook(db, pipeline_run, success)
        except Exception:
            logger.exception(
                "agent_run.on_run_complete failed for run %s (the run is "
                "already terminal; card/playground state may be stale)",
                pipeline_run.id[:8],
            )

    async def _end_debug_session(
        self, db: AsyncSession, run_id: str, reason: str
    ) -> None:
        """End the run's debug session, loudly but never fatally (12.7).

        The run is already terminal and committed by the time this runs;
        losing that fact to a debug bookkeeping bug would be worse than the
        bug. The session's own `end_reason` names any breakpoint that never
        fired, so an unreachable breakpoint is a visible fact rather than
        silence.
        """
        try:
            await debug_session_service.end_for_run(db, run_id, reason=reason)
        except Exception:
            logger.exception(
                "Could not end the debug session for run %s (the run itself "
                "is already terminal)",
                run_id[:8],
            )

    async def _cleanup_workspace(self, db: AsyncSession, run_id: str) -> None:
        """Remove the run's workspace volume via WorkspaceService.cleanup.

        Called UNCONDITIONALLY on every run completion (fix 11): cleanup is
        idempotent (missing row / already-CLEANED row / missing volume are
        no-ops), which also covers runs whose workspace predates a backend
        restart - no in-memory bookkeeping to go stale. Failures are loud
        but never clobber run completion; audit_orphans is the net.
        """
        try:
            workspace_service = self._get_workspace_service()
        except Exception as e:
            logger.error(
                f"Workspace service unavailable; volume for run {run_id[:8]} "
                f"may be leaked until audit_orphans sweeps: {e}"
            )
            return
        try:
            await workspace_service.cleanup(db, run_id)
            logger.info(f"Workspace cleaned for run {run_id[:8]}")
        except Exception as e:
            logger.error(
                f"Workspace cleanup FAILED for run {run_id[:8]} "
                f"(audit_orphans will sweep): {e}",
                exc_info=True,
            )
        await self._cleanup_remote_workspaces(run_id)

    async def _cleanup_remote_workspaces(self, run_id: str) -> None:
        """Tell every connected runner it may reap this run's volume (12.6).

        `retain_key` is the pipeline_run_id, so this is the remote half of
        the cleanup above: a runner agent provisions its OWN volume from
        `config.workspace` and cannot see this backend's.

        Broadcast rather than targeted, deliberately: the backend does not
        track which runners touched a run (steps can be reassigned across
        several), and a runner that never saw this retain_key no-ops. The
        agent's WORKSPACE_IDLE_REAP_SECONDS reaper remains the backstop for
        a frame that never lands, so this is an optimization, never a
        correctness dependency - which is why it never raises.
        """
        try:
            from app.services.execution.runner_protocol import (
                CleanupWorkspaceMessage,
            )
            from app.services.execution.runner_registry import runner_registry

            message = CleanupWorkspaceMessage(retain_key=run_id)
            # `machines()` is the registry's public enumeration of LIVE
            # connections - one machine exists per connected runner and both
            # are created and dropped together.
            for runner_id, _machine in runner_registry.machines():
                await runner_registry.send(runner_id, message)
        except Exception:
            logger.warning(
                f"could not announce workspace cleanup for run {run_id[:8]} to "
                "the runners; their idle reaper is the backstop",
                exc_info=True,
            )

    async def _execute_trigger_action(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        context: dict,
        action: str,
        success: bool,
    ) -> None:
        """
        Execute a trigger action after pipeline completion.

        Actions:
        - "merge" or "merge:{branch}": Approve and merge the card
        - "reject": Reject the card back to todo
        """
        card_id = context.get("card_id")
        if not card_id:
            logger.warning(f"No card_id in trigger context, cannot execute action '{action}'")
            return

        # Fetch the card
        result = await db.execute(select(Card).where(Card.id == card_id))
        card = result.scalar_one_or_none()
        if not card:
            logger.warning(f"Card {card_id} not found, cannot execute action '{action}'")
            return

        # Fetch the repo for merge operations
        result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
        repo = result.scalar_one_or_none()

        logger.info(f"Executing trigger action '{action}' for card {card_id[:8]}")

        if action == "merge" or action.startswith("merge:"):
            # Determine target branch
            if action.startswith("merge:"):
                target_branch = action[6:]  # Remove "merge:" prefix
            else:
                target_branch = repo.default_branch if repo else "main"

            # Only merge if card has a branch and is in a mergeable state
            if card.branch_name and card.status in ("in_review", "in_progress"):
                merge_result = git_repo_manager.merge_branch(
                    repo_id=card.repo_id,
                    source_branch=card.branch_name,
                    target_branch=target_branch,
                )

                if merge_result.get("success"):
                    card.status = "done"
                    await db.commit()
                    await db.refresh(card)
                    logger.info(f"Card {card_id[:8]} merged to {target_branch} and marked done")

                    # Broadcast card update
                    await manager.send_card_updated({
                        "id": card.id,
                        "repo_id": card.repo_id,
                        "title": card.title,
                        "status": card.status,
                        "branch_name": card.branch_name,
                    })
                else:
                    logger.error(f"Merge failed for card {card_id[:8]}: {merge_result.get('error')}")
            else:
                logger.warning(
                    f"Cannot merge card {card_id[:8]}: "
                    f"branch={card.branch_name}, status={card.status}"
                )

        elif action == "reject":
            # Reject card back to todo
            if card.status in ("in_review", "failed", "in_progress"):
                card.status = "todo"
                card.branch_name = None
                card.pr_url = None
                await db.commit()
                await db.refresh(card)
                logger.info(f"Card {card_id[:8]} rejected back to todo")

                # Broadcast card update
                await manager.send_card_updated({
                    "id": card.id,
                    "repo_id": card.repo_id,
                    "title": card.title,
                    "status": card.status,
                    "branch_name": card.branch_name,
                })
            else:
                logger.warning(f"Cannot reject card {card_id[:8]}: status={card.status}")

        elif action == "fail":
            # Mark card as failed (user can retry)
            if card.status in ("in_review", "in_progress"):
                card.status = "failed"
                await db.commit()
                await db.refresh(card)
                logger.info(f"Card {card_id[:8]} marked as failed")

                # Broadcast card update
                await manager.send_card_updated({
                    "id": card.id,
                    "repo_id": card.repo_id,
                    "title": card.title,
                    "status": card.status,
                    "branch_name": card.branch_name,
                })
            else:
                logger.warning(f"Cannot fail card {card_id[:8]}: status={card.status}")

        else:
            logger.warning(f"Unknown trigger action: {action}")

    # -------------------------------------------------------------------------
    # Run start
    # -------------------------------------------------------------------------

    async def start_pipeline(
        self,
        db: AsyncSession,
        pipeline: Pipeline,
        repo: Repo,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        trigger_context: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        on_run_created: Callable[
            [AsyncSession, PipelineRun], Awaitable[None]
        ] | None = None,
    ) -> PipelineRun:
        """
        Start a new pipeline run.

        For graph-based pipelines (v2): Executes ALL entry points in parallel.
        For legacy pipelines (v1): Executes steps sequentially.

        Async model (R5): dispatching a step never awaits a container. Legacy
        steps are a fast job enqueue; local steps spawn an asyncio task with
        its own session scope that streams execution. This method returns as
        soon as the run row exists and the entry steps are dispatched.

        trigger_context can contain:
        - branch: The branch to work on
        - commit_sha: The specific commit
        - card_id: The card that triggered the pipeline (for card_complete triggers)

        `on_run_created` (12.7) is awaited exactly once, AFTER the run row is
        committed and BEFORE any step is dispatched. It exists because a
        debug re-run has an ordering requirement nothing else does: its
        DebugSession row must be visible to the breakpoint gate before the
        first step task can reach it, and the row cannot exist before the run
        it points at. Inserting the session after `start_pipeline` returns is
        a race the entry step usually wins. Default None: every other caller
        is byte-for-byte unaffected.
        """
        graph = parse_steps_graph(pipeline.steps_graph)

        if graph:
            # Graph-based (v2) pipeline - execute entry points in parallel
            entry_points = graph.get("entry_points", [])
            steps_dict = graph.get("steps", {})
            total_steps = count_total_steps(graph)

            logger.info(f"Using steps_graph with {total_steps} steps, {len(entry_points)} entry points")

            # Create the pipeline run
            pipeline_run = PipelineRun(
                id=str(uuid4()),
                pipeline_id=pipeline.id,
                status=RunStatus.RUNNING.value,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
                trigger_context=json.dumps(trigger_context) if trigger_context else None,
                current_step=0,
                steps_completed=0,
                steps_total=total_steps,
                active_step_ids=json.dumps([]),
                completed_step_ids=json.dumps([]),
                started_at=datetime.utcnow(),
            )
            db.add(pipeline_run)
            await db.commit()
            await db.refresh(pipeline_run)

            if on_run_created is not None:
                await on_run_created(db, pipeline_run)

            self._init_state_machine(pipeline_run.id, total_steps)

            logger.info(f"Started pipeline run {pipeline_run.id[:8]} for pipeline {pipeline.name}")
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

            # Image preflight (12.3 hardening): every distinct step image is
            # resolved ONCE up front; a run referencing missing tags fails
            # with ONE message before step 0 dispatches.
            preflight_error = await self._preflight_step_images(
                list(steps_dict.values())
            )
            if preflight_error is not None:
                logger.error(
                    f"Pipeline run {pipeline_run.id[:8]} failed image "
                    f"preflight: {preflight_error}"
                )
                await self._complete_pipeline(db, pipeline_run, success=False)
                return pipeline_run

            if not entry_points:
                # No entry points, mark as passed
                await self._complete_pipeline(db, pipeline_run, success=True)
            else:
                # Execute ALL entry points in parallel. The run lock keeps a
                # fast-finishing local step from clobbering active_step_ids
                # while later entry points are still being dispatched.
                async with self._run_lock(pipeline_run.id):
                    for step_id in entry_points:
                        if step_id in steps_dict:
                            await self._execute_graph_step(
                                db, pipeline_run, pipeline, repo, graph, step_id, params
                            )
                        else:
                            logger.warning(f"Entry point {step_id} not found in steps")

            return pipeline_run
        else:
            # Legacy (v1) pipeline - execute sequentially
            steps = parse_steps(pipeline.steps)
            logger.info(f"Using legacy steps with {len(steps)} steps")

            pipeline_run = PipelineRun(
                id=str(uuid4()),
                pipeline_id=pipeline.id,
                status=RunStatus.RUNNING.value,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
                trigger_context=json.dumps(trigger_context) if trigger_context else None,
                current_step=0,
                steps_completed=0,
                steps_total=len(steps),
                started_at=datetime.utcnow(),
            )
            db.add(pipeline_run)
            await db.commit()
            await db.refresh(pipeline_run)

            if on_run_created is not None:
                await on_run_created(db, pipeline_run)

            self._init_state_machine(pipeline_run.id, len(steps))

            logger.info(f"Started pipeline run {pipeline_run.id[:8]} for pipeline {pipeline.name}")
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

            # Image preflight (12.3 hardening): see the graph branch above.
            preflight_error = await self._preflight_step_images(steps)
            if preflight_error is not None:
                logger.error(
                    f"Pipeline run {pipeline_run.id[:8]} failed image "
                    f"preflight: {preflight_error}"
                )
                await self._complete_pipeline(db, pipeline_run, success=False)
                return pipeline_run

            if steps:
                async with self._run_lock(pipeline_run.id):
                    await self._execute_step(db, pipeline_run, repo, steps, 0, params)
            else:
                await self._complete_pipeline(db, pipeline_run, success=True)

            return pipeline_run

    async def _preflight_step_images(self, step_defs: list[dict]) -> str | None:
        """Resolve every distinct explicitly-configured step image ONCE at
        run start (12.3 hardening).

        Returns None when all images resolve, else ONE human-readable
        message naming every missing tag - the caller fails the run with it
        BEFORE dispatching step 0, instead of dribbling per-step
        ImageNotFound failures across a partially-executed run.

        Scope: images named in step configs, PLUS (12.5) the default image of
        every agent step, because an agent step cannot fall back to
        settings.step_default_image - it needs the runner-common runtime.
        Steps without an explicit image use settings.step_default_image,
        which app startup pre-pulls; resolving it here would force a docker
        client into the no-Docker test tier. Preflight infrastructure
        failures (docker down,
        guard-blocked client) are logged and non-fatal - per-step dispatch
        surfaces them loudly.

        Agent images get ONE extra assertion: they must DECLARE
        `lazyaf.agent-runtime=1`. A user pointing an agent step at
        lazyaf-test-runner:dev gets that message instead of
        `ModuleNotFoundError: runner_common` thirty seconds in.
        """
        agent_images: set[str] = set()
        for step in step_defs:
            if step.get("type") != "agent":
                continue
            config = step.get("config") or {}
            if config.get("image"):
                agent_images.add(config["image"])
                continue
            try:
                agent_images.add(DEFAULT_AGENT_IMAGE[resolve_agent_type(config)])
            except ValueError:
                # Vocabulary errors belong to the STEP, not the run: dispatch
                # fails exactly that step with the message naming the bad
                # value, so a good step 0 still runs. Preflight only answers
                # "can these images be spawned at all".
                continue

        images = sorted(
            {
                (step.get("config") or {}).get("image")
                for step in step_defs
                if (step.get("config") or {}).get("image")
            }
            | agent_images
        )
        if not images:
            return None
        try:
            executor = await self._get_local_executor()
            missing = await executor.find_missing_images(images)
        except Exception as e:
            logger.warning(
                f"Image preflight could not run ({e!r}); dispatch will "
                f"surface any missing images per-step"
            )
            return None
        if missing:
            return (
                "missing step image(s): "
                + ", ".join(sorted(missing))
                + " - build or pull them before running this pipeline"
            )
        if agent_images:
            unlabeled = await self._agent_images_without_runtime_label(
                sorted(agent_images)
            )
            if unlabeled:
                return (
                    "image(s) "
                    + ", ".join(unlabeled)
                    + f" do not declare {AGENT_RUNTIME_LABEL}=1 and cannot run "
                    "an agent step - use an agent image "
                    f"({', '.join(sorted(set(DEFAULT_AGENT_IMAGE.values())))})"
                )
        return None

    async def _agent_images_without_runtime_label(
        self, images: list[str]
    ) -> list[str]:
        """Agent images that do not DECLARE `lazyaf.agent-runtime=1`.

        Three things this got wrong before (12.5 hardening):

        (a) It tested for the label's PRESENCE. `LABEL lazyaf.agent-runtime=0`
            is an image author saying "not an agent image" and it passed the
            preflight. The rule is the same one `image_supports_control_layer`
            already uses: the VALUE must be exactly "1".
        (b) It re-inspected every image on every run start with no cache,
            while the control-layer check next to it caches by resolved image
            ID.
        (c) It reached through `executor._docker`, so a seam that is not a
            real LocalExecutor (test stubs, any future remote executor)
            silently turned the preflight OFF and shipped a green "nothing to
            report" - the loudest possible thing to be quiet about.

        Inspection failures are still NOT reported as unlabeled: an
        unreachable daemon is an infrastructure problem, and turning it into
        "your image is wrong" would be a lie that sends the operator the
        wrong way. But every skip now says so.
        """
        try:
            executor = await self._get_local_executor()
        except Exception as e:
            logger.warning(
                f"Agent-runtime label preflight could not run ({e!r}); "
                "dispatch will surface a bad agent image per-step"
            )
            return []

        unlabeled: list[str] = []
        for image in images:
            declared = await self._image_declares_label(
                executor, image, AGENT_RUNTIME_LABEL
            )
            if declared is None:
                continue  # inspection failed / no seam - already logged
            if not declared:
                unlabeled.append(image)
        return unlabeled

    async def _image_declares_label(
        self, executor, image: str, label: str
    ) -> bool | None:
        """Does `image` declare `label=1`? None = could not tell.

        The real seam is `LocalExecutor.image_declares_label(image, label)`,
        which sits beside `image_supports_control_layer` and shares its
        cache-by-resolved-image-ID discipline; that is what runs in
        production and it is what the delegate branch below calls.

        The inline fallback under it is kept for executor seams that predate
        the method (test stubs, any future remote executor): same VALUE ==
        "1" rule, same cache discipline, and - the part that matters - a LOUD
        skip rather than a silent pass when there is nothing to inspect with.
        A preflight that quietly turns itself off reports "nothing to report".
        """
        delegate = getattr(executor, "image_declares_label", None)
        if delegate is not None:
            try:
                declared = await delegate(image, label)
                # None from the delegate means "could not tell" and must NOT
                # collapse to "unlabeled" - a daemon hiccup is not a claim
                # about the image.
                return None if declared is None else bool(declared)
            except Exception:
                logger.warning(
                    "Could not inspect image %s for the %s label",
                    image,
                    label,
                    exc_info=True,
                )
                return None

        docker_client = getattr(executor, "_docker", None)
        if docker_client is None:
            logger.warning(
                "Agent-runtime label preflight SKIPPED: %s exposes neither "
                "image_declares_label() nor a docker client, so no image can "
                "be checked for %s=%s. A mis-pinned agent image will only "
                "surface per-step at dispatch.",
                type(executor).__name__,
                label,
                LABEL_DECLARED_VALUE,
            )
            return None

        from fastapi.concurrency import run_in_threadpool

        try:
            inspected = await run_in_threadpool(docker_client.images.get, image)
        except Exception:
            logger.warning(
                "Could not inspect image %s for the %s label", image, label
            )
            return None

        image_id = getattr(inspected, "id", None) or image
        cache_key = (image_id, label)
        cached = self._image_label_cache.get(cache_key)
        if cached is None:
            cached = (inspected.labels or {}).get(label) == LABEL_DECLARED_VALUE
            self._image_label_cache[cache_key] = cached
        return cached

    def _init_state_machine(self, run_id: str, total_steps: int) -> None:
        """Create the run's state machine and drive it to RUNNING."""
        machine = PipelineStateMachine(PipelineStatus.PENDING, total_steps=total_steps)
        try:
            machine.transition_to(PipelineStatus.PREPARING)
            machine.transition_to(PipelineStatus.RUNNING)
        except ValueError as e:  # pragma: no cover - transitions above are valid
            logger.error(f"Pipeline state machine error starting run {run_id[:8]}: {e}")
        self._state_machines[run_id] = machine

    # -------------------------------------------------------------------------
    # Step dispatch (shared between graph and linear paths, fix 11)
    # -------------------------------------------------------------------------

    async def _dispatch_step_run(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        *,
        step_index: int,
        step_name: str,
        step_type: str,
        step_config: dict,
        params: dict[str, Any] | None,
        step_id: str | None = None,
    ) -> tuple[StepRun, ExecutorMode | None, str | None]:
        """Route a step, create its StepRun (executor recorded at birth, R1),
        broadcast, and dispatch it (asyncio task with its own session scope).
        LOCAL and REMOTE are the only routes; a routing failure fails the step.

        Returns (step_run, mode, route_error). On a routing failure the
        StepRun is already FAILED and broadcast; the caller drives the run
        continuation.

        12.6: LOCAL and REMOTE take the SAME dispatch line. That is the test
        of the executor contract - if this method had to learn what "remote"
        is beyond picking the executor instance, the contract was not met.
        """
        route_error: str | None = None
        mode: ExecutorMode | None = None
        requirements: dict = {}
        try:
            mode, _reason, requirements = self._decide_route(
                step_type, step_config, step_name
            )
        except Exception as e:
            logger.exception(
                f"Routing failed for step {step_index} ({step_name}) of run "
                f"{pipeline_run.id[:8]}"
            )
            route_error = f"execution routing failed: {e}"

        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=step_index,
            step_id=step_id,
            step_name=step_name,
            status=RunStatus.RUNNING.value,
            executor=mode.value if mode is not None else None,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)
        await db.commit()
        await db.refresh(step_run)
        await db.refresh(pipeline_run)

        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        if route_error is not None:
            await self._fail_step_run(db, pipeline_run, step_run, route_error)
            return step_run, None, route_error

        if mode in (ExecutorMode.LOCAL, ExecutorMode.REMOTE):
            factory = self._session_factory_for(pipeline_run.id, db)
            self._spawn_task(
                f"step:{pipeline_run.id}:{step_run.id}",
                self._run_executor_step(
                    mode,
                    factory,
                    pipeline_run.id,
                    step_run.id,
                    params,
                    requirements=requirements,
                ),
            )
        return step_run, mode, None

    # -------------------------------------------------------------------------
    # Step dispatch (graph)
    # -------------------------------------------------------------------------

    async def _execute_graph_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        graph: dict,
        step_id: str,
        params: dict[str, Any] | None = None,
        previous_runner_id: str | None = None,
    ) -> None:
        """
        Execute a single step in a graph-based pipeline.

        This method:
        1. Creates a StepRun for tracking (recording the routed executor)
        2. Routes the step: local -> asyncio task around LocalExecutor,
           legacy -> temporary Card + Job enqueued for the runner system
        3. Updates active_step_ids to track running steps
        """
        steps_dict = graph.get("steps", {})
        step = steps_dict.get(step_id)
        if not step:
            logger.error(f"Step {step_id} not found in graph")
            return

        step_name = step.get("name", step_id)
        step_type = step.get("type", "script")
        step_config = step.get("config", {})

        # Get step index for legacy compatibility (use insertion order)
        step_ids = list(steps_dict.keys())
        step_index = step_ids.index(step_id) if step_id in step_ids else 0

        logger.info(f"[GRAPH] _execute_graph_step called for step '{step_id}': {step_name} (type={step_type})")

        # Add to active steps (persisted by _dispatch_step_run's commit)
        active_ids = parse_json_list(pipeline_run.active_step_ids)
        if step_id not in active_ids:
            active_ids.append(step_id)
            pipeline_run.active_step_ids = json.dumps(active_ids)

        step_run, mode, route_error = await self._dispatch_step_run(
            db,
            pipeline_run,
            step_index=step_index,
            step_name=step_name,
            step_type=step_type,
            step_config=step_config,
            params=params,
            step_id=step_id,
        )

        if route_error is not None:
            await self._handle_graph_step_complete(
                db, pipeline_run, pipeline, repo, graph, step_id, False, None
            )
            return

        # LOCAL and REMOTE are the only routes _decide_route can return, and
        # a routing failure already returned above - so reaching here means
        # the step is dispatched and there is nothing left to fall back to.
        if step.get("continue_in_context"):
            self._log_local_continue_in_context()
        logger.info(
            f"[GRAPH] Dispatched step '{step_id}' ({step_name}) to the "
            f"{mode.value} executor"
        )

    # -------------------------------------------------------------------------
    # Step dispatch (legacy linear)
    # -------------------------------------------------------------------------

    async def _execute_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        step_index: int,
        params: dict[str, Any] | None = None,
        previous_runner_id: str | None = None,
    ) -> None:
        """
        Execute a single step in a linear (v1) pipeline.

        Routes the step: local -> asyncio task around LocalExecutor,
        legacy -> temporary Card + Job enqueued for the runner system.

        Args:
            previous_runner_id: The runner that executed the previous step (for continuation affinity)
        """
        if step_index >= len(steps):
            # All steps completed
            await self._complete_pipeline(db, pipeline_run, success=True)
            return

        step = steps[step_index]
        step_name = step.get("name", f"Step {step_index + 1}")
        step_type = step.get("type", "script")
        step_config = step.get("config", {})
        timeout = step.get("timeout", 300)
        continue_in_context = step.get("continue_in_context", False)
        step_id = step.get("id")  # Optional step ID for context directory naming

        # Extract agent-specific fields from step config (Phase 9.1c)
        agent_file_ids = step_config.get("agent_file_ids", []) if step_type == "agent" else []
        prompt_template = step_config.get("prompt_template") if step_type == "agent" else None

        # Check if this step is a continuation from the previous step
        is_continuation = False
        previous_step_logs = None
        if step_index > 0:
            prev_step_config = steps[step_index - 1]
            is_continuation = prev_step_config.get("continue_in_context", False)

            # Get previous step logs
            prev_step_run = await db.execute(
                select(StepRun)
                .where(StepRun.pipeline_run_id == pipeline_run.id)
                .where(StepRun.step_index == step_index - 1)
            )
            prev_step = prev_step_run.scalar_one_or_none()
            if prev_step and prev_step.logs:
                previous_step_logs = prev_step.logs

        logger.info(f"Executing step {step_index}: {step_name} (type={step_type}, continue_in_context={continue_in_context}, is_continuation={is_continuation})")

        # Update pipeline run's current step (persisted by _dispatch_step_run)
        pipeline_run.current_step = step_index

        step_run, mode, route_error = await self._dispatch_step_run(
            db,
            pipeline_run,
            step_index=step_index,
            step_name=step_name,
            step_type=step_type,
            step_config=step_config,
            params=params,
        )

        if route_error is not None:
            action = step.get("on_failure", "stop")
            await self._handle_action(
                db, pipeline_run, repo, steps, step_index, action, step_success=False
            )
            return

        # LOCAL and REMOTE are the only routes; a routing failure already
        # returned above. There is no third path to fall through to, and no
        # runner AFFINITY to arrange either: a continuation keeps its state on
        # the run's workspace volume, which the executor addresses by name,
        # rather than on whichever machine happened to run the previous step.
        if continue_in_context or is_continuation:
            self._log_local_continue_in_context()
        logger.info(
            f"Dispatched step {step_index} ({step_name}) to the "
            f"{mode.value} executor"
        )

    async def _fail_step_run(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        step_run: StepRun,
        error: str,
    ) -> None:
        """Mark a step run failed with an error and broadcast it (loudly)."""
        step_run.status = RunStatus.FAILED.value
        step_run.completed_at = datetime.utcnow()
        step_run.error = error
        await db.commit()
        await db.refresh(step_run)
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.publish_step_update(
            pipeline_run.id, step_run.step_index, RunStatus.FAILED.value
        )
        logger.error(
            f"Step {step_run.step_index} ({step_run.step_name}) of run "
            f"{pipeline_run.id[:8]} failed: {error}"
        )

    # -------------------------------------------------------------------------
    # Executor-driven execution path (12.2-INT local / 12.6 remote)
    # -------------------------------------------------------------------------

    async def _run_local_step(
        self,
        session_factory,
        run_id: str,
        step_run_id: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """The LOCAL specialization of `_run_executor_step`.

        Kept as its own name because it is the entry point the local-dispatch
        tests drive directly; it adds nothing but the mode.
        """
        await self._run_executor_step(
            ExecutorMode.LOCAL, session_factory, run_id, step_run_id, params
        )

    async def _debug_gate(
        self, session_factory, run_id: str, step_run_id: str, mode: ExecutorMode
    ):
        """Hold this step if a debug breakpoint names it (Phase 12.7).

        WHY IT SITS IN `_run_executor_step` AND NOWHERE ELSE. The obvious
        gate is `_dispatch_step_run`, the shared LOCAL/REMOTE dispatch line.
        It is the wrong place: `_dispatch_step_run` is called from
        `_execute_graph_step` and `_execute_step`, and BOTH run under
        `self._run_lock(run_id)` (from `start_pipeline`, and from
        `_finish_local_step_locked` -> `_handle_graph_step_complete`).
        Awaiting a human there holds the run lock for up to four hours, so
        every sibling step of a parallel graph wedges trying to finish - a
        gate that deadlocks the run it exists to debug.

        `_run_executor_step` is the per-step asyncio task. It runs OUTSIDE
        the run lock, has its own session scope, and receives `mode`, so a
        gate at its first statement buys four properties for free:

        1. It fires identically for LOCAL and REMOTE - it is above the
           `is_remote` fork.
        2. A paused step CANNOT be reaped as dead. The gate is above
           `_prepare_control_mode`, so at a breakpoint there is no
           StepExecution row: no `timeout_at`, no `last_heartbeat`, nothing
           for `recover_orphaned_executions` to find. It is also above the
           `asyncio.wait_for(..., hard_deadline)`, so that clock has not
           started either. "The heartbeat timeout is suspended at a
           breakpoint" is satisfied BY CONSTRUCTION - there is no suspension
           flag because there is nothing to suspend (contract C3).
        3. The pause holds no DB session (the service opens short ones);
           this method opens its own only after the gate returns.
        4. Failure and abort reuse the ONE completion path.

        Errors here are logged and read as "not breakpointed": a debug
        bookkeeping fault must never be able to wedge an ordinary run.
        """
        try:
            return await debug_session_service.gate(
                session_factory, run_id, step_run_id, mode
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Debug gate failed for step %s of run %s; running the step "
                "WITHOUT pausing",
                step_run_id,
                run_id[:8],
            )
            from app.services.execution.debug_session_service import DebugGateResult

            return DebugGateResult(DebugGateOutcome.RESUME)

    async def _run_executor_step(
        self,
        mode: ExecutorMode,
        session_factory,
        run_id: str,
        step_run_id: str,
        params: dict[str, Any] | None = None,
        *,
        requirements: dict | None = None,
    ) -> None:
        """Execute one executor-routed step inside its own session scope.

        Acquires the run's workspace (creating it on first use), streams the
        executor's event stream into the StepRun row and over the typed WS
        publish API, releases the workspace, then drives the run continuation
        (next steps / completion) exactly like a legacy job callback would.

        12.6: `mode` selects the executor INSTANCE and nothing else. The
        event consumer, the deadline discipline, the completion path and the
        control-mode reconciliation are byte-for-byte identical for LOCAL and
        REMOTE, because RemoteExecutor reproduces LocalExecutor's event
        contract (cross-agent contract #3). If any of them had to learn what
        "remote" is, the contract was not met.

        Two things differ, and both are properties of WHERE the container
        runs, not of how it is driven:

        1. The workspace. A remote host cannot see the backend's volume, so
           the AGENT provisions its own from `config.workspace` (section
           3.4). The backend therefore does NOT create, populate, acquire or
           release a local workspace for a remote step - cloning a repo into
           a volume nobody will mount is pure waste, and on a real remote
           host it is waste the backend pays for every step.
        2. The wire config. `_build_remote_execution_config` turns the same
           (exec_config, exec_context) the local path builds into the
           `execute_step.config` payload, via the single producer
           `runner_protocol.build_execute_step_config`.

        Wedge-proofing (fix 2): every context-load failure routes through
        _fail_wedged_local_step - no path leaves a RUNNING StepRun unowned.

        Deadline discipline (fix 3): the event-stream consumer is never
        hard-cancelled mid-commit. On the outer deadline the container is
        killed first; if the consumer still does not end within a bounded
        grace, it is abandoned (logged done-callback, session handed to a
        reaper task) and the step is failed from a FRESH session.

        Debug breakpoints (12.7, contract C1): the gate is the FIRST
        statement of this method - see `_debug_gate` for why it is here and
        nowhere else.
        """
        gate = await self._debug_gate(session_factory, run_id, step_run_id, mode)
        if gate.outcome is DebugGateOutcome.ABORTED:
            # cancel_run (or _complete_pipeline) already owns every row for
            # this run. Touching anything here would race its commits.
            return

        is_remote = mode is ExecutorMode.REMOTE
        db = session_factory()
        session_abandoned = False
        try:
            try:
                loaded = await self._load_local_step_context(db, run_id, step_run_id)
            except LocalStepContextError as err:
                await self._fail_wedged_local_step(db, run_id, err)
                return
            pipeline_run, pipeline, repo, step_run, graph, steps, step, is_graph = loaded

            if gate.outcome is DebugGateOutcome.FAILED:
                # A timed-out pause fails the step through the ORDINARY
                # completion path. No new terminal path exists for debug runs.
                await self._finish_local_step(
                    db, pipeline_run, pipeline, repo, step_run,
                    graph, steps, step, is_graph,
                    False, None, gate.error, None,
                )
                return

            step_type = step.get("type", "script")
            step_config = step.get("config", {}) or {}
            # Agent steps default to 1800s: 300 is a rounding error for an
            # agent, and the in-container watchdog stays the ONE timeout
            # owner regardless.
            timeout = step.get("timeout") or default_timeout_for(step_type)
            hard_deadline = timeout + LOCAL_STEP_HARD_TIMEOUT_GRACE

            success = False
            exit_code: int | None = None
            error: str | None = None
            log_tail: list[str] | None = None
            acquired = False
            workspace_service = None
            workspace_id: str | None = None
            consumer_task: asyncio.Task | None = None

            try:
                workspace_service = self._get_workspace_service()

                context = {}
                if pipeline_run.trigger_context:
                    try:
                        context = json.loads(pipeline_run.trigger_context) or {}
                    except (json.JSONDecodeError, TypeError):
                        context = {}
                branch = context.get("branch") or repo.default_branch
                commit_sha = context.get("commit_sha")

                if not is_remote:
                    workspace = await workspace_service.get_or_create(
                        db, run_id, repo.id, branch, commit_sha
                    )
                    await workspace_service.acquire(db, workspace.id)
                    acquired = True
                    workspace_id = workspace.id

                executor = await self._get_executor(mode)
                exec_config, exec_context = self._build_local_execution_config(
                    pipeline_run, step_run, step_type, step_config, timeout, params,
                )
                if step_type == "agent":
                    await self._attach_agent_payload(
                        db, pipeline_run, pipeline, repo, step_run,
                        step_config, exec_config,
                    )
                await self._prepare_control_mode(
                    db, executor, step_run, step_config, exec_config,
                    exec_context, timeout, mode=mode,
                )
                if is_remote:
                    self._build_remote_execution_config(
                        repo, exec_config, exec_context,
                        branch=branch,
                        commit_sha=commit_sha,
                        requirements=requirements or {},
                    )

                consumer_task = asyncio.create_task(
                    self._consume_local_events(
                        db, pipeline_run, step_run, executor, exec_config, exec_context
                    )
                )
                try:
                    success, exit_code, error, log_tail = await asyncio.wait_for(
                        asyncio.shield(consumer_task), timeout=hard_deadline
                    )
                except asyncio.TimeoutError:
                    success, exit_code, error = False, None, (
                        f"step exceeded hard deadline of {hard_deadline}s "
                        f"(container timeout did not fire)"
                    )
                    logger.error(
                        f"Local step {step_run.step_index} of run {run_id[:8]}: "
                        f"{error}"
                    )
                    # 1) Kill the container so the stream ends NATURALLY -
                    #    never cancel the consumer mid-commit (fix 3).
                    try:
                        await executor.cancel_step(exec_context["execution_key"])
                    except Exception:
                        logger.exception(
                            f"Failed to kill container for deadline-exceeded "
                            f"step {step_run.step_index} of run {run_id[:8]}"
                        )
                    # 2) Bounded grace for the consumer to end on its own.
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(consumer_task),
                            timeout=LOCAL_STEP_CONSUMER_GRACE,
                        )
                    except asyncio.TimeoutError:
                        # 3) Still stuck: abandon the task (loud done-callback)
                        #    and finish from a FRESH session below - this one
                        #    may be wedged mid-commit.
                        session_abandoned = True
                        consumer_task.add_done_callback(
                            self._log_abandoned_consumer(run_id, step_run_id)
                        )
                        logger.error(
                            f"Local step {step_run.step_index} of run "
                            f"{run_id[:8]}: consumer did not end within "
                            f"{LOCAL_STEP_CONSUMER_GRACE}s of container kill; "
                            f"abandoning it and failing the step in a fresh "
                            f"session"
                        )
                    except Exception:
                        # Consumer crashed while draining - deadline error
                        # stands; the session is usable.
                        pass
            except asyncio.CancelledError:
                # Run cancelled / reset last-resort: stop the consumer too,
                # then leave state to cancel_run.
                if consumer_task is not None and not consumer_task.done():
                    consumer_task.cancel()
                raise
            except Exception as e:
                success = False
                error = f"local execution error: {e}"
                logger.exception(
                    f"Local step {step_run.step_index} of run {run_id[:8]} crashed"
                )

            if session_abandoned:
                # Hand the poisoned session to a reaper (closed only once the
                # stuck consumer truly ends) and finish on a fresh one.
                self._spawn_task(
                    f"step-reap:{run_id}:{step_run_id}",
                    self._reap_abandoned_consumer(run_id, step_run_id, consumer_task, db),
                )
                await self._finish_local_step_fresh_session(
                    session_factory, run_id, step_run_id,
                    pipeline, repo, graph, steps, step, is_graph,
                    error, workspace_service if acquired else None, workspace_id,
                )
                return

            if acquired and workspace_service is not None:
                try:
                    await workspace_service.release(db, workspace_id)
                except Exception:
                    logger.exception(
                        f"Workspace release failed for run {run_id[:8]} "
                        f"(step {step_run.step_index})"
                    )

            await self._finish_local_step(
                db, pipeline_run, pipeline, repo, step_run,
                graph, steps, step, is_graph, success, exit_code, error,
                log_tail,
            )
        finally:
            if not session_abandoned:
                await db.close()

    def _log_abandoned_consumer(self, run_id: str, step_run_id: str):
        """Done-callback factory: an abandoned consumer must never finish
        silently (fix 3)."""

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                logger.error(
                    f"Abandoned local-step consumer for step {step_run_id} of "
                    f"run {run_id[:8]} was cancelled"
                )
                return
            exc = task.exception()
            logger.error(
                f"Abandoned local-step consumer for step {step_run_id} of run "
                f"{run_id[:8]} finally ended "
                f"({'crashed: ' + repr(exc) if exc else 'cleanly'})"
            )

        return _on_done

    async def _reap_abandoned_consumer(
        self, run_id: str, step_run_id: str, consumer_task: asyncio.Task, db
    ) -> None:
        """Wait out an abandoned consumer, then close its session.

        The session cannot be closed while the stuck task may still be using
        it (that is exactly the mid-commit teardown fix 3 forbids); the reaper
        owns both until the task truly ends.
        """
        try:
            await asyncio.gather(consumer_task, return_exceptions=True)
        finally:
            try:
                await db.close()
            except Exception:
                logger.exception(
                    f"Closing abandoned session for step {step_run_id} of run "
                    f"{run_id[:8]} failed"
                )

    async def _finish_local_step_fresh_session(
        self,
        session_factory,
        run_id: str,
        step_run_id: str,
        pipeline: Pipeline,
        repo: Repo,
        graph: dict | None,
        steps: list[dict],
        step: dict,
        is_graph: bool,
        error: str | None,
        workspace_service,
        workspace_id: str | None,
    ) -> None:
        """Fail a deadline-abandoned step from a FRESH session (fix 3).

        Re-fetches the run/step rows (the originals belong to the abandoned
        session), releases the workspace, and drives the normal completion
        flow with success=False. The step ALWAYS reaches FAILED here.
        """
        async with session_factory() as fresh_db:
            if workspace_service is not None and workspace_id is not None:
                try:
                    await workspace_service.release(fresh_db, workspace_id)
                except Exception:
                    logger.exception(
                        f"Workspace release (fresh session) failed for run "
                        f"{run_id[:8]}"
                    )
            result = await fresh_db.execute(
                select(PipelineRun)
                .where(PipelineRun.id == run_id)
                .options(selectinload(PipelineRun.step_runs))
            )
            pipeline_run = result.scalar_one_or_none()
            result = await fresh_db.execute(
                select(StepRun).where(StepRun.id == step_run_id)
            )
            step_run = result.scalar_one_or_none()
            if pipeline_run is None or step_run is None:
                logger.error(
                    f"Fresh-session finish: run {run_id} / step {step_run_id} "
                    f"row(s) missing; cannot persist the deadline failure"
                )
                return
            await self._finish_local_step(
                fresh_db, pipeline_run, pipeline, repo, step_run,
                graph, steps, step, is_graph, False, None, error,
            )

    async def _fail_wedged_local_step(
        self, db: AsyncSession, run_id: str, err: LocalStepContextError
    ) -> None:
        """Route a context-load failure through the normal failure flow
        (fix 2 - mirrors the route-failure path): fail the StepRun, then
        either drive the normal continuation (step definition missing but the
        run is intact) or fail the whole run (rows missing mid-run). Never
        warn-and-return with a RUNNING StepRun left behind.
        """
        message = f"local step context error: {err}"
        logger.error(
            f"Local step task for run {run_id[:8]} wedged at load: {err}"
        )
        pipeline_run = err.pipeline_run
        if pipeline_run is None:
            # Nothing in the DB to drive - already as loud as it gets.
            return
        async with self._run_lock(run_id):
            await db.refresh(pipeline_run)
            if pipeline_run.status not in (
                RunStatus.RUNNING.value,
                RunStatus.PENDING.value,
            ):
                return
            if (
                err.step_run is not None
                and err.step_run.status == RunStatus.RUNNING.value
            ):
                await self._fail_step_run(db, pipeline_run, err.step_run, message)
            if err.can_continue and err.step_run is not None:
                if err.is_graph:
                    await self._handle_graph_step_complete(
                        db, pipeline_run, err.pipeline, err.repo, err.graph,
                        err.step_run.step_id, False, None,
                    )
                else:
                    await self._handle_action(
                        db, pipeline_run, err.repo, err.steps,
                        err.step_run.step_index, "stop", False,
                    )
            else:
                await self._complete_pipeline(db, pipeline_run, success=False)

    async def _load_local_step_context(
        self, db: AsyncSession, run_id: str, step_run_id: str
    ):
        """Load everything a local step task needs from its own session.

        Raises LocalStepContextError on any missing row/definition, carrying
        whatever loaded so _fail_wedged_local_step can drive the step to
        FAILED and the run through completion (fix 2) - a plain return here
        would strand a RUNNING StepRun with no owner.
        """
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        pipeline_run = result.scalar_one_or_none()
        if not pipeline_run:
            raise LocalStepContextError(f"PipelineRun {run_id} not found")

        result = await db.execute(
            select(StepRun).where(StepRun.id == step_run_id)
        )
        step_run = result.scalar_one_or_none()
        if not step_run:
            raise LocalStepContextError(
                f"StepRun {step_run_id} not found",
                pipeline_run=pipeline_run,
            )

        result = await db.execute(
            select(Pipeline).where(Pipeline.id == pipeline_run.pipeline_id)
        )
        pipeline = result.scalar_one_or_none()
        if not pipeline:
            raise LocalStepContextError(
                f"Pipeline {pipeline_run.pipeline_id} not found",
                pipeline_run=pipeline_run,
                step_run=step_run,
            )

        result = await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
        repo = result.scalar_one_or_none()
        if not repo:
            raise LocalStepContextError(
                f"Repo {pipeline.repo_id} not found",
                pipeline_run=pipeline_run,
                step_run=step_run,
                pipeline=pipeline,
            )

        graph = parse_steps_graph(pipeline.steps_graph)
        steps = parse_steps(pipeline.steps)
        is_graph = bool(graph and step_run.step_id)

        if is_graph:
            step = (graph.get("steps") or {}).get(step_run.step_id)
        else:
            step = steps[step_run.step_index] if step_run.step_index < len(steps) else None
        if step is None:
            raise LocalStepContextError(
                f"step definition not found for StepRun {step_run_id} "
                f"(index={step_run.step_index}, id={step_run.step_id})",
                pipeline_run=pipeline_run,
                step_run=step_run,
                pipeline=pipeline,
                repo=repo,
                graph=graph,
                steps=steps,
                is_graph=is_graph,
                can_continue=True,
            )

        return pipeline_run, pipeline, repo, step_run, graph, steps, step, is_graph

    def _build_local_execution_config(
        self,
        pipeline_run: PipelineRun,
        step_run: StepRun,
        step_type: str,
        step_config: dict,
        timeout: int,
        params: dict[str, Any] | None,
    ) -> tuple[dict, dict]:
        """Build (step_config, execution_context) for LocalExecutor.execute_step.

        Only EXPLICIT step overrides pass through; image/working_dir/HOME/
        network defaults are single-sourced in the LocalExecutor itself
        (settings-driven there, fix 11). Raises ValueError on unknown
        `needs:` capabilities - the caller fails the step loudly.

        Agent steps (12.5) additionally get: the fixed wrapper command (users
        never write it), the agent's default image, and `secret_environment`
        carrying the provider API key. The DB-sourced half of the agent
        payload is attached afterwards by `_attach_agent_payload`, which can
        await the session this synchronous builder does not have.
        """
        environment = dict(step_config.get("environment") or {})
        if params:
            environment.update({str(k): str(v) for k, v in params.items()})

        is_agent = step_type == "agent"
        agent = resolve_agent_type(step_config) if is_agent else None

        exec_step_config: dict[str, Any] = {
            "type": step_type,
            "command": (
                AGENT_WRAPPER_COMMAND if is_agent
                else step_config.get("command", "")
            ),
            "timeout": timeout,
        }
        if is_agent:
            # Secrets travel in the config FILE only; a missing key raises
            # HERE, at dispatch, with the variable named.
            exec_step_config["secret_environment"] = agent_secret_environment(
                agent, step_run.step_name or ""
            )
            exec_step_config["usage_provider"] = AGENT_USAGE_PROVIDER.get(
                agent, "self-hosted"
            )
            if step_config.get("role"):
                exec_step_config["role"] = step_config["role"]
        if environment:
            exec_step_config["environment"] = environment
        if step_config.get("image"):
            exec_step_config["image"] = step_config["image"]
        elif is_agent:
            exec_step_config["image"] = DEFAULT_AGENT_IMAGE[agent]
        if step_config.get("working_dir"):
            exec_step_config["working_dir"] = step_config["working_dir"]
        if step_config.get("memory_limit"):
            exec_step_config["memory_limit"] = step_config["memory_limit"]
        if step_config.get("shell"):
            exec_step_config["shell"] = step_config["shell"]

        # Mount specs keep their EXPLICIT addressing - LocalExecutor gates
        # bind sources against the allowlist (R6 / fix 10).
        mounts = list(step_config.get("mounts") or [])
        # Step-config sugar (fix 10): `needs: [docker]` translates to the
        # docker-socket bind mount HERE, so 12.4 changes one site while
        # raw-bind-with-allowlist stays the mechanism underneath.
        needs = step_config.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        for need in needs:
            if need == "docker":
                from app.services.execution.local_executor import DOCKER_SOCKET_SOURCE

                mounts.append({
                    "addressing": "bind",
                    "source": DOCKER_SOCKET_SOURCE,
                    "target": DOCKER_SOCKET_SOURCE,
                    "mode": "rw",
                })
            else:
                raise ValueError(
                    f"unknown step 'needs' capability {need!r} (known: docker)"
                )
        if mounts:
            exec_step_config["mounts"] = mounts

        exec_context = {
            "pipeline_run_id": pipeline_run.id,
            "step_run_id": step_run.id,
            "step_index": step_run.step_index,
            # Unique per StepRun so a re-run never hits the idempotency cache
            # of an older attempt.
            "execution_key": f"{pipeline_run.id}:{step_run.step_index}:{step_run.id}",
            "workspace_volume": generate_volume_name(pipeline_run.id),
        }
        return exec_step_config, exec_context

    async def _attach_agent_payload(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        step_run: StepRun,
        step_config: dict,
        exec_config: dict,
    ) -> None:
        """Fill `exec_config["agent"]` with the DB-sourced half of the payload.

        Split from `_build_local_execution_config` for one reason: this half
        needs the session (previous-step logs, AgentFile resolution) and that
        builder is synchronous and widely called. The LocalExecutor feeds the
        result straight to `generate_agent_config`, which stays the single
        producer of the file shape (R3) - this method supplies FIELDS, never
        a hand-built file.

        Everything the container would otherwise have to look up itself is
        resolved HERE, because the container has no DB: the prompt is
        rendered backend-side (agent_prompt.py), agent_file_ids become one
        `agents_json` string, and the previous step's logs come from the
        StepRun row rather than the legacy `.lazyaf-context/` directory.
        """
        from app.services.agent_prompt import render_agent_prompt
        from app.services.control_layer.workspace import (
            truncate_previous_step_logs,
        )

        agent = resolve_agent_type(step_config)
        settings = get_settings()
        backend_url = getattr(
            settings, "container_backend_url", "http://backend:8000"
        )

        context = {}
        if pipeline_run.trigger_context:
            try:
                context = json.loads(pipeline_run.trigger_context) or {}
            except (json.JSONDecodeError, TypeError):
                context = {}

        base_branch = context.get("branch") or repo.default_branch or "main"
        work_branch, branch_declared = resolve_agent_work_branch(
            step_config, context, base_branch, step_run.id
        )

        # `task:` is the pipeline-YAML spelling of a one-line instruction
        # (the dogfood ratchet's mock-agent step uses it); `title` is the
        # card-shaped one. Both feed {{title}} - an agent step that states
        # what to do must not have that string silently dropped.
        card_title = (
            step_config.get("title")
            or step_config.get("task")
            or context.get("card_title")
            or (step_run.step_name or "")
        )
        card_description = (
            step_config.get("description")
            or context.get("card_description")
            or ""
        )

        previous_name, previous_logs = await self._load_previous_step_output(
            db, pipeline_run, step_run
        )
        # Cap BEFORE rendering: the prompt carries the same text, so an
        # uncapped log would blow the prompt as well as the wire payload.
        capped_logs, _ = truncate_previous_step_logs(previous_logs)

        prompt = render_agent_prompt(
            card_title=card_title,
            card_description=card_description,
            prompt_template=step_config.get("prompt_template"),
            previous_step_logs=capped_logs,
        )

        agents_json = await self._resolve_agents_json(
            db, repo, base_branch, step_config.get("agent_file_ids") or []
        )

        commit = step_config.get("commit")
        if isinstance(commit, dict):
            commit_enabled = bool(commit.get("enabled", True))
            commit_message = commit.get("message")
            push = bool(commit.get("push", True))
            allow_empty = bool(commit.get("allow_empty", False))
        else:
            # `commit: false` is the dogfood ratchet's spelling: a real agent
            # step through the real runtime that must never push to its own
            # repo.
            commit_enabled = bool(commit) if commit is not None else True
            commit_message = None
            push = commit_enabled
            allow_empty = False

        # Post-condition of resolve_agent_work_branch, asserted rather than
        # assumed: the ONE way to push to the run's own trigger/base branch is
        # an explicit `branch:` in the step config. If this ever fires, the
        # resolver regressed and the self-triggering push loop is back.
        if push and not branch_declared and work_branch == base_branch:
            raise ValueError(  # pragma: no cover - unreachable by construction
                f"agent step {step_run.step_name!r} would push to the run's "
                f"own branch {base_branch!r} without declaring it; pushing to "
                "the trigger branch requires an explicit `branch:` in the "
                "step config"
            )

        exec_config["agent"] = {
            "agent": agent,
            "prompt": prompt,
            "model": step_config.get("model"),
            "agents_json": agents_json,
            "stream": bool(step_config.get("stream", True)),
            "card_id": context.get("card_id"),
            "card_title": card_title,
            "card_description": card_description,
            "step_index": step_run.step_index,
            "step_name": step_run.step_name or "",
            "previous_step_name": previous_name,
            "previous_step_logs": previous_logs,
            "repo_id": repo.id,
            "workdir": self._agent_repo_workdir(exec_config, settings),
            "base_branch": base_branch,
            "branch": work_branch,
            "remote_url": f"{backend_url}/git/{repo.id}.git",
            "commit_enabled": commit_enabled,
            "commit_message": commit_message,
            "push": push,
            "allow_empty": allow_empty,
            "mock_config": step_config.get("mock_config"),
            # M13 seam: on the wire NOW, null everywhere in 12.5.
            "role": step_config.get("role"),
        }

    @staticmethod
    def _agent_repo_workdir(exec_config: dict, settings) -> str:
        """Where the agent runs AND where its commit is staged.

        COMMIT SCOPE (12.5 hardening). The workspace volume is shared by
        every step of a run, so by the time an agent step runs it can already
        contain artifacts an earlier step dropped there - caches, build
        output, downloaded fixtures. The wrapper stages with `git add -A`,
        which is bounded by the git worktree it runs in and by nothing else,
        so the ONE thing that keeps a shared-workspace artifact out of a
        pushed commit is running that staging inside the REPO CHECKOUT.

        This pins that: the agent payload's workdir is the repo checkout
        (`settings.step_working_dir`, `/workspace/repo`) or a directory
        underneath it, never a `working_dir` override that points elsewhere
        in the volume. An override outside the checkout is honored for
        nothing - it is dropped with a warning rather than silently turning
        the whole workspace into the commit's staging area.

        The remaining gap is artifacts written INTO the checkout by an
        earlier step; narrowing that needs the wrapper to diff the tree
        before/after the agent, which is runner-common's side of the seam
        (see the requested edit to runner_common/agent_wrapper.py).
        """
        checkout = settings.step_working_dir
        declared = exec_config.get("working_dir")
        if not declared:
            return checkout
        if declared == checkout or declared.startswith(
            checkout.rstrip("/") + "/"
        ):
            return declared
        logger.warning(
            "Agent step declares working_dir %r, which is outside the repo "
            "checkout %r; the agent runs (and commits) in the checkout - a "
            "commit staged from the shared workspace root would carry earlier "
            "steps' artifacts",
            declared,
            checkout,
        )
        return checkout

    async def _load_previous_step_output(
        self, db: AsyncSession, pipeline_run: PipelineRun, step_run: StepRun
    ) -> tuple[str | None, str | None]:
        """(name, logs) of the step before this one, or (None, None).

        Replaces the legacy `.lazyaf-context/step_N.log` channel with the
        DB row that is already the single source of a step's logs (R3).
        """
        if step_run.step_index <= 0:
            return None, None
        result = await db.execute(
            select(StepRun)
            .where(StepRun.pipeline_run_id == pipeline_run.id)
            .where(StepRun.step_index == step_run.step_index - 1)
        )
        previous = result.scalars().first()
        if previous is None:
            return None, None
        return previous.step_name, (previous.logs or None)

    async def _resolve_agents_json(
        self, db: AsyncSession, repo: Repo, branch: str, agent_file_ids: list
    ) -> str | None:
        """Resolve agent_file_ids to the `claude --agents` JSON string.

        The backend owns AgentFile and the repo-agent overlay; the container
        has no DB, so resolution happens HERE and travels as one string.
        A file that cannot be resolved is skipped with a WARNING rather than
        failing the step: a missing optional sub-agent is not a reason to
        lose the work.
        """
        if not agent_file_ids:
            return None
        try:
            from app.models import AgentFile
            from app.services.agent_resolver import AgentResolver

            result = await db.execute(
                select(AgentFile).where(AgentFile.id.in_(list(agent_file_ids)))
            )
            files = list(result.scalars().all())
            missing = set(agent_file_ids) - {f.id for f in files}
            if missing:
                logger.warning(
                    "agent step references unknown agent_file_ids %s "
                    "(skipped)",
                    sorted(missing),
                )
            resolver = AgentResolver()
            resolved = await resolver.resolve_agents(
                db, repo.id, branch, [f.name for f in files]
            )
            if not resolved:
                return None
            return json.dumps({
                entry["name"]: {
                    "description": entry.get("description") or "",
                    "prompt": entry.get("prompt_template") or "",
                }
                for entry in resolved
            })
        except Exception:
            logger.exception(
                "agent file resolution failed; dispatching the step without "
                "sub-agents"
            )
            return None

    async def _prepare_control_mode(
        self,
        db: AsyncSession,
        executor,
        step_run: StepRun,
        step_config: dict,
        exec_config: dict,
        exec_context: dict,
        timeout: int,
        *,
        mode: ExecutorMode = ExecutorMode.LOCAL,
    ) -> None:
        """Decide the step's reporting mode AT DISPATCH TIME (12.3), never
        mid-flight, and stamp it EXPLICITLY into exec_context["control_mode"]
        so neither the executor nor the event consumer ever guesses.

        Control mode requires ALL of:
        - the image bakes the `lazyaf.control-layer` capability label
          (declared by the image author; LocalExecutor inspects+caches it)
        - the step did not opt out via `config.control: false` (debug escape
          hatch; there is NO `control: true` promotion for unlabeled images)
        - the command is a string (exec-form list commands are the explicit
          shell-less opt-out and keep stdout mode)

        Selecting control mode creates what the /api/steps/* router
        authenticates against: the StepExecution row (PREPARING, timeout_at
        = now + timeout + hard grace) plus a per-step-execution JWT scoped
        to that row's id, with lifetime = timeout + grace +
        STEP_TOKEN_TTL_SLACK (not the 24h default, and no longer a full
        hour: terminal reconciliation 409s zombie posts anyway, so the token
        only needs to outlive a legitimately late final report). Both travel
        to the container ONLY via the config file the LocalExecutor delivers
        with put_archive.

        Stock/unlabeled images take the stdout path with ZERO behavior
        change.

        AGENT STEPS (12.5) are one exception: for them control mode is
        MANDATORY, so every escape hatch RAISES instead of downgrading. An
        agent step in stdout mode would run the wrapper with no config file
        (and the API key would have to travel in inspectable container env),
        so it fails loudly at dispatch instead.

        REMOTE STEPS (12.6) are the other, for the two reasons spelled out
        at `_REMOTE_NEEDS_CONTROL`. They also skip the image-label probe:
        the label lives in an image on a host the backend does not own, so
        inspecting a local image of the same tag would answer a question
        about the wrong machine. Image presence is the agent's preflight.
        """
        exec_context["control_mode"] = False
        is_agent = exec_config.get("type") == "agent"
        is_remote = mode is ExecutorMode.REMOTE

        if step_config.get("control") is False:
            if is_agent:
                raise ValueError(
                    f"agent step {step_run.step_name!r} cannot run with "
                    "`control: false`: the wrapper is configured through the "
                    "step config FILE, which only control mode delivers, and "
                    "the provider API key must never travel in inspectable "
                    "container environment"
                )
            if is_remote:
                raise ValueError(_REMOTE_NEEDS_CONTROL.format(
                    step=step_run.step_name, why="`control: false` was set"
                ))
            logger.info(
                f"Step {step_run.step_index} ({step_run.step_name}): control "
                f"mode disabled by step config (control: false) - stdout mode"
            )
            return
        if not isinstance(exec_config.get("command", ""), str):
            if is_agent:  # pragma: no cover - the builder always emits a str
                raise ValueError(
                    f"agent step {step_run.step_name!r} has a non-string "
                    "command; the wrapper command is platform-owned"
                )
            if is_remote:
                raise ValueError(_REMOTE_NEEDS_CONTROL.format(
                    step=step_run.step_name,
                    why="the command is an exec-form list",
                ))
            return  # exec-form list command: explicit stdout-mode opt-out

        settings = get_settings()
        image = exec_config.get("image") or settings.step_default_image
        # REMOTE steps skip the image-label probe entirely (12.6). The label
        # lives in an image on a host the BACKEND does not own and may not
        # even have pulled; inspecting a local image of the same tag would
        # answer a question about the wrong machine. Image presence and its
        # capability label are the AGENT's preflight (section 7.1) - a step
        # whose image is missing there fails with the identical
        # "Image not found: <tag>" message the local path produces.
        if mode is not ExecutorMode.REMOTE and not await (
            executor.image_supports_control_layer(image)
        ):
            if is_agent:
                raise ValueError(
                    f"agent step {step_run.step_name!r} is pinned to image "
                    f"{image!r}, which does not declare the control-layer "
                    "capability label. Agent steps require an agent image "
                    f"({', '.join(sorted(set(DEFAULT_AGENT_IMAGE.values())))})."
                )
            return

        from app.services.execution.idempotency import ExecutionService
        from app.services.control_layer.auth import generate_step_token

        execution_service = ExecutionService(db)
        execution = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=exec_context["execution_key"],
        )
        execution.status = StepExecutionStatus.PREPARING.value
        execution.timeout_at = datetime.utcnow() + timedelta(
            seconds=timeout + LOCAL_STEP_HARD_TIMEOUT_GRACE
        )
        await db.commit()

        token = generate_step_token(
            step_id=execution.id,
            execution_key=exec_context["execution_key"],
            expires_in_seconds=(
                timeout + LOCAL_STEP_HARD_TIMEOUT_GRACE + STEP_TOKEN_TTL_SLACK
            ),
        )

        exec_context["control_mode"] = True
        exec_context["step_execution_id"] = execution.id
        exec_context["step_auth_token"] = token
        logger.info(
            f"Step {step_run.step_index} ({step_run.step_name}): control mode "
            f"(image {image}, step_execution {execution.id[:8]})"
        )

    # -------------------------------------------------------------------------
    # Remote dispatch (12.6)
    # -------------------------------------------------------------------------

    def _build_remote_execution_config(
        self,
        repo: Repo,
        exec_config: dict,
        exec_context: dict,
        *,
        branch: str,
        commit_sha: str | None,
        requirements: dict,
    ) -> dict:
        """Stamp the remote half of `exec_context` and build the wire config.

        Adds to `exec_context`, in place:
            runner_requirements
                          - the parsed `requires:` block the dispatcher
                            matches against the registry. RemoteExecutor
                            persists it onto the StepExecution column of the
                            same name, which is what makes a step requeued
                            after a backend restart still matchable (the
                            dispatch closure does not survive a restart; the
                            row does).
            repo_id / clone_url / branch / commit_sha / retain_key
                          - the workspace provisioning inputs the AGENT
                            needs, because it clones into its OWN volume.
            remote_config - the `execute_step.config` payload.

        The payload itself is produced ONLY by
        `runner_protocol.build_execute_step_config` (cross-agent contract
        #2). This method's job is to hand that single producer the two
        control FILES the local path builds inside the executor - so the
        producers (`generate_step_config` / `generate_agent_config`) stay
        single-sourced and only the DELIVERY changes (R3).

        Secret boundary (cross-agent contract #9): the step JWT and
        `secret_environment` go into `control_files` and NOWHERE else.
        `container.environment` is what `docker inspect` shows on the remote
        host, so it carries the non-secret table and CONFIG_PATH only.
        """
        from app.services.execution.runner_protocol import build_execute_step_config

        settings = get_settings()
        exec_context["runner_requirements"] = dict(requirements or {})
        exec_context["repo_id"] = repo.id
        exec_context["clone_url"] = settings.container_git_url_template.format(
            repo_id=repo.id
        )
        exec_context["branch"] = branch
        exec_context["commit_sha"] = commit_sha
        # One volume per RUN, exactly as locally: HOME=/workspace/home
        # persistence between steps has to work the same on both hosts.
        exec_context["retain_key"] = exec_context["pipeline_run_id"]

        step_config_file, agent_config_file = self._build_control_files(
            exec_config, exec_context
        )
        config = build_execute_step_config(
            exec_config, exec_context, step_config_file, agent_config_file
        )
        exec_context["remote_config"] = config
        logger.info(
            "Remote step %s of run %s: config keys=%s image=%s volume=%s "
            "requirements=%s",
            exec_context["step_index"],
            str(exec_context["pipeline_run_id"])[:8],
            sorted(config.keys()),
            config["container"]["image"],
            config["workspace"]["volume"],
            exec_context["runner_requirements"],
        )
        return config

    def _build_control_files(
        self, exec_config: dict, exec_context: dict
    ) -> tuple[dict | None, dict | None]:
        """The control files for a remote step, from the SAME producers the
        local executor uses (R3).

        Returns (step_config_file, agent_config_file); both None when the
        step is not in control mode, which is how a stdout-mode remote step
        travels with no config file at all - the same shape the local path
        produces for an unlabeled image.
        """
        if not exec_context.get("control_mode"):
            return None, None

        from app.services.control_layer.workspace import (
            generate_agent_config,
            generate_step_config,
        )
        from app.services.execution.local_executor import (
            AGENT_CONFIG_PATH_ENV,
            AGENT_CONFIG_PREFIX,
            CONTROL_CONFIG_DIR,
        )

        settings = get_settings()
        step_execution_id = exec_context["step_execution_id"]
        user_env = exec_config.get("environment", {}) or {}
        secret_env = exec_config.get("secret_environment") or {}
        agent_payload = exec_config.get("agent")

        # The FILE environment is the secret channel (12.5), verbatim from
        # the local path: the in-container executor does
        # env.update(config.environment) before Popen, so these reach the
        # step process without ever entering inspectable container env.
        file_environment = {**user_env, **secret_env}
        agent_filename = f"{AGENT_CONFIG_PREFIX}{step_execution_id}.json"
        if agent_payload is not None:
            file_environment[AGENT_CONFIG_PATH_ENV] = (
                f"/workspace/{CONTROL_CONFIG_DIR}/{agent_filename}"
            )

        step_config_file = generate_step_config(
            step_id=step_execution_id,
            step_run_id=exec_context["step_run_id"],
            execution_key=exec_context["execution_key"],
            command=exec_config.get("command", ""),
            backend_url=getattr(
                settings, "container_backend_url", "http://backend:8000"
            ),
            auth_token=exec_context["step_auth_token"],
            environment=file_environment,
            timeout_seconds=exec_config.get("timeout", 300),
            working_directory=exec_config.get(
                "working_dir", settings.step_working_dir
            ),
            shell=exec_config.get("shell", "bash"),
        )
        agent_config_file = (
            generate_agent_config(**agent_payload) if agent_payload is not None
            else None
        )
        return step_config_file, agent_config_file

    async def _consume_local_events(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        step_run: StepRun,
        executor,
        exec_config: dict,
        exec_context: dict,
    ) -> tuple[bool, int | None, str | None, list[str] | None]:
        """Consume the LocalExecutor event stream, persisting incrementally.

        Event shape (see app/services/execution/local_executor.py):
          {"type": "status", "status": "preparing"|"running"|...}
          {"type": "log", "line": "..."}
          {"type": "result", "status": "completed"|"failed"|"timeout",
           "exit_code": int|None, "error": str|None,
           "log_tail": list[str] (control mode only)}

        Returns (success, exit_code, error, log_tail) - log_tail is the
        executor's bounded stdout forensics tail (control mode), passed
        through to _finish_local_step.

        Log persistence is BATCHED (fix 7): lines buffer and flush to the
        StepRun row (one commit) plus the typed WS batch publish every
        LOG_FLUSH_MAX_LINES lines or LOG_FLUSH_INTERVAL_SECONDS - whichever
        first - with a final flush on the terminal event. A pull task (never
        cancelled on the flush timer) keeps the executor generator safe.

        Control mode (12.3, R3 - one writer per datum): when
        exec_context["control_mode"] is set (EXPLICIT, decided at dispatch -
        never guessed here), the /api/steps/* router is the sole writer of
        StepRun.logs / step_log frames and of the intermediate step_update
        broadcast, so this consumer DROPS log and status events (debug
        logger only - the runtime still echoes to container stdout for
        docker-logs forensics). The stream is consumed solely for liveness,
        the backstop timeout, and the `result` event: the container exit
        code stays ground truth for terminal state in BOTH modes.
        """
        run_id = pipeline_run.id
        step_index = step_run.step_index
        control_mode = bool(exec_context.get("control_mode"))
        loop = asyncio.get_running_loop()
        buffer: list[str] = []
        flush_deadline = loop.time() + LOG_FLUSH_INTERVAL_SECONDS

        async def flush() -> None:
            nonlocal flush_deadline
            if buffer:
                lines = buffer[:]
                buffer.clear()
                step_run.logs = (step_run.logs or "") + "".join(
                    f"{line}\n" for line in lines
                )
                await db.commit()
                await manager.publish_step_logs(run_id, step_index, lines)
            flush_deadline = loop.time() + LOG_FLUSH_INTERVAL_SECONDS

        stream = executor.execute_step(exec_config, exec_context)
        pull: asyncio.Task | None = None
        try:
            while True:
                if pull is None:
                    pull = asyncio.ensure_future(anext(stream))
                # With buffered lines, wake at the flush deadline; the pull
                # task itself is never cancelled by the timer (cancelling
                # anext() would tear down the executor generator).
                timeout = (
                    max(0.0, flush_deadline - loop.time()) if buffer else None
                )
                done, _pending = await asyncio.wait({pull}, timeout=timeout)
                if not done:
                    await flush()
                    continue
                finished, pull = pull, None
                try:
                    event = finished.result()
                except StopAsyncIteration:
                    break

                event_type = event.get("type")

                if event_type == "status":
                    status = event.get("status", "")
                    if control_mode:
                        # Router owns intermediate status frames (R3).
                        logger.debug(
                            "control-mode step %s of run %s: dropped executor "
                            "status event %r",
                            step_index, run_id[:8], status,
                        )
                    else:
                        # Terminal statuses are persisted from the result
                        # event; the StepRun stays RUNNING through
                        # preparing/running.
                        await manager.publish_step_update(run_id, step_index, status)

                elif event_type == "log":
                    if control_mode:
                        # Router owns StepRun.logs + step_log frames (R3); no
                        # buffer append, no WS - stdout stays in docker logs.
                        logger.debug(
                            "control-mode step %s of run %s: dropped stdout "
                            "line %r",
                            step_index, run_id[:8], event.get("line", ""),
                        )
                        continue
                    buffer.append(event.get("line", ""))
                    if (
                        len(buffer) >= LOG_FLUSH_MAX_LINES
                        or loop.time() >= flush_deadline
                    ):
                        await flush()

                elif event_type == "result":
                    await flush()  # final flush on the terminal event
                    status = event.get("status")
                    exit_code = event.get("exit_code")
                    error = event.get("error")
                    log_tail = event.get("log_tail")
                    if status == "completed":
                        return True, exit_code, None, log_tail
                    if status == "timeout":
                        timeout_s = event.get(
                            "timeout_seconds", exec_config.get("timeout")
                        )
                        return False, exit_code, (
                            error or f"step timed out after {timeout_s}s"
                        ), log_tail
                    if exit_code is not None and not error:
                        error = f"step failed with exit code {exit_code}"
                    return False, exit_code, error or "step failed", log_tail

                else:
                    logger.warning(
                        f"Local step {step_index} of run {run_id[:8]}: unknown "
                        f"executor event type {event_type!r}"
                    )
        finally:
            # Abnormal exit only (an exception escaped): stop the pull task
            # so the executor generator is finalized, not leaked.
            if pull is not None and not pull.done():
                pull.cancel()
                await asyncio.gather(pull, return_exceptions=True)

        # The stream ending without a result event is a contract violation -
        # surface it, never treat it as success (R1).
        await flush()
        return False, None, "executor event stream ended without a result event", None

    async def _finish_local_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        step_run: StepRun,
        graph: dict | None,
        steps: list[dict],
        step: dict,
        is_graph: bool,
        success: bool,
        exit_code: int | None,
        error: str | None,
        log_tail: list[str] | None = None,
    ) -> None:
        """Persist a local step's final state and drive the run continuation.

        Serialized on the run lock: parallel graph steps finishing together
        must not interleave their read-modify-writes of the run's tracking
        columns.
        """
        async with self._run_lock(pipeline_run.id):
            await self._finish_local_step_locked(
                db, pipeline_run, pipeline, repo, step_run,
                graph, steps, step, is_graph, success, exit_code, error,
                log_tail,
            )

    async def _load_control_execution(
        self, db: AsyncSession, pipeline_run: PipelineRun, step_run: StepRun
    ) -> StepExecution | None:
        """Load the step's StepExecution row, present iff the step
        dispatched in control mode (_prepare_control_mode creates it under
        the dispatch execution key)."""
        execution_key = f"{pipeline_run.id}:{step_run.step_index}:{step_run.id}"
        result = await db.execute(
            select(StepExecution).where(
                StepExecution.execution_key == execution_key
            )
        )
        return result.scalar_one_or_none()

    def _reconcile_control_execution(
        self,
        execution: StepExecution,
        step: dict,
        success: bool,
        exit_code: int | None,
        error: str | None,
        warning_lines: list[str],
    ) -> tuple[bool, str | None]:
        """Reconcile control-runtime telemetry with the executor's ground
        truth at step finish (12.3 hardening fix 2).

        (a) A row that never left PREPARING means the control runtime never
            reported - the step FAILS loudly regardless of exit code 0 (an
            image without a working /control runtime must never read green).
        (b) An in-container timeout (exit 124 / runtime-reported timeout)
            surfaces as a timeout error, not a generic failure.
        (c) A runtime-reported error (e.g. dropped log lines) is surfaced:
            a loud warning line for StepRun.logs plus StepRun.error - the
            step keeps its real exit status.
        (d) The row is marked terminal so the /api/steps router 409s any
            zombie-token post arriving after the step finished.

        Returns the possibly-amended (success, error).
        """
        timed_out = (
            exit_code == 124
            or execution.status == StepExecutionStatus.TIMEOUT.value
        )
        if execution.status in NEVER_REPORTED_STEP_EXECUTION_STATUSES:
            never_msg = (
                "control runtime never reported "
                "(image lacks a working /control runtime?)"
            )
            success = False
            error = never_msg if not error else f"{never_msg}; {error}"
        elif not success and timed_out:
            timeout_s = step.get("timeout", 300)
            error = (
                f"step timed out after {timeout_s}s "
                f"(in-container timeout, exit code 124)"
            )
        if execution.error:
            warning_lines.append(f"[lazyaf] WARNING: {execution.error}\n")
            if not error:
                error = execution.error
        if execution.status not in TERMINAL_STEP_EXECUTION_STATUSES:
            if timed_out and not success:
                execution.status = StepExecutionStatus.TIMEOUT.value
            elif success:
                execution.status = StepExecutionStatus.COMPLETED.value
            else:
                execution.status = StepExecutionStatus.FAILED.value
            if execution.completed_at is None:
                execution.completed_at = datetime.utcnow()
        if execution.exit_code is None and exit_code is not None:
            execution.exit_code = exit_code
        return success, error

    async def _apply_test_result_gate(
        self,
        db: AsyncSession,
        step_run: StepRun,
        success: bool,
        error: str | None,
        warning_lines: list[str],
    ) -> tuple[bool, str | None]:
        """Demote a green step whose ingested test results are RED.

        THE LOST GATE. The legacy runner path had exactly one line of this
        (`on_step_complete`: ``if step_success and job.tests_run and not
        job.tests_passed: step_success = False``). Local steps have no Job
        row, so the control-layer path shipped without it and the step's exit
        code became the only verdict - a test command that swallows a failing
        suite (a wrapper script, a `|| true`, a runner that reports results
        and still exits 0) read as a PASSING step, which for an ad-hoc card
        run means the card is offered for merge red.

        The equivalent datum on the control layer is the TestRun rows the
        step posted to ``/api/steps/{id}/test-results``. This only ever
        DEMOTES: an already-failed step stays failed, and a step that
        ingested nothing is untouched (no results is not a pass, but it is
        also not evidence of failure - that judgement belongs to whoever
        required the tests, see ``run_test_summary``).

        Never fatal: a broken count must not turn a finished step into a
        crash, so an inspection failure leaves the executor's verdict alone
        and says so loudly.
        """
        if not success:
            return success, error
        try:
            result = await db.execute(
                select(func.count())
                .select_from(TestRun)
                .where(TestRun.step_run_id == step_run.id)
                .where(TestRun.status == TestRunStatus.FAILED.value)
            )
            failed = int(result.scalar() or 0)
        except Exception:
            logger.exception(
                "Test-result gate could not read results for step %s; keeping "
                "the executor's verdict",
                step_run.id[:8],
            )
            return success, error

        if not failed:
            return success, error

        message = (
            f"{failed} test(s) reported FAILED by this step - the step is red "
            "regardless of its exit code"
        )
        logger.warning(
            "Step %s (%s) exited successfully but reported %d failing "
            "test(s); demoting to FAILED",
            step_run.step_index,
            step_run.step_name,
            failed,
        )
        warning_lines.append(f"[lazyaf] {message}\n")
        return False, message if not error else f"{message}; {error}"

    async def _finish_local_step_locked(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        step_run: StepRun,
        graph: dict | None,
        steps: list[dict],
        step: dict,
        is_graph: bool,
        success: bool,
        exit_code: int | None,
        error: str | None,
        log_tail: list[str] | None = None,
    ) -> None:
        await db.refresh(pipeline_run)
        if pipeline_run.status not in (RunStatus.RUNNING.value, RunStatus.PENDING.value):
            logger.info(
                f"Pipeline run {pipeline_run.id[:8]} is {pipeline_run.status}, "
                f"ignoring local step completion"
            )
            return

        # Control-mode reconciliation (fix 2): the executor exit code stays
        # ground truth, but the StepExecution row's telemetry can amend the
        # verdict (never-reported => fail loudly; exit 124 => timeout error)
        # and is itself driven terminal here.
        execution = await self._load_control_execution(db, pipeline_run, step_run)
        warning_lines: list[str] = []
        if execution is not None:
            success, error = self._reconcile_control_execution(
                execution, step, success, exit_code, error, warning_lines
            )

        # The test gate the legacy path had and the local path lost: exit code
        # 0 does not outrank a RED ingested suite.
        success, error = await self._apply_test_result_gate(
            db, step_run, success, error, warning_lines
        )

        step_run.status = RunStatus.PASSED.value if success else RunStatus.FAILED.value
        step_run.completed_at = datetime.utcnow()
        step_run.error = error

        # Assemble everything this finish appends to StepRun.logs, then
        # append it with ONE targeted SQL expression
        # (logs = COALESCE(logs,'') || :suffix). NEVER a read-modify-write
        # of the session-cached blob: in control mode the /api/steps router
        # wrote StepRun.logs from other sessions, and writing back a stale
        # cached value would clobber every line it landed (fix 1).
        suffix_parts: list[str] = []
        if execution is not None and log_tail:
            # Forensics (fix 5): persist the executor's bounded stdout tail
            # when the step failed OR the router landed zero log bytes.
            result = await db.execute(
                select(StepRun.logs).where(StepRun.id == step_run.id)
            )
            current_logs = result.scalar_one_or_none() or ""
            if not success or not current_logs:
                suffix_parts.append(
                    "".join(f"[container] {line}\n" for line in log_tail)
                )
        suffix_parts.extend(warning_lines)
        if exit_code is not None:
            suffix_parts.append(f"[lazyaf] exit code: {exit_code}\n")
        if suffix_parts:
            await db.execute(
                update(StepRun)
                .where(StepRun.id == step_run.id)
                .values(
                    logs=func.coalesce(StepRun.logs, "") + "".join(suffix_parts)
                )
                .execution_options(synchronize_session=False)
            )

        if success:
            pipeline_run.steps_completed += 1
            machine = self._state_machines.get(pipeline_run.id)
            if machine is not None:
                machine.mark_step_completed(step_run.step_index)

        await db.commit()
        await db.refresh(step_run)
        await db.refresh(pipeline_run)

        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.publish_step_update(
            pipeline_run.id, step_run.step_index, step_run.status
        )
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        logger.info(
            f"Local step {step_run.step_index} ({step_run.step_name}) completed: "
            f"{'success' if success else 'failed'} (exit_code={exit_code})"
        )

        if is_graph:
            await self._handle_graph_step_complete(
                db, pipeline_run, pipeline, repo, graph, step_run.step_id, success, None
            )
        else:
            if step_run.step_index >= len(steps):
                logger.error(f"Step index {step_run.step_index} out of range")
                return
            action = step.get(
                "on_success" if success else "on_failure",
                "next" if success else "stop",
            )
            await self._handle_action(
                db, pipeline_run, repo, steps, step_run.step_index, action, success
            )

    # -------------------------------------------------------------------------
    # Step completion (legacy job callback)
    # -------------------------------------------------------------------------

    async def on_step_complete(
        self,
        db: AsyncSession,
        step_run_id: str,
        job: Job,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle step completion.

        Called from job_callback when a job with step_run_id completes.

        For graph-based pipelines:
        - Updates completed_step_ids and active_step_ids
        - Finds all downstream edges based on success/failure
        - Triggers ready downstream steps (fan-out)
        - Handles fan-in by checking all upstream dependencies

        For legacy pipelines:
        - Uses sequential step execution with on_success/on_failure

        Args:
            runner_id: The runner that executed this step (for continuation affinity)
        """
        # Get the step run
        result = await db.execute(
            select(StepRun).where(StepRun.id == step_run_id)
        )
        step_run = result.scalar_one_or_none()
        if not step_run:
            logger.error(f"StepRun {step_run_id} not found")
            return

        # Get the pipeline run with steps
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == step_run.pipeline_run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        pipeline_run = result.scalar_one_or_none()
        if not pipeline_run:
            logger.error(f"PipelineRun {step_run.pipeline_run_id} not found")
            return

        # Get the pipeline and repo
        result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_run.pipeline_id))
        pipeline = result.scalar_one_or_none()
        if not pipeline:
            logger.error(f"Pipeline {pipeline_run.pipeline_id} not found")
            return

        result = await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
        repo = result.scalar_one_or_none()
        if not repo:
            logger.error(f"Repo {pipeline.repo_id} not found")
            return

        # Serialize with concurrently-finishing local steps of the same run
        # (read-modify-write of the run's tracking columns).
        async with self._run_lock(pipeline_run.id):
            await self._on_step_complete_locked(
                db, pipeline_run, pipeline, repo, step_run, job, runner_id
            )

    async def _on_step_complete_locked(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        step_run: StepRun,
        job: Job,
        runner_id: str | None,
    ) -> None:
        await db.refresh(pipeline_run)

        # Check if pipeline was already cancelled or completed
        if pipeline_run.status not in (RunStatus.RUNNING.value, RunStatus.PENDING.value):
            logger.info(f"Pipeline run {pipeline_run.id[:8]} is {pipeline_run.status}, ignoring step completion")
            return

        # Determine if step succeeded
        step_success = job.status == "completed"

        # Check if tests failed (Phase 8 integration)
        if step_success and job.tests_run and not job.tests_passed:
            step_success = False

        # Update step run status
        step_run.status = RunStatus.PASSED.value if step_success else RunStatus.FAILED.value
        step_run.completed_at = datetime.utcnow()
        step_run.logs = job.logs or ""
        step_run.error = job.error

        if step_success:
            pipeline_run.steps_completed += 1
            machine = self._state_machines.get(pipeline_run.id)
            if machine is not None:
                machine.mark_step_completed(step_run.step_index)

        await db.commit()
        await db.refresh(step_run)
        await db.refresh(pipeline_run)

        # Broadcast step completion
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        logger.info(f"Step {step_run.step_index} ({step_run.step_name}) completed: {'success' if step_success else 'failed'}")
        logger.info(f"[GRAPH] on_step_complete - step_run.step_id={step_run.step_id}, pipeline.steps_graph exists={pipeline.steps_graph is not None}")

        # Check if this is a graph-based pipeline
        graph = parse_steps_graph(pipeline.steps_graph)
        logger.info(f"[GRAPH] Parsed graph: {graph is not None}")

        if graph and step_run.step_id:
            logger.info(f"[GRAPH] Using graph-based execution for step '{step_run.step_id}'")
            # Graph-based execution with parallel support
            await self._handle_graph_step_complete(
                db, pipeline_run, pipeline, repo, graph, step_run.step_id, step_success, runner_id
            )
        else:
            logger.info(f"[GRAPH] Using LEGACY execution (graph={graph is not None}, step_id={step_run.step_id})")
            # Legacy sequential execution
            steps = parse_steps(pipeline.steps)
            if step_run.step_index >= len(steps):
                logger.error(f"Step index {step_run.step_index} out of range")
                return

            step = steps[step_run.step_index]
            action = step.get("on_success" if step_success else "on_failure", "stop" if not step_success else "next")
            await self._handle_action(db, pipeline_run, repo, steps, step_run.step_index, action, step_success, runner_id=runner_id)

    async def _handle_graph_step_complete(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        graph: dict,
        completed_step_id: str,
        step_success: bool,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle completion of a graph step with parallel execution support.

        This method:
        1. Updates completed_step_ids and active_step_ids
        2. Finds downstream edges based on success/failure condition
        3. For each downstream step, checks if all upstream dependencies are satisfied (fan-in)
        4. Executes ready downstream steps (fan-out)
        5. Completes pipeline when all steps are done
        """
        logger.info(f"[GRAPH] _handle_graph_step_complete called for step '{completed_step_id}' success={step_success}")
        steps_dict = graph.get("steps", {})
        logger.info(f"[GRAPH] Graph has {len(steps_dict)} steps: {list(steps_dict.keys())}")
        logger.info(f"[GRAPH] Graph edges: {graph.get('edges', [])}")

        # Update tracking sets
        completed_ids = set(parse_json_list(pipeline_run.completed_step_ids))
        active_ids = set(parse_json_list(pipeline_run.active_step_ids))
        logger.info(f"[GRAPH] Before update - Active: {active_ids}, Completed: {completed_ids}")

        # Mark this step as completed
        completed_ids.add(completed_step_id)
        active_ids.discard(completed_step_id)

        pipeline_run.completed_step_ids = json.dumps(list(completed_ids))
        pipeline_run.active_step_ids = json.dumps(list(active_ids))
        await db.commit()
        await db.refresh(pipeline_run)

        logger.info(f"[GRAPH] After update - Active: {list(active_ids)}, Completed: {list(completed_ids)}")

        # Find downstream edges based on the step result
        condition = "success" if step_success else "failure"
        downstream_edges = get_downstream_edges(graph, completed_step_id, condition)

        logger.info(f"[GRAPH] Found {len(downstream_edges)} downstream edges for condition '{condition}': {downstream_edges}")

        # Track which steps are ready to execute
        steps_to_execute = []

        for edge in downstream_edges:
            next_step_id = edge.get("to_step")
            logger.info(f"[GRAPH] Checking edge to '{next_step_id}'")
            if not next_step_id or next_step_id not in steps_dict:
                logger.info(f"[GRAPH] Skipping edge - next_step_id invalid or not in steps_dict")
                continue

            # Skip if already completed or currently active
            if next_step_id in completed_ids or next_step_id in active_ids:
                logger.info(f"[GRAPH] Skipping {next_step_id} - already completed or active")
                continue

            # Fan-in check: are ALL upstream dependencies satisfied?
            upstream_ids = get_upstream_step_ids(graph, next_step_id)
            logger.info(f"[GRAPH] Step {next_step_id} has upstream deps: {upstream_ids}")

            if self._all_upstream_satisfied(graph, next_step_id, completed_ids):
                steps_to_execute.append(next_step_id)
                logger.info(f"[GRAPH] Step {next_step_id} is READY (all {len(upstream_ids)} upstream deps satisfied)")
            else:
                logger.info(f"[GRAPH] Step {next_step_id} NOT ready - waiting for upstream. Upstream: {upstream_ids}, Completed: {completed_ids}")

        # Execute ready downstream steps (fan-out)
        logger.info(f"[GRAPH] Executing {len(steps_to_execute)} ready steps: {steps_to_execute}")
        for step_id in steps_to_execute:
            logger.info(f"[GRAPH] Triggering execution of step '{step_id}'")
            await self._execute_graph_step(
                db, pipeline_run, pipeline, repo, graph, step_id, None, runner_id
            )

        # Refresh to get latest state after executing new steps
        await db.refresh(pipeline_run)

        # Check if pipeline is complete
        # Complete when: no active steps AND (all steps completed OR we failed with no more to run)
        active_ids = set(parse_json_list(pipeline_run.active_step_ids))
        completed_ids = set(parse_json_list(pipeline_run.completed_step_ids))
        total_steps = count_total_steps(graph)

        logger.info(f"[GRAPH] Pipeline completion check - Active: {active_ids}, Completed: {completed_ids}, Total: {total_steps}")

        if not active_ids:
            logger.info(f"[GRAPH] No active steps remaining")
            # No steps running - check if we're done
            if len(completed_ids) >= total_steps:
                # All steps completed
                logger.info(f"[GRAPH] All {total_steps} steps completed - marking pipeline complete")
                all_passed = await self._check_all_steps_passed(db, pipeline_run)
                await self._complete_pipeline(db, pipeline_run, success=all_passed)
            elif not steps_to_execute:
                # No more steps can run (failed branch or dead end)
                # Pipeline is complete, but may have failed
                logger.info(f"[GRAPH] No more steps to execute - marking pipeline complete (dead end or failure)")
                all_passed = await self._check_all_steps_passed(db, pipeline_run)
                await self._complete_pipeline(db, pipeline_run, success=all_passed)
            else:
                logger.info(f"[GRAPH] Steps were triggered, waiting for them to complete")
        else:
            logger.info(f"[GRAPH] Still have active steps, not completing pipeline yet")

    def _all_upstream_satisfied(
        self,
        graph: dict,
        step_id: str,
        completed_ids: set[str],
    ) -> bool:
        """
        Check if all upstream dependencies for a step are satisfied.

        A step can execute when ALL its incoming edges come from completed steps
        AND the edge conditions match (success edge requires success, etc).
        """
        edges = graph.get("edges", [])

        # Find all edges pointing to this step
        incoming_edges = [e for e in edges if e.get("to_step") == step_id]

        if not incoming_edges:
            # Entry point or no dependencies - can execute
            return True

        # Check if at least one edge's source is completed (OR semantic for multiple paths)
        # For fan-in, we need ALL sources to be completed
        for edge in incoming_edges:
            from_step = edge.get("from_step")
            if from_step not in completed_ids:
                return False

        return True

    async def _check_all_steps_passed(self, db: AsyncSession, pipeline_run: PipelineRun) -> bool:
        """Check if all completed step runs passed."""
        result = await db.execute(
            select(StepRun).where(StepRun.pipeline_run_id == pipeline_run.id)
        )
        step_runs = result.scalars().all()

        for sr in step_runs:
            if sr.status == RunStatus.FAILED.value:
                return False

        return True

    async def _handle_action(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        action: str,
        step_success: bool,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle on_success/on_failure action.

        Actions:
        - "next": Execute next step
        - "stop": Complete pipeline (status based on step_success)
        - "trigger:{card_id}": Clone card as template and run it
        - "trigger:pipeline:{pipeline_id}": Start another pipeline
        - "merge:{branch}": Merge current branch to target

        Args:
            runner_id: The runner that completed the previous step (for continuation affinity)
        """
        logger.info(f"Handling action '{action}' after step {current_step} (success={step_success})")

        if action == "next":
            # Execute next step, passing runner_id for affinity
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1, previous_runner_id=runner_id)

        elif action == "stop":
            # Complete the pipeline
            await self._complete_pipeline(db, pipeline_run, success=step_success)

        elif action.startswith("trigger:pipeline:"):
            # Start another pipeline
            target_pipeline_id = action[17:]  # Remove "trigger:pipeline:" prefix
            await self._trigger_pipeline(db, pipeline_run, repo, steps, current_step, target_pipeline_id)

        elif action.startswith("trigger:"):
            # Clone card as template and run it
            card_id = action[8:]  # Remove "trigger:" prefix
            await self._trigger_card(db, pipeline_run, repo, steps, current_step, card_id)

        elif action.startswith("merge:"):
            # Merge the step's branch to target
            target_branch = action[6:]  # Remove "merge:" prefix
            await self._merge_branch(db, pipeline_run, repo, steps, current_step, target_branch)

        else:
            logger.warning(f"Unknown action '{action}', treating as 'stop'")
            await self._complete_pipeline(db, pipeline_run, success=step_success)

    async def _trigger_card(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        template_card_id: str,
    ) -> None:
        """Clone a card as template and run it, on the control layer, to fix
        issues.

        12.5: this was the last caller of the polling queue on the card
        path. Card START and card RETRY had already moved to the ad-hoc agent
        run (``agent_run.start_card_work``); the ``trigger:{card_id}`` action
        had not, so "nothing enqueues any more" was true of the paths people
        look at and false of this one - and a queue with one live caller is a
        queue nobody notices has stopped being polled. It takes exactly the
        same path card start does, and 12.6 deleted the queue itself.

        CONTINUATION. The old shape blocked the parent run: the fix job's
        runner callback re-entered ``on_step_complete`` for a StepRun parked
        at the SAME index, which then re-applied that index's action - so a
        fix that failed re-fired ``trigger:{card_id}`` and looped. An ad-hoc
        run has no such callback into this run, so the parent continues
        immediately, exactly like the sibling ``trigger:pipeline:`` action
        (fire-and-forget). That is the old SUCCESS path's destination
        (``on_success: next`` -> ``current_step + 1``) minus the wait, and it
        has no re-trigger lap.

        A marker StepRun records that the fix was dispatched and names the
        run that carries it; it is terminal on creation, because the work it
        points at is not in this run.

        KNOWN LIMITATION, inherited verbatim from ``trigger:pipeline:`` and
        deliberately not changed here: when the triggering step is the LAST
        one, continuing past it completes the run PASSED even though the
        action fired from ``on_failure``. Fixing that means threading the
        step's own verdict through ``_handle_action``, which is a wider
        change than this finding.
        """
        # Get the template card
        result = await db.execute(select(Card).where(Card.id == template_card_id))
        template_card = result.scalar_one_or_none()
        if not template_card:
            logger.error(f"Template card {template_card_id} not found for trigger action")
            # Continue to next step anyway
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        logger.info(f"Triggering card template {template_card_id} to fix step {current_step}")

        # The cards router owns Job creation + branch naming on the card-start
        # path; this is that path's other entry point, so it does the same
        # here and hands ownership of every later transition to agent_run.
        job_id = str(uuid4())
        cloned_card = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title=f"[Pipeline Fix] {template_card.title}",
            description=template_card.description,
            status="in_progress",
            runner_type=template_card.runner_type,
            step_type=template_card.step_type,
            step_config=template_card.step_config,
            job_id=job_id,
            branch_name=f"lazyaf/{job_id[:8]}",
        )
        db.add(cloned_card)
        job = Job(
            id=job_id,
            card_id=cloned_card.id,
            status="queued",
            step_type=cloned_card.step_type,
            step_config=cloned_card.step_config,
        )
        db.add(job)

        # Marker StepRun: terminal on creation (the work lives in the ad-hoc
        # run named in its logs), and deliberately carrying NO job_id - a
        # second row at this index claiming the step's job would poison
        # _resolve_merge_source_branch for a later `merge:` action.
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=current_step,  # Same step index (sub-step)
            step_name=f"[Fix] {template_card.title}",
            status=RunStatus.RUNNING.value,
            executor=ExecutorMode.LOCAL.value,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)
        await db.commit()

        step_config = None
        if cloned_card.step_config:
            try:
                step_config = json.loads(cloned_card.step_config)
            except (json.JSONDecodeError, TypeError):
                step_config = None

        from app.services import agent_run

        fix_run_id: str | None = None
        error: str | None = None
        try:
            fix_run = await agent_run.start_card_work(
                db,
                cloned_card,
                repo,
                job_id=job_id,
                step_config=step_config,
            )
            fix_run_id = fix_run.id
        except Exception as e:
            error = f"could not start the fix card's agent run: {e}"
            logger.exception(
                "Fix card %s for run %s could not be started",
                cloned_card.id[:8],
                pipeline_run.id[:8],
            )
            cloned_card.status = "failed"
            job.status = "failed"
            job.error = error
            job.completed_at = datetime.utcnow()

        step_run.status = (
            RunStatus.FAILED.value if error else RunStatus.PASSED.value
        )
        step_run.completed_at = datetime.utcnow()
        step_run.error = error
        step_run.logs = (
            f"[lazyaf] fix card {cloned_card.id[:8]} dispatched as ad-hoc "
            f"card-work run {fix_run_id[:8]}\n"
            if fix_run_id
            else f"[lazyaf] {error}\n"
        )
        await db.commit()
        await db.refresh(step_run)
        await db.refresh(job)

        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_job_status({
            "id": job.id,
            "card_id": cloned_card.id,
            "status": job.status,
            "error": job.error,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": (
                job.completed_at.isoformat() if job.completed_at else None
            ),
        })

        await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)

    async def _trigger_pipeline(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        target_pipeline_id: str,
    ) -> None:
        """
        Trigger another pipeline and wait for it to complete, then continue.

        The triggered pipeline runs independently, and we continue to the next step
        regardless of its outcome (it's fire-and-forget for now).
        """
        # Get the target pipeline
        result = await db.execute(select(Pipeline).where(Pipeline.id == target_pipeline_id))
        target_pipeline = result.scalar_one_or_none()
        if not target_pipeline:
            logger.error(f"Target pipeline {target_pipeline_id} not found for trigger action")
            # Continue to next step anyway
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        # Get the target repo (may be different from current)
        result = await db.execute(select(Repo).where(Repo.id == target_pipeline.repo_id))
        target_repo = result.scalar_one_or_none()
        if not target_repo:
            logger.error(f"Repo {target_pipeline.repo_id} not found for triggered pipeline")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        if not target_repo.is_ingested:
            logger.error(f"Repo {target_repo.id} is not ingested, cannot run pipeline")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        logger.info(f"Triggering pipeline {target_pipeline.name} (id: {target_pipeline_id})")

        # Start the target pipeline (fire-and-forget for now)
        # The triggered pipeline runs independently
        await self.start_pipeline(
            db=db,
            pipeline=target_pipeline,
            repo=target_repo,
            trigger_type="pipeline",
            trigger_ref=pipeline_run.id,  # Reference to the triggering pipeline run
        )

        # Continue to next step immediately (don't wait for triggered pipeline)
        await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)

    async def _resolve_merge_source_branch(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        current_step: int,
    ) -> str | None:
        """Resolve which branch a merge action should merge FROM (fix 1).

        Legacy steps carry a job whose card names the working branch. Local
        steps have NO job - the branch comes from the run's own trigger
        context (PipelineRun.trigger_context records the triggering branch).
        Returns None when neither source resolves - the caller must FAIL the
        run, never warn-and-continue-green.
        """
        # Legacy path: the step's job -> card -> branch_name.
        result = await db.execute(
            select(StepRun)
            .where(StepRun.pipeline_run_id == pipeline_run.id)
            .where(StepRun.step_index == current_step)
        )
        step_run = result.scalars().first()
        if step_run is not None and step_run.job_id:
            result = await db.execute(select(Job).where(Job.id == step_run.job_id))
            job = result.scalar_one_or_none()
            if job is not None:
                result = await db.execute(select(Card).where(Card.id == job.card_id))
                card = result.scalar_one_or_none()
                if card is not None and card.branch_name:
                    return card.branch_name

        # Local path: the run's own trigger context.
        if pipeline_run.trigger_context:
            try:
                context = json.loads(pipeline_run.trigger_context) or {}
            except (json.JSONDecodeError, TypeError):
                context = {}
            branch = context.get("branch")
            if branch:
                return branch

        return None

    async def _merge_branch(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        target_branch: str,
    ) -> None:
        """
        Merge the step's working branch to the target branch, then continue.

        Branch resolution (fix 1): job/card branch for legacy steps, the
        run's trigger-context branch for local steps. An unresolvable branch
        FAILS the run loudly - a merge that silently does nothing is
        indistinguishable from a merge that worked.
        """
        source_branch = await self._resolve_merge_source_branch(
            db, pipeline_run, current_step
        )
        if not source_branch:
            logger.error(
                f"Merge action after step {current_step} of run "
                f"{pipeline_run.id[:8]} cannot resolve a source branch "
                f"(no job/card branch and no trigger-context branch) - "
                f"failing the run"
            )
            result = await db.execute(
                select(StepRun)
                .where(StepRun.pipeline_run_id == pipeline_run.id)
                .where(StepRun.step_index == current_step)
            )
            step_run = result.scalars().first()
            if step_run is not None:
                step_run.error = (
                    (step_run.error + "\n") if step_run.error else ""
                ) + (
                    f"merge:{target_branch} failed: could not resolve the "
                    f"source branch for this run"
                )
                await db.commit()
            await self._complete_pipeline(db, pipeline_run, success=False)
            return

        if source_branch == target_branch:
            # Nothing to merge - the run already worked on the target branch.
            logger.info(
                f"Merge action: source and target are both '{target_branch}' "
                f"- nothing to merge, continuing"
            )
            if current_step + 1 < len(steps):
                await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            else:
                await self._complete_pipeline(db, pipeline_run, success=True)
            return

        logger.info(f"Merging branch {source_branch} to {target_branch}")

        # Perform the merge
        merge_result = git_repo_manager.merge_branch(
            repo_id=repo.id,
            source_branch=source_branch,
            target_branch=target_branch,
        )

        if merge_result["success"]:
            logger.info(f"Merge successful: {merge_result}")

            # Clean up .lazyaf-context directory from merged branch (Phase 9.1d)
            cleanup_result = git_repo_manager.delete_directory_from_branch(
                repo_id=repo.id,
                branch=target_branch,
                directory=".lazyaf-context",
            )
            if cleanup_result["success"]:
                logger.info(f"Context cleanup: {cleanup_result.get('message', 'done')}")
            else:
                logger.warning(f"Context cleanup failed: {cleanup_result.get('error', 'unknown')}")

            # Continue to next step or complete
            if current_step + 1 < len(steps):
                await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            else:
                await self._complete_pipeline(db, pipeline_run, success=True)
        else:
            logger.error(f"Merge failed: {merge_result}")
            await self._complete_pipeline(db, pipeline_run, success=False)

    async def cancel_run(self, db: AsyncSession, pipeline_run: PipelineRun) -> PipelineRun:
        """
        Cancel a running pipeline.

        Marks the run as cancelled, cancels any running jobs, kills in-flight
        local containers, cancels the run's asyncio tasks, and cleans up the
        workspace.
        """
        logger.info(f"Cancelling pipeline run {pipeline_run.id[:8]}")

        # Kill in-flight local containers (best effort, loud on failure).
        # The step tasks themselves are NOT hard-cancelled: killing the
        # container ends their event stream, and _finish_local_step's status
        # guard sees the CANCELLED run and stops without continuing. A hard
        # task.cancel() mid-DB-await can tear down the shared aiosqlite
        # connection under the caller's feet.
        # The execution key is DERIVED (fix 11: no shadow registry to drift):
        # it is deterministic from the run/step rows, exactly as
        # _build_local_execution_config mints it.
        if self._local_executor is not None:
            for step_run in pipeline_run.step_runs:
                if step_run.status != RunStatus.RUNNING.value:
                    continue
                execution_key = (
                    f"{pipeline_run.id}:{step_run.step_index}:{step_run.id}"
                )
                try:
                    await self._local_executor.cancel_step(execution_key)
                except Exception as e:
                    logger.warning(
                        f"Failed to cancel local container for step "
                        f"{step_run.step_index}: {e}"
                    )

        # Drive the state machine to CANCELLED
        machine = self._state_machines.pop(pipeline_run.id, None)
        if machine is not None and not machine.is_terminal():
            try:
                machine.transition_to(PipelineStatus.CANCELLED)
            except ValueError as e:
                logger.error(
                    f"Pipeline state machine error cancelling run "
                    f"{pipeline_run.id[:8]}: {e}"
                )

        pipeline_run.status = RunStatus.CANCELLED.value
        pipeline_run.completed_at = datetime.utcnow()

        # Cancel any running step runs
        for step_run in pipeline_run.step_runs:
            if step_run.status == RunStatus.RUNNING.value:
                step_run.status = RunStatus.CANCELLED.value
                step_run.completed_at = datetime.utcnow()
                step_run.error = "Cancelled by user"

                # Cancel the job if it exists
                if step_run.job_id:
                    result = await db.execute(select(Job).where(Job.id == step_run.job_id))
                    job = result.scalar_one_or_none()
                    if job and job.status in ("queued", "running"):
                        job.status = "failed"
                        job.error = "Pipeline cancelled"

        await db.commit()
        await db.refresh(pipeline_run)

        # 12.7: same as _complete_pipeline - end the debug session (and tear
        # its sidecar down) before the volume is removed. Idempotent: an
        # abort has already ended the session, so this finds nothing.
        await self._end_debug_session(db, pipeline_run.id, "run cancelled")

        # Workspace cleanup (cancellation is a completion path too)
        await self._cleanup_workspace(db, pipeline_run.id)
        self._session_factories.pop(pipeline_run.id, None)
        # Deferred eviction (fix 4): straggler step tasks still serialize on
        # the same lock object until they drain.
        self._schedule_run_lock_eviction(pipeline_run.id)

        # Broadcast updates
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        for step_run in pipeline_run.step_runs:
            await manager.send_step_run_status(step_run_to_ws_dict(step_run))

        return pipeline_run


# Global pipeline executor instance
pipeline_executor = PipelineExecutor()
