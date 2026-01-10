"""
Idempotent Step Execution Service.

Provides idempotency guarantees for step execution:
- Same execution_key always returns the same StepExecution
- Different attempt numbers create new executions (for retries)
- Duplicate requests are safely ignored

Execution key format: {pipeline_run_id}:{step_index}:{attempt}
"""
from uuid import uuid4
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.pipeline import StepExecution, StepExecutionStatus


def generate_execution_key(
    pipeline_run_id: str,
    step_index: int,
    attempt: int
) -> str:
    """
    Generate an execution key for idempotency.

    Format: {pipeline_run_id}:{step_index}:{attempt}

    Args:
        pipeline_run_id: UUID of the pipeline run
        step_index: 0-based index of the step in the pipeline
        attempt: Attempt number (1 for first try, 2 for first retry, etc.)

    Returns:
        Execution key string
    """
    return f"{pipeline_run_id}:{step_index}:{attempt}"


def parse_execution_key(key: str) -> Tuple[str, int, int]:
    """
    Parse an execution key back to its components.

    Args:
        key: Execution key in format {pipeline_run_id}:{step_index}:{attempt}

    Returns:
        Tuple of (pipeline_run_id, step_index, attempt)

    Note:
        If pipeline_run_id contains colons, this takes the last two
        colon-separated values as step_index and attempt.
    """
    parts = key.rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid execution key format: {key}")

    run_id = parts[0]
    step_index = int(parts[1])
    attempt = int(parts[2])
    return run_id, step_index, attempt


class ExecutionService:
    """
    Service for managing step executions with idempotency guarantees.

    Provides get_or_create semantics - calling with the same execution_key
    will always return the same StepExecution, never creating duplicates.
    """

    def __init__(self, session: AsyncSession):
        """Initialize with a database session."""
        self._session = session

    async def get_or_create_execution(
        self,
        step_run_id: str,
        execution_key: str,
    ) -> StepExecution:
        """
        Get existing execution or create new one.

        If an execution with the given key already exists, returns it.
        Otherwise, creates a new execution with pending status.

        Args:
            step_run_id: ID of the StepRun this execution belongs to
            execution_key: Unique key for idempotency

        Returns:
            StepExecution instance (existing or newly created)
        """
        # Try to find existing execution
        existing = await self.get_by_key(execution_key)
        if existing:
            return existing

        # Create new execution
        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run_id,
            execution_key=execution_key,
            status=StepExecutionStatus.PENDING.value,
        )

        try:
            self._session.add(execution)
            await self._session.commit()
            await self._session.refresh(execution)
            return execution
        except IntegrityError:
            # Race condition - another process created it first
            await self._session.rollback()
            existing = await self.get_by_key(execution_key)
            if existing:
                return existing
            raise  # Something else went wrong

    async def get_by_key(self, execution_key: str) -> StepExecution | None:
        """
        Get execution by its unique key.

        Args:
            execution_key: The execution key to look up

        Returns:
            StepExecution if found, None otherwise
        """
        result = await self._session.execute(
            select(StepExecution).where(StepExecution.execution_key == execution_key)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, execution_id: str) -> StepExecution | None:
        """
        Get execution by its ID.

        Args:
            execution_id: The execution UUID to look up

        Returns:
            StepExecution if found, None otherwise
        """
        result = await self._session.execute(
            select(StepExecution).where(StepExecution.id == execution_id)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        execution_id: str,
        status: StepExecutionStatus,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> StepExecution | None:
        """
        Update execution status.

        Args:
            execution_id: ID of the execution to update
            status: New status value
            exit_code: Exit code if completed/failed
            error: Error message if failed

        Returns:
            Updated StepExecution or None if not found
        """
        from datetime import datetime

        execution = await self.get_by_id(execution_id)
        if not execution:
            return None

        execution.status = status.value
        if exit_code is not None:
            execution.exit_code = exit_code
        if error is not None:
            execution.error = error

        # Set timestamps based on status
        if status == StepExecutionStatus.RUNNING and execution.started_at is None:
            execution.started_at = datetime.utcnow()
        if status in (
            StepExecutionStatus.COMPLETED,
            StepExecutionStatus.FAILED,
            StepExecutionStatus.CANCELLED,
            StepExecutionStatus.TIMEOUT,
        ):
            execution.completed_at = datetime.utcnow()

        await self._session.commit()
        await self._session.refresh(execution)
        return execution
