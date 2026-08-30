"""Job recovery for remote runners - Phase 12.6.

What happens to a step when the machine executing it stops answering.

Four events, one rule: **the database is the source of truth and a step
executes on at most one runner**. Every method here is idempotent, because
death detection, socket close and reconnect can all fire for the same runner
within the same second and in any order.

    on_runner_death       heartbeat timeout      -> runner dead, step requeued
    on_runner_disconnect  socket closed          -> runner disconnected, step requeued
    on_runner_reconnect   register with a resume -> idle | continue | abort
    recover_orphaned_steps backend startup       -> every stranded step -> pending

Row loading is `db.execute(select(...))` + `.scalar_one_or_none()` (single
row) or `.scalars().all()` (the sweep), NEVER `db.get()`. That is not a
style choice: the contract suite
(``tdd/unit/execution/test_job_recovery.py``) drives this service with
`AsyncMock` sessions whose `execute(...)` returns an object exposing exactly
those two shapes. `db.get()` on such a mock returns a coroutine-shaped
MagicMock and every assertion downstream silently passes over garbage.

failure_01's version of this file was not wrong so much as *unreachable*:
`Runner.current_step_execution_id` was declared and never written, so a dead
runner's in-flight step could not be found and nothing was ever requeued.
12.6 writes that column on every assignment (the dispatcher's CAS), which is
what makes the code below do anything at all.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select

from app.models.pipeline import StepExecution, StepExecutionStatus
from app.models.runner import Runner
from app.services.execution.runner_protocol import (
    RESUME_ABORT,
    RESUME_CONTINUE,
    RESUME_IDLE,
)
from app.services.execution.runner_state import RunnerState

logger = logging.getLogger(__name__)


#: Step-execution states that a runner failure strands. A step in any of
#: these was, as far as the backend knows, in the hands of the runner that
#: just went away, so it goes back to PENDING for the dispatcher.
#:
#: PENDING is deliberately absent: an already-requeued step needs no second
#: requeue, and re-writing it would race the dispatcher that may already have
#: claimed it.
REQUEUEABLE_STATUSES = frozenset({
    StepExecutionStatus.ASSIGNED.value,
    StepExecutionStatus.PREPARING.value,
    StepExecutionStatus.RUNNING.value,
    StepExecutionStatus.COMPLETING.value,
})

#: Runner states that mean "this runner is not coming back for this step".
#: The orphan sweep pairs them with "no runner row at all".
LOST_RUNNER_STATUSES = (
    RunnerState.DISCONNECTED.value,
    RunnerState.DEAD.value,
)

#: Signature of the requeue hook: (step_id, runner_id, reason) -> None.
RequeueHook = Callable[[str, "str | None", str], None]


class JobRecoveryService:
    """Requeue policy for steps whose runner failed.

    Holds no state beyond an optional requeue hook, so constructing one per
    test (as the contract suite does) is free and the process-wide singleton
    is a convenience rather than a requirement.
    """

    def __init__(self) -> None:
        #: Installed by the dispatcher at startup. A callback rather than an
        #: import so this module never depends on the dispatcher - the
        #: contract suite drives the service with no dispatcher in the
        #: process at all, and a requeue that nobody is listening for must
        #: still be a correct requeue.
        self._requeue_hook: RequeueHook | None = None

    # -- wiring ---------------------------------------------------------------

    def set_requeue_hook(self, hook: RequeueHook | None) -> None:
        """Register the dispatcher's "a step came back to pending" callback."""
        self._requeue_hook = hook

    def _notify_requeue(self, step_id: str, runner_id: str | None, reason: str) -> None:
        if self._requeue_hook is None:
            return
        try:
            self._requeue_hook(step_id, runner_id, reason)
        except Exception:  # pragma: no cover - a listener must never break recovery
            logger.exception(
                "requeue hook raised for step %s (reason=%s); the requeue itself "
                "already committed",
                step_id,
                reason,
            )

    # -- single-runner events -------------------------------------------------

    async def on_runner_death(self, db, runner) -> None:
        """Heartbeat timeout: mark the runner dead and requeue its step.

        The early return when the runner holds no step is load-bearing: the
        overwhelmingly common case is an idle runner whose agent was stopped,
        and issuing a query per dead idle runner turns a fleet-wide network
        blip into a query storm.
        """
        runner.status = RunnerState.DEAD.value
        step_id = getattr(runner, "current_step_execution_id", None)
        if not step_id:
            await db.commit()
            logger.info("runner %s died with no step in flight", runner.id)
            return

        await self._requeue_step(
            db, runner, step_id, reason=f"runner {runner.id} died (heartbeat timeout)"
        )

    async def on_runner_disconnect(self, db, runner) -> None:
        """Socket closed: mark the runner disconnected and requeue its step.

        Identical requeue to death, different runner status. A disconnect is
        not evidence of a crash - the agent may be restarting - but the step
        cannot wait for it, and `on_runner_reconnect` is what makes the
        returning runner discover that its work moved on.
        """
        runner.status = RunnerState.DISCONNECTED.value
        step_id = getattr(runner, "current_step_execution_id", None)
        if not step_id:
            # RETURN BEFORE TOUCHING db.execute. An idle runner disconnecting
            # is the single most frequent event this service sees.
            await db.commit()
            return

        await self._requeue_step(
            db, runner, step_id, reason=f"runner {runner.id} disconnected mid-step"
        )

    async def _requeue_step(self, db, runner, step_id: str, *, reason: str) -> None:
        """Shared body of death and disconnect: one load, one decision.

        failure_01 had `_requeue_step` on the WS endpoint AND a
        JobRecoveryService that duplicated it, which is how two callers ended
        up with two different notions of "requeueable".
        """
        result = await db.execute(
            select(StepExecution).where(StepExecution.id == step_id)
        )
        execution = result.scalar_one_or_none()

        if execution is None:
            logger.warning(
                "runner %s referenced step execution %s, which no longer exists; "
                "clearing the reference",
                runner.id,
                step_id,
            )
            runner.current_step_execution_id = None
            await db.commit()
            return

        requeued = execution.status in REQUEUEABLE_STATUSES
        if requeued:
            execution.status = StepExecutionStatus.PENDING.value
            execution.runner_id = None
            logger.warning("requeued step execution %s: %s", step_id, reason)
        else:
            # A terminal step is left completely untouched: the runner
            # finished the work and then died, which costs nothing.
            logger.info(
                "step execution %s is already %s; %s requires no requeue",
                step_id,
                execution.status,
                reason,
            )

        runner.current_step_execution_id = None
        await db.commit()

        if requeued:
            self._notify_requeue(step_id, runner.id, reason)

    async def on_runner_reconnect(self, db, runner) -> dict:
        """A returning runner asks what to do with the step it still holds.

        Returns one of:
            {"action": "idle"}
            {"action": "continue", "step_id": ...}
            {"action": "abort",    "step_id": ...}

        `runner_id is None` maps to **abort**, not continue. The step was
        requeued while this runner was away and the dispatcher may already be
        handing it to someone else, so the returning runner's container must
        die rather than race. That costs one re-execution and buys the
        invariant "a step is executing on at most one runner" - the cheaper
        trade every time.
        """
        step_id = getattr(runner, "current_step_execution_id", None)
        if not step_id:
            return {"action": RESUME_IDLE}

        result = await db.execute(
            select(StepExecution).where(StepExecution.id == step_id)
        )
        execution = result.scalar_one_or_none()

        if execution is None:
            logger.warning(
                "runner %s reconnected holding step %s, which no longer exists",
                runner.id,
                step_id,
            )
            runner.current_step_execution_id = None
            await db.commit()
            return {"action": RESUME_IDLE}

        if execution.runner_id == runner.id:
            logger.info(
                "runner %s reconnected and still owns step %s: continue",
                runner.id,
                step_id,
            )
            return {"action": RESUME_CONTINUE, "step_id": step_id}

        logger.warning(
            "runner %s reconnected holding step %s, now owned by %r: abort",
            runner.id,
            step_id,
            execution.runner_id,
        )
        runner.current_step_execution_id = None
        await db.commit()
        return {"action": RESUME_ABORT, "step_id": step_id}

    # -- startup sweep --------------------------------------------------------

    async def recover_orphaned_steps(self, db) -> list:
        """Every step whose runner is gone goes back to `pending`.

        "Gone" is: the runner row says disconnected or dead, OR there is no
        runner row at all (the reference was to a runner this backend has
        never heard of - a stale DB, a hand-edited row, a deleted runner).

        A step with NO `runner_id` is out of scope: it was never any runner's
        to lose, so "the runner that had it is gone" is vacuous. Those rows
        belong to the local path, which fails them - and without this
        predicate the outer join's `Runner.id IS NULL` would match every
        local execution in the database and requeue work whose container is
        provably dead.

        Idempotent by construction: re-running over rows that are already
        pending is a no-op that still returns them, so a caller can invoke
        this from startup AND from a periodic sweep without special-casing.
        """
        result = await db.execute(
            select(StepExecution)
            .outerjoin(Runner, Runner.id == StepExecution.runner_id)
            .where(
                StepExecution.status.in_(sorted(REQUEUEABLE_STATUSES)),
                StepExecution.runner_id.is_not(None),
                Runner.status.in_(LOST_RUNNER_STATUSES) | Runner.id.is_(None),
            )
            .order_by(StepExecution.created_at)
        )
        orphaned = list(result.scalars().all())

        if not orphaned:
            logger.info("no orphaned remote step executions found")
            return []

        for execution in orphaned:
            logger.warning(
                "orphaned remote step execution %s (status=%s, runner=%s) -> pending",
                execution.id,
                execution.status,
                execution.runner_id,
            )
            execution.status = StepExecutionStatus.PENDING.value
            execution.runner_id = None

        await db.commit()

        for execution in orphaned:
            self._notify_requeue(execution.id, None, "orphan sweep")

        logger.info("recovered %d orphaned remote step execution(s)", len(orphaned))
        return orphaned


#: Process-wide singleton. The dispatcher installs its requeue hook on this
#: instance at startup, so every caller that reaches recovery through
#: `get_job_recovery_service()` wakes the same dispatcher.
_job_recovery_service = JobRecoveryService()


def get_job_recovery_service() -> JobRecoveryService:
    """The process-wide JobRecoveryService."""
    return _job_recovery_service


__all__ = [
    "JobRecoveryService",
    "get_job_recovery_service",
    "REQUEUEABLE_STATUSES",
    "LOST_RUNNER_STATUSES",
]
