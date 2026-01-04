"""
Runner State Machine for remote runner lifecycle tracking.

Defines valid state transitions for remote runners connected via WebSocket
and provides a state machine class for tracking runner status.

Phase 12.6: RemoteExecutor & Runner State Machine
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class RunnerState(str, Enum):
    """
    Possible states for a remote runner.

    State flow:
        DISCONNECTED -> CONNECTING -> IDLE -> ASSIGNED -> BUSY -> IDLE
                                  \\-> DISCONNECTED (graceful close)
                                           \\-> DEAD (timeout)
        DEAD -> CONNECTING (reconnect)
    """
    DISCONNECTED = "disconnected"  # No WebSocket connection
    CONNECTING = "connecting"      # WebSocket open, registration pending
    IDLE = "idle"                  # Ready to accept jobs
    ASSIGNED = "assigned"          # Job sent, awaiting ACK
    BUSY = "busy"                  # Executing step
    DEAD = "dead"                  # Heartbeat timeout, presumed crashed


# Valid state transitions map: from_state -> set of valid to_states
VALID_TRANSITIONS: dict[RunnerState, set[RunnerState]] = {
    RunnerState.DISCONNECTED: {RunnerState.CONNECTING},
    RunnerState.CONNECTING: {RunnerState.IDLE, RunnerState.DISCONNECTED},
    RunnerState.IDLE: {RunnerState.ASSIGNED, RunnerState.DISCONNECTED},
    RunnerState.ASSIGNED: {RunnerState.BUSY, RunnerState.DEAD, RunnerState.DISCONNECTED},
    RunnerState.BUSY: {RunnerState.IDLE, RunnerState.DEAD, RunnerState.DISCONNECTED},
    RunnerState.DEAD: {RunnerState.CONNECTING},
}


class InvalidRunnerTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, from_state: RunnerState, to_state: RunnerState, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        message = f"Invalid runner transition: {from_state.value} -> {to_state.value}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


@dataclass
class RunnerStateTransition:
    """Record of a state transition."""
    from_state: RunnerState
    to_state: RunnerState
    timestamp: datetime
    reason: Optional[str] = None

    def __repr__(self) -> str:
        return f"Transition({self.from_state.value} -> {self.to_state.value} at {self.timestamp})"


class RunnerStateMachine:
    """
    State machine for tracking remote runner lifecycle.

    Usage:
        machine = RunnerStateMachine(runner_id="pi-workshop-1")
        machine.transition(RunnerState.CONNECTING)
        machine.transition(RunnerState.IDLE)
        machine.assign_step("step-123")  # Transitions to ASSIGNED
        machine.transition(RunnerState.BUSY)  # ACK received
        machine.complete_step()  # Transitions back to IDLE
    """

    def __init__(
        self,
        runner_id: str,
        initial_state: RunnerState = RunnerState.DISCONNECTED,
    ):
        """
        Initialize state machine.

        Args:
            runner_id: Unique identifier for this runner
            initial_state: Starting state (default: DISCONNECTED)
        """
        self._runner_id = runner_id
        self._state = initial_state
        self._history: list[RunnerStateTransition] = []
        self._created_at = datetime.utcnow()
        self._last_heartbeat = datetime.utcnow()
        self._current_step_id: Optional[str] = None

    @property
    def runner_id(self) -> str:
        """Runner ID."""
        return self._runner_id

    @property
    def state(self) -> RunnerState:
        """Current state."""
        return self._state

    @property
    def history(self) -> list[RunnerStateTransition]:
        """List of all state transitions."""
        return list(self._history)

    @property
    def last_transition(self) -> Optional[RunnerStateTransition]:
        """Most recent transition, or None if no transitions yet."""
        return self._history[-1] if self._history else None

    @property
    def last_heartbeat(self) -> datetime:
        """Last heartbeat timestamp."""
        return self._last_heartbeat

    @property
    def current_step_id(self) -> Optional[str]:
        """ID of step currently being executed, or None."""
        return self._current_step_id

    @property
    def is_available(self) -> bool:
        """Check if runner is available to accept jobs."""
        return self._state == RunnerState.IDLE

    @property
    def is_connected(self) -> bool:
        """Check if runner has an active WebSocket connection."""
        return self._state in {
            RunnerState.CONNECTING,
            RunnerState.IDLE,
            RunnerState.ASSIGNED,
            RunnerState.BUSY,
        }

    def can_transition(self, to_state: RunnerState) -> bool:
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
        to_state: RunnerState,
        reason: Optional[str] = None,
    ) -> RunnerStateTransition:
        """
        Transition to a new state.

        Args:
            to_state: Target state
            reason: Optional reason for transition

        Returns:
            The transition record

        Raises:
            InvalidRunnerTransitionError: If transition is not valid
        """
        if not self.can_transition(to_state):
            raise InvalidRunnerTransitionError(self._state, to_state)

        transition = RunnerStateTransition(
            from_state=self._state,
            to_state=to_state,
            timestamp=datetime.utcnow(),
            reason=reason,
        )

        self._history.append(transition)
        self._state = to_state

        # Update heartbeat on certain state entries
        if to_state in {RunnerState.IDLE, RunnerState.BUSY}:
            self._last_heartbeat = datetime.utcnow()

        return transition

    def assign_step(self, step_id: str) -> RunnerStateTransition:
        """
        Assign a step to this runner.

        Transitions from IDLE to ASSIGNED and stores the step ID.

        Args:
            step_id: ID of the step being assigned

        Returns:
            The transition record

        Raises:
            InvalidRunnerTransitionError: If runner is not IDLE
        """
        transition = self.transition(
            RunnerState.ASSIGNED,
            reason=f"Assigned step {step_id}"
        )
        self._current_step_id = step_id
        return transition

    def complete_step(self) -> RunnerStateTransition:
        """
        Mark the current step as complete.

        Transitions from BUSY to IDLE and clears the step ID.

        Returns:
            The transition record

        Raises:
            InvalidRunnerTransitionError: If runner is not BUSY
        """
        step_id = self._current_step_id
        self._current_step_id = None
        return self.transition(
            RunnerState.IDLE,
            reason=f"Completed step {step_id}"
        )

    def update_heartbeat(self) -> None:
        """Update the last heartbeat timestamp."""
        self._last_heartbeat = datetime.utcnow()

    def is_alive(self, timeout_seconds: int = 30) -> bool:
        """
        Check if runner is alive based on heartbeat.

        Args:
            timeout_seconds: Maximum seconds since last heartbeat

        Returns:
            True if runner is alive, False if heartbeat is stale
        """
        elapsed = datetime.utcnow() - self._last_heartbeat
        return elapsed.total_seconds() < timeout_seconds

    def to_dict(self) -> dict:
        """Serialize state machine to dictionary."""
        return {
            "runner_id": self._runner_id,
            "state": self._state.value,
            "created_at": self._created_at.isoformat(),
            "last_heartbeat": self._last_heartbeat.isoformat(),
            "current_step_id": self._current_step_id,
            "is_available": self.is_available,
            "is_connected": self.is_connected,
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
    def from_dict(cls, data: dict) -> "RunnerStateMachine":
        """Deserialize state machine from dictionary."""
        machine = cls(
            runner_id=data["runner_id"],
            initial_state=RunnerState(data["state"]),
        )
        machine._created_at = datetime.fromisoformat(data["created_at"])
        machine._last_heartbeat = datetime.fromisoformat(data["last_heartbeat"])
        machine._current_step_id = data.get("current_step_id")
        machine._history = [
            RunnerStateTransition(
                from_state=RunnerState(t["from_state"]),
                to_state=RunnerState(t["to_state"]),
                timestamp=datetime.fromisoformat(t["timestamp"]),
                reason=t.get("reason"),
            )
            for t in data.get("history", [])
        ]
        return machine
