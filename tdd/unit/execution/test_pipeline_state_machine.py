"""
Tests for Pipeline State Machine (Phase 12.2).

These tests DEFINE the pipeline run lifecycle contract.
Write tests first, then implement to make them pass.

Pipeline Run States:
- pending: Created, waiting to start
- preparing: Workspace being created, initial setup
- running: Steps are executing
- completing: All steps done, cleanup in progress
- completed: Successfully finished
- failed: Step failed or error occurred
- cancelled: User cancelled

Valid Transitions:
- pending -> preparing (start called)
- preparing -> running (workspace ready)
- preparing -> failed (workspace creation failed)
- running -> completing (all steps done)
- running -> failed (step failed)
- running -> cancelled (user cancelled)
- completing -> completed (cleanup done)
- completing -> failed (cleanup failed)
"""
import sys
from pathlib import Path
from datetime import datetime

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.pipeline_state import (
        PipelineRunState,
        PipelineStateMachine,
        InvalidPipelineTransitionError,
        PipelineStateTransition,
    )
    PIPELINE_STATE_MODULE_AVAILABLE = True
except ImportError:
    PIPELINE_STATE_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    from enum import Enum
    PipelineRunState = Enum("PipelineRunState", [
        "PENDING", "PREPARING", "RUNNING", "COMPLETING",
        "COMPLETED", "FAILED", "CANCELLED"
    ])
    PipelineStateMachine = None
    InvalidPipelineTransitionError = Exception
    PipelineStateTransition = None


pytestmark = pytest.mark.skipif(
    not PIPELINE_STATE_MODULE_AVAILABLE,
    reason="pipeline state module not yet implemented"
)


class TestPipelineRunStates:
    """Tests for PipelineRunState enum values."""

    def test_has_pending_state(self):
        """PipelineRunState has PENDING state."""
        assert PipelineRunState.PENDING is not None
        assert PipelineRunState.PENDING.value == "pending"

    def test_has_preparing_state(self):
        """PipelineRunState has PREPARING state."""
        assert PipelineRunState.PREPARING is not None
        assert PipelineRunState.PREPARING.value == "preparing"

    def test_has_running_state(self):
        """PipelineRunState has RUNNING state."""
        assert PipelineRunState.RUNNING is not None
        assert PipelineRunState.RUNNING.value == "running"

    def test_has_completing_state(self):
        """PipelineRunState has COMPLETING state."""
        assert PipelineRunState.COMPLETING is not None
        assert PipelineRunState.COMPLETING.value == "completing"

    def test_has_completed_state(self):
        """PipelineRunState has COMPLETED state."""
        assert PipelineRunState.COMPLETED is not None
        assert PipelineRunState.COMPLETED.value == "completed"

    def test_has_failed_state(self):
        """PipelineRunState has FAILED state."""
        assert PipelineRunState.FAILED is not None
        assert PipelineRunState.FAILED.value == "failed"

    def test_has_cancelled_state(self):
        """PipelineRunState has CANCELLED state."""
        assert PipelineRunState.CANCELLED is not None
        assert PipelineRunState.CANCELLED.value == "cancelled"


class TestPendingTransitions:
    """Tests for transitions from PENDING state."""

    @pytest.fixture
    def machine(self):
        """Create a pipeline state machine in PENDING state."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.PENDING,
        )

    def test_pending_to_preparing(self, machine):
        """PENDING -> PREPARING when start is called."""
        machine.transition(PipelineRunState.PREPARING)
        assert machine.state == PipelineRunState.PREPARING

    def test_pending_to_cancelled(self, machine):
        """PENDING -> CANCELLED is valid (cancel before start)."""
        machine.transition(PipelineRunState.CANCELLED)
        assert machine.state == PipelineRunState.CANCELLED

    def test_pending_to_running_invalid(self, machine):
        """PENDING -> RUNNING is invalid (must prepare first)."""
        with pytest.raises(InvalidPipelineTransitionError):
            machine.transition(PipelineRunState.RUNNING)

    def test_pending_to_completed_invalid(self, machine):
        """PENDING -> COMPLETED is invalid."""
        with pytest.raises(InvalidPipelineTransitionError):
            machine.transition(PipelineRunState.COMPLETED)


class TestPreparingTransitions:
    """Tests for transitions from PREPARING state."""

    @pytest.fixture
    def machine(self):
        """Create a pipeline state machine in PREPARING state."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.PREPARING,
        )

    def test_preparing_to_running(self, machine):
        """PREPARING -> RUNNING when workspace is ready."""
        machine.transition(PipelineRunState.RUNNING)
        assert machine.state == PipelineRunState.RUNNING

    def test_preparing_to_failed(self, machine):
        """PREPARING -> FAILED when preparation fails."""
        machine.transition(PipelineRunState.FAILED, reason="Workspace creation failed")
        assert machine.state == PipelineRunState.FAILED

    def test_preparing_to_cancelled(self, machine):
        """PREPARING -> CANCELLED is valid."""
        machine.transition(PipelineRunState.CANCELLED)
        assert machine.state == PipelineRunState.CANCELLED

    def test_preparing_to_completed_invalid(self, machine):
        """PREPARING -> COMPLETED is invalid (must run first)."""
        with pytest.raises(InvalidPipelineTransitionError):
            machine.transition(PipelineRunState.COMPLETED)


class TestRunningTransitions:
    """Tests for transitions from RUNNING state."""

    @pytest.fixture
    def machine(self):
        """Create a pipeline state machine in RUNNING state."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.RUNNING,
        )

    def test_running_to_completing(self, machine):
        """RUNNING -> COMPLETING when all steps done."""
        machine.transition(PipelineRunState.COMPLETING)
        assert machine.state == PipelineRunState.COMPLETING

    def test_running_to_failed(self, machine):
        """RUNNING -> FAILED when step fails."""
        machine.transition(PipelineRunState.FAILED, reason="Step 'build' failed")
        assert machine.state == PipelineRunState.FAILED

    def test_running_to_cancelled(self, machine):
        """RUNNING -> CANCELLED when user cancels."""
        machine.transition(PipelineRunState.CANCELLED)
        assert machine.state == PipelineRunState.CANCELLED

    def test_running_to_completed_invalid(self, machine):
        """RUNNING -> COMPLETED is invalid (must complete first)."""
        with pytest.raises(InvalidPipelineTransitionError):
            machine.transition(PipelineRunState.COMPLETED)


class TestCompletingTransitions:
    """Tests for transitions from COMPLETING state."""

    @pytest.fixture
    def machine(self):
        """Create a pipeline state machine in COMPLETING state."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.COMPLETING,
        )

    def test_completing_to_completed(self, machine):
        """COMPLETING -> COMPLETED when cleanup succeeds."""
        machine.transition(PipelineRunState.COMPLETED)
        assert machine.state == PipelineRunState.COMPLETED

    def test_completing_to_failed(self, machine):
        """COMPLETING -> FAILED when cleanup fails."""
        machine.transition(PipelineRunState.FAILED, reason="Cleanup failed")
        assert machine.state == PipelineRunState.FAILED

    def test_completing_to_running_invalid(self, machine):
        """COMPLETING -> RUNNING is invalid."""
        with pytest.raises(InvalidPipelineTransitionError):
            machine.transition(PipelineRunState.RUNNING)


class TestTerminalStates:
    """Tests for terminal states."""

    def test_completed_is_terminal(self):
        """No transitions from COMPLETED."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.COMPLETED,
        )

        for target in PipelineRunState:
            if target != PipelineRunState.COMPLETED:
                with pytest.raises(InvalidPipelineTransitionError):
                    machine.transition(target)

    def test_failed_is_terminal(self):
        """No transitions from FAILED."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.FAILED,
        )

        for target in PipelineRunState:
            if target != PipelineRunState.FAILED:
                with pytest.raises(InvalidPipelineTransitionError):
                    machine.transition(target)

    def test_cancelled_is_terminal(self):
        """No transitions from CANCELLED."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.CANCELLED,
        )

        for target in PipelineRunState:
            if target != PipelineRunState.CANCELLED:
                with pytest.raises(InvalidPipelineTransitionError):
                    machine.transition(target)

    def test_is_terminal_property(self):
        """is_terminal returns True for terminal states."""
        for terminal_state in [
            PipelineRunState.COMPLETED,
            PipelineRunState.FAILED,
            PipelineRunState.CANCELLED,
        ]:
            machine = PipelineStateMachine(
                pipeline_run_id="run-test-123",
                initial_state=terminal_state,
            )
            assert machine.is_terminal is True

    def test_is_terminal_false_for_active_states(self):
        """is_terminal returns False for active states."""
        for active_state in [
            PipelineRunState.PENDING,
            PipelineRunState.PREPARING,
            PipelineRunState.RUNNING,
            PipelineRunState.COMPLETING,
        ]:
            machine = PipelineStateMachine(
                pipeline_run_id="run-test-123",
                initial_state=active_state,
            )
            assert machine.is_terminal is False


class TestStepFailureFailsPipeline:
    """Tests for step failure handling."""

    @pytest.fixture
    def machine(self):
        """Create a running pipeline state machine."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.RUNNING,
        )

    def test_step_failure_fails_pipeline(self, machine):
        """When step fails, pipeline transitions to FAILED."""
        machine.on_step_failed(
            step_index=2,
            step_name="test",
            error="Exit code 1",
        )
        assert machine.state == PipelineRunState.FAILED

    def test_step_failure_records_error(self, machine):
        """Step failure error is recorded."""
        machine.on_step_failed(
            step_index=2,
            step_name="test",
            error="Tests failed: 3 assertions",
        )
        assert machine.error is not None
        assert "test" in machine.error.lower() or "Tests failed" in machine.error

    def test_step_failure_records_failed_step(self, machine):
        """Failed step info is recorded."""
        machine.on_step_failed(
            step_index=2,
            step_name="build",
            error="Compilation error",
        )
        assert machine.failed_step_index == 2
        assert machine.failed_step_name == "build"

    def test_on_failure_next_continues(self, machine):
        """When on_failure='next', pipeline continues despite step failure."""
        machine.on_step_failed(
            step_index=2,
            step_name="lint",
            error="Lint warnings",
            on_failure="next",  # Continue to next step
        )
        # Pipeline should still be RUNNING
        assert machine.state == PipelineRunState.RUNNING


class TestStepCompletion:
    """Tests for step completion tracking."""

    @pytest.fixture
    def machine(self):
        """Create a running pipeline with 3 steps."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.RUNNING,
            total_steps=3,
        )

    def test_step_completion_tracked(self, machine):
        """Completed steps are tracked."""
        machine.on_step_completed(step_index=0, step_name="setup")
        assert machine.completed_step_count == 1

        machine.on_step_completed(step_index=1, step_name="build")
        assert machine.completed_step_count == 2

    def test_all_steps_complete_triggers_completing(self, machine):
        """When all steps complete, pipeline transitions to COMPLETING."""
        machine.on_step_completed(step_index=0, step_name="setup")
        machine.on_step_completed(step_index=1, step_name="build")
        machine.on_step_completed(step_index=2, step_name="test")

        assert machine.state == PipelineRunState.COMPLETING

    def test_current_step_tracked(self, machine):
        """Current step index is tracked."""
        assert machine.current_step_index == 0

        machine.on_step_started(step_index=0, step_name="setup")
        assert machine.current_step_index == 0

        machine.on_step_completed(step_index=0, step_name="setup")
        machine.on_step_started(step_index=1, step_name="build")
        assert machine.current_step_index == 1


class TestTransitionMetadata:
    """Tests for transition metadata recording."""

    @pytest.fixture
    def machine(self):
        """Create a new pipeline state machine."""
        return PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.PENDING,
        )

    def test_transition_records_timestamp(self, machine):
        """State changes record timestamp."""
        before = datetime.utcnow()
        machine.transition(PipelineRunState.PREPARING)
        after = datetime.utcnow()

        last_transition = machine.last_transition
        assert last_transition is not None
        assert before <= last_transition.timestamp <= after

    def test_transition_records_from_state(self, machine):
        """Transitions record the source state."""
        machine.transition(PipelineRunState.PREPARING)
        assert machine.last_transition.from_state == PipelineRunState.PENDING

    def test_transition_records_to_state(self, machine):
        """Transitions record the target state."""
        machine.transition(PipelineRunState.PREPARING)
        assert machine.last_transition.to_state == PipelineRunState.PREPARING

    def test_transition_records_reason(self, machine):
        """Transitions can include a reason."""
        machine.transition(PipelineRunState.PREPARING)
        machine.transition(PipelineRunState.FAILED, reason="Workspace creation timeout")

        assert machine.last_transition.reason == "Workspace creation timeout"

    def test_transition_history_preserved(self, machine):
        """All transitions are preserved in history."""
        machine.transition(PipelineRunState.PREPARING)
        machine.transition(PipelineRunState.RUNNING)
        machine.transition(PipelineRunState.COMPLETING)
        machine.transition(PipelineRunState.COMPLETED)

        assert len(machine.history) >= 4


class TestDurationTracking:
    """Tests for pipeline duration tracking."""

    def test_started_at_set_on_preparing(self):
        """started_at is set when transitioning to PREPARING."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.PENDING,
        )
        before = datetime.utcnow()
        machine.transition(PipelineRunState.PREPARING)
        after = datetime.utcnow()

        assert machine.started_at is not None
        assert before <= machine.started_at <= after

    def test_completed_at_set_on_terminal(self):
        """completed_at is set when reaching terminal state."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.COMPLETING,
        )
        before = datetime.utcnow()
        machine.transition(PipelineRunState.COMPLETED)
        after = datetime.utcnow()

        assert machine.completed_at is not None
        assert before <= machine.completed_at <= after

    def test_duration_calculated(self):
        """duration property returns time between start and complete."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.PENDING,
        )
        machine.transition(PipelineRunState.PREPARING)
        machine.transition(PipelineRunState.RUNNING)
        machine.transition(PipelineRunState.COMPLETING)
        machine.transition(PipelineRunState.COMPLETED)

        assert machine.duration is not None
        assert machine.duration.total_seconds() >= 0


class TestHelperMethods:
    """Tests for pipeline state machine helper methods."""

    def test_is_running_true(self):
        """is_running returns True when in RUNNING state."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.RUNNING,
        )
        assert machine.is_running is True

    def test_is_running_false(self):
        """is_running returns False when not in RUNNING state."""
        for state in [
            PipelineRunState.PENDING,
            PipelineRunState.PREPARING,
            PipelineRunState.COMPLETED,
        ]:
            machine = PipelineStateMachine(
                pipeline_run_id="run-test-123",
                initial_state=state,
            )
            assert machine.is_running is False

    def test_can_cancel_true_for_active(self):
        """can_cancel returns True for non-terminal states."""
        for state in [
            PipelineRunState.PENDING,
            PipelineRunState.PREPARING,
            PipelineRunState.RUNNING,
        ]:
            machine = PipelineStateMachine(
                pipeline_run_id="run-test-123",
                initial_state=state,
            )
            assert machine.can_cancel is True

    def test_can_cancel_false_for_terminal(self):
        """can_cancel returns False for terminal states."""
        for state in [
            PipelineRunState.COMPLETED,
            PipelineRunState.FAILED,
            PipelineRunState.CANCELLED,
        ]:
            machine = PipelineStateMachine(
                pipeline_run_id="run-test-123",
                initial_state=state,
            )
            assert machine.can_cancel is False

    def test_success_property(self):
        """success property returns True only for COMPLETED."""
        completed = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.COMPLETED,
        )
        assert completed.success is True

        failed = PipelineStateMachine(
            pipeline_run_id="run-test-123",
            initial_state=PipelineRunState.FAILED,
        )
        assert failed.success is False


class TestPipelineRunId:
    """Tests for pipeline run ID handling."""

    def test_run_id_stored(self):
        """Pipeline run ID is stored in machine."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-abc-123",
            initial_state=PipelineRunState.PENDING,
        )
        assert machine.pipeline_run_id == "run-abc-123"

    def test_to_dict_includes_run_id(self):
        """Serialization includes pipeline run ID."""
        machine = PipelineStateMachine(
            pipeline_run_id="run-xyz-789",
            initial_state=PipelineRunState.RUNNING,
        )
        data = machine.to_dict()
        assert data["pipeline_run_id"] == "run-xyz-789"

    def test_from_dict_restores_state(self):
        """Machine can be restored from dict."""
        original = PipelineStateMachine(
            pipeline_run_id="run-restore-test",
            initial_state=PipelineRunState.PENDING,
        )
        original.transition(PipelineRunState.PREPARING)
        original.transition(PipelineRunState.RUNNING)

        data = original.to_dict()
        restored = PipelineStateMachine.from_dict(data)

        assert restored.pipeline_run_id == original.pipeline_run_id
        assert restored.state == original.state
        assert len(restored.history) == len(original.history)
