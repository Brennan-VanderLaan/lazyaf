"""
Tests for Step State Machine (Phase 12.1).

These tests DEFINE the state transition contract for step executions.
Write tests first, then implement to make them pass.

Step States:
- pending: Step is queued, waiting for execution
- preparing: Container is being created/pulled
- running: Container is executing
- completing: Container finished, processing results
- completed: Step finished successfully (exit_code = 0)
- failed: Step finished with error (exit_code != 0, crash, timeout)
- cancelled: Step was cancelled by user/system

Valid Transitions:
- pending -> preparing (step assigned to executor)
- preparing -> running (container started)
- preparing -> failed (container creation failed)
- running -> completing (container exited)
- running -> failed (timeout, crash)
- running -> cancelled (user cancellation)
- completing -> completed (exit_code = 0)
- completing -> failed (exit_code != 0)
- * -> cancelled (can cancel from any non-terminal state)
"""

from datetime import datetime, timedelta
from enum import Enum

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.step_state import (
        StepState,
        StepStateMachine,
        InvalidTransitionError,
        StepStateTransition,
    )
    EXECUTION_MODULE_AVAILABLE = True
except ImportError:
    EXECUTION_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    StepState = Enum("StepState", [
        "PENDING", "PREPARING", "RUNNING", "COMPLETING",
        "COMPLETED", "FAILED", "CANCELLED"
    ])
    StepStateMachine = None
    InvalidTransitionError = Exception
    StepStateTransition = None


pytestmark = pytest.mark.skipif(
    not EXECUTION_MODULE_AVAILABLE,
    reason="execution module not yet implemented"
)


class TestStepStates:
    """Tests for StepState enum values."""

    def test_has_pending_state(self):
        """StepState has PENDING state."""
        assert StepState.PENDING is not None
        assert StepState.PENDING.value == "pending"

    def test_has_preparing_state(self):
        """StepState has PREPARING state."""
        assert StepState.PREPARING is not None
        assert StepState.PREPARING.value == "preparing"

    def test_has_running_state(self):
        """StepState has RUNNING state."""
        assert StepState.RUNNING is not None
        assert StepState.RUNNING.value == "running"

    def test_has_completing_state(self):
        """StepState has COMPLETING state."""
        assert StepState.COMPLETING is not None
        assert StepState.COMPLETING.value == "completing"

    def test_has_completed_state(self):
        """StepState has COMPLETED state."""
        assert StepState.COMPLETED is not None
        assert StepState.COMPLETED.value == "completed"

    def test_has_failed_state(self):
        """StepState has FAILED state."""
        assert StepState.FAILED is not None
        assert StepState.FAILED.value == "failed"

    def test_has_cancelled_state(self):
        """StepState has CANCELLED state."""
        assert StepState.CANCELLED is not None
        assert StepState.CANCELLED.value == "cancelled"


class TestValidTransitions:
    """Tests for valid state transitions."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine starting in PENDING state."""
        return StepStateMachine(initial_state=StepState.PENDING)

    def test_pending_to_preparing_valid(self, machine):
        """PENDING -> PREPARING is valid (step assigned to executor)."""
        machine.transition(StepState.PREPARING)
        assert machine.state == StepState.PREPARING

    def test_preparing_to_running_valid(self, machine):
        """PREPARING -> RUNNING is valid (container started)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        assert machine.state == StepState.RUNNING

    def test_preparing_to_failed_valid(self, machine):
        """PREPARING -> FAILED is valid (container creation failed)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.FAILED, reason="Failed to pull image")
        assert machine.state == StepState.FAILED

    def test_running_to_completing_valid(self, machine):
        """RUNNING -> COMPLETING is valid (container exited)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        assert machine.state == StepState.COMPLETING

    def test_running_to_failed_valid(self, machine):
        """RUNNING -> FAILED is valid (timeout, crash)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.FAILED, reason="Timeout exceeded")
        assert machine.state == StepState.FAILED

    def test_running_to_cancelled_valid(self, machine):
        """RUNNING -> CANCELLED is valid (user cancellation)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.CANCELLED)
        assert machine.state == StepState.CANCELLED

    def test_completing_to_completed_valid(self, machine):
        """COMPLETING -> COMPLETED is valid (exit_code = 0)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        machine.transition(StepState.COMPLETED, exit_code=0)
        assert machine.state == StepState.COMPLETED

    def test_completing_to_failed_valid(self, machine):
        """COMPLETING -> FAILED is valid (exit_code != 0)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        machine.transition(StepState.FAILED, exit_code=1, reason="Non-zero exit code")
        assert machine.state == StepState.FAILED


class TestInvalidTransitions:
    """Tests for invalid state transitions."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine starting in PENDING state."""
        return StepStateMachine(initial_state=StepState.PENDING)

    def test_pending_to_running_invalid(self, machine):
        """PENDING -> RUNNING is invalid (must go through PREPARING)."""
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.RUNNING)

    def test_pending_to_completing_invalid(self, machine):
        """PENDING -> COMPLETING is invalid."""
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.COMPLETING)

    def test_pending_to_completed_invalid(self, machine):
        """PENDING -> COMPLETED is invalid."""
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.COMPLETED)

    def test_preparing_to_completing_invalid(self, machine):
        """PREPARING -> COMPLETING is invalid (must go through RUNNING)."""
        machine.transition(StepState.PREPARING)
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.COMPLETING)

    def test_preparing_to_completed_invalid(self, machine):
        """PREPARING -> COMPLETED is invalid."""
        machine.transition(StepState.PREPARING)
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.COMPLETED)

    def test_running_to_completed_invalid(self, machine):
        """RUNNING -> COMPLETED is invalid (must go through COMPLETING)."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        with pytest.raises(InvalidTransitionError):
            machine.transition(StepState.COMPLETED)


class TestTerminalStates:
    """Tests for terminal (final) states."""

    def test_completed_is_terminal(self):
        """No transitions allowed from COMPLETED state."""
        machine = StepStateMachine(initial_state=StepState.COMPLETED)

        for target_state in StepState:
            if target_state != StepState.COMPLETED:
                with pytest.raises(InvalidTransitionError):
                    machine.transition(target_state)

    def test_failed_is_terminal(self):
        """No transitions allowed from FAILED state."""
        machine = StepStateMachine(initial_state=StepState.FAILED)

        for target_state in StepState:
            if target_state != StepState.FAILED:
                with pytest.raises(InvalidTransitionError):
                    machine.transition(target_state)

    def test_cancelled_is_terminal(self):
        """No transitions allowed from CANCELLED state."""
        machine = StepStateMachine(initial_state=StepState.CANCELLED)

        for target_state in StepState:
            if target_state != StepState.CANCELLED:
                with pytest.raises(InvalidTransitionError):
                    machine.transition(target_state)


class TestCancellation:
    """Tests for cancellation from any non-terminal state."""

    def test_cancel_from_pending(self):
        """PENDING -> CANCELLED is valid."""
        machine = StepStateMachine(initial_state=StepState.PENDING)
        machine.transition(StepState.CANCELLED)
        assert machine.state == StepState.CANCELLED

    def test_cancel_from_preparing(self):
        """PREPARING -> CANCELLED is valid."""
        machine = StepStateMachine(initial_state=StepState.PREPARING)
        machine.transition(StepState.CANCELLED)
        assert machine.state == StepState.CANCELLED

    def test_cancel_from_running(self):
        """RUNNING -> CANCELLED is valid."""
        machine = StepStateMachine(initial_state=StepState.RUNNING)
        machine.transition(StepState.CANCELLED)
        assert machine.state == StepState.CANCELLED

    def test_cancel_from_completing(self):
        """COMPLETING -> CANCELLED is valid."""
        machine = StepStateMachine(initial_state=StepState.COMPLETING)
        machine.transition(StepState.CANCELLED)
        assert machine.state == StepState.CANCELLED


class TestTransitionMetadata:
    """Tests for transition metadata recording."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine."""
        return StepStateMachine(initial_state=StepState.PENDING)

    def test_transition_records_timestamp(self, machine):
        """State changes record timestamp."""
        before = datetime.utcnow()
        machine.transition(StepState.PREPARING)
        after = datetime.utcnow()

        last_transition = machine.last_transition
        assert last_transition is not None
        assert before <= last_transition.timestamp <= after

    def test_transition_records_from_state(self, machine):
        """Transitions record the source state."""
        machine.transition(StepState.PREPARING)

        last_transition = machine.last_transition
        assert last_transition.from_state == StepState.PENDING

    def test_transition_records_to_state(self, machine):
        """Transitions record the target state."""
        machine.transition(StepState.PREPARING)

        last_transition = machine.last_transition
        assert last_transition.to_state == StepState.PREPARING

    def test_transition_records_reason(self, machine):
        """Transitions can include a reason."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.FAILED, reason="Image pull failed")

        last_transition = machine.last_transition
        assert last_transition.reason == "Image pull failed"

    def test_transition_history_preserved(self, machine):
        """All transitions are preserved in history."""
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        machine.transition(StepState.COMPLETED)

        assert len(machine.history) == 4
        assert machine.history[0].to_state == StepState.PREPARING
        assert machine.history[1].to_state == StepState.RUNNING
        assert machine.history[2].to_state == StepState.COMPLETING
        assert machine.history[3].to_state == StepState.COMPLETED


class TestExitCodeHandling:
    """Tests for exit code-based transitions."""

    def test_exit_code_zero_completes(self):
        """Exit code 0 transitions to COMPLETED."""
        machine = StepStateMachine(initial_state=StepState.COMPLETING)
        machine.transition(StepState.COMPLETED, exit_code=0)
        assert machine.state == StepState.COMPLETED
        assert machine.exit_code == 0

    def test_exit_code_nonzero_fails(self):
        """Exit code != 0 transitions to FAILED."""
        machine = StepStateMachine(initial_state=StepState.COMPLETING)
        machine.transition(StepState.FAILED, exit_code=1)
        assert machine.state == StepState.FAILED
        assert machine.exit_code == 1

    def test_timeout_sets_special_exit_code(self):
        """Timeout uses a special exit code (e.g., -1 or 124)."""
        machine = StepStateMachine(initial_state=StepState.RUNNING)
        machine.transition(StepState.FAILED, reason="timeout", exit_code=-1)
        assert machine.state == StepState.FAILED
        assert machine.exit_code == -1


class TestHelperMethods:
    """Tests for state machine helper methods."""

    def test_is_terminal_true_for_completed(self):
        """is_terminal() returns True for COMPLETED."""
        machine = StepStateMachine(initial_state=StepState.COMPLETED)
        assert machine.is_terminal is True

    def test_is_terminal_true_for_failed(self):
        """is_terminal() returns True for FAILED."""
        machine = StepStateMachine(initial_state=StepState.FAILED)
        assert machine.is_terminal is True

    def test_is_terminal_true_for_cancelled(self):
        """is_terminal() returns True for CANCELLED."""
        machine = StepStateMachine(initial_state=StepState.CANCELLED)
        assert machine.is_terminal is True

    def test_is_terminal_false_for_running(self):
        """is_terminal() returns False for non-terminal states."""
        machine = StepStateMachine(initial_state=StepState.RUNNING)
        assert machine.is_terminal is False

    def test_can_transition_true_for_valid(self):
        """can_transition() returns True for valid transitions."""
        machine = StepStateMachine(initial_state=StepState.PENDING)
        assert machine.can_transition(StepState.PREPARING) is True

    def test_can_transition_false_for_invalid(self):
        """can_transition() returns False for invalid transitions."""
        machine = StepStateMachine(initial_state=StepState.PENDING)
        assert machine.can_transition(StepState.RUNNING) is False

    def test_duration_calculated(self):
        """Duration from first to last transition is calculated."""
        machine = StepStateMachine(initial_state=StepState.PENDING)
        machine.transition(StepState.PREPARING)
        machine.transition(StepState.RUNNING)
        machine.transition(StepState.COMPLETING)
        machine.transition(StepState.COMPLETED)

        duration = machine.duration
        assert duration is not None
        assert isinstance(duration, timedelta)
