"""
Workspace State Machine for Phase 12.2.

Manages the lifecycle of pipeline workspaces (Docker volumes containing
git checkout, home directory, and control files).

States:
- CREATING: Volume is being created/cloned
- READY: Volume created, available for use
- IN_USE: One or more steps actively using workspace
- CLEANING: Cleanup in progress
- CLEANED: Resources released, can be deleted
- FAILED: Creation or cleanup failed
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class WorkspaceState(Enum):
    """Workspace lifecycle states."""
    CREATING = "creating"
    READY = "ready"
    IN_USE = "in_use"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    FAILED = "failed"


class InvalidWorkspaceTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


@dataclass
class WorkspaceStateTransition:
    """Record of a state transition."""
    from_state: WorkspaceState
    to_state: WorkspaceState
    timestamp: datetime
    reason: Optional[str] = None


# Valid state transitions
VALID_TRANSITIONS: dict[WorkspaceState, set[WorkspaceState]] = {
    WorkspaceState.CREATING: {WorkspaceState.READY, WorkspaceState.FAILED},
    WorkspaceState.READY: {WorkspaceState.IN_USE, WorkspaceState.CLEANING},
    WorkspaceState.IN_USE: {WorkspaceState.READY, WorkspaceState.IN_USE},  # IN_USE -> IN_USE for use_count changes
    WorkspaceState.CLEANING: {WorkspaceState.CLEANED, WorkspaceState.FAILED},
    WorkspaceState.CLEANED: set(),  # Terminal state
    WorkspaceState.FAILED: {WorkspaceState.CLEANING},  # Can retry cleanup
}


@dataclass
class WorkspaceStateMachine:
    """
    State machine managing workspace lifecycle.

    Tracks state, use count for concurrent access, and transition history.
    """
    workspace_id: str
    initial_state: WorkspaceState = WorkspaceState.CREATING
    use_count: int = 0

    # Internal state
    _state: WorkspaceState = field(init=False)
    _history: list[WorkspaceStateTransition] = field(default_factory=list, init=False)
    _last_activity: datetime = field(default_factory=datetime.utcnow, init=False)
    _created_at: datetime = field(default_factory=datetime.utcnow, init=False)

    def __post_init__(self):
        self._state = self.initial_state
        # If starting IN_USE, use_count should be at least 1
        if self.initial_state == WorkspaceState.IN_USE and self.use_count == 0:
            self.use_count = 1

    @property
    def state(self) -> WorkspaceState:
        """Current workspace state."""
        return self._state

    @property
    def history(self) -> list[WorkspaceStateTransition]:
        """List of all state transitions."""
        return self._history.copy()

    @property
    def last_transition(self) -> Optional[WorkspaceStateTransition]:
        """Most recent transition, or None if no transitions."""
        return self._history[-1] if self._history else None

    @property
    def is_terminal(self) -> bool:
        """True if in terminal state (CLEANED)."""
        return self._state == WorkspaceState.CLEANED

    def can_transition(self, target: WorkspaceState) -> bool:
        """Check if transition to target state is valid."""
        if self._state == target:
            return False
        return target in VALID_TRANSITIONS.get(self._state, set())

    def transition(self, target: WorkspaceState, reason: Optional[str] = None) -> None:
        """
        Transition to a new state.

        Args:
            target: Target state
            reason: Optional reason for the transition

        Raises:
            InvalidWorkspaceTransitionError: If transition is invalid
        """
        # Special case: CLEANING requires use_count == 0
        if target == WorkspaceState.CLEANING and self.use_count > 0:
            raise InvalidWorkspaceTransitionError(
                f"Cannot transition to CLEANING while use_count={self.use_count} > 0"
            )

        # Check if transition is valid
        if target not in VALID_TRANSITIONS.get(self._state, set()):
            raise InvalidWorkspaceTransitionError(
                f"Invalid transition from {self._state.value} to {target.value}"
            )

        # Record transition
        transition = WorkspaceStateTransition(
            from_state=self._state,
            to_state=target,
            timestamp=datetime.utcnow(),
            reason=reason,
        )
        self._history.append(transition)
        self._state = target
        self._last_activity = datetime.utcnow()

    def acquire(self) -> None:
        """
        Acquire the workspace for step execution.

        Increments use_count and transitions to IN_USE if needed.

        Raises:
            InvalidWorkspaceTransitionError: If workspace cannot be acquired
        """
        if not self.can_acquire():
            raise InvalidWorkspaceTransitionError(
                f"Cannot acquire workspace in state {self._state.value}"
            )

        self.use_count += 1
        self._last_activity = datetime.utcnow()

        # Transition to IN_USE if not already
        if self._state == WorkspaceState.READY:
            transition = WorkspaceStateTransition(
                from_state=self._state,
                to_state=WorkspaceState.IN_USE,
                timestamp=datetime.utcnow(),
                reason=f"Acquired (use_count={self.use_count})",
            )
            self._history.append(transition)
            self._state = WorkspaceState.IN_USE

    def release(self) -> None:
        """
        Release the workspace after step completion.

        Decrements use_count and transitions to READY if use_count reaches 0.

        Raises:
            InvalidWorkspaceTransitionError: If use_count is already 0
        """
        if self.use_count <= 0:
            raise InvalidWorkspaceTransitionError(
                "Cannot release workspace with use_count=0"
            )

        self.use_count -= 1
        self._last_activity = datetime.utcnow()

        # Transition to READY if no more users
        if self.use_count == 0 and self._state == WorkspaceState.IN_USE:
            transition = WorkspaceStateTransition(
                from_state=self._state,
                to_state=WorkspaceState.READY,
                timestamp=datetime.utcnow(),
                reason="Released (use_count=0)",
            )
            self._history.append(transition)
            self._state = WorkspaceState.READY

    def can_acquire(self) -> bool:
        """Check if workspace can be acquired."""
        return self._state in {WorkspaceState.READY, WorkspaceState.IN_USE}

    def can_cleanup(self) -> bool:
        """Check if workspace can be cleaned up."""
        return self._state == WorkspaceState.READY and self.use_count == 0

    def is_orphaned(self, threshold: timedelta) -> bool:
        """
        Check if workspace is orphaned (inactive beyond threshold).

        Workspaces in IN_USE or CLEANED states are never considered orphaned.

        Args:
            threshold: Time since last activity to consider orphaned

        Returns:
            True if workspace appears orphaned
        """
        # Active workspaces are never orphaned
        if self._state in {WorkspaceState.IN_USE, WorkspaceState.CLEANED}:
            return False

        # Check activity threshold
        elapsed = datetime.utcnow() - self._last_activity
        return elapsed > threshold

    def to_dict(self) -> dict:
        """Serialize state machine to dict."""
        return {
            "workspace_id": self.workspace_id,
            "state": self._state.value,
            "use_count": self.use_count,
            "last_activity": self._last_activity.isoformat(),
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
    def from_dict(cls, data: dict) -> WorkspaceStateMachine:
        """Deserialize state machine from dict."""
        machine = cls(
            workspace_id=data["workspace_id"],
            initial_state=WorkspaceState(data["state"]),
            use_count=data.get("use_count", 0),
        )
        machine._last_activity = datetime.fromisoformat(data["last_activity"])
        machine._created_at = datetime.fromisoformat(data["created_at"])
        machine._history = [
            WorkspaceStateTransition(
                from_state=WorkspaceState(t["from_state"]),
                to_state=WorkspaceState(t["to_state"]),
                timestamp=datetime.fromisoformat(t["timestamp"]),
                reason=t.get("reason"),
            )
            for t in data.get("history", [])
        ]
        return machine
