"""
Tests for Workspace State Machine (Phase 12.2).

These tests DEFINE the workspace lifecycle contract.
Write tests first, then implement to make them pass.

Workspace States:
- creating: Volume is being created/cloned
- ready: Volume created, available for use
- in_use: One or more steps actively using workspace
- cleaning: Cleanup in progress
- cleaned: Resources released, can be deleted
- failed: Creation or cleanup failed

Valid Transitions:
- creating -> ready (volume created successfully)
- creating -> failed (volume creation failed)
- ready -> in_use (step starts using workspace)
- in_use -> ready (last step finishes, use_count=0)
- in_use -> in_use (concurrent steps, use_count changes)
- ready -> cleaning (pipeline complete, cleanup starts)
- cleaning -> cleaned (cleanup successful)
- cleaning -> failed (cleanup failed)
- failed -> cleaning (retry cleanup)
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from enum import Enum

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.workspace_state import (
        WorkspaceState,
        WorkspaceStateMachine,
        InvalidWorkspaceTransitionError,
        WorkspaceStateTransition,
    )
    WORKSPACE_MODULE_AVAILABLE = True
except ImportError:
    WORKSPACE_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    WorkspaceState = Enum("WorkspaceState", [
        "CREATING", "READY", "IN_USE", "CLEANING", "CLEANED", "FAILED"
    ])
    WorkspaceStateMachine = None
    InvalidWorkspaceTransitionError = Exception
    WorkspaceStateTransition = None


pytestmark = pytest.mark.skipif(
    not WORKSPACE_MODULE_AVAILABLE,
    reason="workspace module not yet implemented"
)


class TestWorkspaceStates:
    """Tests for WorkspaceState enum values."""

    def test_has_creating_state(self):
        """WorkspaceState has CREATING state."""
        assert WorkspaceState.CREATING is not None
        assert WorkspaceState.CREATING.value == "creating"

    def test_has_ready_state(self):
        """WorkspaceState has READY state."""
        assert WorkspaceState.READY is not None
        assert WorkspaceState.READY.value == "ready"

    def test_has_in_use_state(self):
        """WorkspaceState has IN_USE state."""
        assert WorkspaceState.IN_USE is not None
        assert WorkspaceState.IN_USE.value == "in_use"

    def test_has_cleaning_state(self):
        """WorkspaceState has CLEANING state."""
        assert WorkspaceState.CLEANING is not None
        assert WorkspaceState.CLEANING.value == "cleaning"

    def test_has_cleaned_state(self):
        """WorkspaceState has CLEANED state."""
        assert WorkspaceState.CLEANED is not None
        assert WorkspaceState.CLEANED.value == "cleaned"

    def test_has_failed_state(self):
        """WorkspaceState has FAILED state."""
        assert WorkspaceState.FAILED is not None
        assert WorkspaceState.FAILED.value == "failed"


class TestCreatingTransitions:
    """Tests for transitions from CREATING state."""

    @pytest.fixture
    def machine(self):
        """Create a workspace state machine in CREATING state."""
        return WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CREATING,
        )

    def test_creating_to_ready_on_success(self, machine):
        """CREATING -> READY when volume created successfully."""
        machine.transition(WorkspaceState.READY)
        assert machine.state == WorkspaceState.READY

    def test_creating_to_failed_on_error(self, machine):
        """CREATING -> FAILED when volume creation fails."""
        machine.transition(WorkspaceState.FAILED, reason="Failed to create volume")
        assert machine.state == WorkspaceState.FAILED

    def test_creating_to_in_use_invalid(self, machine):
        """CREATING -> IN_USE is invalid (must go through READY)."""
        with pytest.raises(InvalidWorkspaceTransitionError):
            machine.transition(WorkspaceState.IN_USE)

    def test_creating_to_cleaning_invalid(self, machine):
        """CREATING -> CLEANING is invalid."""
        with pytest.raises(InvalidWorkspaceTransitionError):
            machine.transition(WorkspaceState.CLEANING)


class TestReadyTransitions:
    """Tests for transitions from READY state."""

    @pytest.fixture
    def machine(self):
        """Create a workspace state machine in READY state."""
        return WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )

    def test_ready_to_in_use_increments_count(self, machine):
        """READY -> IN_USE increments use_count."""
        assert machine.use_count == 0
        machine.acquire()  # Helper method to increment and transition
        assert machine.state == WorkspaceState.IN_USE
        assert machine.use_count == 1

    def test_ready_to_cleaning_valid(self, machine):
        """READY -> CLEANING is valid (cleanup starts)."""
        machine.transition(WorkspaceState.CLEANING)
        assert machine.state == WorkspaceState.CLEANING

    def test_ready_to_creating_invalid(self, machine):
        """READY -> CREATING is invalid."""
        with pytest.raises(InvalidWorkspaceTransitionError):
            machine.transition(WorkspaceState.CREATING)


class TestInUseTransitions:
    """Tests for transitions from IN_USE state."""

    @pytest.fixture
    def machine(self):
        """Create a workspace state machine in IN_USE state with use_count=1."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        machine.acquire()  # Now IN_USE with use_count=1
        return machine

    def test_in_use_to_ready_decrements_count(self, machine):
        """IN_USE -> READY when use_count decrements to 0."""
        assert machine.use_count == 1
        machine.release()  # Helper method to decrement
        assert machine.use_count == 0
        assert machine.state == WorkspaceState.READY

    def test_in_use_stays_in_use_with_multiple_users(self, machine):
        """IN_USE stays IN_USE when use_count > 0 after release."""
        machine.acquire()  # use_count = 2
        assert machine.use_count == 2
        machine.release()  # use_count = 1
        assert machine.use_count == 1
        assert machine.state == WorkspaceState.IN_USE

    def test_acquire_increments_count(self, machine):
        """Acquiring workspace increments use_count."""
        initial_count = machine.use_count
        machine.acquire()
        assert machine.use_count == initial_count + 1

    def test_in_use_to_cleaning_invalid_when_count_nonzero(self, machine):
        """IN_USE -> CLEANING is invalid when use_count > 0."""
        assert machine.use_count > 0
        with pytest.raises(InvalidWorkspaceTransitionError):
            machine.transition(WorkspaceState.CLEANING)


class TestCleaningRequiresZeroUseCount:
    """Tests for cleaning prerequisite."""

    def test_cleaning_requires_zero_use_count(self):
        """Can't transition to CLEANING while use_count > 0."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.IN_USE,
            use_count=2,
        )

        with pytest.raises(InvalidWorkspaceTransitionError) as exc_info:
            machine.transition(WorkspaceState.CLEANING)

        assert "use_count" in str(exc_info.value).lower()

    def test_cleaning_allowed_when_zero_use_count(self):
        """Can transition to CLEANING when use_count = 0."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        assert machine.use_count == 0

        machine.transition(WorkspaceState.CLEANING)
        assert machine.state == WorkspaceState.CLEANING


class TestCleaningTransitions:
    """Tests for transitions from CLEANING state."""

    @pytest.fixture
    def machine(self):
        """Create a workspace state machine in CLEANING state."""
        return WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CLEANING,
        )

    def test_cleaning_to_cleaned_on_success(self, machine):
        """CLEANING -> CLEANED when cleanup succeeds."""
        machine.transition(WorkspaceState.CLEANED)
        assert machine.state == WorkspaceState.CLEANED

    def test_cleaning_to_failed_on_error(self, machine):
        """CLEANING -> FAILED when cleanup fails."""
        machine.transition(WorkspaceState.FAILED, reason="Cleanup failed")
        assert machine.state == WorkspaceState.FAILED


class TestFailedTransitions:
    """Tests for transitions from FAILED state."""

    @pytest.fixture
    def machine(self):
        """Create a workspace state machine in FAILED state."""
        return WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.FAILED,
        )

    def test_failed_to_cleaning_valid(self, machine):
        """FAILED -> CLEANING is valid (retry cleanup)."""
        machine.transition(WorkspaceState.CLEANING)
        assert machine.state == WorkspaceState.CLEANING

    def test_failed_to_ready_invalid(self, machine):
        """FAILED -> READY is invalid."""
        with pytest.raises(InvalidWorkspaceTransitionError):
            machine.transition(WorkspaceState.READY)


class TestCleanedIsTerminal:
    """Tests for CLEANED terminal state."""

    def test_cleaned_is_terminal(self):
        """No transitions allowed from CLEANED state."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CLEANED,
        )

        for target_state in WorkspaceState:
            if target_state != WorkspaceState.CLEANED:
                with pytest.raises(InvalidWorkspaceTransitionError):
                    machine.transition(target_state)

    def test_is_terminal_property(self):
        """is_terminal returns True for CLEANED."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CLEANED,
        )
        assert machine.is_terminal is True


class TestOrphanDetection:
    """Tests for orphaned workspace detection."""

    def test_orphaned_detection_no_activity(self):
        """Workspace with no activity for threshold is orphaned."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        # Simulate old last_activity
        machine._last_activity = datetime.utcnow() - timedelta(hours=2)

        assert machine.is_orphaned(threshold=timedelta(hours=1)) is True

    def test_not_orphaned_with_recent_activity(self):
        """Workspace with recent activity is not orphaned."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        # Recent activity
        machine._last_activity = datetime.utcnow()

        assert machine.is_orphaned(threshold=timedelta(hours=1)) is False

    def test_in_use_not_orphaned(self):
        """Workspace in IN_USE state is never orphaned."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.IN_USE,
            use_count=1,
        )
        machine._last_activity = datetime.utcnow() - timedelta(hours=24)

        # Even with old activity, IN_USE is not orphaned
        assert machine.is_orphaned(threshold=timedelta(hours=1)) is False

    def test_cleaned_not_orphaned(self):
        """Workspace in CLEANED state is not orphaned (already cleaned)."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CLEANED,
        )
        assert machine.is_orphaned(threshold=timedelta(hours=1)) is False


class TestTransitionMetadata:
    """Tests for transition metadata recording."""

    @pytest.fixture
    def machine(self):
        """Create a new workspace state machine."""
        return WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CREATING,
        )

    def test_transition_records_timestamp(self, machine):
        """State changes record timestamp."""
        before = datetime.utcnow()
        machine.transition(WorkspaceState.READY)
        after = datetime.utcnow()

        last_transition = machine.last_transition
        assert last_transition is not None
        assert before <= last_transition.timestamp <= after

    def test_transition_records_from_state(self, machine):
        """Transitions record the source state."""
        machine.transition(WorkspaceState.READY)

        last_transition = machine.last_transition
        assert last_transition.from_state == WorkspaceState.CREATING

    def test_transition_records_to_state(self, machine):
        """Transitions record the target state."""
        machine.transition(WorkspaceState.READY)

        last_transition = machine.last_transition
        assert last_transition.to_state == WorkspaceState.READY

    def test_transition_records_reason(self, machine):
        """Transitions can include a reason."""
        machine.transition(WorkspaceState.FAILED, reason="Volume creation failed")

        last_transition = machine.last_transition
        assert last_transition.reason == "Volume creation failed"

    def test_transition_history_preserved(self, machine):
        """All transitions are preserved in history."""
        machine.transition(WorkspaceState.READY)
        machine.acquire()
        machine.release()
        machine.transition(WorkspaceState.CLEANING)
        machine.transition(WorkspaceState.CLEANED)

        assert len(machine.history) >= 4


class TestHelperMethods:
    """Tests for workspace state machine helper methods."""

    def test_can_acquire_when_ready(self):
        """can_acquire() returns True when READY."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        assert machine.can_acquire() is True

    def test_can_acquire_when_in_use(self):
        """can_acquire() returns True when IN_USE (concurrent access)."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.IN_USE,
            use_count=1,
        )
        assert machine.can_acquire() is True

    def test_cannot_acquire_when_creating(self):
        """can_acquire() returns False when CREATING."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CREATING,
        )
        assert machine.can_acquire() is False

    def test_cannot_acquire_when_cleaning(self):
        """can_acquire() returns False when CLEANING."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.CLEANING,
        )
        assert machine.can_acquire() is False

    def test_can_cleanup_when_ready(self):
        """can_cleanup() returns True when READY with use_count=0."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.READY,
        )
        assert machine.can_cleanup() is True

    def test_cannot_cleanup_when_in_use(self):
        """can_cleanup() returns False when IN_USE."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-test-123",
            initial_state=WorkspaceState.IN_USE,
            use_count=1,
        )
        assert machine.can_cleanup() is False


class TestWorkspaceId:
    """Tests for workspace ID format."""

    def test_workspace_id_stored(self):
        """Workspace ID is stored in machine."""
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-run-123",
            initial_state=WorkspaceState.CREATING,
        )
        assert machine.workspace_id == "lazyaf-ws-run-123"

    def test_workspace_id_format(self):
        """Workspace ID follows expected format."""
        workspace_id = "lazyaf-ws-run-abc123"
        machine = WorkspaceStateMachine(
            workspace_id=workspace_id,
            initial_state=WorkspaceState.CREATING,
        )
        assert machine.workspace_id.startswith("lazyaf-ws-")
