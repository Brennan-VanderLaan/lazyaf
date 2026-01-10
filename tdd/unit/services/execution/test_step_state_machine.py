"""
Unit tests for Step Execution State Machine.

These tests define the contract for step execution state transitions.
The state machine tracks the lifecycle of individual step executions:

    pending -> assigned -> preparing -> running -> completing -> completed
                                            |            |
                                            | exit_0     | finalized
                                            v            v
                                       [timeout]    [completed]
                                            |
                                            v
    cancelled <-- cancel (any) -------- [failed]

Tests are written FIRST to define the contract, then implementation makes them pass.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Step Execution Status Enum
# -----------------------------------------------------------------------------

class TestStepExecutionStatusEnum:
    """Tests that define what statuses must exist."""

    def test_pending_status_exists(self):
        """pending status exists for steps waiting to be assigned."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.PENDING.value == "pending"

    def test_assigned_status_exists(self):
        """assigned status exists for steps assigned to an executor."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.ASSIGNED.value == "assigned"

    def test_preparing_status_exists(self):
        """preparing status exists for steps setting up workspace/container."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.PREPARING.value == "preparing"

    def test_running_status_exists(self):
        """running status exists for steps actively executing."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.RUNNING.value == "running"

    def test_completing_status_exists(self):
        """completing status exists for steps processing results."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.COMPLETING.value == "completing"

    def test_completed_status_exists(self):
        """completed status exists for successfully finished steps."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.COMPLETED.value == "completed"

    def test_failed_status_exists(self):
        """failed status exists for steps that failed execution."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.FAILED.value == "failed"

    def test_cancelled_status_exists(self):
        """cancelled status exists for user-cancelled steps."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.CANCELLED.value == "cancelled"

    def test_timeout_status_exists(self):
        """timeout status exists for steps that exceeded time limit."""
        from app.services.execution.state_machine import StepExecutionStatus
        assert StepExecutionStatus.TIMEOUT.value == "timeout"


# -----------------------------------------------------------------------------
# Contract: Valid State Transitions
# -----------------------------------------------------------------------------

class TestValidTransitions:
    """Tests that define which state transitions are allowed."""

    def test_pending_to_assigned_valid(self):
        """Can transition from pending to assigned."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        result = sm.transition_to(StepExecutionStatus.ASSIGNED)
        assert result is True
        assert sm.status == StepExecutionStatus.ASSIGNED

    def test_assigned_to_preparing_valid(self):
        """Can transition from assigned to preparing."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.ASSIGNED)
        result = sm.transition_to(StepExecutionStatus.PREPARING)
        assert result is True
        assert sm.status == StepExecutionStatus.PREPARING

    def test_preparing_to_running_valid(self):
        """Can transition from preparing to running."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PREPARING)
        result = sm.transition_to(StepExecutionStatus.RUNNING)
        assert result is True
        assert sm.status == StepExecutionStatus.RUNNING

    def test_running_to_completing_valid(self):
        """Can transition from running to completing."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        result = sm.transition_to(StepExecutionStatus.COMPLETING)
        assert result is True
        assert sm.status == StepExecutionStatus.COMPLETING

    def test_completing_to_completed_valid(self):
        """Can transition from completing to completed."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETING)
        result = sm.transition_to(StepExecutionStatus.COMPLETED)
        assert result is True
        assert sm.status == StepExecutionStatus.COMPLETED

    def test_running_to_failed_valid(self):
        """Can transition from running to failed (non-zero exit)."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        result = sm.transition_to(StepExecutionStatus.FAILED)
        assert result is True
        assert sm.status == StepExecutionStatus.FAILED

    def test_running_to_timeout_valid(self):
        """Can transition from running to timeout."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        result = sm.transition_to(StepExecutionStatus.TIMEOUT)
        assert result is True
        assert sm.status == StepExecutionStatus.TIMEOUT

    def test_preparing_to_failed_valid(self):
        """Can transition from preparing to failed (setup error)."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PREPARING)
        result = sm.transition_to(StepExecutionStatus.FAILED)
        assert result is True
        assert sm.status == StepExecutionStatus.FAILED


# -----------------------------------------------------------------------------
# Contract: Invalid State Transitions
# -----------------------------------------------------------------------------

class TestInvalidTransitions:
    """Tests that define which state transitions are NOT allowed."""

    def test_pending_to_running_invalid(self):
        """Cannot skip assigned and preparing states."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        result = sm.transition_to(StepExecutionStatus.RUNNING)
        assert result is False
        assert sm.status == StepExecutionStatus.PENDING  # Unchanged

    def test_pending_to_completed_invalid(self):
        """Cannot jump directly to completed from pending."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        result = sm.transition_to(StepExecutionStatus.COMPLETED)
        assert result is False
        assert sm.status == StepExecutionStatus.PENDING

    def test_completed_to_running_invalid(self):
        """Cannot go back from completed to running."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETED)
        result = sm.transition_to(StepExecutionStatus.RUNNING)
        assert result is False
        assert sm.status == StepExecutionStatus.COMPLETED

    def test_failed_to_running_invalid(self):
        """Cannot go back from failed to running."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.FAILED)
        result = sm.transition_to(StepExecutionStatus.RUNNING)
        assert result is False
        assert sm.status == StepExecutionStatus.FAILED


# -----------------------------------------------------------------------------
# Contract: Cancel From Any Non-Terminal State
# -----------------------------------------------------------------------------

class TestCancelTransitions:
    """Tests that cancel can occur from any non-terminal state."""

    def test_cancel_from_pending(self):
        """Can cancel from pending state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is True
        assert sm.status == StepExecutionStatus.CANCELLED

    def test_cancel_from_assigned(self):
        """Can cancel from assigned state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.ASSIGNED)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is True
        assert sm.status == StepExecutionStatus.CANCELLED

    def test_cancel_from_preparing(self):
        """Can cancel from preparing state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PREPARING)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is True
        assert sm.status == StepExecutionStatus.CANCELLED

    def test_cancel_from_running(self):
        """Can cancel from running state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is True
        assert sm.status == StepExecutionStatus.CANCELLED

    def test_cancel_from_completing(self):
        """Can cancel from completing state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETING)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is True
        assert sm.status == StepExecutionStatus.CANCELLED

    def test_cancel_from_completed_invalid(self):
        """Cannot cancel from completed (terminal state)."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETED)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is False
        assert sm.status == StepExecutionStatus.COMPLETED

    def test_cancel_from_failed_invalid(self):
        """Cannot cancel from failed (terminal state)."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.FAILED)
        result = sm.transition_to(StepExecutionStatus.CANCELLED)
        assert result is False
        assert sm.status == StepExecutionStatus.FAILED


# -----------------------------------------------------------------------------
# Contract: Terminal States
# -----------------------------------------------------------------------------

class TestTerminalStates:
    """Tests that define which states are terminal (no outgoing transitions)."""

    def test_completed_is_terminal(self):
        """completed is a terminal state with no valid outgoing transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETED)
        assert sm.is_terminal() is True

    def test_failed_is_terminal(self):
        """failed is a terminal state with no valid outgoing transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.FAILED)
        assert sm.is_terminal() is True

    def test_cancelled_is_terminal(self):
        """cancelled is a terminal state with no valid outgoing transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.CANCELLED)
        assert sm.is_terminal() is True

    def test_timeout_is_terminal(self):
        """timeout is a terminal state with no valid outgoing transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.TIMEOUT)
        assert sm.is_terminal() is True

    def test_pending_is_not_terminal(self):
        """pending is NOT a terminal state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        assert sm.is_terminal() is False

    def test_running_is_not_terminal(self):
        """running is NOT a terminal state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        assert sm.is_terminal() is False


# -----------------------------------------------------------------------------
# Contract: Transition Timestamps
# -----------------------------------------------------------------------------

class TestTransitionTimestamps:
    """Tests that transitions record timestamps."""

    def test_transition_records_timestamp(self):
        """State transitions should record when they occurred."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)

        before = datetime.utcnow()
        sm.transition_to(StepExecutionStatus.ASSIGNED)
        after = datetime.utcnow()

        assert sm.last_transition_at is not None
        assert before <= sm.last_transition_at <= after

    def test_transition_history_recorded(self):
        """State machine should maintain history of transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)

        sm.transition_to(StepExecutionStatus.ASSIGNED)
        sm.transition_to(StepExecutionStatus.PREPARING)
        sm.transition_to(StepExecutionStatus.RUNNING)

        history = sm.get_history()
        assert len(history) >= 3
        # History entries should have (status, timestamp) pairs
        assert history[0][0] == StepExecutionStatus.ASSIGNED
        assert history[1][0] == StepExecutionStatus.PREPARING
        assert history[2][0] == StepExecutionStatus.RUNNING


# -----------------------------------------------------------------------------
# Contract: Helper Methods
# -----------------------------------------------------------------------------

class TestHelperMethods:
    """Tests for helper methods on the state machine."""

    def test_can_transition_to_returns_true_for_valid(self):
        """can_transition_to returns True for valid transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        assert sm.can_transition_to(StepExecutionStatus.ASSIGNED) is True

    def test_can_transition_to_returns_false_for_invalid(self):
        """can_transition_to returns False for invalid transitions."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        assert sm.can_transition_to(StepExecutionStatus.COMPLETED) is False

    def test_valid_next_states_from_pending(self):
        """get_valid_next_states returns allowed transitions from pending."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.PENDING)
        valid = sm.get_valid_next_states()
        assert StepExecutionStatus.ASSIGNED in valid
        assert StepExecutionStatus.CANCELLED in valid
        assert StepExecutionStatus.FAILED in valid  # For immediate failures
        assert StepExecutionStatus.RUNNING not in valid

    def test_valid_next_states_from_running(self):
        """get_valid_next_states returns allowed transitions from running."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.RUNNING)
        valid = sm.get_valid_next_states()
        assert StepExecutionStatus.COMPLETING in valid
        assert StepExecutionStatus.FAILED in valid
        assert StepExecutionStatus.TIMEOUT in valid
        assert StepExecutionStatus.CANCELLED in valid
        assert StepExecutionStatus.PENDING not in valid

    def test_valid_next_states_from_terminal_is_empty(self):
        """get_valid_next_states returns empty set from terminal state."""
        from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
        sm = StepStateMachine(StepExecutionStatus.COMPLETED)
        valid = sm.get_valid_next_states()
        assert len(valid) == 0
