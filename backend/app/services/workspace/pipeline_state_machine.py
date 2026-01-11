"""
Pipeline State Machine - Phase 12.2

Manages pipeline lifecycle with proper state transitions:
- pending: Awaiting start
- preparing: Workspace creation in progress
- running: Steps actively executing
- completing: Cleanup in progress
- completed: Successfully finished (terminal)
- failed: Error state (terminal)
- cancelled: User cancelled (terminal)
"""
from enum import Enum
from datetime import datetime
from typing import List, Dict, Any, Optional, Set


class PipelineStatus(str, Enum):
    """Pipeline lifecycle states."""
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Valid state transitions
VALID_TRANSITIONS: Dict[PipelineStatus, List[PipelineStatus]] = {
    PipelineStatus.PENDING: [PipelineStatus.PREPARING, PipelineStatus.CANCELLED],
    PipelineStatus.PREPARING: [PipelineStatus.RUNNING, PipelineStatus.FAILED, PipelineStatus.CANCELLED],
    PipelineStatus.RUNNING: [PipelineStatus.COMPLETING, PipelineStatus.FAILED, PipelineStatus.CANCELLED],
    PipelineStatus.COMPLETING: [PipelineStatus.COMPLETED, PipelineStatus.FAILED, PipelineStatus.CANCELLED],
    PipelineStatus.COMPLETED: [],  # Terminal
    PipelineStatus.FAILED: [],  # Terminal
    PipelineStatus.CANCELLED: [],  # Terminal
}

# Terminal states (no further transitions)
TERMINAL_STATES: Set[PipelineStatus] = {
    PipelineStatus.COMPLETED,
    PipelineStatus.FAILED,
    PipelineStatus.CANCELLED,
}


class PipelineStateMachine:
    """
    State machine for pipeline lifecycle management.

    Tracks:
    - Current status and valid transitions
    - Step completion and failure
    - Transition history with timestamps
    - Started/completed timestamps
    """

    def __init__(
        self,
        initial_status: PipelineStatus,
        total_steps: int = 0,
    ):
        self._status = initial_status
        self._total_steps = total_steps
        self._completed_steps = 0
        self._completed_step_indices: Set[int] = set()
        self._failed_step_index: Optional[int] = None
        self._failure_error: Optional[str] = None
        self._history: List[Dict[str, Any]] = []
        self._created_at = datetime.utcnow()
        self._started_at: Optional[datetime] = None
        self._completed_at: Optional[datetime] = None

    @property
    def current_status(self) -> PipelineStatus:
        """Get current pipeline status."""
        return self._status

    @property
    def total_steps(self) -> int:
        """Get total number of steps."""
        return self._total_steps

    @property
    def completed_steps(self) -> int:
        """Get number of completed steps."""
        return self._completed_steps

    @property
    def failed_step_index(self) -> Optional[int]:
        """Get index of failed step, if any."""
        return self._failed_step_index

    @property
    def failure_error(self) -> Optional[str]:
        """Get failure error message, if any."""
        return self._failure_error

    @property
    def started_at(self) -> Optional[datetime]:
        """Get when pipeline started (transitioned to PREPARING)."""
        return self._started_at

    @property
    def completed_at(self) -> Optional[datetime]:
        """Get when pipeline completed/failed/cancelled."""
        return self._completed_at

    @property
    def created_at(self) -> datetime:
        """Get pipeline creation timestamp."""
        return self._created_at

    def can_transition_to(self, new_status: PipelineStatus) -> bool:
        """Check if transition to new_status is valid."""
        valid_next = VALID_TRANSITIONS.get(self._status, [])
        return new_status in valid_next

    def transition_to(self, new_status: PipelineStatus) -> None:
        """
        Transition to a new status.

        Raises:
            ValueError: If transition is invalid
        """
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Invalid pipeline transition: {self._status.value} -> {new_status.value}"
            )

        old_status = self._status
        self._status = new_status
        now = datetime.utcnow()

        # Track started_at when entering PREPARING
        if new_status == PipelineStatus.PREPARING and self._started_at is None:
            self._started_at = now

        # Track completed_at when entering terminal states
        if new_status in TERMINAL_STATES:
            self._completed_at = now

        self._history.append({
            "from": old_status,
            "to": new_status,
            "timestamp": now,
        })

    def mark_step_completed(self, step_index: int) -> None:
        """
        Mark a step as completed.

        If all steps are now complete, transitions to COMPLETING.

        Args:
            step_index: Index of the completed step
        """
        if step_index not in self._completed_step_indices:
            self._completed_step_indices.add(step_index)
            self._completed_steps += 1

        # If all steps complete, transition to COMPLETING
        if self._completed_steps >= self._total_steps and self._status == PipelineStatus.RUNNING:
            self._status = PipelineStatus.COMPLETING
            now = datetime.utcnow()
            self._history.append({
                "from": PipelineStatus.RUNNING,
                "to": PipelineStatus.COMPLETING,
                "timestamp": now,
            })

    def mark_step_failed(self, step_index: int, error: str = "") -> None:
        """
        Mark a step as failed.

        Transitions pipeline to FAILED.

        Args:
            step_index: Index of the failed step
            error: Error message
        """
        self._failed_step_index = step_index
        self._failure_error = error

        # Transition to FAILED
        if self._status not in TERMINAL_STATES:
            old_status = self._status
            self._status = PipelineStatus.FAILED
            self._completed_at = datetime.utcnow()
            self._history.append({
                "from": old_status,
                "to": PipelineStatus.FAILED,
                "timestamp": self._completed_at,
            })

    def is_terminal(self) -> bool:
        """Check if pipeline is in a terminal state."""
        return self._status in TERMINAL_STATES

    def get_valid_next_states(self) -> List[PipelineStatus]:
        """Get list of valid next states from current state."""
        return VALID_TRANSITIONS.get(self._status, [])

    def get_history(self) -> List[Dict[str, Any]]:
        """Get transition history."""
        return self._history.copy()
