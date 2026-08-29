"""
Runner State Machine for remote runner lifecycle tracking.

Defines valid state transitions for remote runners connected via WebSocket
and provides a state machine class for tracking runner status.

State flow:

    DISCONNECTED -> CONNECTING -> IDLE -> ASSIGNED -> BUSY -> IDLE
                        |            |         |         |
                        |            |         | ACK     | heartbeat
                        |            |         | timeout | timeout
                        v            v         v         v
                  DISCONNECTED     DEAD      DEAD      DEAD
                                (silent
                                 death)

    Any connected state -> DISCONNECTED (WebSocket closes)
    DEAD -> CONNECTING (runner reconnects)
    DEAD -> DISCONNECTED (socket close observed after death)

Ported from failure_01 (Phase 12.6) with two transitions added per the
salvage audit: IDLE -> DEAD and DEAD -> DISCONNECTED.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# History is bounded: a long-lived runner cycling jobs forever must not grow
# memory without limit. The in-memory ring keeps the last 200 transitions;
# serialization emits only the most recent 50.
HISTORY_MAXLEN = 200
SERIALIZED_HISTORY_LIMIT = 50


class RunnerState(str, Enum):
    """Possible states for a remote runner."""
    DISCONNECTED = "disconnected"  # No WebSocket connection
    CONNECTING = "connecting"      # WebSocket open, registration pending
    IDLE = "idle"                  # Ready to accept jobs
    ASSIGNED = "assigned"          # Job sent, awaiting ACK
    BUSY = "busy"                  # Executing step
    DEAD = "dead"                  # Heartbeat timeout, presumed crashed


# Valid state transitions: from_state -> {valid_to_states}
VALID_TRANSITIONS: dict[RunnerState, set[RunnerState]] = {
    RunnerState.DISCONNECTED: {RunnerState.CONNECTING},
    RunnerState.CONNECTING: {RunnerState.IDLE, RunnerState.DISCONNECTED},
    # IDLE -> DEAD: a silently-dead idle runner (heartbeat timeout with no
    # work assigned) must be markable dead without faking an assignment.
    RunnerState.IDLE: {
        RunnerState.ASSIGNED,
        RunnerState.DISCONNECTED,
        RunnerState.DEAD,
    },
    RunnerState.ASSIGNED: {
        RunnerState.BUSY,
        RunnerState.DEAD,
        RunnerState.DISCONNECTED,
    },
    RunnerState.BUSY: {
        RunnerState.IDLE,
        RunnerState.DEAD,
        RunnerState.DISCONNECTED,
    },
    # DEAD -> DISCONNECTED: the WebSocket close for an already-dead runner
    # arrives after the heartbeat monitor marked it dead; the close handler
    # must not crash on that ordering.
    RunnerState.DEAD: {RunnerState.CONNECTING, RunnerState.DISCONNECTED},
}


class InvalidRunnerTransitionError(ValueError):
    """Raised when an invalid state transition is attempted.

    Subclasses ValueError to match the other state machines (workspace,
    pipeline, step) whose transition_to raises ValueError - callers can
    catch either.
    """

    def __init__(self, from_state: RunnerState, to_state: RunnerState, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        message = f"Invalid runner transition: {from_state.value} -> {to_state.value}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


@dataclass
class RunnerStateTransition:
    """Record of a single state transition."""
    from_state: RunnerState
    to_state: RunnerState
    timestamp: datetime
    reason: str | None = None

    def __repr__(self) -> str:
        return f"Transition({self.from_state.value} -> {self.to_state.value} at {self.timestamp})"


class RunnerStateMachine:
    """
    State machine for tracking remote runner lifecycle.

    Example usage:
        machine = RunnerStateMachine(runner_id="pi-workshop-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.assign_step("step-123")          # -> ASSIGNED
        machine.transition_to(RunnerState.BUSY)  # ACK received
        machine.complete_step()                  # -> IDLE
    """

    def __init__(
        self,
        runner_id: str,
        initial_state: RunnerState = RunnerState.DISCONNECTED,
    ):
        self._runner_id = runner_id
        self._state = initial_state
        self._history: deque[RunnerStateTransition] = deque(maxlen=HISTORY_MAXLEN)
        self._created_at = datetime.utcnow()
        self._last_heartbeat = datetime.utcnow()
        self._current_step_id: str | None = None

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
        """List of retained state transitions (last HISTORY_MAXLEN)."""
        return list(self._history)

    @property
    def last_transition(self) -> RunnerStateTransition | None:
        """Most recent transition, or None if no transitions yet."""
        return self._history[-1] if self._history else None

    @property
    def last_heartbeat(self) -> datetime:
        """Last heartbeat timestamp."""
        return self._last_heartbeat

    @property
    def current_step_id(self) -> str | None:
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

    def can_transition_to(self, to_state: RunnerState) -> bool:
        """Check if transition to target state is valid from current state."""
        valid_targets = VALID_TRANSITIONS.get(self._state, set())
        return to_state in valid_targets

    def transition_to(
        self,
        to_state: RunnerState,
        reason: str | None = None,
    ) -> RunnerStateTransition:
        """
        Transition to a new state.

        Raises:
            InvalidRunnerTransitionError: If transition is not valid
        """
        if not self.can_transition_to(to_state):
            raise InvalidRunnerTransitionError(self._state, to_state)

        transition = RunnerStateTransition(
            from_state=self._state,
            to_state=to_state,
            timestamp=datetime.utcnow(),
            reason=reason,
        )

        self._history.append(transition)
        self._state = to_state

        # Entering IDLE/BUSY implies a live runner just spoke to us.
        if to_state in {RunnerState.IDLE, RunnerState.BUSY}:
            self._last_heartbeat = datetime.utcnow()

        return transition

    def assign_step(self, step_id: str) -> RunnerStateTransition:
        """
        Assign a step to this runner (IDLE -> ASSIGNED, stores the step ID).

        Raises:
            InvalidRunnerTransitionError: If runner is not IDLE
        """
        transition = self.transition_to(
            RunnerState.ASSIGNED,
            reason=f"Assigned step {step_id}",
        )
        self._current_step_id = step_id
        return transition

    def complete_step(self) -> RunnerStateTransition:
        """
        Mark the current step as complete (BUSY -> IDLE, clears the step ID).

        The transition is validated FIRST; the step ID is cleared only after
        it succeeds. A complete_step() racing an ASSIGNED runner (e.g. a
        stale completion message before the ACK) raises and must NOT wipe
        the step ID recovery relies on.

        Raises:
            InvalidRunnerTransitionError: If runner is not BUSY
        """
        step_id = self._current_step_id
        transition = self.transition_to(
            RunnerState.IDLE,
            reason=f"Completed step {step_id}",
        )
        self._current_step_id = None
        return transition

    def update_heartbeat(self) -> None:
        """Update the last heartbeat timestamp."""
        self._last_heartbeat = datetime.utcnow()

    def is_alive(self, timeout_seconds: int = 30) -> bool:
        """Check if runner is alive based on heartbeat recency."""
        elapsed = datetime.utcnow() - self._last_heartbeat
        return elapsed.total_seconds() < timeout_seconds

    def to_dict(self) -> dict:
        """Serialize state machine to dictionary.

        History is truncated to the last SERIALIZED_HISTORY_LIMIT transitions
        (the in-memory ring already caps at HISTORY_MAXLEN).
        """
        recent_history = list(self._history)[-SERIALIZED_HISTORY_LIMIT:]
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
                for t in recent_history
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
        machine._history = deque(
            (
                RunnerStateTransition(
                    from_state=RunnerState(t["from_state"]),
                    to_state=RunnerState(t["to_state"]),
                    timestamp=datetime.fromisoformat(t["timestamp"]),
                    reason=t.get("reason"),
                )
                for t in data.get("history", [])
            ),
            maxlen=HISTORY_MAXLEN,
        )
        return machine
