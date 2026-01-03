"""
Idempotency management for step executions.

Ensures at-most-once execution semantics by tracking execution keys
and their results.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4


@dataclass(frozen=True)
class ExecutionKey:
    """
    Unique key identifying a specific step execution attempt.

    Format: "{pipeline_run_id}:{step_index}:{attempt}"

    This key is used for:
    - Idempotency: Same key = same execution
    - Retries: Different attempt = new execution
    - Tracking: Links execution to pipeline run
    """
    pipeline_run_id: str
    step_index: int
    attempt: int

    def __str__(self) -> str:
        """Return string representation for storage."""
        return f"{self.pipeline_run_id}:{self.step_index}:{self.attempt}"

    def __hash__(self) -> int:
        """Make hashable for use in sets/dicts."""
        return hash((self.pipeline_run_id, self.step_index, self.attempt))

    def __eq__(self, other: object) -> bool:
        """Check equality with another key."""
        if not isinstance(other, ExecutionKey):
            return False
        return (
            self.pipeline_run_id == other.pipeline_run_id
            and self.step_index == other.step_index
            and self.attempt == other.attempt
        )

    @classmethod
    def from_string(cls, key_str: str) -> "ExecutionKey":
        """
        Parse key from string representation.

        Args:
            key_str: Key string in format "run_id:step_index:attempt"

        Returns:
            ExecutionKey instance

        Raises:
            ValueError: If string format is invalid
        """
        parts = key_str.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid execution key format: {key_str}")

        return cls(
            pipeline_run_id=parts[0],
            step_index=int(parts[1]),
            attempt=int(parts[2]),
        )


@dataclass
class ExecutionResult:
    """
    Result of a completed execution.

    Stored by IdempotencyStore when execution completes.
    """
    success: bool
    exit_code: int
    error: Optional[str] = None
    completed_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutionRecord:
    """
    Internal record of an execution.

    Tracks both in-progress and completed executions.
    """
    execution_id: str
    key: ExecutionKey
    started_at: datetime
    result: Optional[ExecutionResult] = None

    @property
    def is_complete(self) -> bool:
        """Check if execution has completed."""
        return self.result is not None


class IdempotencyStore:
    """
    In-memory store for tracking execution idempotency.

    Ensures that:
    - Same execution key returns same execution ID
    - Different attempt numbers create new executions
    - Completed executions return stored results

    Usage:
        store = IdempotencyStore()
        key = ExecutionKey("run-123", step_index=0, attempt=1)

        # Start execution (idempotent)
        exec_id = store.start_execution(key)

        # Complete execution
        store.complete_execution(key, success=True, exit_code=0)

        # Check result
        result = store.get_result(key)
    """

    def __init__(self):
        """Initialize empty store."""
        self._executions: dict[ExecutionKey, ExecutionRecord] = {}
        # Track max attempt per (run_id, step_index)
        self._max_attempts: dict[tuple[str, int], int] = {}

    def start_execution(self, key: ExecutionKey) -> str:
        """
        Start or retrieve an execution for the given key.

        If an execution already exists for this key, returns the existing
        execution ID (idempotent behavior).

        Args:
            key: Execution key

        Returns:
            Execution ID (UUID string)
        """
        if key in self._executions:
            return self._executions[key].execution_id

        execution_id = str(uuid4())
        record = ExecutionRecord(
            execution_id=execution_id,
            key=key,
            started_at=datetime.utcnow(),
        )
        self._executions[key] = record

        # Track max attempt
        step_key = (key.pipeline_run_id, key.step_index)
        current_max = self._max_attempts.get(step_key, 0)
        self._max_attempts[step_key] = max(current_max, key.attempt)

        return execution_id

    def complete_execution(
        self,
        key: ExecutionKey,
        success: bool,
        exit_code: int,
        error: Optional[str] = None,
    ) -> None:
        """
        Mark an execution as complete with its result.

        Args:
            key: Execution key
            success: Whether execution succeeded
            exit_code: Process exit code
            error: Optional error message

        Raises:
            KeyError: If no execution exists for this key
        """
        if key not in self._executions:
            raise KeyError(f"No execution found for key: {key}")

        self._executions[key].result = ExecutionResult(
            success=success,
            exit_code=exit_code,
            error=error,
        )

    def get_result(self, key: ExecutionKey) -> Optional[ExecutionResult]:
        """
        Get the result of a completed execution.

        Args:
            key: Execution key

        Returns:
            ExecutionResult if execution completed, None otherwise
        """
        record = self._executions.get(key)
        if record is None:
            return None
        return record.result

    def is_executing(self, key: ExecutionKey) -> bool:
        """
        Check if an execution is currently in progress.

        Args:
            key: Execution key

        Returns:
            True if execution started but not completed
        """
        record = self._executions.get(key)
        if record is None:
            return False
        return not record.is_complete

    def has_executed(self, key: ExecutionKey) -> bool:
        """
        Check if an execution has completed.

        Args:
            key: Execution key

        Returns:
            True if execution completed (success or failure)
        """
        record = self._executions.get(key)
        if record is None:
            return False
        return record.is_complete

    def get_next_attempt(self, pipeline_run_id: str, step_index: int) -> int:
        """
        Get the next attempt number for a step.

        Args:
            pipeline_run_id: Pipeline run ID
            step_index: Step index

        Returns:
            Next attempt number (1 for first attempt)
        """
        step_key = (pipeline_run_id, step_index)
        current_max = self._max_attempts.get(step_key, 0)
        return current_max + 1

    def get_execution_id(self, key: ExecutionKey) -> Optional[str]:
        """
        Get the execution ID for a key.

        Args:
            key: Execution key

        Returns:
            Execution ID or None if not found
        """
        record = self._executions.get(key)
        return record.execution_id if record else None

    def clear(self) -> None:
        """Clear all stored executions (for testing)."""
        self._executions.clear()
        self._max_attempts.clear()


# Global singleton instance
_idempotency_store: Optional[IdempotencyStore] = None


def get_idempotency_store() -> IdempotencyStore:
    """Get the global idempotency store instance."""
    global _idempotency_store
    if _idempotency_store is None:
        _idempotency_store = IdempotencyStore()
    return _idempotency_store
