"""
Pipeline execution service.

Orchestrates multi-step pipeline workflows by:
1. Creating pipeline runs and step runs
2. Routing each step through the ExecutionRouter (Phase 12.2-INT):
   - mode=legacy: enqueue a job via a temporary card (existing runner path)
   - mode=local:  execute in a Docker container via LocalExecutor, in an
     asyncio task with its OWN session scope, streaming status/log events
     incrementally into the StepRun row and over the typed WS publish API
3. Handling step completion callbacks (legacy) / local task continuations
4. Graph-based parallel execution with fan-out/fan-in
5. Broadcasting status via WebSocket

Async model (R5 / failure_01 landmine 4): request and git-push handlers never
await container execution. start_pipeline creates the run row and dispatches
the entry steps; dispatching a legacy step is a fast enqueue and dispatching a
local step spawns an asyncio task (registered in a task registry with a
done-callback that logs exceptions, so nothing leaks or dies silently). All
container execution, log streaming, and continuation logic for local steps
run inside those tasks using a session factory derived from the caller's
engine - never the request's session.

Observability (R1): every StepRun records which executor ran it in
StepRun.executor ("local" | "legacy"), set at dispatch time. Routing failures
fail the step and the run loudly; there is no silent fallback to the legacy
path. Run lifecycle is driven through main's PipelineStateMachine.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import Pipeline, PipelineRun, StepRun, RunStatus, Job, Card, Repo
from app.models.pipeline import ExecutorMode, StepExecution, StepExecutionStatus
from app.services.job_queue import job_queue, QueuedJob
from app.services.websocket import manager
from app.services.git_server import git_repo_manager
from app.services.workspace.pipeline_state_machine import (
    PipelineStateMachine,
    PipelineStatus,
)
from app.services.workspace.state_machine import generate_volume_name

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
        # Serializes LocalExecutor construction so exactly one docker client
        # ever exists (fix 5).
        self._local_executor_init_lock = asyncio.Lock()
        self._continue_in_context_logged = False

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
    ) -> tuple[ExecutorMode, str]:
        """Route a step via the ExecutionRouter contract.

        Returns (ExecutorMode, reason). Raises on any router failure, on an
        unknown mode, and on modes without an execution path yet (remote,
        until 12.6) - a routing error must fail the step loudly, never
        quietly fall back to legacy. Every compare/write site uses the
        ExecutorMode enum (cross-file contract #3).
        """
        router = self._get_router()
        decision = router.decide(step_type, step_config)
        reason = decision.reason
        try:
            mode = ExecutorMode(decision.mode)
        except ValueError:
            raise RuntimeError(
                f"ExecutionRouter returned unknown mode {decision.mode!r} "
                f"(reason={reason!r}) for step '{step_name}'"
            ) from None
        if mode not in (ExecutorMode.LOCAL, ExecutorMode.LEGACY):
            raise RuntimeError(
                f"ExecutionRouter returned mode {mode.value!r} "
                f"(reason={reason!r}) for step '{step_name}', which has no "
                "execution path until Phase 12.6"
            )
        log = logger.warning if reason == "explicit-override" else logger.info
        log(f"[ROUTE] step '{step_name}' (type={step_type}) -> {mode.value} ({reason})")
        return mode, reason

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

        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        logger.info(f"Pipeline run {pipeline_run.id[:8]} completed with status {pipeline_run.status}")

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

        Scope: only images named in step configs. Steps without an explicit
        image use settings.step_default_image, which app startup pre-pulls;
        resolving it here would force a docker client into runs that route
        entirely legacy (and into the no-Docker test tier). Preflight
        infrastructure failures (docker down, guard-blocked client) are
        logged and non-fatal - per-step dispatch surfaces them loudly.
        """
        images = sorted({
            (step.get("config") or {}).get("image")
            for step in step_defs
            if (step.get("config") or {}).get("image")
        })
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
        return None

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
        broadcast, and dispatch the LOCAL path (asyncio task with its own
        session scope). Legacy enqueueing stays with the caller (payloads
        differ between graph and linear) via _enqueue_legacy_step.

        Returns (step_run, mode, route_error). On a routing failure the
        StepRun is already FAILED and broadcast; the caller drives the run
        continuation.
        """
        route_error: str | None = None
        mode: ExecutorMode | None = None
        try:
            mode, _reason = self._decide_route(step_type, step_config, step_name)
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

        if mode is ExecutorMode.LOCAL:
            factory = self._session_factory_for(pipeline_run.id, db)
            self._spawn_task(
                f"step:{pipeline_run.id}:{step_run.id}",
                self._run_local_step(factory, pipeline_run.id, step_run.id, params),
            )
        return step_run, mode, None

    async def _enqueue_legacy_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        step_run: StepRun,
        *,
        step_type: str,
        step_config: dict,
        step_index: int,
        step_name: str,
        card_title: str,
        card_description: str,
        step_id: str | None = None,
        agent_file_ids: list | None = None,
        prompt_template: str | None = None,
        continue_in_context: bool = False,
        is_continuation: bool = False,
        previous_step_logs: str | None = None,
        required_runner_id: str | None = None,
    ) -> None:
        """Legacy path (unchanged semantics): temporary Card + Job + queue.

        Guard (12.4 fallout): script/docker steps must NEVER reach the runner
        queue. Phase 12.4 deleted their execution from every runner entrypoint
        (`execute_job` rejects them now), so enqueueing one produces a job that
        is picked up and immediately failed - the silent in_progress -> failed
        loop. The router already refuses to route them legacy; this is the
        belt-and-braces stop at the enqueue site, and it raises so the caller's
        dispatch error path fails the step with a real message.
        """
        from app.services.workspace.execution_router import LOCAL_STEP_TYPES

        if step_type in LOCAL_STEP_TYPES:
            raise RuntimeError(
                f"Refusing to enqueue a {step_type!r} step ('{step_name}') to the "
                "legacy runner queue: runners reject script/docker jobs since "
                "Phase 12.4. This step belongs on the local executor - a legacy "
                "route for it is a routing bug, not a fallback."
            )

        # For agent steps, use title/description from config
        if step_type == "agent":
            card_title = step_config.get("title", card_title)
            card_description = step_config.get("description", card_description)

        card = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title=card_title,
            description=card_description,
            status="in_progress",
            runner_type=step_config.get("runner_type", "any"),
            step_type=step_type,
            step_config=json.dumps(step_config) if step_config else None,
            # Agent-specific fields (Phase 9.1c)
            agent_file_ids=json.dumps(agent_file_ids) if agent_file_ids else None,
            prompt_template=prompt_template,
            pipeline_run_id=pipeline_run.id,
            pipeline_step_index=step_index,
        )
        db.add(card)

        # Create the job
        job_id = str(uuid4())
        job = Job(
            id=job_id,
            card_id=card.id,
            status="queued",
            step_type=step_type,
            step_config=json.dumps(step_config) if step_config else None,
            step_run_id=step_run.id,  # Link job to step run
        )
        db.add(job)

        # Update card and step_run with job reference
        card.job_id = job_id
        card.branch_name = f"lazyaf/{job_id[:8]}"
        step_run.job_id = job_id

        await db.commit()

        # Queue the job for a runner
        queued_job = QueuedJob(
            id=job_id,
            card_id=card.id,
            repo_id=repo.id,
            repo_url=repo.remote_url or "",
            base_branch=repo.default_branch,
            card_title=card_title,
            card_description=card_description,
            runner_type=step_config.get("runner_type", "any"),
            use_internal_git=True,
            step_type=step_type,
            step_config=step_config,
            # Agent-specific fields (Phase 9.1c)
            agent_file_ids=agent_file_ids or [],
            prompt_template=prompt_template,
            # Pipeline context
            continue_in_context=continue_in_context,
            is_continuation=is_continuation,
            previous_step_logs=previous_step_logs,
            pipeline_run_id=pipeline_run.id,
            # Step metadata for context directory (Phase 9.1d)
            step_id=step_id,
            step_index=step_index,
            step_name=step_name,
            # Runner affinity for continuations
            required_runner_id=required_runner_id,
        )
        await job_queue.enqueue(queued_job)

        logger.info(
            f"Enqueued job {job_id[:8]} for step {step_index} "
            f"({step_id or step_name}): {step_name}"
        )

        # Broadcast job queued
        await manager.send_job_status({
            "id": job_id,
            "card_id": card.id,
            "status": "queued",
            "error": None,
            "started_at": None,
            "completed_at": None,
        })

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

        if mode is ExecutorMode.LOCAL:
            if step.get("continue_in_context"):
                self._log_local_continue_in_context()
            logger.info(
                f"[GRAPH] Dispatched step '{step_id}' ({step_name}) to local executor"
            )
            return

        await self._enqueue_legacy_step(
            db,
            pipeline_run,
            repo,
            step_run,
            step_type=step_type,
            step_config=step_config,
            step_index=step_index,
            step_name=step_name,
            card_title=f"[Pipeline] {step_name}",
            card_description=f"Pipeline: {pipeline.name}\nStep: {step_name}",
            step_id=step_id,
            required_runner_id=previous_runner_id,
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

        if mode is ExecutorMode.LOCAL:
            if continue_in_context or is_continuation:
                self._log_local_continue_in_context()
            logger.info(f"Dispatched step {step_index} ({step_name}) to local executor")
            return

        # If this is a continuation, require the same runner for affinity
        required_runner_id = previous_runner_id if is_continuation else None
        logger.info(f"Step {step_index}: is_continuation={is_continuation}, previous_runner_id={previous_runner_id[:8] if previous_runner_id else None}, required_runner_id={required_runner_id[:8] if required_runner_id else None}")

        await self._enqueue_legacy_step(
            db,
            pipeline_run,
            repo,
            step_run,
            step_type=step_type,
            step_config=step_config,
            step_index=step_index,
            step_name=step_name,
            card_title=f"[Pipeline] {step_name}",
            card_description=(
                f"Pipeline: {pipeline_run.pipeline_id}\n"
                f"Step {step_index + 1} of {pipeline_run.steps_total}"
            ),
            step_id=step_id,
            agent_file_ids=agent_file_ids,
            prompt_template=prompt_template,
            continue_in_context=continue_in_context,
            is_continuation=is_continuation,
            previous_step_logs=previous_step_logs,
            required_runner_id=required_runner_id,
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
    # Local execution path (12.2-INT)
    # -------------------------------------------------------------------------

    async def _run_local_step(
        self,
        session_factory,
        run_id: str,
        step_run_id: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Execute one locally-routed step inside its own session scope.

        Acquires the run's workspace (creating it on first use), streams the
        LocalExecutor event stream into the StepRun row and over the typed WS
        publish API, releases the workspace, then drives the run continuation
        (next steps / completion) exactly like a legacy job callback would.

        Wedge-proofing (fix 2): every context-load failure routes through
        _fail_wedged_local_step - no path leaves a RUNNING StepRun unowned.

        Deadline discipline (fix 3): the event-stream consumer is never
        hard-cancelled mid-commit. On the outer deadline the container is
        killed first; if the consumer still does not end within a bounded
        grace, it is abandoned (logged done-callback, session handed to a
        reaper task) and the step is failed from a FRESH session.
        """
        db = session_factory()
        session_abandoned = False
        try:
            try:
                loaded = await self._load_local_step_context(db, run_id, step_run_id)
            except LocalStepContextError as err:
                await self._fail_wedged_local_step(db, run_id, err)
                return
            pipeline_run, pipeline, repo, step_run, graph, steps, step, is_graph = loaded

            step_type = step.get("type", "script")
            step_config = step.get("config", {}) or {}
            timeout = step.get("timeout", 300)
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

                workspace = await workspace_service.get_or_create(
                    db, run_id, repo.id, branch, commit_sha
                )
                await workspace_service.acquire(db, workspace.id)
                acquired = True
                workspace_id = workspace.id

                executor = await self._get_local_executor()
                exec_config, exec_context = self._build_local_execution_config(
                    pipeline_run, step_run, step_type, step_config, timeout, params,
                )
                await self._prepare_control_mode(
                    db, executor, step_run, step_config, exec_config,
                    exec_context, timeout,
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
        """
        environment = dict(step_config.get("environment") or {})
        if params:
            environment.update({str(k): str(v) for k, v in params.items()})

        exec_step_config: dict[str, Any] = {
            "type": step_type,
            "command": step_config.get("command", ""),
            "timeout": timeout,
        }
        if environment:
            exec_step_config["environment"] = environment
        if step_config.get("image"):
            exec_step_config["image"] = step_config["image"]
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

    async def _prepare_control_mode(
        self,
        db: AsyncSession,
        executor,
        step_run: StepRun,
        step_config: dict,
        exec_config: dict,
        exec_context: dict,
        timeout: int,
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
        """
        exec_context["control_mode"] = False

        if step_config.get("control") is False:
            logger.info(
                f"Step {step_run.step_index} ({step_run.step_name}): control "
                f"mode disabled by step config (control: false) - stdout mode"
            )
            return
        if not isinstance(exec_config.get("command", ""), str):
            return  # exec-form list command: explicit stdout-mode opt-out

        settings = get_settings()
        image = exec_config.get("image") or settings.step_default_image
        if not await executor.image_supports_control_layer(image):
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
        """
        Clone a card as template and run it to fix issues.

        The triggered card runs as an additional step, then continues to next step.
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

        # Create step run for the triggered card (always the legacy runner path)
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=current_step,  # Same step index (sub-step)
            step_name=f"[Fix] {template_card.title}",
            status=RunStatus.RUNNING.value,
            executor=ExecutorMode.LEGACY.value,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)

        # Clone the template card
        cloned_card = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title=f"[Pipeline Fix] {template_card.title}",
            description=template_card.description,
            status="in_progress",
            runner_type=template_card.runner_type,
            step_type=template_card.step_type,
            step_config=template_card.step_config,
        )
        db.add(cloned_card)

        # Create job for the cloned card
        job_id = str(uuid4())
        job = Job(
            id=job_id,
            card_id=cloned_card.id,
            status="queued",
            step_type=cloned_card.step_type,
            step_config=cloned_card.step_config,
            step_run_id=step_run.id,
        )
        db.add(job)

        # Update references
        cloned_card.job_id = job_id
        cloned_card.branch_name = f"lazyaf/{job_id[:8]}"
        step_run.job_id = job_id

        await db.commit()

        # Parse step_config for the queued job
        step_config = None
        if cloned_card.step_config:
            try:
                step_config = json.loads(cloned_card.step_config)
            except (json.JSONDecodeError, TypeError):
                pass

        # Queue the job
        queued_job = QueuedJob(
            id=job_id,
            card_id=cloned_card.id,
            repo_id=repo.id,
            repo_url=repo.remote_url or "",
            base_branch=repo.default_branch,
            card_title=cloned_card.title,
            card_description=cloned_card.description,
            runner_type=cloned_card.runner_type,
            use_internal_git=True,
            step_type=cloned_card.step_type,
            step_config=step_config,
        )
        await job_queue.enqueue(queued_job)

        logger.info(f"Enqueued triggered job {job_id[:8]} for fix card")

        # Broadcast updates
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_job_status({
            "id": job_id,
            "card_id": cloned_card.id,
            "status": "queued",
            "error": None,
            "started_at": None,
            "completed_at": None,
        })

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
