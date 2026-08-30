"""Contract #2 END TO END, in one process: the REAL writer -> the REAL shipper.

Added by the 12.5 verification pass to close a seam the wave left open.

`tdd/unit/control_runtime/usage_contract.py` pins the wire shape, and three
suites check themselves against it - but each of them builds its manifest BY
HAND:

- `test_usage_shipping.py` feeds `run.py` a hand-written WRAPPER_MANIFEST,
- `test_usage_model.py` / `test_usage_ingestion.py` post hand-written bodies,
- `runner-common/tests/test_usage_scrape.py` checks the scrapers' output
  against key names spelled out in that file.

So the one thing nobody checked was the actual JOIN: that the bytes
`runner_common.usage.write_usage_manifest` puts on disk are the bytes
`images/base/control/run.py` picks up, normalizes and POSTs. A rename on
either side of that seam (writer emits `cost`, shipper reads `cost_usd`)
would leave every existing test green and silently degrade every agent step
to `cost_source="unknown"` - exactly the quietly-too-cheap median
`docs/milestone-13/api-surface.md` section 2.6 warns about.

This module drives the real producers - the real `MockExecutor`'s usage dict
and the real `scrape_claude_usage` over a stream-json transcript - through
the real writer and the real shipper, and asserts the POSTed body against
the shared pin. The `control_runtime` conftest already puts both
`images/base` and `runner-common` on `sys.path`, which is what makes a
one-process round trip possible.
"""
import json

import pytest

from tdd.unit.control_runtime.usage_contract import (
    COST_SOURCES,
    PROVIDERS,
    USAGE_VERSION,
    assert_manifest_conforms,
)

SHIPPER = "shipper (images/base/control/run.py)"

# A minimal claude `--output-format stream-json --verbose` transcript: two
# event lines, then the result object the scraper is supposed to find.
CLAUDE_STREAM_JSON = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}),
        json.dumps({"type": "assistant", "message": {"content": "working"}}),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.1841,
                "model": "claude-haiku-4-5",
                "usage": {
                    "input_tokens": 1234,
                    "output_tokens": 567,
                    "cache_read_input_tokens": 89,
                    "cache_creation_input_tokens": 10,
                },
            }
        ),
    ]
)


def usage_posts(session, posts_to):
    return posts_to(session, "/usage")


@pytest.fixture
def usage_file(tmp_path):
    """The path run.py derives for step_execution_id 'exec-1' (contract #2)."""
    return tmp_path / "usage.exec-1.json"


class TestRealWriterThroughRealShipper:
    def test_claude_scrape_survives_the_whole_channel(
        self, run_main, posts_to, usage_file
    ):
        """scrape -> build -> write -> pick up -> normalize -> POST.

        Every wrapper-owned datum must arrive at the endpoint unchanged, and
        the body must satisfy the shared wire pin.
        """
        from runner_common.usage import scrape_claude_usage, write_usage_manifest

        usage = scrape_claude_usage(
            CLAUDE_STREAM_JSON, "", fallback_model="claude-haiku-4-5"
        )
        assert usage["cost_source"] == "cli-reported", (
            "the scraper failed on its own transcript shape - the rest of "
            f"this assertion chain would be vacuous: {usage!r}"
        )
        assert write_usage_manifest(
            str(usage_file), "claude-code", usage, wall_clock_ms=4321
        )

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posts = usage_posts(session, posts_to)
        assert len(posts) == 1, "the shipper did not POST the wrapper's manifest"
        posted = posts[0]
        assert_manifest_conforms(posted, SHIPPER)

        assert posted["version"] == USAGE_VERSION
        assert posted["provider"] == "anthropic"
        assert posted["provider"] in PROVIDERS
        assert posted["cost_source"] == "cli-reported"
        assert posted["cost_source"] in COST_SOURCES
        assert posted["model"] == "claude-haiku-4-5"
        assert posted["input_tokens"] == 1234
        assert posted["output_tokens"] == 567
        assert posted["cache_read_tokens"] == 89
        assert posted["cache_write_tokens"] == 10
        # Money crosses the seam as a STRING, so no float rounds a cent away.
        assert posted["cost_usd"] is not None
        assert float(posted["cost_usd"]) == pytest.approx(0.1841)

        # run.py owns timing and overwrites whatever the wrapper guessed.
        assert posted["container_seconds"] is not None
        assert isinstance(posted["wall_clock_ms"], int)

        # Consume-once: the manifest must not outlive the step on the shared
        # workspace volume.
        assert not usage_file.exists()

    def test_mock_executor_usage_survives_the_whole_channel(
        self, run_main, posts_to, usage_file
    ):
        """The dogfood ratchet's agent is the mock one - so this is the path
        that runs on EVERY push. Its numbers must reach the endpoint."""
        from runner_common.executors.mock import MockExecutor
        from runner_common.usage import write_usage_manifest

        usage = MockExecutor._usage(
            "a prompt with a handful of words in it",
            [{"type": "content", "text": "Analyzing the workspace..."}],
        )
        assert write_usage_manifest(str(usage_file), "mock", usage, wall_clock_ms=12)

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posted = usage_posts(session, posts_to)[0]
        assert_manifest_conforms(posted, SHIPPER)
        assert posted["provider"] == "self-hosted"
        assert posted["model"] == "mock"
        # Non-null token counts are what scripts/verify_executor.py asserts
        # about the dogfood run's agent step; a null here fails that gate.
        assert posted["input_tokens"] > 0
        assert posted["output_tokens"] > 0
        assert posted["cost_source"] == "cli-reported"
        assert not usage_file.exists()

    def test_executor_reported_nothing_still_ships_an_honest_record(
        self, run_main, posts_to, usage_file
    ):
        """usage=None (the CLI said nothing, or the executor raised) is a
        RECORDED FACT, not a gap: cost_source='unknown', null cost, and the
        row still reaches the server."""
        from runner_common.usage import write_usage_manifest

        assert write_usage_manifest(str(usage_file), "gemini", None, wall_clock_ms=7)

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posted = usage_posts(session, posts_to)[0]
        assert_manifest_conforms(posted, SHIPPER)
        assert posted["provider"] == "google"
        assert posted["cost_source"] == "unknown"
        assert posted["cost_usd"] is None
        assert posted["input_tokens"] is None
        assert not usage_file.exists()

    def test_a_scrape_failure_survives_the_whole_channel(
        self, run_main, posts_to, usage_file
    ):
        """12.5 F3.1: the fact that the SCRAPER broke must reach the server.

        This is the seam that makes the marker worth having: run.py drops
        every manifest key it does not own, so the only way a scrape failure
        can reach the database is inside `raw` - the one free-form dict on
        the pinned wire. If a future normalization starts filtering `raw`,
        this test fails HERE rather than a quarter later, when every agent
        step has been recorded at zero cost.
        """
        from runner_common.usage import (
            RAW_SCRAPE_ERROR,
            RAW_SCRAPE_FAILED,
            scrape_claude_usage,
            write_usage_manifest,
        )

        # A claude CLI that stopped emitting its result object.
        usage = scrape_claude_usage("wrote three files, exiting\n")
        assert write_usage_manifest(
            str(usage_file), "claude-code", usage, wall_clock_ms=99
        )

        exit_code, session, _ = run_main("true")

        assert exit_code == 0, "telemetry never fails a step"
        posted = usage_posts(session, posts_to)[0]
        assert_manifest_conforms(posted, SHIPPER)

        assert posted["raw"][RAW_SCRAPE_FAILED] is True
        assert "claude CLI output" in posted["raw"][RAW_SCRAPE_ERROR]
        # It is still a wire-valid record: an unpriced row, not a rejected one.
        assert posted["cost_source"] == "unknown"
        assert posted["cost_source"] in COST_SOURCES
        assert posted["cost_usd"] is None
        assert not usage_file.exists()

    def test_a_healthy_scrape_ships_no_failure_marker(
        self, run_main, posts_to, usage_file
    ):
        """The marker must mean something at the far end too."""
        from runner_common.usage import (
            RAW_SCRAPE_FAILED,
            scrape_claude_usage,
            write_usage_manifest,
        )

        write_usage_manifest(
            str(usage_file),
            "claude-code",
            scrape_claude_usage(CLAUDE_STREAM_JSON),
            wall_clock_ms=1,
        )
        _, session, _ = run_main("true")

        assert RAW_SCRAPE_FAILED not in usage_posts(session, posts_to)[0]["raw"]

    def test_writer_and_shipper_agree_on_every_key(self, usage_file):
        """No hand-written fixture: the writer's own output IS the pin.

        A key the writer emits that the wire shape does not know (or the
        reverse) fails HERE, at the seam, instead of silently degrading a
        real agent step's row.
        """
        from runner_common.usage import build_manifest, write_usage_manifest

        assert write_usage_manifest(str(usage_file), "claude-code", None)
        on_disk = json.loads(usage_file.read_text())

        assert on_disk == build_manifest("claude-code")
        assert_manifest_conforms(on_disk, "writer (runner_common.usage)")
