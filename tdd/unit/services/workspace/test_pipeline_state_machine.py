"""
Unit tests for Pipeline State Machine.

These tests define the contract for pipeline lifecycle management:
- States: pending, preparing, running, completing, completed, failed, cancelled
- Workspace integration (preparing = workspace creation)
- Step failure propagation
- Proper cleanup on completion

Write these tests BEFORE implementing the pipeline state machine.
"""
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime

import pytest

# Tests enabled - pipeline state machine implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Pipeline Status Enum
# -----------------------------------------------------------------------------

class TestPipelineStatusEnum:
    """Tests that verify PipelineStatus enum exists with correct values."""

    def test_pending_status_exists(self):
        """PENDING status exists for pipeline awaiting start."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.PENDING.value == "pending"

    def test_preparing_status_exists(self):
        """PREPARING status exists for workspace creation."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.PREPARING.value == "preparing"

    def test_running_status_exists(self):
        """RUNNING status exists for step execution."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.RUNNING.value == "running"

    def test_completing_status_exists(self):
        """COMPLETING status exists for cleanup phase."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.COMPLETING.value == "completing"

    def test_completed_status_exists(self):
        """COMPLETED status exists for successful finish."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.COMPLETED.value == "completed"

    def test_failed_status_exists(self):
        """FAILED status exists for failure."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.FAILED.value == "failed"

    def test_cancelled_status_exists(self):
        """CANCELLED status exists for user cancellation."""
        from app.services.workspace.pipeline_state_machine import PipelineStatus
        assert PipelineStatus.CANCELLED.value == "cancelled"


# -----------------------------------------------------------------------------
# Contract: Valid State Transitions
# -----------------------------------------------------------------------------

class TestPipelineValidTransitions:
    """Tests that verify valid pipeline state transitions."""

    def test_pending_to_preparing(self):
        """Pipeline start transitions from PENDING to PREPARING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING)
        assert sm.can_transition_to(PipelineStatus.PREPARING)

        sm.transition_to(PipelineStatus.PREPARING)
        assert sm.current_status == PipelineStatus.PREPARING

    def test_preparing_to_running(self):
        """Workspace ready transitions from PREPARING to RUNNING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PREPARING)
        assert sm.can_transition_to(PipelineStatus.RUNNING)

        sm.transition_to(PipelineStatus.RUNNING)
        assert sm.current_status == PipelineStatus.RUNNING

    def test_running_to_completing(self):
        """All steps done transitions from RUNNING to COMPLETING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING)
        assert sm.can_transition_to(PipelineStatus.COMPLETING)

        sm.transition_to(PipelineStatus.COMPLETING)
        assert sm.current_status == PipelineStatus.COMPLETING

    def test_completing_to_completed(self):
        """Cleanup done transitions from COMPLETING to COMPLETED."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETING)
        assert sm.can_transition_to(PipelineStatus.COMPLETED)

        sm.transition_to(PipelineStatus.COMPLETED)
        assert sm.current_status == PipelineStatus.COMPLETED

    def test_preparing_to_failed(self):
        """Workspace creation failure transitions to FAILED."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PREPARING)
        assert sm.can_transition_to(PipelineStatus.FAILED)

    def test_running_to_failed(self):
        """Step failure transitions from RUNNING to FAILED."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING)
        assert sm.can_transition_to(PipelineStatus.FAILED)


# -----------------------------------------------------------------------------
# Contract: Invalid State Transitions
# -----------------------------------------------------------------------------

class TestPipelineInvalidTransitions:
    """Tests that verify invalid pipeline state transitions are rejected."""

    def test_pending_to_running_invalid(self):
        """Cannot go directly from PENDING to RUNNING (must prepare first)."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING)
        assert not sm.can_transition_to(PipelineStatus.RUNNING)

        with pytest.raises(ValueError, match="Invalid.*transition"):
            sm.transition_to(PipelineStatus.RUNNING)

    def test_pending_to_completed_invalid(self):
        """Cannot go directly from PENDING to COMPLETED."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING)
        assert not sm.can_transition_to(PipelineStatus.COMPLETED)

    def test_completed_to_running_invalid(self):
        """Cannot go back from COMPLETED to RUNNING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETED)
        assert not sm.can_transition_to(PipelineStatus.RUNNING)

    def test_failed_to_running_invalid(self):
        """Cannot go back from FAILED to RUNNING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.FAILED)
        assert not sm.can_transition_to(PipelineStatus.RUNNING)


# -----------------------------------------------------------------------------
# Contract: Cancel Transitions
# -----------------------------------------------------------------------------

class TestPipelineCancelTransitions:
    """Tests that verify cancel transitions from various states."""

    def test_cancel_from_pending(self):
        """Can cancel from PENDING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING)
        assert sm.can_transition_to(PipelineStatus.CANCELLED)

        sm.transition_to(PipelineStatus.CANCELLED)
        assert sm.current_status == PipelineStatus.CANCELLED

    def test_cancel_from_preparing(self):
        """Can cancel from PREPARING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PREPARING)
        assert sm.can_transition_to(PipelineStatus.CANCELLED)

    def test_cancel_from_running(self):
        """Can cancel from RUNNING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING)
        assert sm.can_transition_to(PipelineStatus.CANCELLED)

    def test_cancel_from_completing(self):
        """Can cancel from COMPLETING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETING)
        assert sm.can_transition_to(PipelineStatus.CANCELLED)

    def test_cannot_cancel_from_completed(self):
        """Cannot cancel already COMPLETED pipeline."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETED)
        assert not sm.can_transition_to(PipelineStatus.CANCELLED)

    def test_cannot_cancel_from_failed(self):
        """Cannot cancel already FAILED pipeline."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.FAILED)
        assert not sm.can_transition_to(PipelineStatus.CANCELLED)


# -----------------------------------------------------------------------------
# Contract: Terminal States
# -----------------------------------------------------------------------------

class TestPipelineTerminalStates:
    """Tests that verify terminal states are handled correctly."""

    def test_completed_is_terminal(self):
        """COMPLETED is a terminal state."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETED)
        assert sm.is_terminal()

    def test_failed_is_terminal(self):
        """FAILED is a terminal state."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.FAILED)
        assert sm.is_terminal()

    def test_cancelled_is_terminal(self):
        """CANCELLED is a terminal state."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.CANCELLED)
        assert sm.is_terminal()

    def test_running_is_not_terminal(self):
        """RUNNING is not a terminal state."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING)
        assert not sm.is_terminal()


# -----------------------------------------------------------------------------
# Contract: Step Tracking
# -----------------------------------------------------------------------------

class TestPipelineStepTracking:
    """Tests that verify step execution tracking."""

    def test_tracks_total_steps(self):
        """Pipeline tracks total number of steps."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING, total_steps=5)
        assert sm.total_steps == 5

    def test_tracks_completed_steps(self):
        """Pipeline tracks completed step count."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=5)
        assert sm.completed_steps == 0

        sm.mark_step_completed(0)
        assert sm.completed_steps == 1

        sm.mark_step_completed(1)
        assert sm.completed_steps == 2

    def test_tracks_failed_step(self):
        """Pipeline tracks which step failed."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=5)
        sm.mark_step_failed(2, error="Test failure")

        assert sm.failed_step_index == 2
        assert sm.failure_error == "Test failure"

    def test_step_failure_fails_pipeline(self):
        """Step failure transitions pipeline to FAILED."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=5)
        sm.mark_step_failed(2, error="Step crashed")

        assert sm.current_status == PipelineStatus.FAILED

    def test_all_steps_complete_triggers_completing(self):
        """All steps completing transitions to COMPLETING."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=3)
        sm.mark_step_completed(0)
        sm.mark_step_completed(1)

        # Still running after 2/3
        assert sm.current_status == PipelineStatus.RUNNING

        sm.mark_step_completed(2)

        # After 3/3, transitions to completing
        assert sm.current_status == PipelineStatus.COMPLETING


# -----------------------------------------------------------------------------
# Contract: Timestamps
# -----------------------------------------------------------------------------

class TestPipelineTimestamps:
    """Tests that verify timestamp tracking."""

    def test_tracks_started_at(self):
        """Pipeline tracks when it started."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        before = datetime.utcnow()
        sm = PipelineStateMachine(PipelineStatus.PENDING)
        sm.transition_to(PipelineStatus.PREPARING)
        after = datetime.utcnow()

        assert sm.started_at is not None
        assert before <= sm.started_at <= after

    def test_tracks_completed_at(self):
        """Pipeline tracks when it completed."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.COMPLETING)

        before = datetime.utcnow()
        sm.transition_to(PipelineStatus.COMPLETED)
        after = datetime.utcnow()

        assert sm.completed_at is not None
        assert before <= sm.completed_at <= after

    def test_completed_at_set_on_failure(self):
        """completed_at is set when pipeline fails."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.RUNNING, total_steps=3)
        sm.mark_step_failed(1, error="Crash")

        assert sm.completed_at is not None

    def test_transition_history_recorded(self):
        """State transitions are recorded with timestamps."""
        from app.services.workspace.pipeline_state_machine import (
            PipelineStateMachine, PipelineStatus
        )

        sm = PipelineStateMachine(PipelineStatus.PENDING)
        sm.transition_to(PipelineStatus.PREPARING)
        sm.transition_to(PipelineStatus.RUNNING)

        history = sm.get_history()
        assert len(history) == 2
        assert history[0]["from"] == PipelineStatus.PENDING
        assert history[0]["to"] == PipelineStatus.PREPARING
        assert history[1]["from"] == PipelineStatus.PREPARING
        assert history[1]["to"] == PipelineStatus.RUNNING
