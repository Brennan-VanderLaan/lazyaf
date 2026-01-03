"""
Step State Machine for execution lifecycle tracking.

Defines valid state transitions for step executions and provides
a state machine class for tracking step progress.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class StepState(str, Enum):
    """
    Possible states for a step execution.

    State flow:
        PENDING -> PREPARING -> RUNNING -> COMPLETING -> COMPLETED
                            \\-> FAILED (at any point)
                            \\-> CANCELLED (at any point)
    """
    PENDING = "pending"          # Queued, waiting for execution
    PREPARING = "preparing"      # Container being created/pulled
    RUNNING = "running"          # Container executing
    COMPLETING = "completing"    # Container finished, processing results
    COMPLETED = "completed"      # Successfully finished (exit_code = 0)
    FAILED = "failed"            # Failed (exit_code != 0, crash, timeout)
    CANCELLED = "cancelled"      # Cancelled by user/system


# Terminal states - no transitions allowed from these
TERMINAL_STATES = {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED}

# Valid state transitions map: from_state -> set of valid to_states
VALID_TRANSITIONS: dict[StepState, set[StepState]] = {
    StepState.PENDING: {StepState.PREPARING, StepState.CANCELLED},
    StepState.PREPARING: {StepState.RUNNING, StepState.FAILED, StepState.CANCELLED},
    StepState.RUNNING: {StepState.COMPLETING, StepState.FAILED, StepState.CANCELLED},
    StepState.COMPLETING: {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED},
    StepState.COMPLETED: set(),  # Terminal - no transitions
    StepState.FAILED: set(),     # Terminal - no transitions
    StepState.CANCELLED: set(),  # Terminal - no transitions
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, from_state: StepState, to_state: StepState, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        message = f"Invalid transition: {from_state.value} -> {to_state.value}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


@dataclass
class StepStateTransition:
    """Record of a state transition."""
    from_state: StepState
    to_state: StepState
    timestamp: datetime
    reason: Optional[str] = None
    exit_code: Optional[int] = None

    def __repr__(self) -> str:
        return f"Transition({self.from_state.value} -> {self.to_state.value} at {self.timestamp})"


class StepStateMachine:
    """
    State machine for tracking step execution lifecycle.

    Usage:
        machine = StepStateMachine(initial_state=StepState.PENDING)
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        machine.transition(StepState.COMPLETED, exit_code=0)
    """

    def __init__(self, initial_state: StepState = StepState.PENDING):
        """
        Initialize state machine.

        Args:
            initial_state: Starting state (default: PENDING)
        """
        self._state = initial_state
        self._exit_code: Optional[int] = None
        self._history: list[StepStateTransition] = []
        self._created_at = datetime.utcnow()

    @property
    def state(self) -> StepState:
        """Current state."""
        return self._state

    @property
    def exit_code(self) -> Optional[int]:
        """Exit code (set when transitioning to COMPLETED or FAILED)."""
        return self._exit_code

    @property
    def history(self) -> list[StepStateTransition]:
        """List of all state transitions."""
        return list(self._history)

    @property
    def last_transition(self) -> Optional[StepStateTransition]:
        """Most recent transition, or None if no transitions yet."""
        return self._history[-1] if self._history else None

    @property
    def is_terminal(self) -> bool:
        """Check if current state is terminal (no more transitions possible)."""
        return self._state in TERMINAL_STATES

    @property
    def duration(self) -> Optional[timedelta]:
        """
        Total duration from first to last transition.

        Returns None if no transitions have occurred.
        """
        if not self._history:
            return None

        first = self._history[0].timestamp
        last = self._history[-1].timestamp
        return last - first

    def can_transition(self, to_state: StepState) -> bool:
        """
        Check if transition to target state is valid.

        Args:
            to_state: Target state

        Returns:
            True if transition is valid, False otherwise
        """
        valid_targets = VALID_TRANSITIONS.get(self._state, set())
        return to_state in valid_targets

    def transition(
        self,
        to_state: StepState,
        reason: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> StepStateTransition:
        """
        Transition to a new state.

        Args:
            to_state: Target state
            reason: Optional reason for transition
            exit_code: Optional exit code (for COMPLETED/FAILED transitions)

        Returns:
            The transition record

        Raises:
            InvalidTransitionError: If transition is not valid
        """
        if not self.can_transition(to_state):
            if self.is_terminal:
                raise InvalidTransitionError(
                    self._state, to_state,
                    f"Cannot transition from terminal state {self._state.value}"
                )
            raise InvalidTransitionError(self._state, to_state)

        transition = StepStateTransition(
            from_state=self._state,
            to_state=to_state,
            timestamp=datetime.utcnow(),
            reason=reason,
            exit_code=exit_code,
        )

        self._history.append(transition)
        self._state = to_state

        if exit_code is not None:
            self._exit_code = exit_code

        return transition

    def to_dict(self) -> dict:
        """Serialize state machine to dictionary."""
        return {
            "state": self._state.value,
            "exit_code": self._exit_code,
            "is_terminal": self.is_terminal,
            "created_at": self._created_at.isoformat(),
            "history": [
                {
                    "from_state": t.from_state.value,
                    "to_state": t.to_state.value,
                    "timestamp": t.timestamp.isoformat(),
                    "reason": t.reason,
                    "exit_code": t.exit_code,
                }
                for t in self._history
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StepStateMachine":
        """Deserialize state machine from dictionary."""
        machine = cls(initial_state=StepState(data["state"]))
        machine._exit_code = data.get("exit_code")
        machine._created_at = datetime.fromisoformat(data["created_at"])
        machine._history = [
            StepStateTransition(
                from_state=StepState(t["from_state"]),
                to_state=StepState(t["to_state"]),
                timestamp=datetime.fromisoformat(t["timestamp"]),
                reason=t.get("reason"),
                exit_code=t.get("exit_code"),
            )
            for t in data.get("history", [])
        ]
        return machine
