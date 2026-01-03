"""
Pipeline State Machine for Phase 12.2.

Manages the lifecycle of pipeline runs with proper state tracking,
step completion monitoring, and failure handling.

States:
- PENDING: Created, waiting to start
- PREPARING: Workspace being created, initial setup
- RUNNING: Steps are executing
- COMPLETING: All steps done, cleanup in progress
- COMPLETED: Successfully finished
- FAILED: Step failed or error occurred
- CANCELLED: User cancelled
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any


class PipelineRunState(Enum):
    """Pipeline run lifecycle states."""
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidPipelineTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


@dataclass
class PipelineStateTransition:
    """Record of a state transition."""
    from_state: PipelineRunState
    to_state: PipelineRunState
    timestamp: datetime
    reason: Optional[str] = None


# Valid state transitions
VALID_TRANSITIONS: Dict[PipelineRunState, set[PipelineRunState]] = {
    PipelineRunState.PENDING: {
        PipelineRunState.PREPARING,
        PipelineRunState.CANCELLED,
    },
    PipelineRunState.PREPARING: {
        PipelineRunState.RUNNING,
        PipelineRunState.FAILED,
        PipelineRunState.CANCELLED,
    },
    PipelineRunState.RUNNING: {
        PipelineRunState.COMPLETING,
        PipelineRunState.FAILED,
        PipelineRunState.CANCELLED,
    },
    PipelineRunState.COMPLETING: {
        PipelineRunState.COMPLETED,
        PipelineRunState.FAILED,
    },
    # Terminal states
    PipelineRunState.COMPLETED: set(),
    PipelineRunState.FAILED: set(),
    PipelineRunState.CANCELLED: set(),
}


@dataclass
class PipelineStateMachine:
    """
    State machine managing pipeline run lifecycle.

    Tracks state, step progress, and transition history.
    """
    pipeline_run_id: str
    initial_state: PipelineRunState = PipelineRunState.PENDING
    total_steps: int = 0

    # Internal state
    _state: PipelineRunState = field(init=False)
    _history: List[PipelineStateTransition] = field(default_factory=list, init=False)
    _completed_steps: set[int] = field(default_factory=set, init=False)
    _current_step_index: int = field(default=0, init=False)
    _current_step_name: Optional[str] = field(default=None, init=False)
    _failed_step_index: Optional[int] = field(default=None, init=False)
    _failed_step_name: Optional[str] = field(default=None, init=False)
    _error: Optional[str] = field(default=None, init=False)
    _started_at: Optional[datetime] = field(default=None, init=False)
    _completed_at: Optional[datetime] = field(default=None, init=False)
    _created_at: datetime = field(default_factory=datetime.utcnow, init=False)

    def __post_init__(self):
        self._state = self.initial_state
        # Set started_at if starting in PREPARING or later
        if self.initial_state not in {PipelineRunState.PENDING}:
            self._started_at = datetime.utcnow()
        # Set completed_at if starting in terminal state
        if self.initial_state in {
            PipelineRunState.COMPLETED,
            PipelineRunState.FAILED,
            PipelineRunState.CANCELLED,
        }:
            self._completed_at = datetime.utcnow()

    @property
    def state(self) -> PipelineRunState:
        """Current pipeline run state."""
        return self._state

    @property
    def history(self) -> List[PipelineStateTransition]:
        """List of all state transitions."""
        return self._history.copy()

    @property
    def last_transition(self) -> Optional[PipelineStateTransition]:
        """Most recent transition, or None if no transitions."""
        return self._history[-1] if self._history else None

    @property
    def is_terminal(self) -> bool:
        """True if in terminal state."""
        return self._state in {
            PipelineRunState.COMPLETED,
            PipelineRunState.FAILED,
            PipelineRunState.CANCELLED,
        }

    @property
    def is_running(self) -> bool:
        """True if pipeline is actively running."""
        return self._state == PipelineRunState.RUNNING

    @property
    def can_cancel(self) -> bool:
        """True if pipeline can be cancelled."""
        return self._state in {
            PipelineRunState.PENDING,
            PipelineRunState.PREPARING,
            PipelineRunState.RUNNING,
        }

    @property
    def success(self) -> bool:
        """True if pipeline completed successfully."""
        return self._state == PipelineRunState.COMPLETED

    @property
    def completed_step_count(self) -> int:
        """Number of completed steps."""
        return len(self._completed_steps)

    @property
    def current_step_index(self) -> int:
        """Index of currently executing step."""
        return self._current_step_index

    @property
    def failed_step_index(self) -> Optional[int]:
        """Index of failed step, if any."""
        return self._failed_step_index

    @property
    def failed_step_name(self) -> Optional[str]:
        """Name of failed step, if any."""
        return self._failed_step_name

    @property
    def error(self) -> Optional[str]:
        """Error message if failed."""
        return self._error

    @property
    def started_at(self) -> Optional[datetime]:
        """When pipeline started (transitioned to PREPARING)."""
        return self._started_at

    @property
    def completed_at(self) -> Optional[datetime]:
        """When pipeline reached terminal state."""
        return self._completed_at

    @property
    def duration(self) -> Optional[timedelta]:
        """Duration from start to completion."""
        if self._started_at and self._completed_at:
            return self._completed_at - self._started_at
        return None

    def can_transition(self, target: PipelineRunState) -> bool:
        """Check if transition to target state is valid."""
        if self._state == target:
            return False
        return target in VALID_TRANSITIONS.get(self._state, set())

    def transition(
        self,
        target: PipelineRunState,
        reason: Optional[str] = None,
    ) -> None:
        """
        Transition to a new state.

        Args:
            target: Target state
            reason: Optional reason for the transition

        Raises:
            InvalidPipelineTransitionError: If transition is invalid
        """
        # Check if transition is valid
        if target not in VALID_TRANSITIONS.get(self._state, set()):
            raise InvalidPipelineTransitionError(
                f"Invalid transition from {self._state.value} to {target.value}"
            )

        now = datetime.utcnow()

        # Record transition
        transition = PipelineStateTransition(
            from_state=self._state,
            to_state=target,
            timestamp=now,
            reason=reason,
        )
        self._history.append(transition)

        # Handle state-specific logic
        if target == PipelineRunState.PREPARING and self._started_at is None:
            self._started_at = now

        if target in {
            PipelineRunState.COMPLETED,
            PipelineRunState.FAILED,
            PipelineRunState.CANCELLED,
        }:
            self._completed_at = now

        self._state = target

    def on_step_started(self, step_index: int, step_name: str) -> None:
        """
        Record that a step has started.

        Args:
            step_index: Index of the step
            step_name: Name of the step
        """
        self._current_step_index = step_index
        self._current_step_name = step_name

    def on_step_completed(self, step_index: int, step_name: str) -> None:
        """
        Record that a step has completed successfully.

        Args:
            step_index: Index of the step
            step_name: Name of the step
        """
        self._completed_steps.add(step_index)

        # Check if all steps complete
        if self.total_steps > 0 and len(self._completed_steps) >= self.total_steps:
            self.transition(
                PipelineRunState.COMPLETING,
                reason="All steps completed",
            )

    def on_step_failed(
        self,
        step_index: int,
        step_name: str,
        error: str,
        on_failure: str = "stop",
    ) -> None:
        """
        Record that a step has failed.

        Args:
            step_index: Index of the step
            step_name: Name of the step
            error: Error message
            on_failure: Action to take ("stop" or "next")
        """
        self._failed_step_index = step_index
        self._failed_step_name = step_name
        self._error = f"Step '{step_name}' failed: {error}"

        # Handle based on on_failure setting
        if on_failure == "next":
            # Continue to next step (don't fail pipeline)
            self._completed_steps.add(step_index)  # Mark as "done" for progress
        else:
            # Default: stop pipeline
            self.transition(
                PipelineRunState.FAILED,
                reason=f"Step '{step_name}' failed: {error}",
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state machine to dict."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "state": self._state.value,
            "total_steps": self.total_steps,
            "completed_steps": list(self._completed_steps),
            "current_step_index": self._current_step_index,
            "current_step_name": self._current_step_name,
            "failed_step_index": self._failed_step_index,
            "failed_step_name": self._failed_step_name,
            "error": self._error,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "completed_at": self._completed_at.isoformat() if self._completed_at else None,
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
    def from_dict(cls, data: Dict[str, Any]) -> PipelineStateMachine:
        """Deserialize state machine from dict."""
        machine = cls(
            pipeline_run_id=data["pipeline_run_id"],
            initial_state=PipelineRunState(data["state"]),
            total_steps=data.get("total_steps", 0),
        )
        machine._completed_steps = set(data.get("completed_steps", []))
        machine._current_step_index = data.get("current_step_index", 0)
        machine._current_step_name = data.get("current_step_name")
        machine._failed_step_index = data.get("failed_step_index")
        machine._failed_step_name = data.get("failed_step_name")
        machine._error = data.get("error")

        if data.get("started_at"):
            machine._started_at = datetime.fromisoformat(data["started_at"])
        if data.get("completed_at"):
            machine._completed_at = datetime.fromisoformat(data["completed_at"])
        machine._created_at = datetime.fromisoformat(data["created_at"])

        machine._history = [
            PipelineStateTransition(
                from_state=PipelineRunState(t["from_state"]),
                to_state=PipelineRunState(t["to_state"]),
                timestamp=datetime.fromisoformat(t["timestamp"]),
                reason=t.get("reason"),
            )
            for t in data.get("history", [])
        ]
        return machine
