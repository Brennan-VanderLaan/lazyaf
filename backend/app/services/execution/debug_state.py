"""Debug session state machine and breakpoint identity - Phase 12.7.

Ported from the abandoned failure_01 branch (the one genuinely shelf-ready
artifact of its 12.7 attempt: dependency-free, mock-free, densely tested).
Two things changed on the way in, and both are load-bearing:

1. **`VALID_TRANSITIONS` gained two edges into `PENDING`.**
   failure_01's `resume` drove the session to `ENDED`, which is precisely
   why multi-breakpoint debugging never worked there: the second breakpoint
   had no live session left to pause into. `PENDING` means "the debug run is
   executing and is NOT at a breakpoint" - it is the state a resumed session
   returns to, so `WAITING_AT_BP -> PENDING` (resume without ever attaching)
   and `CONNECTED -> PENDING` (resume after attaching) are both legal. Only
   `abort`, `timeout` and run completion reach a terminal state.

2. **`debug_step_key` lives here** rather than in a fourth place. It is the
   ONE definition of what a breakpoint names, imported by the executor gate,
   the create-endpoint validator and the API responses (contract C2).

This module imports nothing from `app` on purpose: the model, the service,
the router and the tests all depend on it, and none of them may depend on
each other through it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class DebugState(str, Enum):
    """
    Possible states for a debug session.

    State flow::

        PENDING <----------------+
           |                     |
           v                     | resume
        WAITING_AT_BP -> CONNECTED
           |                |
           v                v
        TIMEOUT / ENDED   TIMEOUT / ENDED

    - PENDING: debug run executing, NOT at a breakpoint
    - WAITING_AT_BP: paused at a breakpoint, nobody attached
    - CONNECTED: a terminal is attached
    - TIMEOUT: the pause deadline passed (terminal)
    - ENDED: aborted, or the pipeline run finished (terminal)
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
    DebugState.WAITING_AT_BP: {
        DebugState.CONNECTED,
        DebugState.TIMEOUT,
        DebugState.ENDED,
        # 12.7: resume WITHOUT ever attaching a terminal.
        DebugState.PENDING,
    },
    DebugState.CONNECTED: {
        DebugState.ENDED,
        DebugState.TIMEOUT,
        DebugState.WAITING_AT_BP,  # terminal detached, may reconnect
        # 12.7: resume after attaching. THE multi-breakpoint fix.
        DebugState.PENDING,
    },
    DebugState.TIMEOUT: set(),  # Terminal - no transitions
    DebugState.ENDED: set(),    # Terminal - no transitions
}


#: Prefix for the key of a row that is NOT a graph step. See
#: `debug_step_key` - it exists so a non-step row can never be handed the
#: identity of a real one, whatever ids the pipeline author chose.
NON_STEP_KEY_PREFIX = "!not-a-step:"


def debug_step_key(step_run) -> str:
    """The breakpoint identity of a step (contract C2).

    **A step IS its `step_id`.** ONE function, so the gate, the
    create-endpoint validator and the UI checkbox list cannot drift into
    three different notions of "which step is breakpointed" - a drift whose
    only symptom would be a breakpoint that silently never fires.

    12.8: the v1 array is retired, so an INDEX is no longer an address.
    Every step a run dispatches comes from the graph and carries a
    `step_id`; the index fallback below did not disappear, it NARROWED, and
    what it now covers is the two bookkeeping rows a graph run writes that
    are not steps at all:

    * `_verify_graph_coverage`'s defect row - `step_id=None`,
      `step_name="pipeline graph"`, `step_index=len(steps)`. It is a verdict
      about the graph, not a step, and it is written terminal at completion.
    * `_spawn_fix_card`'s `trigger:` marker, which carries **a real step's
      `step_index`** on purpose (that is how the websocket and the state
      machine address it) and deliberately no `step_id`, so that
      `_latest_step_run_for` and `_graph_step_outcomes` can never mistake it
      for the step that spawned it.

    Neither is dispatchable and neither can be named by a breakpoint:
    `resolve_step_keys` offers graph step ids and nothing else. So the
    fallback does not address them by index - `str(step_index)` would hand
    the marker the identity of a real step, because `array_to_graph` honours
    author-supplied ids since 12.8 and a step may legally be called `"2"`.
    The prefix puts these rows outside the breakpoint vocabulary by
    construction, which is the marker's identity rule: **a row with no
    `step_id` is not a step, and nothing can breakpoint it.**

    Total on purpose rather than raising: the gate calls this on whatever row
    it was handed, and the right answer for a row no breakpoint can name is
    "resume", not an exception that fails the run it was asked to debug.

    Accepts anything with `.step_id` / `.step_index` (a StepRun row, or a
    step-definition shim built from a pipeline's graph at validation time).
    """
    step_id = getattr(step_run, "step_id", None)
    if step_id:
        return str(step_id)
    return f"{NON_STEP_KEY_PREFIX}{getattr(step_run, 'step_index', 0)}"


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

    Usage::

        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit")
        machine.transition(DebugState.CONNECTED, reason="CLI connected")
        machine.transition(DebugState.PENDING, reason="User resumed")
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


__all__ = [
    "DebugState",
    "DebugStateMachine",
    "DebugStateTransition",
    "InvalidDebugTransitionError",
    "NON_STEP_KEY_PREFIX",
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "debug_step_key",
]
