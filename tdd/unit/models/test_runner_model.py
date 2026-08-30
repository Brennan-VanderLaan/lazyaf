"""Unit tests for the Runner registry model (Phase 12.6).

Supersedes the polling-era ``test_runner.py``, which asserted the deleted
``RunnerStatus`` vocabulary (idle/busy/offline) and the container/job columns
that go away in migration 0007. Everything still meaningful from that file is
carried forward here.

The centre of gravity is ``matches_requirements``: the requirement grammar is
the only thing standing between a ``requires:`` block and the wrong machine,
and failure_01's version silently matched EVERY runner for any key it did not
recognize.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Runner
from app.models.runner import DEFAULT_RUNNER_TYPE, _DEFAULT_STATUS
from app.services.execution.runner_state import RunnerState

from tdd.shared.assertions import assert_model_has_id
from tdd.shared.factories import RunnerFactory


class TestRunnerVocabulary:
    """RunnerState is the single status vocabulary (cross-agent contract #4)."""

    def test_runner_status_enum_is_deleted(self):
        """The old three-value RunnerStatus must not come back.

        Two enums for one concept is exactly how failure_01 ended up with a
        DB status that never left 'idle' while the state machine walked a
        different path.
        """
        import app.models.runner as runner_module

        assert not hasattr(runner_module, "RunnerStatus")

    def test_default_status_matches_runner_state(self):
        """The column default is pinned to RunnerState.DISCONNECTED.

        The model cannot import RunnerState at module scope (that would drag
        app.services.execution.__init__, and therefore docker, into a models
        import), so the literal is duplicated - and pinned here.
        """
        assert _DEFAULT_STATUS == RunnerState.DISCONNECTED.value

    def test_table_name(self):
        assert Runner.__tablename__ == "runners"


class TestRunnerModel:
    """Structure and defaults."""

    def test_runner_creation(self):
        runner = RunnerFactory.build()
        assert_model_has_id(runner)

    def test_runner_defaults_to_disconnected(self):
        runner = RunnerFactory.build()
        assert runner.status == RunnerState.DISCONNECTED.value

    def test_runner_has_heartbeat(self):
        runner = RunnerFactory.build()
        assert runner.last_heartbeat is not None

    def test_runner_has_no_step_by_default(self):
        runner = RunnerFactory.build()
        assert runner.current_step_execution_id is None

    def test_default_runner_type_constant(self):
        """The model default and the migration server_default agree."""
        assert DEFAULT_RUNNER_TYPE == "claude-code"

    def test_idle_trait(self):
        runner = RunnerFactory.build(idle=True)
        assert runner.status == RunnerState.IDLE.value
        assert runner.websocket_id is not None

    def test_busy_trait_carries_a_step(self):
        """current_step_execution_id is what makes a busy runner recoverable.

        failure_01 declared this column and never wrote it, which silently
        neutered every recovery path.
        """
        runner = RunnerFactory.build(busy=True)
        assert runner.status == RunnerState.BUSY.value
        assert runner.current_step_execution_id is not None


class TestRunnerLabels:
    """labels round-trip through JSON."""

    def test_labels_default_to_empty_dict(self):
        runner = RunnerFactory.build()
        assert runner.get_labels() == {}

    def test_set_and_get_labels(self):
        runner = RunnerFactory.build()
        runner.set_labels({"arch": "arm64", "has": ["gpio", "camera"]})
        assert runner.get_labels() == {"arch": "arm64", "has": ["gpio", "camera"]}

    def test_set_labels_none_is_empty(self):
        runner = RunnerFactory.build()
        runner.set_labels(None)
        assert runner.get_labels() == {}

    def test_malformed_labels_read_as_empty(self):
        """A corrupt blob must make a runner match NOTHING, never crash the
        dispatcher scanning every row."""
        runner = RunnerFactory.build()
        runner.labels = "{not json"
        assert runner.get_labels() == {}

    def test_non_dict_labels_read_as_empty(self):
        runner = RunnerFactory.build()
        runner.labels = "[1, 2, 3]"
        assert runner.get_labels() == {}


class TestMatchesRequirements:
    """The requirement grammar (design section 2.4)."""

    def test_empty_requirements_match_everything(self):
        runner = RunnerFactory.build(idle=True)
        assert runner.matches_requirements({}) is True

    def test_none_requirements_match_everything(self):
        runner = RunnerFactory.build(idle=True)
        assert runner.matches_requirements(None) is True

    def test_runner_id_pin_matches(self):
        runner = RunnerFactory.build(id="pi-workshop-1")
        assert runner.matches_requirements({"runner_id": "pi-workshop-1"}) is True

    def test_runner_id_pin_rejects_other_runners(self):
        runner = RunnerFactory.build(id="pi-workshop-2")
        assert runner.matches_requirements({"runner_id": "pi-workshop-1"}) is False

    def test_runner_type_exact_match(self):
        runner = RunnerFactory.build(runner_type="generic")
        assert runner.matches_requirements({"runner_type": "generic"}) is True

    def test_runner_type_mismatch(self):
        runner = RunnerFactory.build(runner_type="claude-code")
        assert runner.matches_requirements({"runner_type": "generic"}) is False

    def test_runner_type_any_is_a_wildcard(self):
        runner = RunnerFactory.build(runner_type="claude-code")
        assert runner.matches_requirements({"runner_type": "any"}) is True

    def test_arch_matches_after_normalization_on_both_sides(self):
        """The label says x86_64, the pipeline says amd64 - same machine."""
        runner = RunnerFactory.build()
        runner.set_labels({"arch": "x86_64"})
        assert runner.matches_requirements({"arch": "amd64"}) is True

    def test_arch_normalizes_the_requirement_too(self):
        runner = RunnerFactory.build()
        runner.set_labels({"arch": "arm64"})
        assert runner.matches_requirements({"arch": "aarch64"}) is True

    def test_arch_mismatch_rejects(self):
        runner = RunnerFactory.build()
        runner.set_labels({"arch": "amd64"})
        assert runner.matches_requirements({"arch": "arm64"}) is False

    def test_arch_missing_label_rejects(self):
        runner = RunnerFactory.build()
        assert runner.matches_requirements({"arch": "arm64"}) is False

    def test_has_is_subset_containment(self):
        runner = RunnerFactory.build()
        runner.set_labels({"has": ["docker", "gpio", "camera"]})
        assert runner.matches_requirements({"has": ["gpio", "camera"]}) is True

    def test_has_rejects_a_missing_capability(self):
        runner = RunnerFactory.build()
        runner.set_labels({"has": ["docker"]})
        assert runner.matches_requirements({"has": ["gpio"]}) is False

    def test_has_empty_list_matches(self):
        runner = RunnerFactory.build()
        runner.set_labels({"has": []})
        assert runner.matches_requirements({"has": []}) is True

    def test_has_accepts_a_bare_string(self):
        runner = RunnerFactory.build()
        runner.set_labels({"has": ["gpio"]})
        assert runner.matches_requirements({"has": "gpio"}) is True

    def test_unknown_key_matches_against_labels(self):
        runner = RunnerFactory.build()
        runner.set_labels({"zone": "workshop"})
        assert runner.matches_requirements({"zone": "workshop"}) is True

    def test_unknown_key_is_NOT_ignored(self):
        """THE failure_01 regression: it ignored unrecognized requirement
        keys, so `requires: {gpu: a100}` matched every runner in the fleet.
        An unsatisfiable pin must be visibly unsatisfiable."""
        runner = RunnerFactory.build()
        runner.set_labels({"arch": "amd64"})
        assert runner.matches_requirements({"gpu": "a100"}) is False

    def test_all_clauses_must_hold(self):
        runner = RunnerFactory.build(runner_type="generic")
        runner.set_labels({"arch": "arm64", "has": ["gpio"], "zone": "workshop"})
        assert (
            runner.matches_requirements(
                {
                    "runner_type": "generic",
                    "arch": "aarch64",
                    "has": ["gpio"],
                    "zone": "workshop",
                }
            )
            is True
        )
        assert (
            runner.matches_requirements(
                {
                    "runner_type": "generic",
                    "arch": "aarch64",
                    "has": ["gpio"],
                    "zone": "garage",
                }
            )
            is False
        )


class TestAvailabilityProperties:
    """is_available / is_connected delegate to RunnerState."""

    def test_only_idle_is_available(self):
        for state in RunnerState:
            runner = RunnerFactory.build(status=state.value)
            assert runner.is_available is (state is RunnerState.IDLE), state

    @pytest.mark.parametrize(
        "state,connected",
        [
            (RunnerState.DISCONNECTED, False),
            (RunnerState.CONNECTING, True),
            (RunnerState.IDLE, True),
            (RunnerState.ASSIGNED, True),
            (RunnerState.BUSY, True),
            (RunnerState.DEAD, False),
        ],
    )
    def test_is_connected_matches_the_state_machine(self, state, connected):
        runner = RunnerFactory.build(status=state.value)
        assert runner.is_connected is connected
