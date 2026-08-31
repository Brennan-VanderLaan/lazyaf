"""
Workspace State Machine - Phase 12.2

Manages workspace lifecycle with proper state transitions:
- creating: Volume creation in progress
- ready: Workspace available for use
- in_use: Steps actively using workspace (use_count > 0)
- cleaning: Cleanup in progress
- cleaned: Successfully removed (terminal)
- failed: Error state (can retry cleanup)

M13-1: a pipeline run may own MANY workspaces - one per parallel worker,
each an independent checkout. generate_volume_name therefore takes an
optional lane key (see workspace/worker_key.py); the default lane's name is
byte-identical to the one-workspace-per-run name it replaces. use_count
still counts concurrent HOLDERS of one lane's volume (parallel entry points
in the same lane, plus the debug gate's pin), so it stays an integer.
"""
import hashlib
import re
from enum import Enum
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from app.services.workspace.worker_key import DEFAULT_WORKER_KEY


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

VOLUME_PREFIX = "lazyaf-ws-"

#: Pipeline run ids are uuid4 strings, i.e. always this wide. The lane
#: suffix is split off at this offset (see parse_volume_name_parts).
_RUN_ID_LENGTH = 36

#: Ceiling on the lane slug. models/workspace.py caps volume_name at
#: String(100); the budget is 10 (prefix) + 36 (run id) + 1 (separator)
#: + 32 (slug) + 1 + 8 (disambiguating hash) = 88.
_MAX_SLUG = 32

#: Docker volume names must match [a-zA-Z0-9][a-zA-Z0-9_.-]* - the first
#: character alphanumeric, the rest alphanumerics, underscore, dot, hyphen.
#: Everything outside this set is replaced when a lane key is slugged.
_SLUG_SAFE = re.compile(r"[^a-z0-9._-]")
_SLUG_RUNS = re.compile(r"-{2,}")


def _volume_slug(worker_key: str) -> str:
    """A docker-legal, collision-free volume suffix for a lane key.

    Lowercases, replaces every character outside the docker charset with a
    hyphen, collapses runs, and strips leading/trailing separators (the
    first character of the full volume name is always ``l``, but a trailing
    separator would be ugly and a doubled one unreadable).

    A short sha256 prefix is appended IF AND ONLY IF information was lost -
    the slug is empty, differs from the lowercased key, or had to be
    truncated. That is what keeps two different lanes from ever slugging to
    one volume, while leaving the clean cases (``w1``, ``integrate``)
    legible in ``docker volume ls``. It is not a round-trippable encoding:
    the workspaces row is the one source of truth for name -> lane.
    """
    lowered = worker_key.lower()
    cleaned = _SLUG_RUNS.sub("-", _SLUG_SAFE.sub("-", lowered)).strip("-._")
    if cleaned and cleaned == lowered and len(cleaned) <= _MAX_SLUG:
        return cleaned
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:8]
    head = cleaned[:_MAX_SLUG].rstrip("-._")
    return f"{head}-{digest}" if head else digest


def generate_volume_name(pipeline_run_id: str, worker_key: str | None = None) -> str:
    """
    Generate the Docker volume name for one WORKSPACE LANE of a pipeline run.

    Format: lazyaf-ws-{pipeline_run_id}            (the default lane)
            lazyaf-ws-{pipeline_run_id}-{slug}     (any other lane)

    A run may own many workspaces - one per parallel worker - each an
    independent checkout that integrates through git rather than by sharing
    a working tree (M13-1). ``worker_key`` names which one.

    The default lane emits NO suffix, deliberately: that makes this function
    byte-identical to its pre-M13-1 self for every existing caller, so the
    single-worker path keeps the volume it already has (no rename, no
    re-clone, no orphan) and ``worker_key`` can stay a keyword argument that
    nobody is forced to pass.
    """
    if worker_key is None or worker_key == DEFAULT_WORKER_KEY:
        return f"{VOLUME_PREFIX}{pipeline_run_id}"
    return f"{VOLUME_PREFIX}{pipeline_run_id}-{_volume_slug(worker_key)}"


def parse_volume_name(volume_name: str) -> str:
    """
    Extract pipeline_run_id from a volume name.

    Args:
        volume_name: lazyaf-ws-{pipeline_run_id}[-{lane slug}]

    Returns:
        The pipeline_run_id portion.

    DIAGNOSTIC ONLY. Lane slugs are lossy (see _volume_slug), so nothing may
    be built on recovering the lane from a name - the workspaces row is the
    source of truth (R3).
    """
    return parse_volume_name_parts(volume_name)[0]


def parse_volume_name_parts(volume_name: str) -> tuple[str, Optional[str]]:
    """
    Split a volume name into (pipeline_run_id, lane slug or None).

    The default lane has no slug and yields ``None``. DIAGNOSTIC ONLY - see
    parse_volume_name.
    """
    rest = volume_name
    if rest.startswith(VOLUME_PREFIX):
        rest = rest[len(VOLUME_PREFIX):]
    if len(rest) > _RUN_ID_LENGTH and rest[_RUN_ID_LENGTH] == "-":
        return rest[:_RUN_ID_LENGTH], rest[_RUN_ID_LENGTH + 1:]
    return rest, None


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
