"""
Unit tests for Workspace State Machine.

These tests define the contract for workspace lifecycle management:
- States: creating, ready, in_use, cleaning, cleaned, failed
- Use count tracking for concurrent step access
- Orphan detection for abandoned workspaces
- Cleanup lifecycle with proper locking

Write these tests BEFORE implementing the workspace state machine.
"""
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timedelta

import pytest

# Tests enabled - workspace state machine implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Workspace Status Enum
# -----------------------------------------------------------------------------

class TestWorkspaceStatusEnum:
    """Tests that verify WorkspaceStatus enum exists with correct values."""

    def test_creating_status_exists(self):
        """CREATING status exists for volume creation in progress."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.CREATING.value == "creating"

    def test_ready_status_exists(self):
        """READY status exists for workspace available for use."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.READY.value == "ready"

    def test_in_use_status_exists(self):
        """IN_USE status exists for workspace being used by steps."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.IN_USE.value == "in_use"

    def test_cleaning_status_exists(self):
        """CLEANING status exists for cleanup in progress."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.CLEANING.value == "cleaning"

    def test_cleaned_status_exists(self):
        """CLEANED status exists for workspace successfully removed."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.CLEANED.value == "cleaned"

    def test_failed_status_exists(self):
        """FAILED status exists for workspace in error state."""
        from app.services.workspace.state_machine import WorkspaceStatus
        assert WorkspaceStatus.FAILED.value == "failed"


# -----------------------------------------------------------------------------
# Contract: Valid State Transitions
# -----------------------------------------------------------------------------

class TestWorkspaceValidTransitions:
    """Tests that verify valid state transitions are allowed."""

    def test_creating_to_ready_on_success(self):
        """Volume created successfully transitions to READY."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        assert sm.can_transition_to(WorkspaceStatus.READY)

        sm.transition_to(WorkspaceStatus.READY)
        assert sm.current_status == WorkspaceStatus.READY

    def test_creating_to_failed_on_error(self):
        """Volume creation failure transitions to FAILED."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        assert sm.can_transition_to(WorkspaceStatus.FAILED)

        sm.transition_to(WorkspaceStatus.FAILED)
        assert sm.current_status == WorkspaceStatus.FAILED

    def test_ready_to_in_use_on_step_start(self):
        """Step starting transitions READY to IN_USE."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert sm.can_transition_to(WorkspaceStatus.IN_USE)

        sm.transition_to(WorkspaceStatus.IN_USE)
        assert sm.current_status == WorkspaceStatus.IN_USE

    def test_in_use_to_ready_on_step_complete(self):
        """Step completing with no other users transitions to READY."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE)
        assert sm.can_transition_to(WorkspaceStatus.READY)

        sm.transition_to(WorkspaceStatus.READY)
        assert sm.current_status == WorkspaceStatus.READY

    def test_ready_to_cleaning_on_cleanup(self):
        """Cleanup request transitions READY to CLEANING."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert sm.can_transition_to(WorkspaceStatus.CLEANING)

        sm.transition_to(WorkspaceStatus.CLEANING)
        assert sm.current_status == WorkspaceStatus.CLEANING

    def test_cleaning_to_cleaned_on_success(self):
        """Cleanup success transitions to CLEANED."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CLEANING)
        assert sm.can_transition_to(WorkspaceStatus.CLEANED)

        sm.transition_to(WorkspaceStatus.CLEANED)
        assert sm.current_status == WorkspaceStatus.CLEANED

    def test_cleaning_to_failed_on_error(self):
        """Cleanup failure transitions to FAILED."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CLEANING)
        assert sm.can_transition_to(WorkspaceStatus.FAILED)

        sm.transition_to(WorkspaceStatus.FAILED)
        assert sm.current_status == WorkspaceStatus.FAILED


# -----------------------------------------------------------------------------
# Contract: Invalid State Transitions
# -----------------------------------------------------------------------------

class TestWorkspaceInvalidTransitions:
    """Tests that verify invalid state transitions are rejected."""

    def test_creating_to_in_use_invalid(self):
        """Cannot go directly from CREATING to IN_USE."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        assert not sm.can_transition_to(WorkspaceStatus.IN_USE)

        with pytest.raises(ValueError, match="Invalid.*transition"):
            sm.transition_to(WorkspaceStatus.IN_USE)

    def test_ready_to_creating_invalid(self):
        """Cannot go back from READY to CREATING."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert not sm.can_transition_to(WorkspaceStatus.CREATING)

    def test_in_use_to_cleaning_invalid(self):
        """Cannot clean while workspace is IN_USE (use_count > 0)."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE)
        assert not sm.can_transition_to(WorkspaceStatus.CLEANING)

    def test_cleaned_is_terminal(self):
        """CLEANED is terminal - cannot transition to anything."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CLEANED)
        assert not sm.can_transition_to(WorkspaceStatus.READY)
        assert not sm.can_transition_to(WorkspaceStatus.CREATING)
        assert not sm.can_transition_to(WorkspaceStatus.IN_USE)

    def test_failed_is_terminal(self):
        """FAILED is terminal (except for cleanup retry)."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.FAILED)
        # Can retry cleanup from failed state
        assert sm.can_transition_to(WorkspaceStatus.CLEANING)
        # But cannot go back to normal flow
        assert not sm.can_transition_to(WorkspaceStatus.READY)
        assert not sm.can_transition_to(WorkspaceStatus.IN_USE)


# -----------------------------------------------------------------------------
# Contract: Use Count Tracking
# -----------------------------------------------------------------------------

class TestWorkspaceUseCount:
    """Tests that verify use count tracking for concurrent access."""

    def test_acquire_increments_use_count(self):
        """acquire() increments use count and transitions to IN_USE."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert sm.use_count == 0

        sm.acquire()
        assert sm.use_count == 1
        assert sm.current_status == WorkspaceStatus.IN_USE

    def test_release_decrements_use_count(self):
        """release() decrements use count."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE, use_count=2)

        sm.release()
        assert sm.use_count == 1
        assert sm.current_status == WorkspaceStatus.IN_USE  # Still in use

    def test_release_to_zero_transitions_to_ready(self):
        """release() to 0 transitions back to READY."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE, use_count=1)

        sm.release()
        assert sm.use_count == 0
        assert sm.current_status == WorkspaceStatus.READY

    def test_multiple_acquires_stack(self):
        """Multiple acquires() increment use count correctly."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)

        sm.acquire()
        sm.acquire()
        sm.acquire()

        assert sm.use_count == 3
        assert sm.current_status == WorkspaceStatus.IN_USE

    def test_release_below_zero_raises(self):
        """release() when use_count is 0 raises error."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert sm.use_count == 0

        with pytest.raises(ValueError, match="[Uu]se count.*cannot.*negative"):
            sm.release()

    def test_cleaning_requires_zero_use_count(self):
        """Cannot transition to CLEANING while use_count > 0."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE, use_count=1)
        assert not sm.can_transition_to(WorkspaceStatus.CLEANING)

        # Must release first
        sm.release()
        assert sm.use_count == 0
        assert sm.current_status == WorkspaceStatus.READY
        assert sm.can_transition_to(WorkspaceStatus.CLEANING)


# -----------------------------------------------------------------------------
# Contract: Terminal States
# -----------------------------------------------------------------------------

class TestWorkspaceTerminalStates:
    """Tests that verify terminal states are handled correctly."""

    def test_cleaned_is_terminal(self):
        """CLEANED is a terminal state."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.CLEANED)
        assert sm.is_terminal()

    def test_ready_is_not_terminal(self):
        """READY is not a terminal state."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.READY)
        assert not sm.is_terminal()

    def test_in_use_is_not_terminal(self):
        """IN_USE is not a terminal state."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.IN_USE)
        assert not sm.is_terminal()

    def test_failed_is_quasi_terminal(self):
        """FAILED allows cleanup retry but is otherwise terminal."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        sm = WorkspaceStateMachine(WorkspaceStatus.FAILED)
        # Not fully terminal - can retry cleanup
        assert not sm.is_terminal()
        # But can only transition to CLEANING
        valid_next = sm.get_valid_next_states()
        assert valid_next == [WorkspaceStatus.CLEANING]


# -----------------------------------------------------------------------------
# Contract: Workspace Volume Naming
# -----------------------------------------------------------------------------

class TestWorkspaceVolumeNaming:
    """Tests that verify workspace volume naming convention."""

    def test_volume_name_format(self):
        """Volume name follows format: lazyaf-ws-{pipeline_run_id}."""
        from app.services.workspace.state_machine import generate_volume_name

        pipeline_run_id = "abc123"
        name = generate_volume_name(pipeline_run_id)
        assert name == "lazyaf-ws-abc123"

    def test_volume_name_with_uuid(self):
        """Volume name works with UUID pipeline run IDs."""
        from app.services.workspace.state_machine import generate_volume_name

        pipeline_run_id = str(uuid4())
        name = generate_volume_name(pipeline_run_id)
        assert name.startswith("lazyaf-ws-")
        assert pipeline_run_id in name

    def test_parse_volume_name(self):
        """Can extract pipeline_run_id from volume name."""
        from app.services.workspace.state_machine import (
            generate_volume_name, parse_volume_name
        )

        pipeline_run_id = "abc123"
        name = generate_volume_name(pipeline_run_id)
        parsed_id = parse_volume_name(name)
        assert parsed_id == pipeline_run_id


# -----------------------------------------------------------------------------
# Contract: Orphan Detection
# -----------------------------------------------------------------------------

class TestWorkspaceOrphanDetection:
    """Tests that verify orphan detection logic."""

    def test_workspace_with_active_pipeline_not_orphaned(self):
        """Workspace linked to running pipeline is not orphaned."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        # Pipeline is running - not orphaned
        assert not is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status="running",
            last_activity=datetime.utcnow()
        )

    def test_workspace_with_completed_pipeline_is_orphaned(self):
        """Workspace linked to completed pipeline is orphaned (should be cleaned)."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        assert is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status="completed",
            last_activity=datetime.utcnow() - timedelta(hours=1)
        )

    def test_workspace_with_failed_pipeline_is_orphaned(self):
        """Workspace linked to failed pipeline is orphaned (should be cleaned)."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        assert is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status="failed",
            last_activity=datetime.utcnow() - timedelta(hours=1)
        )

    def test_workspace_with_no_pipeline_is_orphaned(self):
        """Workspace with no linked pipeline is orphaned."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        assert is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status=None,  # No pipeline
            last_activity=datetime.utcnow() - timedelta(hours=1)
        )

    def test_recently_active_workspace_not_orphaned(self):
        """Workspace with recent activity is not orphaned (grace period)."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        # Even if pipeline is done, recent activity = not orphaned yet
        assert not is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status="completed",
            last_activity=datetime.utcnow(),  # Just now
            grace_period_minutes=5
        )

    def test_stale_workspace_is_orphaned(self):
        """Workspace idle past grace period is orphaned."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus, is_orphaned
        )

        assert is_orphaned(
            workspace_status=WorkspaceStatus.READY,
            pipeline_status="completed",
            last_activity=datetime.utcnow() - timedelta(minutes=10),
            grace_period_minutes=5
        )


# -----------------------------------------------------------------------------
# Contract: Transition Timestamps
# -----------------------------------------------------------------------------

class TestWorkspaceTransitionTimestamps:
    """Tests that verify transitions record timestamps."""

    def test_transition_records_timestamp(self):
        """State transitions record when they occurred."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        before = datetime.utcnow()
        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        sm.transition_to(WorkspaceStatus.READY)
        after = datetime.utcnow()

        history = sm.get_history()
        assert len(history) == 1
        assert history[0]["from"] == WorkspaceStatus.CREATING
        assert history[0]["to"] == WorkspaceStatus.READY
        assert before <= history[0]["timestamp"] <= after

    def test_created_at_tracked(self):
        """Workspace tracks when it was created."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )

        before = datetime.utcnow()
        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        after = datetime.utcnow()

        assert before <= sm.created_at <= after

    def test_last_activity_updated_on_transition(self):
        """last_activity is updated on state transitions."""
        from app.services.workspace.state_machine import (
            WorkspaceStateMachine, WorkspaceStatus
        )
        import time

        sm = WorkspaceStateMachine(WorkspaceStatus.CREATING)
        first_activity = sm.last_activity

        time.sleep(0.01)  # Small delay
        sm.transition_to(WorkspaceStatus.READY)

        assert sm.last_activity > first_activity
