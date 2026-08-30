"""
Tests for scripts/ci_gate.py - the R4 skip-baseline / executed-floor gate.

The gate is exercised through its real CLI (subprocess) because that is
exactly how the dogfood pipeline invokes it inside a runner container.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "ci_gate.py"
COMMITTED_BASELINE = REPO_ROOT / "tdd" / "skip_baseline.json"
COMMITTED_FLOORS = REPO_ROOT / "tdd" / "tier_floors.json"


def run_gate(*args: str):
    """Run the gate CLI and return the completed process."""
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        capture_output=True,
        text=True,
    )


def junitxml(cases: list[str]) -> str:
    body = "\n".join(cases)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}">'
        f"{body}</testsuite></testsuites>"
    )


def passed(name: str) -> str:
    return f'<testcase classname="tdd.fake" name="{name}" time="0.01" />'


def failed(name: str) -> str:
    return (
        f'<testcase classname="tdd.fake" name="{name}" time="0.01">'
        '<failure message="assert 1 == 2">boom</failure></testcase>'
    )


def errored(name: str) -> str:
    return (
        f'<testcase classname="tdd.fake" name="{name}" time="0.01">'
        f'<error message="kaboom">kaboom</error></testcase>'
    )


def skipped(name: str, reason: str) -> str:
    return (
        f'<testcase classname="tdd.fake" name="{name}" time="0">'
        f'<skipped type="pytest.skip" message="{reason}" /></testcase>'
    )


def collection_skipped(name: str, reason: str) -> str:
    """Module-level importorskip form: generic message, reason in the body."""
    return (
        f'<testcase classname="tdd.fake_module" name="{name}" time="0">'
        '<skipped type="pytest.skip" message="collection skipped">'
        f"('/repo/tdd/fake.py', 29, 'Skipped: {reason}')</skipped></testcase>"
    )


def xfailed(name: str) -> str:
    return (
        f'<testcase classname="tdd.fake" name="{name}" time="0">'
        '<skipped type="pytest.xfail" message="target not implemented" /></testcase>'
    )


@pytest.fixture
def gate_env(tmp_path):
    """Write a baseline + floors pair and return a helper that runs the gate."""
    baseline = tmp_path / "skip_baseline.json"
    baseline.write_text(json.dumps([
        {"reason_prefix": "12.6-dormant:", "note": "dormant contract suites"},
        {"reason_prefix": "known-flake:", "note": "test entry"},
    ]))
    floors = tmp_path / "tier_floors.json"
    floors.write_text(json.dumps({"T1": {"floor": 3}}))

    def run(cases: list[str], tier: str = "T1", xml_name: str = "junit.xml"):
        xml = tmp_path / xml_name
        xml.write_text(junitxml(cases))
        return run_gate(str(xml), "--tier", tier, "--baseline", str(baseline), "--floors", str(floors))

    run.tmp_path = tmp_path
    run.baseline = baseline
    run.floors = floors
    return run


class TestGatePasses:
    def test_all_passed_meets_floor(self, gate_env):
        result = gate_env([passed("a"), passed("b"), passed("c")])
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        assert "executed=3" in result.stdout

    def test_baselined_skip_is_allowed(self, gate_env):
        result = gate_env([
            passed("a"), passed("b"), passed("c"),
            skipped("d", "12.6-dormant: runner_protocol lands in 12.6"),
        ])
        assert result.returncode == 0, result.stderr

    def test_collection_skip_reason_extracted_from_body(self, gate_env):
        """importorskip skips carry the reason in the element body, not the message."""
        result = gate_env([
            passed("a"), passed("b"), passed("c"),
            collection_skipped("mod", "12.6-dormant: job_recovery lands in 12.6"),
        ])
        assert result.returncode == 0, result.stderr

    def test_failures_still_count_toward_the_floor(self, gate_env):
        """A failed test DID run, so it counts as executed - the floor exists
        to catch tests that silently stop running, not tests that fail.

        The gate refuses red input as well (see TestGateRefusesRedInput); this
        test pins only the counting rule, using a floor low enough that the
        count is not what rejects it.
        """
        result = gate_env([passed("a"), failed("b"), failed("c")])
        assert "executed=3" in result.stderr + result.stdout

    def test_xfail_is_not_gated_as_skip(self, gate_env):
        result = gate_env([passed("a"), passed("b"), passed("c"), xfailed("d")])
        assert result.returncode == 0, result.stderr
        assert "xfailed=1" in result.stdout

    def test_multiple_xml_files_aggregate(self, gate_env):
        xml_a = gate_env.tmp_path / "a.xml"
        xml_a.write_text(junitxml([passed("a"), passed("b")]))
        xml_b = gate_env.tmp_path / "b.xml"
        xml_b.write_text(junitxml([passed("c")]))
        result = run_gate(
            str(xml_a), str(xml_b), "--tier", "T1",
            "--baseline", str(gate_env.baseline), "--floors", str(gate_env.floors),
        )
        assert result.returncode == 0, result.stderr
        assert "executed=3" in result.stdout


class TestGateFails:
    def test_out_of_baseline_skip_fails(self, gate_env):
        result = gate_env([
            passed("a"), passed("b"), passed("c"),
            skipped("d", "Docker not available"),
        ])
        assert result.returncode == 1
        assert "not in tdd/skip_baseline.json" in result.stderr
        assert "Docker not available" in result.stderr
        assert "tdd.fake::d" in result.stderr

    def test_prefix_must_match_start_of_reason(self, gate_env):
        """A baselined prefix buried mid-reason must not satisfy the gate."""
        result = gate_env([
            passed("a"), passed("b"), passed("c"),
            skipped("d", "unrelated wrapper around 12.6-dormant: text"),
        ])
        assert result.returncode == 1

    def test_executed_below_floor_fails(self, gate_env):
        result = gate_env([passed("a"), passed("b")])
        assert result.returncode == 1
        assert "below committed floor 3" in result.stderr

    def test_baselined_skips_do_not_rescue_floor(self, gate_env):
        """Mass dormancy with valid reasons still trips the floor (R4 ratchet)."""
        result = gate_env([
            passed("a"),
            skipped("b", "12.6-dormant: x"),
            skipped("c", "12.6-dormant: y"),
        ])
        assert result.returncode == 1
        assert "below committed floor" in result.stderr

    def test_unknown_tier_fails(self, gate_env):
        result = gate_env([passed("a"), passed("b"), passed("c")], tier="T9")
        assert result.returncode == 1
        assert "no floor" in result.stderr

    def test_missing_junitxml_fails(self, gate_env):
        result = run_gate(
            str(gate_env.tmp_path / "nope.xml"), "--tier", "T1",
            "--baseline", str(gate_env.baseline), "--floors", str(gate_env.floors),
        )
        assert result.returncode == 1
        assert "cannot read results" in result.stderr

    def test_malformed_junitxml_fails(self, gate_env):
        xml = gate_env.tmp_path / "broken.xml"
        xml.write_text("<testsuites><unclosed")
        result = run_gate(
            str(xml), "--tier", "T1",
            "--baseline", str(gate_env.baseline), "--floors", str(gate_env.floors),
        )
        assert result.returncode == 1
        assert "cannot read results" in result.stderr


class TestCommittedConfig:
    """The committed baseline/floors files must stay parseable by the gate."""

    def test_skip_baseline_shape(self):
        baseline = json.loads(COMMITTED_BASELINE.read_text(encoding="utf-8"))
        assert isinstance(baseline, list) and baseline
        for entry in baseline:
            assert entry["reason_prefix"].strip(), entry
            assert entry["note"].strip(), entry

    def test_tier_floors_shape(self):
        floors = json.loads(COMMITTED_FLOORS.read_text(encoding="utf-8"))
        assert set(floors) == {"T1", "T2", "T3"}
        for tier, spec in floors.items():
            assert isinstance(spec["floor"], int) and spec["floor"] > 0, tier
            assert spec["floor"] <= spec["measured"], f"{tier} floor above measured count"

    def test_the_dormant_12_6_prefix_is_gone(self):
        """The inverse of the assertion this replaces, and the point of R4.

        The 12.6 contract suites were parked behind a `12.6-dormant:`
        importorskip since Phase 0, and that prefix was baselined so the gate
        would tolerate the skips. Their target modules exist now, the suites
        run, and a baseline entry that can no longer match is a standing
        permission to skip that nothing would ever notice being used again.
        It was removed in the deletion commit; this keeps it removed.
        """
        baseline = json.loads(COMMITTED_BASELINE.read_text(encoding="utf-8"))
        assert not [e for e in baseline if e["reason_prefix"] == "12.6-dormant:"], (
            "the 12.6-dormant skip baseline entry is back. Its suites are "
            "awake - a baseline entry for them can only ever hide a "
            "regression now."
        )


class TestGateRefusesRedInput:
    """A gate must never print OK on a red junitxml.

    run_tier.py returns before invoking the gate when pytest is red, so the
    pipeline was never at risk - but a DIRECT invocation printed
    "CI GATE [T1]: OK" on a summary line that also said "failed=3", and a
    verifier read that as a pass. A tool built to prevent fake green must not
    be capable of producing it, however it is called.
    """

    def test_failures_fail_the_gate(self, gate_env):
        result = gate_env([passed("a"), failed("b")])
        assert result.returncode == 1
        assert "never report OK on a red suite" in result.stderr

    def test_errors_fail_the_gate(self, gate_env):
        result = gate_env([passed("a"), errored("b")])
        assert result.returncode == 1

    def test_a_green_suite_still_passes(self, gate_env):
        result = gate_env([passed("a"), passed("b"), passed("c")])
        assert result.returncode == 0, result.stderr
