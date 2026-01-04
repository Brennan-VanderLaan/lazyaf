"""
Debug Session State Machine for debug re-run lifecycle tracking.

Defines valid state transitions for debug sessions and provides
a state machine class for tracking session progress.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class DebugState(str, Enum):
    """
    Possible states for a debug session.

    State flow:
        PENDING -> WAITING_AT_BP -> CONNECTED -> ENDED
                       |               |
                       v               v
                    TIMEOUT         TIMEOUT

    - PENDING: Debug run started, executing before first breakpoint
    - WAITING_AT_BP: At breakpoint, waiting for user to connect
    - CONNECTED: User connected via CLI
    - TIMEOUT: Session timed out
    - ENDED: User resumed/aborted, or pipeline completed
    """
    PENDING = "pending"
    WAITING_AT_BP = "waiting_at_bp"
    CONNECTED = "connected"
    TIMEOUT = "timeout"
    ENDED = "ended"


# Terminal states - no transitions allowed from these
TERMINAL_STATES = {DebugState.TIMEOUT, DebugState.ENDED}

# Valid state transitions map: from_state -> set of valid to_states
VALID_TRANSITIONS: dict[DebugState, set[DebugState]] = {
    DebugState.PENDING: {DebugState.WAITING_AT_BP, DebugState.ENDED},
    DebugState.WAITING_AT_BP: {DebugState.CONNECTED, DebugState.TIMEOUT, DebugState.ENDED},
    DebugState.CONNECTED: {DebugState.ENDED, DebugState.TIMEOUT, DebugState.WAITING_AT_BP},
    DebugState.TIMEOUT: set(),  # Terminal - no transitions
    DebugState.ENDED: set(),    # Terminal - no transitions
}


class InvalidDebugTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, from_state: DebugState, to_state: DebugState, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        message = f"Invalid transition: {from_state.value} -> {to_state.value}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


@dataclass
class DebugStateTransition:
    """Record of a state transition."""
    from_state: DebugState
    to_state: DebugState
    timestamp: datetime
    reason: Optional[str] = None

    def __repr__(self) -> str:
        return f"Transition({self.from_state.value} -> {self.to_state.value} at {self.timestamp})"


class DebugStateMachine:
    """
    State machine for tracking debug session lifecycle.

    Usage:
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit")
        machine.transition(DebugState.CONNECTED, reason="CLI connected")
        machine.transition(DebugState.ENDED, reason="User resumed")
    """

    def __init__(self, initial_state: DebugState = DebugState.PENDING):
        """
        Initialize state machine.

        Args:
            initial_state: Starting state (default: PENDING)
        """
        self._state = initial_state
        self._history: list[DebugStateTransition] = []
        self._created_at = datetime.utcnow()

    @property
    def state(self) -> DebugState:
        """Current state."""
        return self._state

    @property
    def history(self) -> list[DebugStateTransition]:
        """List of all state transitions."""
        return list(self._history)

    @property
    def last_transition(self) -> Optional[DebugStateTransition]:
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

    def can_transition(self, to_state: DebugState) -> bool:
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
        to_state: DebugState,
        reason: Optional[str] = None,
    ) -> DebugStateTransition:
        """
        Transition to a new state.

        Args:
            to_state: Target state
            reason: Optional reason for transition

        Returns:
            The transition record

        Raises:
            InvalidDebugTransitionError: If transition is not valid
        """
        if not self.can_transition(to_state):
            if self.is_terminal:
                raise InvalidDebugTransitionError(
                    self._state, to_state,
                    f"Cannot transition from terminal state {self._state.value}"
                )
            raise InvalidDebugTransitionError(self._state, to_state)

        transition = DebugStateTransition(
            from_state=self._state,
            to_state=to_state,
            timestamp=datetime.utcnow(),
            reason=reason,
        )

        self._history.append(transition)
        self._state = to_state

        return transition

    def to_dict(self) -> dict:
        """Serialize state machine to dictionary."""
        return {
            "state": self._state.value,
            "is_terminal": self.is_terminal,
            "created_at": self._created_at.isoformat(),
            "history": [
                {
                    "from_state": t.from_state.value,
                    "to_state": t.to_state.value,
                    "timestamp": t.timestamp.isoformat(),
                    "reason": t.reason,
                }
                for t in self._history
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DebugStateMachine":
        """Deserialize state machine from dictionary."""
        machine = cls(initial_state=DebugState(data["state"]))
        machine._created_at = datetime.fromisoformat(data["created_at"])
        machine._history = [
            DebugStateTransition(
                from_state=DebugState(t["from_state"]),
                to_state=DebugState(t["to_state"]),
                timestamp=datetime.fromisoformat(t["timestamp"]),
                reason=t.get("reason"),
            )
            for t in data.get("history", [])
        ]
        return machine
