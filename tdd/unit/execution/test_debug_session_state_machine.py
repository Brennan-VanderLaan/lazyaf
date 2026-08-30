"""Debug session state machine contract - Phase 12.7.

33 tests ported verbatim from failure_01, plus 3 for the transitions that
branch never had.

PORT NOTE (R4, stated not silent): the source file opened with a
`try/except ImportError` around its imports AND a module-level
``pytestmark = pytest.mark.skipif(not DEBUG_MODULE_AVAILABLE, ...)``. That is
a conditional skip standing in front of every assertion in the file - the
exact mechanism that let failure_01's suite stay green over deleted code
(`test_polling_removal.py` self-skipped for three and a half hours before
anyone noticed). Both are STRIPPED here: the import is unguarded, so if
`debug_state` ever moves or breaks, this file fails loudly instead of
quietly reporting 33 skips.

THE THREE ADDED TESTS pin the edges failure_01 was missing, which is why
multi-breakpoint debugging never worked there: its `resume` drove the session
to ENDED, so the second breakpoint had no live session to pause into.
`PENDING` means "executing, not at a breakpoint" - the state a resumed
session returns to. None of the 33 ported tests assert that
`WAITING_AT_BP -> PENDING` or `CONNECTED -> PENDING` is refused (the only
invalidity assertions in the file are PENDING->CONNECTED, PENDING->TIMEOUT
and the two terminal-state cases), so the new edges break none of them.
"""

from datetime import datetime, timedelta

import pytest

from app.services.execution.debug_state import (
    DebugState,
    DebugStateMachine,
    DebugStateTransition,
    InvalidDebugTransitionError,
)


class TestDebugStates:
    """Tests for DebugState enum values."""

    def test_has_pending_state(self):
        """DebugState has PENDING state."""
        assert DebugState.PENDING is not None
        assert DebugState.PENDING.value == "pending"

    def test_has_waiting_at_bp_state(self):
        """DebugState has WAITING_AT_BP state."""
        assert DebugState.WAITING_AT_BP is not None
        assert DebugState.WAITING_AT_BP.value == "waiting_at_bp"

    def test_has_connected_state(self):
        """DebugState has CONNECTED state."""
        assert DebugState.CONNECTED is not None
        assert DebugState.CONNECTED.value == "connected"

    def test_has_timeout_state(self):
        """DebugState has TIMEOUT state."""
        assert DebugState.TIMEOUT is not None
        assert DebugState.TIMEOUT.value == "timeout"

    def test_has_ended_state(self):
        """DebugState has ENDED state."""
        assert DebugState.ENDED is not None
        assert DebugState.ENDED.value == "ended"


class TestDebugStateTransitions:
    """Tests for valid state transitions."""

    def test_pending_to_waiting_at_bp_valid(self):
        """Can transition from PENDING to WAITING_AT_BP (breakpoint hit)."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        assert machine.can_transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit at step 2")
        assert machine.state == DebugState.WAITING_AT_BP

    def test_pending_to_ended_valid(self):
        """Can transition from PENDING to ENDED (pipeline completed without breakpoint)."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        assert machine.can_transition(DebugState.ENDED)
        machine.transition(DebugState.ENDED, reason="Pipeline completed")
        assert machine.state == DebugState.ENDED

    def test_pending_to_connected_invalid(self):
        """Cannot transition directly from PENDING to CONNECTED."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        assert not machine.can_transition(DebugState.CONNECTED)
        with pytest.raises(InvalidDebugTransitionError):
            machine.transition(DebugState.CONNECTED)

    def test_pending_to_timeout_invalid(self):
        """Cannot transition directly from PENDING to TIMEOUT."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        assert not machine.can_transition(DebugState.TIMEOUT)

    def test_waiting_at_bp_to_connected_valid(self):
        """Can transition from WAITING_AT_BP to CONNECTED (user connects)."""
        machine = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert machine.can_transition(DebugState.CONNECTED)
        machine.transition(DebugState.CONNECTED, reason="CLI connected")
        assert machine.state == DebugState.CONNECTED

    def test_waiting_at_bp_to_timeout_valid(self):
        """Can transition from WAITING_AT_BP to TIMEOUT."""
        machine = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert machine.can_transition(DebugState.TIMEOUT)
        machine.transition(DebugState.TIMEOUT, reason="Session expired")
        assert machine.state == DebugState.TIMEOUT

    def test_waiting_at_bp_to_ended_valid(self):
        """Can transition from WAITING_AT_BP to ENDED (user aborted)."""
        machine = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert machine.can_transition(DebugState.ENDED)
        machine.transition(DebugState.ENDED, reason="User aborted")
        assert machine.state == DebugState.ENDED

    def test_connected_to_ended_valid(self):
        """Can transition from CONNECTED to ENDED (user resumed or aborted)."""
        machine = DebugStateMachine(initial_state=DebugState.CONNECTED)
        assert machine.can_transition(DebugState.ENDED)
        machine.transition(DebugState.ENDED, reason="User resumed")
        assert machine.state == DebugState.ENDED

    def test_connected_to_timeout_valid(self):
        """Can transition from CONNECTED to TIMEOUT (idle too long)."""
        machine = DebugStateMachine(initial_state=DebugState.CONNECTED)
        assert machine.can_transition(DebugState.TIMEOUT)
        machine.transition(DebugState.TIMEOUT, reason="Idle timeout")
        assert machine.state == DebugState.TIMEOUT

    def test_connected_to_waiting_at_bp_valid(self):
        """Can transition from CONNECTED to WAITING_AT_BP (user disconnected)."""
        machine = DebugStateMachine(initial_state=DebugState.CONNECTED)
        assert machine.can_transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.WAITING_AT_BP, reason="CLI disconnected")
        assert machine.state == DebugState.WAITING_AT_BP


class TestResumeReturnsToPending:
    """The 12.7 edges. THE multi-breakpoint fix (contract C5).

    failure_01 drove `resume` to ENDED, a terminal state, so the second
    breakpoint in a run had no live session to pause into and simply never
    fired. `PENDING` means "executing, not at a breakpoint": it is where a
    resumed session belongs, and only abort, timeout and run completion may
    reach a terminal state.
    """

    def test_waiting_at_bp_to_pending_valid(self):
        """Resume WITHOUT ever attaching a terminal returns to PENDING."""
        machine = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert machine.can_transition(DebugState.PENDING)
        machine.transition(DebugState.PENDING, reason="resumed")
        assert machine.state == DebugState.PENDING
        assert not machine.is_terminal

    def test_connected_to_pending_valid(self):
        """Resume AFTER attaching returns to PENDING, not ENDED."""
        machine = DebugStateMachine(initial_state=DebugState.CONNECTED)
        assert machine.can_transition(DebugState.PENDING)
        machine.transition(DebugState.PENDING, reason="resumed")
        assert machine.state == DebugState.PENDING
        assert not machine.is_terminal

    def test_multi_breakpoint_cycle(self):
        """One machine survives two full breakpoint cycles.

        This is the whole point: pause, attach, resume, pause AGAIN, attach,
        resume, end. Six transitions in one session - impossible when resume
        was terminal.
        """
        machine = DebugStateMachine()
        machine.transition(DebugState.WAITING_AT_BP, reason="breakpoint 1")
        machine.transition(DebugState.CONNECTED, reason="attached")
        machine.transition(DebugState.PENDING, reason="resumed")
        machine.transition(DebugState.WAITING_AT_BP, reason="breakpoint 2")
        machine.transition(DebugState.CONNECTED, reason="attached again")
        machine.transition(DebugState.ENDED, reason="pipeline completed")

        assert machine.state == DebugState.ENDED
        assert machine.is_terminal
        assert len(machine.history) == 6


class TestTerminalStates:
    """Tests for terminal states (no further transitions allowed)."""

    def test_timeout_is_terminal(self):
        """TIMEOUT is a terminal state - no transitions allowed."""
        machine = DebugStateMachine(initial_state=DebugState.TIMEOUT)
        assert machine.is_terminal
        assert not machine.can_transition(DebugState.PENDING)
        assert not machine.can_transition(DebugState.WAITING_AT_BP)
        assert not machine.can_transition(DebugState.CONNECTED)
        assert not machine.can_transition(DebugState.ENDED)

    def test_ended_is_terminal(self):
        """ENDED is a terminal state - no transitions allowed."""
        machine = DebugStateMachine(initial_state=DebugState.ENDED)
        assert machine.is_terminal
        assert not machine.can_transition(DebugState.PENDING)
        assert not machine.can_transition(DebugState.WAITING_AT_BP)
        assert not machine.can_transition(DebugState.CONNECTED)
        assert not machine.can_transition(DebugState.TIMEOUT)

    def test_transition_from_terminal_raises(self):
        """Transitioning from terminal state raises exception."""
        machine = DebugStateMachine(initial_state=DebugState.ENDED)
        with pytest.raises(InvalidDebugTransitionError) as exc_info:
            machine.transition(DebugState.PENDING)
        assert "terminal" in str(exc_info.value).lower()


class TestDebugStateMachine:
    """Tests for DebugStateMachine class functionality."""

    def test_initial_state_default_pending(self):
        """Default initial state is PENDING."""
        machine = DebugStateMachine()
        assert machine.state == DebugState.PENDING

    def test_initial_state_custom(self):
        """Can specify custom initial state."""
        machine = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert machine.state == DebugState.WAITING_AT_BP

    def test_transition_records_history(self):
        """Each transition is recorded in history."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit")
        machine.transition(DebugState.CONNECTED, reason="CLI connected")

        assert len(machine.history) == 2
        assert machine.history[0].from_state == DebugState.PENDING
        assert machine.history[0].to_state == DebugState.WAITING_AT_BP
        assert machine.history[1].from_state == DebugState.WAITING_AT_BP
        assert machine.history[1].to_state == DebugState.CONNECTED

    def test_transition_records_timestamp(self):
        """Transitions record timestamp."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        before = datetime.utcnow()
        machine.transition(DebugState.WAITING_AT_BP)
        after = datetime.utcnow()

        assert machine.history[0].timestamp >= before
        assert machine.history[0].timestamp <= after

    def test_transition_records_reason(self):
        """Transitions record reason when provided."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        machine.transition(DebugState.WAITING_AT_BP, reason="Hit breakpoint at step 3")

        assert machine.history[0].reason == "Hit breakpoint at step 3"

    def test_last_transition_property(self):
        """last_transition returns most recent transition."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        assert machine.last_transition is None

        machine.transition(DebugState.WAITING_AT_BP)
        assert machine.last_transition.to_state == DebugState.WAITING_AT_BP

        machine.transition(DebugState.CONNECTED)
        assert machine.last_transition.to_state == DebugState.CONNECTED

    def test_duration_calculation(self):
        """Duration calculated from first to last transition."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)

        # No transitions yet
        assert machine.duration is None

        machine.transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.CONNECTED)

        duration = machine.duration
        assert duration is not None
        assert isinstance(duration, timedelta)

    def test_is_terminal_property(self):
        """is_terminal property correctly identifies terminal states."""
        pending = DebugStateMachine(initial_state=DebugState.PENDING)
        assert not pending.is_terminal

        waiting = DebugStateMachine(initial_state=DebugState.WAITING_AT_BP)
        assert not waiting.is_terminal

        connected = DebugStateMachine(initial_state=DebugState.CONNECTED)
        assert not connected.is_terminal

        timeout = DebugStateMachine(initial_state=DebugState.TIMEOUT)
        assert timeout.is_terminal

        ended = DebugStateMachine(initial_state=DebugState.ENDED)
        assert ended.is_terminal


class TestDebugStateSerialization:
    """Tests for state machine serialization/deserialization."""

    def test_to_dict(self):
        """State machine can be serialized to dictionary."""
        machine = DebugStateMachine(initial_state=DebugState.PENDING)
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit")

        data = machine.to_dict()

        assert data["state"] == "waiting_at_bp"
        assert data["is_terminal"] is False
        assert "created_at" in data
        assert "history" in data
        assert len(data["history"]) == 1

    def test_from_dict(self):
        """State machine can be deserialized from dictionary."""
        original = DebugStateMachine(initial_state=DebugState.PENDING)
        original.transition(DebugState.WAITING_AT_BP, reason="Breakpoint hit")
        original.transition(DebugState.CONNECTED, reason="CLI connected")

        data = original.to_dict()
        restored = DebugStateMachine.from_dict(data)

        assert restored.state == original.state
        assert len(restored.history) == len(original.history)
        assert restored.is_terminal == original.is_terminal


class TestCompleteWorkflows:
    """Tests for complete debug session workflows."""

    def test_successful_debug_workflow(self):
        """Test successful debug -> connect -> resume workflow."""
        machine = DebugStateMachine()

        # Pipeline starts debug run
        assert machine.state == DebugState.PENDING

        # Hits breakpoint
        machine.transition(DebugState.WAITING_AT_BP, reason="Breakpoint at step 2")
        assert machine.state == DebugState.WAITING_AT_BP

        # User connects
        machine.transition(DebugState.CONNECTED, reason="CLI connected")
        assert machine.state == DebugState.CONNECTED

        # User resumes
        machine.transition(DebugState.ENDED, reason="User resumed")
        assert machine.state == DebugState.ENDED
        assert machine.is_terminal

    def test_abort_workflow(self):
        """Test debug -> connect -> abort workflow."""
        machine = DebugStateMachine()

        machine.transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.CONNECTED)
        machine.transition(DebugState.ENDED, reason="User aborted")

        assert machine.state == DebugState.ENDED
        assert len(machine.history) == 3

    def test_timeout_while_waiting_workflow(self):
        """Test timeout while waiting for user to connect."""
        machine = DebugStateMachine()

        machine.transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.TIMEOUT, reason="No connection within 1 hour")

        assert machine.state == DebugState.TIMEOUT
        assert machine.is_terminal

    def test_disconnect_reconnect_workflow(self):
        """Test user disconnect and reconnect."""
        machine = DebugStateMachine()

        machine.transition(DebugState.WAITING_AT_BP)
        machine.transition(DebugState.CONNECTED)

        # User disconnects
        machine.transition(DebugState.WAITING_AT_BP, reason="CLI disconnected")
        assert machine.state == DebugState.WAITING_AT_BP

        # User reconnects
        machine.transition(DebugState.CONNECTED, reason="CLI reconnected")
        assert machine.state == DebugState.CONNECTED

        # Resume
        machine.transition(DebugState.ENDED, reason="User resumed")
        assert machine.is_terminal

    def test_pipeline_completes_without_breakpoint(self):
        """Test pipeline completing without hitting any breakpoint."""
        machine = DebugStateMachine()

        # Pipeline finishes before reaching any breakpoints
        machine.transition(DebugState.ENDED, reason="Pipeline completed")

        assert machine.state == DebugState.ENDED
        assert machine.is_terminal
        assert len(machine.history) == 1


class TestPortedFileCarriesNoConditionalSkip:
    """R4 / contract C19, asserted rather than asserted-about.

    The ported file's import must be unguarded and no module-level skipif may
    stand in front of these assertions. A future edit that reintroduces the
    failure_01 harness fails HERE, in the file it would silence.
    """

    def test_transition_record_type_is_importable(self):
        """The dataclass the guarded import used to shadow is real."""
        assert DebugStateTransition is not None
        record = DebugStateTransition(
            from_state=DebugState.PENDING,
            to_state=DebugState.WAITING_AT_BP,
            timestamp=datetime.utcnow(),
        )
        assert "pending -> waiting_at_bp" in repr(record)

    def test_module_declares_no_pytestmark_skip(self):
        """This module carries no module-level skip marker."""
        import sys

        module = sys.modules[__name__]
        assert not hasattr(module, "pytestmark"), (
            "a module-level pytestmark reappeared in the ported state-machine "
            "suite - that is the failure_01 fake-green mechanism (R4)"
        )
