"""
Crash Recovery for Step Executions.

On backend startup, finds orphaned step executions (in non-terminal states)
and marks them as failed. This handles the case where the backend crashed
or was restarted while steps were executing.

For LocalExecutor: Containers are gone after restart, so steps must be failed.
For RemoteExecutor: Runners will reconnect and steps can be reassigned (future).
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
    Find and fail orphaned step executions.

    Called on backend startup to clean up steps that were interrupted
    by a crash or restart.

    Args:
        session: Database session

    Returns:
        List of execution IDs that were recovered (marked as failed)
    """
    # Find all executions in non-terminal states
    result = await session.execute(
        select(StepExecution).where(StepExecution.status.in_(ORPHANED_STATES))
    )
    orphaned = result.scalars().all()

    if not orphaned:
        logger.info("No orphaned step executions found")
        return []

    recovered_ids = []
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
    return recovered_ids


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
