"""
Step Execution State Machine.

Manages valid state transitions for step executions.
The state machine enforces the following lifecycle:

    pending -> assigned -> preparing -> running -> completing -> completed
                                            |            |
                                            | exit_0     | finalized
                                            v            v
                                       [timeout]    [completed]
                                            |
                                            v
    cancelled <-- cancel (any) -------- [failed]

Terminal states: completed, failed, cancelled, timeout
"""
from datetime import datetime
from enum import Enum
from typing import Set, List, Tuple


class StepExecutionStatus(str, Enum):
    """Status values for step executions."""
    PENDING = "pending"
    ASSIGNED = "assigned"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


# Define valid transitions: from_state -> {valid_to_states}
VALID_TRANSITIONS: dict[StepExecutionStatus, Set[StepExecutionStatus]] = {
    StepExecutionStatus.PENDING: {
        StepExecutionStatus.ASSIGNED,
        StepExecutionStatus.CANCELLED,
        StepExecutionStatus.FAILED,  # For immediate failures (e.g., invalid config)
    },
    StepExecutionStatus.ASSIGNED: {
        StepExecutionStatus.PREPARING,
        StepExecutionStatus.CANCELLED,
        StepExecutionStatus.FAILED,  # For assignment failures
    },
    StepExecutionStatus.PREPARING: {
        StepExecutionStatus.RUNNING,
        StepExecutionStatus.CANCELLED,
        StepExecutionStatus.FAILED,  # For setup failures (e.g., image pull failed)
    },
    StepExecutionStatus.RUNNING: {
        StepExecutionStatus.COMPLETING,
        StepExecutionStatus.FAILED,
        StepExecutionStatus.TIMEOUT,
        StepExecutionStatus.CANCELLED,
    },
    StepExecutionStatus.COMPLETING: {
        StepExecutionStatus.COMPLETED,
        StepExecutionStatus.FAILED,
        StepExecutionStatus.CANCELLED,
    },
    # Terminal states - no valid outgoing transitions
    StepExecutionStatus.COMPLETED: set(),
    StepExecutionStatus.FAILED: set(),
    StepExecutionStatus.CANCELLED: set(),
    StepExecutionStatus.TIMEOUT: set(),
}

# Terminal states that cannot transition to anything else
TERMINAL_STATES: Set[StepExecutionStatus] = {
    StepExecutionStatus.COMPLETED,
    StepExecutionStatus.FAILED,
    StepExecutionStatus.CANCELLED,
    StepExecutionStatus.TIMEOUT,
}


class StepStateMachine:
    """
    Manages state transitions for a step execution.

    Example usage:
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        if sm.can_transition_to(StepExecutionStatus.ASSIGNED):
            sm.transition_to(StepExecutionStatus.ASSIGNED)
    """

    def __init__(self, initial_status: StepExecutionStatus):
        """Initialize state machine with a starting status."""
        self._status = initial_status
        self._last_transition_at: datetime | None = None
        self._history: List[Tuple[StepExecutionStatus, datetime]] = []

    @property
    def status(self) -> StepExecutionStatus:
        """Get current status."""
        return self._status

    @property
    def last_transition_at(self) -> datetime | None:
        """Get timestamp of last transition."""
        return self._last_transition_at

    def is_terminal(self) -> bool:
        """Check if current state is terminal (no outgoing transitions)."""
        return self._status in TERMINAL_STATES

    def can_transition_to(self, new_status: StepExecutionStatus) -> bool:
        """Check if transition to new_status is valid from current state."""
        valid_next = VALID_TRANSITIONS.get(self._status, set())
        return new_status in valid_next

    def get_valid_next_states(self) -> Set[StepExecutionStatus]:
        """Get all valid states that can be transitioned to from current state."""
        return VALID_TRANSITIONS.get(self._status, set()).copy()

    def transition_to(self, new_status: StepExecutionStatus) -> bool:
        """
        Attempt to transition to a new status.

        Returns True if transition was successful, False if invalid.
        Records timestamp of transition.
        """
        if not self.can_transition_to(new_status):
            return False

        now = datetime.utcnow()
        self._history.append((new_status, now))
        self._status = new_status
        self._last_transition_at = now
        return True

    def get_history(self) -> List[Tuple[StepExecutionStatus, datetime]]:
        """Get history of state transitions (status, timestamp) pairs."""
        return self._history.copy()
