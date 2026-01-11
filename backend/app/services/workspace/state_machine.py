"""
Workspace State Machine - Phase 12.2

Manages workspace lifecycle with proper state transitions:
- creating: Volume creation in progress
- ready: Workspace available for use
- in_use: Steps actively using workspace (use_count > 0)
- cleaning: Cleanup in progress
- cleaned: Successfully removed (terminal)
- failed: Error state (can retry cleanup)
"""
from enum import Enum
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional


class WorkspaceStatus(str, Enum):
    """Workspace lifecycle states."""
    CREATING = "creating"
    READY = "ready"
    IN_USE = "in_use"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    FAILED = "failed"


# Valid state transitions
VALID_TRANSITIONS: Dict[WorkspaceStatus, List[WorkspaceStatus]] = {
    WorkspaceStatus.CREATING: [WorkspaceStatus.READY, WorkspaceStatus.FAILED],
    WorkspaceStatus.READY: [WorkspaceStatus.IN_USE, WorkspaceStatus.CLEANING],
    WorkspaceStatus.IN_USE: [WorkspaceStatus.READY],  # Only when use_count drops to 0
    WorkspaceStatus.CLEANING: [WorkspaceStatus.CLEANED, WorkspaceStatus.FAILED],
    WorkspaceStatus.CLEANED: [],  # Terminal
    WorkspaceStatus.FAILED: [WorkspaceStatus.CLEANING],  # Can retry cleanup
}

# Terminal states (no further transitions except cleanup retry from FAILED)
TERMINAL_STATES = {WorkspaceStatus.CLEANED}


class WorkspaceStateMachine:
    """
    State machine for workspace lifecycle management.

    Tracks:
    - Current status and valid transitions
    - Use count for concurrent step access
    - Transition history with timestamps
    """

    def __init__(
        self,
        initial_status: WorkspaceStatus,
        use_count: int = 0,
    ):
        self._status = initial_status
        self._use_count = use_count
        self._history: List[Dict[str, Any]] = []
        self._created_at = datetime.utcnow()
        self._last_activity = datetime.utcnow()

    @property
    def current_status(self) -> WorkspaceStatus:
        """Get current workspace status."""
        return self._status

    @property
    def use_count(self) -> int:
        """Get current use count (number of active steps)."""
        return self._use_count

    @property
    def created_at(self) -> datetime:
        """Get workspace creation timestamp."""
        return self._created_at

    @property
    def last_activity(self) -> datetime:
        """Get last activity timestamp."""
        return self._last_activity

    def can_transition_to(self, new_status: WorkspaceStatus) -> bool:
        """Check if transition to new_status is valid."""
        # Special case: can't clean while in use
        if new_status == WorkspaceStatus.CLEANING and self._use_count > 0:
            return False

        valid_next = VALID_TRANSITIONS.get(self._status, [])
        return new_status in valid_next

    def transition_to(self, new_status: WorkspaceStatus) -> None:
        """
        Transition to a new status.

        Raises:
            ValueError: If transition is invalid
        """
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Invalid workspace transition: {self._status.value} -> {new_status.value}"
            )

        old_status = self._status
        self._status = new_status
        self._last_activity = datetime.utcnow()

        self._history.append({
            "from": old_status,
            "to": new_status,
            "timestamp": self._last_activity,
        })

    def acquire(self) -> None:
        """
        Acquire workspace for step execution.
        Increments use count and transitions to IN_USE if needed.
        """
        self._use_count += 1
        self._last_activity = datetime.utcnow()

        # Transition to IN_USE if we were READY
        if self._status == WorkspaceStatus.READY:
            self._status = WorkspaceStatus.IN_USE
            self._history.append({
                "from": WorkspaceStatus.READY,
                "to": WorkspaceStatus.IN_USE,
                "timestamp": self._last_activity,
            })

    def release(self) -> None:
        """
        Release workspace after step completion.
        Decrements use count and transitions to READY if count hits 0.

        Raises:
            ValueError: If use_count would go negative
        """
        if self._use_count <= 0:
            raise ValueError("Use count cannot go negative")

        self._use_count -= 1
        self._last_activity = datetime.utcnow()

        # Transition back to READY if no more users
        if self._use_count == 0 and self._status == WorkspaceStatus.IN_USE:
            self._status = WorkspaceStatus.READY
            self._history.append({
                "from": WorkspaceStatus.IN_USE,
                "to": WorkspaceStatus.READY,
                "timestamp": self._last_activity,
            })

    def is_terminal(self) -> bool:
        """Check if workspace is in a terminal state."""
        return self._status in TERMINAL_STATES

    def get_valid_next_states(self) -> List[WorkspaceStatus]:
        """Get list of valid next states from current state."""
        valid = VALID_TRANSITIONS.get(self._status, [])
        # Filter out CLEANING if use_count > 0
        if self._use_count > 0:
            valid = [s for s in valid if s != WorkspaceStatus.CLEANING]
        return valid

    def get_history(self) -> List[Dict[str, Any]]:
        """Get transition history."""
        return self._history.copy()


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

def generate_volume_name(pipeline_run_id: str) -> str:
    """
    Generate Docker volume name for a pipeline run.

    Format: lazyaf-ws-{pipeline_run_id}
    """
    return f"lazyaf-ws-{pipeline_run_id}"


def parse_volume_name(volume_name: str) -> str:
    """
    Extract pipeline_run_id from volume name.

    Args:
        volume_name: Volume name in format lazyaf-ws-{pipeline_run_id}

    Returns:
        The pipeline_run_id portion
    """
    prefix = "lazyaf-ws-"
    if volume_name.startswith(prefix):
        return volume_name[len(prefix):]
    return volume_name


def is_orphaned(
    workspace_status: WorkspaceStatus,
    pipeline_status: Optional[str],
    last_activity: datetime,
    grace_period_minutes: int = 5,
) -> bool:
    """
    Check if a workspace is orphaned and should be cleaned up.

    A workspace is orphaned if:
    - Pipeline is completed/failed/cancelled AND grace period has passed
    - No pipeline is linked (pipeline_status is None) AND grace period has passed

    Args:
        workspace_status: Current workspace status
        pipeline_status: Status of linked pipeline (None if no pipeline)
        last_activity: When workspace was last used
        grace_period_minutes: Minutes to wait after pipeline completion

    Returns:
        True if workspace should be cleaned up
    """
    # Already cleaning or cleaned - not orphaned
    if workspace_status in (WorkspaceStatus.CLEANING, WorkspaceStatus.CLEANED):
        return False

    # Pipeline still running - not orphaned
    if pipeline_status in ("pending", "preparing", "running", "completing"):
        return False

    # Check grace period
    grace_period = timedelta(minutes=grace_period_minutes)
    time_since_activity = datetime.utcnow() - last_activity

    if time_since_activity < grace_period:
        return False

    # Pipeline is done (completed/failed/cancelled) or missing - orphaned
    return True
