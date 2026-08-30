"""
Crash Recovery for Step Executions.

On backend startup, finds orphaned step executions (in non-terminal states)
and deals with them. The two execution paths need OPPOSITE answers, and
Phase 12.6 splits them (this module's docstring flagged the divergence from
the day the remote path was designed):

    LOCAL  (StepExecution.runner_id IS NULL)
        The container died with the backend. There is nothing to reconnect
        to and nothing to reassign, so the execution is FAILED - exactly as
        it has been since 12.2.

    REMOTE (StepExecution.runner_id IS NOT NULL)
        A runner genuinely can reconnect and a step genuinely can be
        reassigned, so the execution goes back to PENDING for the dispatcher
        via `JobRecoveryService.recover_orphaned_steps`. Failing it would
        throw away work that a live agent on another host is still perfectly
        able to do.

The remote branch is safe because it is bounded on both sides: the registry's
startup bootstrap has already marked every runner `disconnected` (no
connection survives a restart), so the sweep sees them as lost; and a remote
step whose PipelineRun no longer has a live executor generator is still
reaped by the run-level orphan sweep. The step is requeued at the
StepExecution layer and then, if nobody claims it, failed at the run layer -
so nothing hangs either way.
"""
import logging
from datetime import datetime
from typing import List

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import StepExecution, StepExecutionStatus

logger = logging.getLogger(__name__)

# Non-terminal states that need recovery
ORPHANED_STATES = [
    StepExecutionStatus.PENDING.value,
    StepExecutionStatus.ASSIGNED.value,
    StepExecutionStatus.PREPARING.value,
    StepExecutionStatus.RUNNING.value,
    StepExecutionStatus.COMPLETING.value,
]


async def recover_orphaned_executions(session: AsyncSession) -> List[str]:
    """
    Recover step executions stranded by a backend crash or restart.

    Local executions are FAILED; remote ones are REQUEUED (see the module
    docstring for why the two answers differ).

    Args:
        session: Database session

    Returns:
        List of LOCAL execution IDs that were failed. Requeued remote
        executions are counted in the log rather than returned: the caller
        reports "N failed", and a requeued step is not a failure - conflating
        them would make a healthy remote handover read as a crash.
    """
    # ORDER MATTERS: the LOCAL sweep runs FIRST.
    #
    # A requeue clears `runner_id`, which is exactly the predicate the local
    # branch selects on. Requeueing first would hand every remote step
    # straight to the local sweep, which would then fail the work the remote
    # branch had just saved - the two branches would silently cancel out and
    # the phase's whole point would be a no-op. Selecting the local rows
    # before any requeue writes makes the two passes independent.
    result = await session.execute(
        select(StepExecution).where(
            StepExecution.status.in_(ORPHANED_STATES),
            # runner_id IS NULL is the local path by definition: only the
            # remote assignment CAS ever writes that column.
            StepExecution.runner_id.is_(None),
        )
    )
    orphaned = result.scalars().all()

    recovered_ids = []
    if orphaned:
        now = datetime.utcnow()
        for execution in orphaned:
            logger.warning(
                f"Recovering orphaned execution {execution.id} "
                f"(key={execution.execution_key}, status={execution.status})"
            )

            execution.status = StepExecutionStatus.FAILED.value
            execution.error = "Execution interrupted by backend restart"
            execution.completed_at = now

            recovered_ids.append(execution.id)

        await session.commit()
        logger.info(f"Recovered {len(recovered_ids)} orphaned step executions")
    else:
        logger.info("No orphaned step executions found")

    requeued = await _requeue_orphaned_remote_executions(session)
    if requeued:
        logger.info(
            "Requeued %d orphaned REMOTE step execution(s) for reassignment",
            len(requeued),
        )

    return recovered_ids


async def _requeue_orphaned_remote_executions(session: AsyncSession) -> List[str]:
    """Hand stranded REMOTE executions to the job recovery service.

    Delegates rather than reimplementing: `JobRecoveryService` owns the
    "which states are requeueable / which runners count as lost" policy for
    every requeue in the system (death, disconnect, startup), and failure_01
    shipped two copies of that policy which promptly disagreed.

    Never raises: a recovery failure must not stop the backend from starting,
    and the dispatcher's 15s self-heal tick plus the run-level orphan sweep
    are both still behind it.
    """
    try:
        from app.services.execution.job_recovery import get_job_recovery_service

        recovered = await get_job_recovery_service().recover_orphaned_steps(session)
        return [execution.id for execution in recovered]
    except Exception:
        logger.exception(
            "Remote orphan requeue failed on startup; local recovery continues"
        )
        return []


async def get_orphaned_execution_count(session: AsyncSession) -> int:
    """
    Count orphaned executions without modifying them.

    Useful for monitoring/health checks.

    Args:
        session: Database session

    Returns:
        Number of orphaned executions
    """
    result = await session.execute(
        select(StepExecution).where(StepExecution.status.in_(ORPHANED_STATES))
    )
    return len(result.scalars().all())


async def cleanup_old_completed_executions(
    session: AsyncSession,
    days_old: int = 30
) -> int:
    """
    Clean up old completed executions to prevent database bloat.

    Args:
        session: Database session
        days_old: Delete executions older than this many days

    Returns:
        Number of executions deleted
    """
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=days_old)

    # Only delete terminal states
    terminal_states = [
        StepExecutionStatus.COMPLETED.value,
        StepExecutionStatus.FAILED.value,
        StepExecutionStatus.CANCELLED.value,
        StepExecutionStatus.TIMEOUT.value,
    ]

    result = await session.execute(
        select(StepExecution).where(
            StepExecution.status.in_(terminal_states),
            StepExecution.completed_at < cutoff
        )
    )
    old_executions = result.scalars().all()

    if not old_executions:
        return 0

    for execution in old_executions:
        await session.delete(execution)

    await session.commit()

    logger.info(f"Cleaned up {len(old_executions)} old step executions")
    return len(old_executions)
