"""
StepExecution model for tracking individual execution attempts.

Each step can have multiple execution attempts (retries).
This model links to StepRun but tracks execution-specific details
like container_id, runner_id, exit_code, etc.
"""
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ExecutionStatus(str, Enum):
    """
    Status of a step execution attempt.

    Matches the state machine in step_state.py:
    - PENDING: Execution queued, waiting to start
    - PREPARING: Container being created/pulled
    - RUNNING: Container executing
    - COMPLETING: Container finished, processing results
    - COMPLETED: Execution succeeded (exit_code = 0)
    - FAILED: Execution failed (exit_code != 0, crash, timeout)
    - CANCELLED: Execution was cancelled
    """
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepExecution(Base):
    """
    Tracks a single execution attempt of a pipeline step.

    Execution Key Format: "{pipeline_run_id}:{step_index}:{attempt}"

    This allows:
    - Multiple attempts per step (retries)
    - Idempotent execution (same key = same execution)
    - Tracking which runner/container handled execution
    """
    __tablename__ = "step_executions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )

    # Unique execution key for idempotency
    # Format: "{pipeline_run_id}:{step_index}:{attempt}"
    execution_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )

    # Links to the step run
    step_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("step_runs.id"),
        nullable=False,
    )

    # Execution status
    status: Mapped[str] = mapped_column(
        String(50),
        default=ExecutionStatus.PENDING.value,
        insert_default=ExecutionStatus.PENDING.value,
    )

    def __init__(self, **kwargs):
        """Initialize StepExecution with defaults."""
        # Apply default for status if not provided
        if "status" not in kwargs:
            kwargs["status"] = ExecutionStatus.PENDING.value
        super().__init__(**kwargs)

    # Which runner is handling this (for remote execution)
    runner_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )

    # Docker container ID (for local execution)
    container_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # Exit code from container/process
    exit_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Timestamps
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    # Relationship to StepRun
    step_run: Mapped["StepRun"] = relationship(
        "StepRun",
        back_populates="executions",
    )

    def __repr__(self) -> str:
        return f"<StepExecution {self.execution_key} status={self.status}>"

    @classmethod
    def parse_execution_key(cls, key: str) -> tuple[str, int, int]:
        """
        Parse execution key into components.

        Args:
            key: Execution key in format "{run_id}:{step_index}:{attempt}"

        Returns:
            Tuple of (pipeline_run_id, step_index, attempt)

        Raises:
            ValueError: If key format is invalid
        """
        parts = key.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid execution key format: {key}")
        return parts[0], int(parts[1]), int(parts[2])

    @classmethod
    def make_execution_key(
        cls,
        pipeline_run_id: str,
        step_index: int,
        attempt: int,
    ) -> str:
        """
        Create an execution key from components.

        Args:
            pipeline_run_id: Pipeline run ID
            step_index: Step index in pipeline
            attempt: Attempt number (1-based)

        Returns:
            Execution key string
        """
        return f"{pipeline_run_id}:{step_index}:{attempt}"
