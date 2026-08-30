"""
THE cross-side test for the 12.2.6 test-results manifest wire contract.

Both sides of the wire are checked against ONE shared schema module
(``tdd/unit/control_runtime/manifest_contract.py``), so a drift fails with a
message that names the side that drifted:

- PRODUCER (runner_common.pytest_lazyaf) — a REAL inner pytest session writes
  a manifest; its bytes must satisfy the contract.
- CONSUMER (images/base/control/run.py) — its validator must ACCEPT exactly
  the contract-conformant shape and reject everything else.
- Round trip — producer output fed straight through the consumer validator
  comes out unchanged (no silent coercion of a valid manifest).

The SERVER side (backend /api/steps/{id}/test-results) imports the same
module from its own tests; if this file and that one disagree, the shared
module is the thing to change — once.
"""
import json

import pytest

from control.run import normalize_manifest
from tdd.unit.control_runtime.manifest_contract import (
    CANONICAL_MANIFEST,
    INVALID_MANIFESTS,
    RESULT_KEYS,
    STATUSES,
    TOP_LEVEL_KEYS,
    assert_manifest_conforms,
    manifest_violations,
)

# Enables the `pytester` fixture. Declared in the test MODULE (allowed) —
# a non-rootdir conftest.py may not declare pytest_plugins.
pytest_plugins = ["pytester"]

PRODUCER = "PRODUCER (runner_common.pytest_lazyaf)"
CONSUMER = "CONSUMER (images/base/control/run.py)"

PLUGIN_ARGS = ("-p", "runner_common.pytest_lazyaf")


class TestContractModuleItself:
    def test_canonical_manifest_conforms(self):
        assert_manifest_conforms(CANONICAL_MANIFEST, "CONTRACT MODULE")

    def test_key_sets_are_the_pinned_ones(self):
        assert set(TOP_LEVEL_KEYS) == {"version", "results"}
        assert set(RESULT_KEYS) == {
            "lazyaf_test_id",
            "status",
            "duration_ms",
            "file_path",
        }
        assert set(STATUSES) == {"passed", "failed", "skipped"}

    @pytest.mark.parametrize("label,value", INVALID_MANIFESTS, ids=lambda v: str(v)[:40])
    def test_invalid_manifests_are_reported(self, label, value):
        assert manifest_violations(value), label


class TestProducerSideConformsToContract:
    """The pytest plugin's real output must satisfy the shared contract."""

    def test_plugin_output_conforms(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.delenv("LAZYAF_REPO_ROOT", raising=False)
        pytester.makepyfile(
            test_contract_sample="""
            import pytest

            @pytest.mark.lazyaf_test_id("contract.passes")
            def test_passes():
                assert True

            @pytest.mark.lazyaf_test_id("contract.fails")
            def test_fails():
                assert False

            @pytest.mark.lazyaf_test_id("contract.skips")
            @pytest.mark.skip(reason="informational")
            def test_skips():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(
            passed=1, failed=1, skipped=1
        )

        with open(out) as fh:
            manifest = json.load(fh)

        assert_manifest_conforms(manifest, PRODUCER)
        assert {r["lazyaf_test_id"] for r in manifest["results"]} == {
            "contract.passes",
            "contract.fails",
            "contract.skips",
        }


class TestConsumerSideAcceptsTheContract:
    """The control runtime's validator must accept EXACTLY the contract."""

    def test_canonical_manifest_passes_through_unchanged(self):
        normalized, problems = normalize_manifest(CANONICAL_MANIFEST)

        assert problems == [], f"{CONSUMER} rejected a canonical manifest"
        assert normalized == CANONICAL_MANIFEST, (
            f"{CONSUMER} DRIFTED: it rewrote a contract-conformant manifest"
        )
        assert_manifest_conforms(normalized, CONSUMER)

    @pytest.mark.parametrize("status", sorted(STATUSES))
    def test_every_contract_status_is_accepted(self, status):
        manifest = {
            "version": 1,
            "results": [
                {
                    "lazyaf_test_id": f"contract.{status}",
                    "status": status,
                    "duration_ms": 1,
                    "file_path": "tdd/x/test_y.py",
                }
            ],
        }
        normalized, problems = normalize_manifest(manifest)

        assert problems == [], f"{CONSUMER} rejected status {status!r}"
        assert normalized == manifest

    @pytest.mark.parametrize("label,value", INVALID_MANIFESTS, ids=lambda v: str(v)[:40])
    def test_off_contract_manifests_are_never_sent(self, label, value):
        normalized, problems = normalize_manifest(value)

        assert normalized is None, (
            f"{CONSUMER} DRIFTED: it accepted an off-contract manifest "
            f"({label}) — the backend would receive {normalized!r}"
        )
        assert problems

    def test_consumer_output_is_always_contract_conformant(self):
        """Whatever junk goes in, whatever comes OUT is on-contract."""
        messy = {
            "version": "1",
            "results": [
                {"lazyaf_test_id": "keep.me", "status": "passed",
                 "duration_ms": 4.9, "file_path": "tdd\\win\\test_a.py",
                 "extra": "ignored"},
                {"lazyaf_test_id": "drop.me", "status": "errored"},
            ],
        }
        normalized, problems = normalize_manifest(messy)

        assert problems
        assert_manifest_conforms(normalized, CONSUMER)
        assert normalized["results"][0]["file_path"] == "tdd/win/test_a.py"


class TestRoundTrip:
    """Producer bytes -> consumer validator -> unchanged, on-contract."""

    def test_real_plugin_output_survives_the_consumer_untouched(
        self, pytester, monkeypatch
    ):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.delenv("LAZYAF_REPO_ROOT", raising=False)
        pytester.makepyfile(
            test_roundtrip_sample="""
            import pytest

            @pytest.mark.lazyaf_test_id("roundtrip.one")
            def test_one():
                pass

            @pytest.mark.lazyaf_test_id("roundtrip.two")
            def test_two():
                assert False
            """
        )
        pytester.runpytest(*PLUGIN_ARGS)

        with open(out) as fh:
            produced = json.load(fh)

        normalized, problems = normalize_manifest(produced)

        assert problems == [], (
            f"{CONSUMER} complained about {PRODUCER} output: {problems}"
        )
        assert normalized == produced, (
            "PRODUCER/CONSUMER DRIFT: the control runtime had to rewrite the "
            f"plugin's manifest.\nproduced:   {produced!r}\nnormalized: "
            f"{normalized!r}"
        )
