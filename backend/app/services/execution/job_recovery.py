"""
Job Recovery Service for Phase 12.6.

Handles recovery scenarios:
- Runner dies mid-job (heartbeat timeout) -> requeue step
- Runner disconnects mid-job (WebSocket closes) -> requeue step
- Runner reconnects after death -> check if step was reassigned
- Backend restarts -> find and recover orphaned steps
"""

import logging
from typing import Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runner import Runner, RunnerStatus
from app.models.step_execution import StepExecution, ExecutionStatus

logger = logging.getLogger(__name__)


# Singleton instance
_job_recovery_service: Optional["JobRecoveryService"] = None


def get_job_recovery_service() -> "JobRecoveryService":
    """Get the JobRecoveryService singleton instance."""
    global _job_recovery_service
    if _job_recovery_service is None:
        _job_recovery_service = JobRecoveryService()
    return _job_recovery_service


class JobRecoveryService:
    """
    Handles recovery scenarios for remote execution.

    Key principles:
    - Database is source of truth
    - Idempotency: duplicate recovery is safe
    - Step can only be assigned to one runner at a time
    - Orphaned steps (no runner) return to pending
    """

    async def on_runner_death(
        self,
        db: AsyncSession,
        runner: Runner,
    ) -> None:
        """
        Handle runner death (heartbeat timeout or crash).

        Requeues any step the runner was working on.

        Args:
            db: Database session
            runner: Runner model
        """
        step_id = runner.current_step_execution_id

        # Update runner status
        runner.status = RunnerStatus.DEAD.value

        if step_id:
            # Get the step
            result = await db.execute(
                select(StepExecution).where(StepExecution.id == step_id)
            )
            step = result.scalar_one_or_none()

            if step and step.status in (
                ExecutionStatus.PREPARING.value,
                ExecutionStatus.RUNNING.value,
            ):
                # Requeue step
                step.status = ExecutionStatus.PENDING.value
                step.runner_id = None
                logger.info(f"Requeued step {step_id} after runner {runner.id} death")

        await db.commit()
        logger.info(f"Runner {runner.id} marked dead")

    async def on_runner_disconnect(
        self,
        db: AsyncSession,
        runner: Runner,
    ) -> None:
        """
        Handle runner disconnection (WebSocket close).

        Requeues any step the runner was working on.

        Args:
            db: Database session
            runner: Runner model
        """
        step_id = runner.current_step_execution_id

        if step_id:
            # Get the step
            result = await db.execute(
                select(StepExecution).where(StepExecution.id == step_id)
            )
            step = result.scalar_one_or_none()

            if step and step.status in (
                ExecutionStatus.PREPARING.value,
                ExecutionStatus.RUNNING.value,
            ):
                # Requeue step
                step.status = ExecutionStatus.PENDING.value
                step.runner_id = None
                await db.commit()
                logger.info(f"Requeued step {step_id} after runner {runner.id} disconnect")

    async def on_runner_reconnect(
        self,
        db: AsyncSession,
        runner: Runner,
    ) -> dict:
        """
        Handle runner reconnection.

        Checks if the step was reassigned and returns appropriate action.

        Args:
            db: Database session
            runner: Runner model

        Returns:
            Action dict with:
            - action: "continue", "abort", or "idle"
            - step_id: Step ID if relevant
        """
        step_id = runner.current_step_execution_id

        if not step_id:
            return {"action": "idle"}

        # Get the step
        result = await db.execute(
            select(StepExecution).where(StepExecution.id == step_id)
        )
        step = result.scalar_one_or_none()

        if not step:
            # Step was deleted
            runner.current_step_execution_id = None
            await db.commit()
            return {"action": "idle"}

        if step.runner_id == runner.id:
            # Step still assigned to this runner - continue
            return {"action": "continue", "step_id": step_id}
        else:
            # Step was reassigned to different runner - abort
            runner.current_step_execution_id = None
            await db.commit()
            return {"action": "abort", "step_id": step_id}

    async def recover_orphaned_steps(
        self,
        db: AsyncSession,
    ) -> list[StepExecution]:
        """
        Find and recover orphaned steps on backend startup.

        An orphaned step is one that:
        - Has status preparing/running
        - Is assigned to a dead/disconnected runner OR
        - Has no runner assignment but not pending

        Args:
            db: Database session

        Returns:
            List of recovered StepExecution objects
        """
        recovered = []

        # Find steps assigned to dead/disconnected runners
        result = await db.execute(
            select(StepExecution)
            .join(Runner, StepExecution.runner_id == Runner.id, isouter=True)
            .where(
                StepExecution.status.in_([
                    ExecutionStatus.PREPARING.value,
                    ExecutionStatus.RUNNING.value,
                ])
            )
            .where(
                or_(
                    Runner.status.in_([
                        RunnerStatus.DISCONNECTED.value,
                        RunnerStatus.DEAD.value,
                    ]),
                    Runner.id.is_(None),  # No runner assigned
                )
            )
        )
        orphaned_steps = result.scalars().all()

        for step in orphaned_steps:
            step.status = ExecutionStatus.PENDING.value
            step.runner_id = None
            recovered.append(step)
            logger.info(f"Recovered orphaned step {step.id}")

        if recovered:
            await db.commit()
            logger.info(f"Recovered {len(recovered)} orphaned steps")

        return recovered
