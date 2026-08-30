"""
The `requires:` grammar - Phase 12.6 (cross-agent contract #5).

`ExecutionRouter.parse_requirements` is the ONE parser for a step's runner
requirements, and `normalize_arch` is applied BACKEND-SIDE to both the
parsed requirements and the labels a runner advertises - so the agent ships
raw `platform.machine()` and there is exactly one implementation (R3).

Grammar:

    runner_id   -> exact match against the runner's id
    runner_type -> exact match; "any" matches everything
    arch        -> normalized on BOTH sides
    has         -> subset containment against labels["has"]
    any other k -> equality against labels[k]

The last rule is the one with teeth. failure_01 IGNORED unknown keys, so
`requires: {gpu: a100}` matched every runner in the fleet - a pin that reads
as a constraint and behaves as a wildcard. The tests below pin both halves:
the parser keeps the key, and the matcher enforces it.

The parser and the matcher are tested TOGETHER here on purpose: a grammar is
only correct if the thing that consumes it agrees, and these two live in
different modules (router / model) owned by different agents.
"""
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.runner import Runner  # noqa: E402
from app.services.workspace.execution_router import (  # noqa: E402
    KNOWN_REQUIREMENT_KEYS,
    ExecutionRouter,
)


@pytest.fixture
def router():
    return ExecutionRouter()


def make_runner(runner_id="r1", runner_type="generic", labels=None) -> Runner:
    runner = Runner(id=runner_id, runner_type=runner_type)
    runner.set_labels(labels or {})
    return runner


# -----------------------------------------------------------------------------
# parse_requirements: shape
# -----------------------------------------------------------------------------

class TestParseShape:
    def test_absent_requires_is_empty(self, router):
        assert router.parse_requirements({"command": "pytest"}, "script") == {}

    def test_empty_requires_is_empty(self, router):
        assert router.parse_requirements({"requires": {}}, "script") == {}

    def test_requires_is_copied_not_aliased(self, router):
        step_config = {"requires": {"zone": "workshop"}}
        parsed = router.parse_requirements(step_config, "script")
        parsed["zone"] = "mutated"
        assert step_config["requires"] == {"zone": "workshop"}

    @pytest.mark.parametrize("bad", [["gpio"], "arm64", 7, True])
    def test_non_mapping_requires_raises(self, router, bad):
        """A list or a string there is an authoring mistake whose silent
        acceptance would produce a pin that matches everything."""
        with pytest.raises(ValueError) as exc:
            router.parse_requirements({"requires": bad}, "script")
        assert "mapping" in str(exc.value)

    def test_error_message_names_the_known_keys(self, router):
        with pytest.raises(ValueError) as exc:
            router.parse_requirements({"requires": ["gpio"]}, "script")
        message = str(exc.value)
        for key in KNOWN_REQUIREMENT_KEYS:
            assert key in message


# -----------------------------------------------------------------------------
# parse_requirements: normalization
# -----------------------------------------------------------------------------

class TestParseNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("x86_64", "amd64"),
            ("amd64", "amd64"),
            ("x64", "amd64"),
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
            ("armv8", "arm64"),
            ("armv7l", "armv7"),
            ("armhf", "armv7"),
            ("RISCV64", "riscv64"),
        ],
    )
    def test_arch_is_normalized(self, router, raw, expected):
        parsed = router.parse_requirements({"requires": {"arch": raw}}, "script")
        assert parsed["arch"] == expected

    def test_has_string_becomes_a_list(self, router):
        parsed = router.parse_requirements({"requires": {"has": "gpio"}}, "script")
        assert parsed["has"] == ["gpio"]

    def test_has_list_is_preserved_in_order(self, router):
        parsed = router.parse_requirements(
            {"requires": {"has": ["gpio", "camera"]}}, "script"
        )
        assert parsed["has"] == ["gpio", "camera"]

    def test_has_set_is_sorted_for_stable_persistence(self, router):
        """`runner_requirements` is persisted as JSON so a requeued step is
        re-matchable after a restart; an unordered set would serialize
        differently every run."""
        parsed = router.parse_requirements(
            {"requires": {"has": {"camera", "gpio"}}}, "script"
        )
        assert parsed["has"] == ["camera", "gpio"]

    def test_runner_id_is_stringified(self, router):
        parsed = router.parse_requirements({"requires": {"runner_id": 7}}, "script")
        assert parsed["runner_id"] == "7"

    def test_unknown_keys_pass_through_untouched(self, router):
        parsed = router.parse_requirements(
            {"requires": {"zone": "workshop", "gpu": "a100"}}, "script"
        )
        assert parsed == {"zone": "workshop", "gpu": "a100"}


# -----------------------------------------------------------------------------
# parse_requirements: the top-level runner_type sugar
# -----------------------------------------------------------------------------

class TestRunnerTypeSugar:
    @pytest.mark.parametrize("step_type", ["script", "docker"])
    def test_top_level_runner_type_is_sugar_on_script_and_docker(
        self, router, step_type
    ):
        parsed = router.parse_requirements(
            {"command": "x", "runner_type": "generic"}, step_type
        )
        assert parsed == {"runner_type": "generic"}

    def test_sugar_does_not_apply_to_agent_steps(self, router):
        """On an agent step `runner_type` names the AI flavor (12.5)."""
        parsed = router.parse_requirements(
            {"prompt": "x", "runner_type": "claude-code"}, "agent"
        )
        assert parsed == {}

    def test_agent_requires_runner_type_is_still_honored(self, router):
        parsed = router.parse_requirements(
            {"prompt": "x", "requires": {"runner_type": "generic"}}, "agent"
        )
        assert parsed == {"runner_type": "generic"}

    def test_explicit_requires_beats_the_sugar(self, router):
        parsed = router.parse_requirements(
            {"runner_type": "sugar", "requires": {"runner_type": "explicit"}},
            "script",
        )
        assert parsed == {"runner_type": "explicit"}

    def test_sugar_applies_when_step_type_is_unknown_to_the_caller(self, router):
        """parse_requirements(step_config) with no step_type is the
        cross-agent contract's documented arity; it applies the sugar."""
        parsed = router.parse_requirements({"runner_type": "generic"})
        assert parsed == {"runner_type": "generic"}


# -----------------------------------------------------------------------------
# The other half: the matcher agrees with the parser
# -----------------------------------------------------------------------------

class TestParserMatcherAgreement:
    def test_empty_requirements_match_everything(self, router):
        parsed = router.parse_requirements({"command": "x"}, "script")
        assert make_runner().matches_requirements(parsed) is True

    def test_runner_id_pin(self, router):
        parsed = router.parse_requirements(
            {"requires": {"runner_id": "pi-workshop-1"}}, "script"
        )
        assert make_runner(runner_id="pi-workshop-1").matches_requirements(parsed)
        assert not make_runner(runner_id="other").matches_requirements(parsed)

    def test_runner_type_any_is_a_wildcard(self, router):
        parsed = router.parse_requirements({"runner_type": "any"}, "script")
        assert make_runner(runner_type="generic").matches_requirements(parsed)
        assert make_runner(runner_type="claude-code").matches_requirements(parsed)

    def test_arch_normalizes_on_both_sides(self, router):
        """The step says aarch64, the agent reported arm64 - one match."""
        parsed = router.parse_requirements({"requires": {"arch": "aarch64"}}, "script")
        assert make_runner(labels={"arch": "arm64"}).matches_requirements(parsed)
        assert make_runner(labels={"arch": "armv8"}).matches_requirements(parsed)
        assert not make_runner(labels={"arch": "x86_64"}).matches_requirements(parsed)

    def test_has_is_subset_containment(self, router):
        parsed = router.parse_requirements(
            {"requires": {"has": ["gpio", "camera"]}}, "script"
        )
        assert make_runner(
            labels={"has": ["gpio", "camera", "docker"]}
        ).matches_requirements(parsed)
        assert not make_runner(labels={"has": ["gpio"]}).matches_requirements(parsed)
        assert not make_runner(labels={}).matches_requirements(parsed)

    def test_unknown_key_is_enforced_not_ignored(self, router):
        """THE failure_01 regression: `requires: {gpu: a100}` matched every
        runner in the fleet because unknown keys were dropped."""
        parsed = router.parse_requirements({"requires": {"gpu": "a100"}}, "script")
        assert parsed == {"gpu": "a100"}
        assert make_runner(labels={"gpu": "a100"}).matches_requirements(parsed)
        assert not make_runner(labels={"gpu": "t4"}).matches_requirements(parsed)
        assert not make_runner(labels={}).matches_requirements(parsed)

    def test_the_dogfood_lane_label(self, router):
        """The loopback runner-agent carries has:[remote-lane]; the
        remote-probe step pins exactly that and nothing else does."""
        parsed = router.parse_requirements(
            {"requires": {"has": ["remote-lane"]}}, "script"
        )
        lane = make_runner(
            runner_id="dogfood-loopback",
            labels={"arch": "amd64", "has": ["docker", "remote-lane"]},
        )
        other = make_runner(runner_id="some-pi", labels={"has": ["docker"]})
        assert lane.matches_requirements(parsed)
        assert not other.matches_requirements(parsed)

    def test_every_clause_must_hold(self, router):
        parsed = router.parse_requirements(
            {
                "requires": {
                    "runner_type": "generic",
                    "arch": "x86_64",
                    "has": ["docker"],
                    "zone": "workshop",
                }
            },
            "script",
        )
        good = make_runner(
            runner_type="generic",
            labels={"arch": "amd64", "has": ["docker"], "zone": "workshop"},
        )
        assert good.matches_requirements(parsed)

        wrong_zone = make_runner(
            runner_type="generic",
            labels={"arch": "amd64", "has": ["docker"], "zone": "lab"},
        )
        assert not wrong_zone.matches_requirements(parsed)


# -----------------------------------------------------------------------------
# The parser is the only one (cross-agent contract #5)
# -----------------------------------------------------------------------------

class TestSingleParser:
    def test_decide_returns_what_parse_requirements_returns(self, router):
        step_config = {
            "command": "x",
            "requires": {"arch": "aarch64", "has": "gpio", "zone": "workshop"},
        }
        decision = router.decide("script", step_config)
        assert decision.requirements == router.parse_requirements(
            step_config, "script"
        )

    def test_no_other_module_parses_requires(self):
        """A second parser is how the grammar drifts. Only the router (and
        its own tests) may read the raw `requires` key out of a step config.
        """
        import subprocess

        root = Path(__file__).parent.parent.parent.parent
        result = subprocess.run(
            [
                "git",
                "grep",
                "-l",
                '"requires"',
                "--",
                "backend/app",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        hits = {
            line.strip().replace("\\", "/")
            for line in result.stdout.splitlines()
            if line.strip()
        }
        allowed = {"backend/app/services/workspace/execution_router.py"}
        assert hits <= allowed, f"a second `requires:` parser appeared: {hits - allowed}"
