"""
Tests for Runner State Machine (Phase 12.6).

These tests DEFINE the state transition contract for remote runners.

Runner States:
- disconnected: No WebSocket connection
- connecting: WebSocket open, registration pending
- idle: Ready to accept jobs
- assigned: Job sent, awaiting ACK
- busy: Executing step
- dead: Heartbeat timeout, presumed crashed

Valid Transitions:
- disconnected -> connecting (WebSocket opens)
- connecting -> idle (registration succeeds)
- connecting -> disconnected (registration timeout/failure)
- idle -> assigned (job pushed to runner)
- idle -> disconnected (WebSocket closes gracefully)
- idle -> dead (heartbeat timeout with no work - silently-dead idle runner)
- assigned -> busy (runner ACKs job)
- assigned -> dead (ACK timeout - 5s)
- assigned -> disconnected (WebSocket closes)
- busy -> idle (step completes)
- busy -> dead (heartbeat timeout - 30s)
- busy -> disconnected (WebSocket closes)
- dead -> connecting (runner reconnects)
- dead -> disconnected (socket close observed after death)
"""

from datetime import datetime, timedelta

import pytest

from app.services.execution.runner_state import (
    HISTORY_MAXLEN,
    SERIALIZED_HISTORY_LIMIT,
    RunnerState,
    RunnerStateMachine,
    InvalidRunnerTransitionError,
    VALID_TRANSITIONS,
)


class TestRunnerStates:
    """Tests for RunnerState enum values."""

    def test_has_disconnected_state(self):
        """RunnerState has DISCONNECTED state."""
        assert RunnerState.DISCONNECTED is not None
        assert RunnerState.DISCONNECTED.value == "disconnected"

    def test_has_connecting_state(self):
        """RunnerState has CONNECTING state."""
        assert RunnerState.CONNECTING is not None
        assert RunnerState.CONNECTING.value == "connecting"

    def test_has_idle_state(self):
        """RunnerState has IDLE state."""
        assert RunnerState.IDLE is not None
        assert RunnerState.IDLE.value == "idle"

    def test_has_assigned_state(self):
        """RunnerState has ASSIGNED state."""
        assert RunnerState.ASSIGNED is not None
        assert RunnerState.ASSIGNED.value == "assigned"

    def test_has_busy_state(self):
        """RunnerState has BUSY state."""
        assert RunnerState.BUSY is not None
        assert RunnerState.BUSY.value == "busy"

    def test_has_dead_state(self):
        """RunnerState has DEAD state."""
        assert RunnerState.DEAD is not None
        assert RunnerState.DEAD.value == "dead"


class TestValidTransitions:
    """Tests for valid state transitions."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine starting in DISCONNECTED state."""
        return RunnerStateMachine(runner_id="test-runner-1")

    def test_disconnected_to_connecting_valid(self, machine):
        """DISCONNECTED -> CONNECTING is valid (WebSocket opens)."""
        machine.transition_to(RunnerState.CONNECTING)
        assert machine.state == RunnerState.CONNECTING

    def test_connecting_to_idle_valid(self, machine):
        """CONNECTING -> IDLE is valid (registration succeeds)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        assert machine.state == RunnerState.IDLE

    def test_connecting_to_disconnected_valid(self, machine):
        """CONNECTING -> DISCONNECTED is valid (registration timeout/failure)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.DISCONNECTED, reason="Registration timeout")
        assert machine.state == RunnerState.DISCONNECTED

    def test_idle_to_assigned_valid(self, machine):
        """IDLE -> ASSIGNED is valid (job pushed to runner)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        assert machine.state == RunnerState.ASSIGNED

    def test_idle_to_disconnected_valid(self, machine):
        """IDLE -> DISCONNECTED is valid (WebSocket closes gracefully)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DISCONNECTED)
        assert machine.state == RunnerState.DISCONNECTED

    def test_idle_to_dead_valid(self, machine):
        """IDLE -> DEAD is valid (silently-dead idle runner: heartbeat timeout, no work)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout while idle")
        assert machine.state == RunnerState.DEAD

    def test_assigned_to_busy_valid(self, machine):
        """ASSIGNED -> BUSY is valid (runner ACKs job)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        assert machine.state == RunnerState.BUSY

    def test_assigned_to_dead_valid(self, machine):
        """ASSIGNED -> DEAD is valid (ACK timeout)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD, reason="ACK timeout")
        assert machine.state == RunnerState.DEAD

    def test_assigned_to_disconnected_valid(self, machine):
        """ASSIGNED -> DISCONNECTED is valid (WebSocket closes)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DISCONNECTED)
        assert machine.state == RunnerState.DISCONNECTED

    def test_busy_to_idle_valid(self, machine):
        """BUSY -> IDLE is valid (step completes)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.IDLE)
        assert machine.state == RunnerState.IDLE

    def test_busy_to_dead_valid(self, machine):
        """BUSY -> DEAD is valid (heartbeat timeout)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout")
        assert machine.state == RunnerState.DEAD

    def test_busy_to_disconnected_valid(self, machine):
        """BUSY -> DISCONNECTED is valid (WebSocket closes)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.DISCONNECTED)
        assert machine.state == RunnerState.DISCONNECTED

    def test_dead_to_connecting_valid(self, machine):
        """DEAD -> CONNECTING is valid (runner reconnects)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD)
        machine.transition_to(RunnerState.CONNECTING)
        assert machine.state == RunnerState.CONNECTING

    def test_dead_to_disconnected_valid(self, machine):
        """DEAD -> DISCONNECTED is valid (socket close observed after death)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD, reason="ACK timeout")
        machine.transition_to(RunnerState.DISCONNECTED, reason="Socket closed after death")
        assert machine.state == RunnerState.DISCONNECTED


class TestInvalidTransitions:
    """Tests for invalid state transitions."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine starting in DISCONNECTED state."""
        return RunnerStateMachine(runner_id="test-runner-1")

    def test_disconnected_to_idle_invalid(self, machine):
        """DISCONNECTED -> IDLE is invalid (must go through CONNECTING)."""
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.IDLE)

    def test_disconnected_to_assigned_invalid(self, machine):
        """DISCONNECTED -> ASSIGNED is invalid."""
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.ASSIGNED)

    def test_disconnected_to_busy_invalid(self, machine):
        """DISCONNECTED -> BUSY is invalid."""
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.BUSY)

    def test_connecting_to_assigned_invalid(self, machine):
        """CONNECTING -> ASSIGNED is invalid (must be IDLE first)."""
        machine.transition_to(RunnerState.CONNECTING)
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.ASSIGNED)

    def test_connecting_to_busy_invalid(self, machine):
        """CONNECTING -> BUSY is invalid."""
        machine.transition_to(RunnerState.CONNECTING)
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.BUSY)

    def test_idle_to_busy_invalid(self, machine):
        """IDLE -> BUSY is invalid (must go through ASSIGNED)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.BUSY)

    def test_assigned_to_idle_invalid(self, machine):
        """ASSIGNED -> IDLE is invalid (must ACK first -> BUSY -> IDLE)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.IDLE)

    def test_dead_to_idle_invalid(self, machine):
        """DEAD -> IDLE is invalid (must reconnect first)."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD)

        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.IDLE)

    def test_dead_to_assigned_invalid(self, machine):
        """DEAD -> ASSIGNED is invalid."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD)

        with pytest.raises(InvalidRunnerTransitionError):
            machine.transition_to(RunnerState.ASSIGNED)

    def test_invalid_transition_error_is_value_error(self, machine):
        """InvalidRunnerTransitionError subclasses ValueError, matching the
        workspace/pipeline/step state machines' transition_to contract."""
        assert issubclass(InvalidRunnerTransitionError, ValueError)
        with pytest.raises(ValueError):
            machine.transition_to(RunnerState.BUSY)


class TestSilentDeathAndDisconnectAfterDeath:
    """Scenario tests for the audit-added transitions (IDLE->DEAD, DEAD->DISCONNECTED)."""

    def test_valid_transitions_map_includes_added_transitions(self):
        """VALID_TRANSITIONS contains the two audit-added edges."""
        assert RunnerState.DEAD in VALID_TRANSITIONS[RunnerState.IDLE]
        assert RunnerState.DISCONNECTED in VALID_TRANSITIONS[RunnerState.DEAD]

    def test_idle_death_has_no_step_to_recover(self):
        """An idle runner dying silently carries no step for recovery."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout while idle")

        assert machine.current_step_id is None
        assert machine.is_connected is False
        assert machine.is_available is False

    def test_silently_dead_idle_runner_can_reconnect(self):
        """IDLE -> DEAD -> CONNECTING -> IDLE round trip works."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout while idle")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)

        assert machine.state == RunnerState.IDLE

    def test_disconnect_after_death_then_fresh_connect(self):
        """DEAD -> DISCONNECTED (late socket close) allows a normal fresh connect."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout")
        machine.transition_to(RunnerState.DISCONNECTED, reason="Socket closed after death")

        assert machine.is_connected is False

        # Fresh connect from DISCONNECTED works as usual
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        assert machine.state == RunnerState.IDLE

    def test_disconnect_after_death_preserves_step_for_recovery(self):
        """The late socket close does not wipe the step ID recovery needs."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        machine.assign_step("step-123")
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout")
        machine.transition_to(RunnerState.DISCONNECTED, reason="Socket closed after death")

        assert machine.current_step_id == "step-123"


class TestReconnectionScenarios:
    """Tests for runner reconnection handling."""

    def test_reconnect_after_dead_goes_through_connecting(self):
        """Runner reconnecting after death must go through CONNECTING."""
        machine = RunnerStateMachine(runner_id="test-runner-1")

        # Get to dead state
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.DEAD)

        # Reconnect
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)

        assert machine.state == RunnerState.IDLE

    def test_reconnect_after_disconnect_valid(self):
        """Runner can reconnect after graceful disconnect."""
        machine = RunnerStateMachine(runner_id="test-runner-1")

        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DISCONNECTED)

        # Reconnect
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)

        assert machine.state == RunnerState.IDLE

    def test_multiple_reconnects_tracked(self):
        """Multiple reconnections are tracked in history."""
        machine = RunnerStateMachine(runner_id="test-runner-1")

        # First connection
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DISCONNECTED)

        # Second connection
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.DISCONNECTED)

        # Count transitions to CONNECTING
        connecting_count = sum(
            1 for t in machine.history if t.to_state == RunnerState.CONNECTING
        )
        assert connecting_count == 2


class TestJobLifecycle:
    """Tests for job execution state transitions."""

    @pytest.fixture
    def idle_machine(self):
        """Create a machine in IDLE state (ready for work)."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        return machine

    def test_full_job_success_lifecycle(self, idle_machine):
        """Full job lifecycle: IDLE -> ASSIGNED -> BUSY -> IDLE."""
        idle_machine.transition_to(RunnerState.ASSIGNED)
        idle_machine.transition_to(RunnerState.BUSY)
        idle_machine.transition_to(RunnerState.IDLE)

        assert idle_machine.state == RunnerState.IDLE

    def test_job_with_ack_timeout(self, idle_machine):
        """Job ACK timeout: IDLE -> ASSIGNED -> DEAD."""
        idle_machine.transition_to(RunnerState.ASSIGNED)
        idle_machine.transition_to(RunnerState.DEAD, reason="ACK timeout (5s)")

        assert idle_machine.state == RunnerState.DEAD

    def test_job_with_heartbeat_timeout(self, idle_machine):
        """Heartbeat timeout during execution: IDLE -> ... -> BUSY -> DEAD."""
        idle_machine.transition_to(RunnerState.ASSIGNED)
        idle_machine.transition_to(RunnerState.BUSY)
        idle_machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout (30s)")

        assert idle_machine.state == RunnerState.DEAD

    def test_multiple_jobs_sequential(self, idle_machine):
        """Runner can execute multiple jobs in sequence."""
        for _ in range(3):
            idle_machine.transition_to(RunnerState.ASSIGNED)
            idle_machine.transition_to(RunnerState.BUSY)
            idle_machine.transition_to(RunnerState.IDLE)

        assert idle_machine.state == RunnerState.IDLE
        # 2 connect transitions from the fixture + 3 jobs x 3 transitions
        assert len(idle_machine.history) == 11


class TestTransitionMetadata:
    """Tests for transition metadata recording."""

    @pytest.fixture
    def machine(self):
        """Create a new state machine."""
        return RunnerStateMachine(runner_id="test-runner-1")

    def test_transition_records_timestamp(self, machine):
        """State changes record timestamp."""
        before = datetime.utcnow()
        machine.transition_to(RunnerState.CONNECTING)
        after = datetime.utcnow()

        last_transition = machine.last_transition
        assert last_transition is not None
        assert before <= last_transition.timestamp <= after

    def test_transition_records_from_state(self, machine):
        """Transitions record the source state."""
        machine.transition_to(RunnerState.CONNECTING)

        last_transition = machine.last_transition
        assert last_transition.from_state == RunnerState.DISCONNECTED

    def test_transition_records_to_state(self, machine):
        """Transitions record the target state."""
        machine.transition_to(RunnerState.CONNECTING)

        last_transition = machine.last_transition
        assert last_transition.to_state == RunnerState.CONNECTING

    def test_transition_records_reason(self, machine):
        """Transitions can include a reason."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.DISCONNECTED, reason="Registration timeout")

        last_transition = machine.last_transition
        assert last_transition.reason == "Registration timeout"

    def test_transition_history_preserved(self, machine):
        """All transitions are preserved in history."""
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        machine.transition_to(RunnerState.ASSIGNED)
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.IDLE)

        assert len(machine.history) == 5
        assert machine.history[0].to_state == RunnerState.CONNECTING
        assert machine.history[1].to_state == RunnerState.IDLE
        assert machine.history[2].to_state == RunnerState.ASSIGNED
        assert machine.history[3].to_state == RunnerState.BUSY
        assert machine.history[4].to_state == RunnerState.IDLE


class TestHelperMethods:
    """Tests for state machine helper methods."""

    def test_is_available_true_for_idle(self):
        """is_available returns True for IDLE state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        assert machine.is_available is True

    def test_is_available_false_for_busy(self):
        """is_available returns False for BUSY state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.BUSY,
        )
        assert machine.is_available is False

    def test_is_available_false_for_assigned(self):
        """is_available returns False for ASSIGNED state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.ASSIGNED,
        )
        assert machine.is_available is False

    def test_is_available_false_for_dead(self):
        """is_available returns False for DEAD state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.DEAD,
        )
        assert machine.is_available is False

    def test_is_connected_true_for_idle(self):
        """is_connected returns True when WebSocket is open."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        assert machine.is_connected is True

    def test_is_connected_true_for_busy(self):
        """is_connected returns True for BUSY state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.BUSY,
        )
        assert machine.is_connected is True

    def test_is_connected_false_for_disconnected(self):
        """is_connected returns False for DISCONNECTED state."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        assert machine.is_connected is False

    def test_is_connected_false_for_dead(self):
        """is_connected returns False for DEAD state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.DEAD,
        )
        assert machine.is_connected is False

    def test_can_transition_true_for_valid(self):
        """can_transition_to() returns True for valid transitions."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        assert machine.can_transition_to(RunnerState.CONNECTING) is True

    def test_can_transition_false_for_invalid(self):
        """can_transition_to() returns False for invalid transitions."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        assert machine.can_transition_to(RunnerState.IDLE) is False

    def test_runner_id_stored(self):
        """Runner ID is accessible."""
        machine = RunnerStateMachine(runner_id="my-runner-123")
        assert machine.runner_id == "my-runner-123"


class TestHeartbeatTracking:
    """Tests for heartbeat timestamp tracking."""

    def test_heartbeat_updates_timestamp(self):
        """update_heartbeat() updates the last heartbeat timestamp."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.BUSY,
        )

        before = datetime.utcnow()
        machine.update_heartbeat()
        after = datetime.utcnow()

        assert before <= machine.last_heartbeat <= after

    def test_is_alive_true_when_recent_heartbeat(self):
        """is_alive() returns True when heartbeat is recent."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.BUSY,
        )
        machine.update_heartbeat()

        assert machine.is_alive(timeout_seconds=30) is True

    def test_is_alive_false_when_stale_heartbeat(self):
        """is_alive() returns False when heartbeat is stale."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.BUSY,
        )
        # Manually set old heartbeat
        machine._last_heartbeat = datetime.utcnow() - timedelta(seconds=60)

        assert machine.is_alive(timeout_seconds=30) is False

    def test_heartbeat_on_state_entry(self):
        """Heartbeat is updated when entering IDLE (runner just spoke to us)."""
        machine = RunnerStateMachine(runner_id="test-runner-1")

        before = datetime.utcnow()
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)
        after = datetime.utcnow()

        assert before <= machine.last_heartbeat <= after


class TestSerialization:
    """Tests for state machine serialization."""

    def test_to_dict_includes_state(self):
        """to_dict() includes current state."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )

        data = machine.to_dict()
        assert data["state"] == "idle"

    def test_to_dict_includes_runner_id(self):
        """to_dict() includes runner ID."""
        machine = RunnerStateMachine(runner_id="test-runner-1")

        data = machine.to_dict()
        assert data["runner_id"] == "test-runner-1"

    def test_to_dict_includes_history(self):
        """to_dict() includes transition history."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)

        data = machine.to_dict()
        assert "history" in data
        assert len(data["history"]) == 2

    def test_from_dict_restores_state(self):
        """from_dict() restores state correctly."""
        original = RunnerStateMachine(runner_id="test-runner-1")
        original.transition_to(RunnerState.CONNECTING)
        original.transition_to(RunnerState.IDLE)

        data = original.to_dict()
        restored = RunnerStateMachine.from_dict(data)

        assert restored.state == original.state
        assert restored.runner_id == original.runner_id

    def test_from_dict_restores_history(self):
        """from_dict() restores history correctly."""
        original = RunnerStateMachine(runner_id="test-runner-1")
        original.transition_to(RunnerState.CONNECTING)
        original.transition_to(RunnerState.IDLE)

        data = original.to_dict()
        restored = RunnerStateMachine.from_dict(data)

        assert len(restored.history) == len(original.history)


class TestCurrentStepTracking:
    """Tests for tracking current step execution."""

    def test_assign_step_sets_current_step(self):
        """Assigning a step stores the step ID."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )

        machine.assign_step("step-123")

        assert machine.current_step_id == "step-123"
        assert machine.state == RunnerState.ASSIGNED

    def test_complete_step_clears_current_step(self):
        """Completing a step clears the step ID."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )

        machine.assign_step("step-123")
        machine.transition_to(RunnerState.BUSY)  # ACK
        machine.complete_step()

        assert machine.current_step_id is None
        assert machine.state == RunnerState.IDLE

    def test_death_preserves_step_for_recovery(self):
        """When runner dies, step ID is preserved for recovery."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )

        machine.assign_step("step-123")
        machine.transition_to(RunnerState.BUSY)
        machine.transition_to(RunnerState.DEAD, reason="Heartbeat timeout")

        # Step ID preserved so we know what to requeue
        assert machine.current_step_id == "step-123"

    def test_complete_step_while_assigned_preserves_step_id(self):
        """complete_step() racing an un-ACKed assignment raises WITHOUT
        wiping the step ID (ASSIGNED -> IDLE is invalid; the transition is
        validated before any state is cleared)."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        machine.assign_step("step-123")
        assert machine.state == RunnerState.ASSIGNED

        with pytest.raises(InvalidRunnerTransitionError):
            machine.complete_step()

        # The raise must not have cleared the step recovery relies on
        assert machine.current_step_id == "step-123"
        assert machine.state == RunnerState.ASSIGNED


class TestHistoryBounds:
    """Tests for bounded transition history (ring buffer + truncated dump)."""

    def _cycle_jobs(self, machine: RunnerStateMachine, cycles: int) -> None:
        """Each cycle appends 3 transitions: ASSIGNED -> BUSY -> IDLE."""
        for n in range(cycles):
            machine.assign_step(f"step-{n}")
            machine.transition_to(RunnerState.BUSY)
            machine.complete_step()

    def test_history_capped_at_maxlen(self):
        """In-memory history never exceeds HISTORY_MAXLEN; oldest drop first."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        self._cycle_jobs(machine, 100)  # 300 transitions

        assert len(machine.history) == HISTORY_MAXLEN
        # The newest transition is retained
        assert machine.history[-1].reason == "Completed step step-99"

    def test_to_dict_emits_only_recent_history(self):
        """to_dict() serializes only the last SERIALIZED_HISTORY_LIMIT."""
        machine = RunnerStateMachine(
            runner_id="test-runner-1",
            initial_state=RunnerState.IDLE,
        )
        self._cycle_jobs(machine, 30)  # 90 transitions

        data = machine.to_dict()
        assert len(data["history"]) == SERIALIZED_HISTORY_LIMIT
        # Truncation keeps the tail, not the head
        assert data["history"][-1]["reason"] == "Completed step step-29"

    def test_short_history_serialized_in_full(self):
        """Histories below the limit round-trip without loss."""
        machine = RunnerStateMachine(runner_id="test-runner-1")
        machine.transition_to(RunnerState.CONNECTING)
        machine.transition_to(RunnerState.IDLE)

        data = machine.to_dict()
        assert len(data["history"]) == 2

        restored = RunnerStateMachine.from_dict(data)
        assert len(restored.history) == 2
        # The restored ring stays bounded like a fresh machine's
        assert restored._history.maxlen == HISTORY_MAXLEN
