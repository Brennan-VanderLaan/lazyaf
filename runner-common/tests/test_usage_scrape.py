"""
Tests for the usage channel's wrapper side (Phase 12.5, contract #2).

What is pinned here:
- the claude scraper reads the CLI's own result object out of BOTH output
  formats (``stream-json`` events and a single ``json`` object) and reports
  ``cli-reported``;
- a run with no result event degrades to ``cost_source="unknown"`` with null
  tokens rather than to an exception (telemetry never fails a step);
- the gemini scraper finds tokens but NEVER invents dollars;
- the mock's usage is deterministic and non-zero, so the dogfood ratchet has
  something real to assert on at zero API cost;
- ``write_usage_manifest`` writes every wire key, atomically, and cannot
  raise no matter what it is handed.

The wire shape itself is owned by ``backend/app/schemas/usage.py``
(api-surface 2.2) and pinned for both sides by
``tdd/unit/control_runtime/usage_contract.py``.
"""
import json
import os

import pytest

from runner_common import usage as usage_module
from runner_common.executors import ExecutorConfig, MockExecutor
from runner_common.usage import (
    PROVIDER_BY_AGENT,
    RAW_MAX_BYTES,
    RAW_SCRAPE_ERROR,
    RAW_SCRAPE_FAILED,
    SCRAPE_FAILED_LOG_MARKER,
    USAGE_VERSION,
    build_manifest,
    scrape_claude_usage,
    scrape_failure_reason,
    scrape_gemini_usage,
    write_usage_manifest,
)

# Every key of UsageManifest (api-surface 2.2). A manifest missing one is a
# manifest the server has to guess about.
WIRE_KEYS = {
    "version",
    "provider",
    "model",
    "model_version",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "cost_source",
    "wall_clock_ms",
    "container_seconds",
    "gpu_node_id",
    "gpu_fraction",
    "determinism",
    "role",
    "raw",
}

CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 184220,
    "model": "claude-haiku-4-5-20260210",
    "total_cost_usd": 0.1841,
    "usage": {
        "input_tokens": 18422,
        "output_tokens": 3110,
        "cache_read_input_tokens": 240110,
        "cache_creation_input_tokens": 12004,
    },
}

CLAUDE_STREAM = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Reading main.py"}]},
            }
        ),
        json.dumps(CLAUDE_RESULT),
    ]
)

GEMINI_STDOUT = """
Writing src/rate_limit.py
Done.

Usage summary:
  input tokens:  12,004
  output tokens: 1,877
  total tokens:  13,881
"""


class TestClaudeScrape:
    def test_stream_json_yields_tokens_cost_and_cli_reported(self):
        scraped = scrape_claude_usage(CLAUDE_STREAM)

        assert scraped["provider"] == "anthropic"
        assert scraped["cost_source"] == "cli-reported"
        assert scraped["cost_usd"] == "0.1841"
        assert scraped["input_tokens"] == 18422
        assert scraped["output_tokens"] == 3110
        assert scraped["cache_read_tokens"] == 240110
        assert scraped["cache_write_tokens"] == 12004
        assert scraped["model"] == "claude-haiku-4-5-20260210"
        assert scraped["model_version"] == "claude-haiku-4-5-20260210"
        assert scraped["raw"]["total_cost_usd"] == 0.1841

    def test_single_object_json_format_yields_the_same_numbers(self):
        """`stream: false` falls back to --output-format json; ONE scraper
        must serve both shapes or the fallback is untested code."""
        pretty = json.dumps(CLAUDE_RESULT, indent=2)
        assert scrape_claude_usage(pretty) == scrape_claude_usage(CLAUDE_STREAM)

    def test_result_event_without_type_but_with_cost_is_still_found(self):
        line = json.dumps({"total_cost_usd": 0.5, "usage": {"input_tokens": 7}})
        scraped = scrape_claude_usage(line)
        assert scraped["cost_source"] == "cli-reported"
        assert scraped["cost_usd"] == "0.5"
        assert scraped["input_tokens"] == 7

    def test_no_result_event_yields_unknown_with_null_tokens(self):
        stream = json.dumps({"type": "system", "subtype": "init"})
        scraped = scrape_claude_usage(stream, fallback_model="claude-haiku-4-5")

        assert scraped["cost_source"] == "unknown"
        assert scraped["cost_usd"] is None
        assert scraped["input_tokens"] is None
        assert scraped["output_tokens"] is None
        # The configured model is still known even when the CLI said nothing.
        assert scraped["model"] == "claude-haiku-4-5"
        # ...and this is a SCRAPE FAILURE, not a legitimate unknown: the
        # claude CLI emits a result object on every run (F3.1).
        assert scrape_failure_reason(scraped) is not None

    @pytest.mark.parametrize(
        "stdout",
        ["", "plain text, no JSON at all", "{broken", "[1, 2, 3]", "null"],
    )
    def test_garbage_stdout_degrades_to_unknown_and_never_raises(self, stdout):
        scraped = scrape_claude_usage(stdout)
        assert scraped["cost_source"] == "unknown"
        assert scraped["provider"] == "anthropic"
        assert scrape_failure_reason(scraped) is not None

    def test_a_found_result_object_is_not_a_scrape_failure(self):
        assert scrape_failure_reason(scrape_claude_usage(CLAUDE_STREAM)) is None

    def test_the_failure_reason_names_the_vendor_output_as_the_suspect(self):
        """The reason lands in the step log and in the stored row; it has to
        point the reader at the CLI's output format, not at LazyAF."""
        reason = scrape_failure_reason(scrape_claude_usage("nothing here"))
        assert "claude CLI output" in reason
        assert "--output-format" in reason or "result schema" in reason

    def test_a_scraper_crash_is_a_scrape_failure_not_an_exception(
        self, monkeypatch, capsys
    ):
        def boom(_text):
            raise RuntimeError("scanner exploded")

        monkeypatch.setattr(usage_module, "_json_objects", boom)
        scraped = scrape_claude_usage(CLAUDE_STREAM)

        assert scraped["cost_source"] == "unknown"
        assert "scanner exploded" in scrape_failure_reason(scraped)
        assert SCRAPE_FAILED_LOG_MARKER in capsys.readouterr().err

    def test_last_result_event_wins(self):
        """A resumed session can emit more than one; the LAST is the bill."""
        first = dict(CLAUDE_RESULT, total_cost_usd=0.01)
        stream = json.dumps(first) + "\n" + json.dumps(CLAUDE_RESULT)
        assert scrape_claude_usage(stream)["cost_usd"] == "0.1841"

    def test_bad_token_values_are_nulled_not_propagated(self):
        event = dict(CLAUDE_RESULT, usage={"input_tokens": "lots", "output_tokens": -4})
        scraped = scrape_claude_usage(json.dumps(event))
        assert scraped["input_tokens"] is None
        assert scraped["output_tokens"] is None
        # A bad token count does not invalidate the reported dollars.
        assert scraped["cost_source"] == "cli-reported"


class TestGeminiScrape:
    def test_usage_summary_yields_tokens_with_no_dollars(self):
        scraped = scrape_gemini_usage(GEMINI_STDOUT)

        assert scraped["provider"] == "google"
        assert scraped["input_tokens"] == 12004
        assert scraped["output_tokens"] == 1877
        # The CLI reports no dollars. A token count is not a price.
        assert scraped["cost_usd"] is None
        assert scraped["cost_source"] == "unknown"

    def test_reads_stderr_too(self):
        scraped = scrape_gemini_usage("", GEMINI_STDOUT)
        assert scraped["input_tokens"] == 12004

    def test_output_tokens_derived_from_a_total_when_absent(self):
        text = "prompt tokens: 100\ntotal tokens: 175"
        scraped = scrape_gemini_usage(text)
        assert scraped["input_tokens"] == 100
        assert scraped["output_tokens"] == 75

    def test_nothing_found_is_unknown_not_an_error(self):
        scraped = scrape_gemini_usage("wrote three files, exiting")
        assert scraped["input_tokens"] is None
        assert scraped["output_tokens"] is None
        assert scraped["cost_source"] == "unknown"
        assert scraped["raw"] is None

    def test_nothing_found_is_flagged_as_a_scrape_failure(self):
        """The patterns are speculative. The day the CLI's wording changes
        must be the day the gate goes red, not the day gemini gets free."""
        assert (
            scrape_failure_reason(scrape_gemini_usage("wrote three files"))
            is not None
        )

    def test_tokens_found_with_no_dollars_is_NOT_a_scrape_failure(self):
        """The two facts the finding is about, side by side: gemini
        genuinely reports no price, and that is not a broken scraper."""
        scraped = scrape_gemini_usage(GEMINI_STDOUT)
        assert scraped["cost_source"] == "unknown"
        assert scrape_failure_reason(scraped) is None


class TestMockUsage:
    def test_mock_reports_deterministic_non_zero_tokens(self, tmp_path):
        executor = MockExecutor(
            mock_config={
                "response_mode": "batch",
                "delay_ms": 0,
                "file_operations": [],
                "output_events": [
                    {"type": "content", "text": "Analyzing the workspace..."},
                    {"type": "complete", "text": "Done."},
                ],
                "exit_code": 0,
            }
        )
        config = ExecutorConfig(workspace=tmp_path, prompt="x" * 400)

        first = executor.execute(config, streaming=False)
        second = executor.execute(config, streaming=False)

        assert first.usage == second.usage, "the mock must be byte-deterministic"
        assert first.usage["provider"] == "self-hosted"
        assert first.usage["model"] == "mock"
        assert first.usage["input_tokens"] == 100
        assert first.usage["output_tokens"] > 0
        assert first.usage["cost_usd"] == "0.000000"
        # DELIBERATE: the mock's cost is genuinely KNOWN to be zero, so the
        # dogfood ratchet exercises the cli-reported branch on every push.
        assert first.usage["cost_source"] == "cli-reported"
        assert first.usage["raw"] == {"mock": True}

    def test_usage_survives_a_configured_non_zero_exit(self, tmp_path):
        executor = MockExecutor(
            mock_config={
                "exit_code": 3,
                "output_events": [{"type": "content", "text": "boom"}],
            }
        )
        result = executor.execute(
            ExecutorConfig(workspace=tmp_path, prompt="p" * 40), streaming=False
        )
        assert result.success is False
        assert result.usage is not None and result.usage["model"] == "mock"


class TestBuildManifest:
    def test_carries_every_wire_key(self):
        manifest = build_manifest("mock", None, wall_clock_ms=12)
        assert set(manifest) == WIRE_KEYS
        assert manifest["version"] == USAGE_VERSION

    def test_no_usage_is_an_honest_unknown_record(self):
        manifest = build_manifest("claude-code", None, wall_clock_ms=5)
        assert manifest["provider"] == PROVIDER_BY_AGENT["claude-code"]
        assert manifest["cost_source"] == "unknown"
        assert manifest["cost_usd"] is None
        assert manifest["input_tokens"] is None
        assert manifest["wall_clock_ms"] == 5

    def test_unknown_agent_falls_back_to_self_hosted(self):
        assert build_manifest("something-new")["provider"] == "self-hosted"

    def test_runtime_owned_fields_are_left_for_run_py(self):
        manifest = build_manifest("mock", {"provider": "self-hosted"})
        assert manifest["container_seconds"] is None
        assert manifest["gpu_node_id"] is None
        assert manifest["gpu_fraction"] is None

    def test_negative_wall_clock_is_clamped(self):
        assert build_manifest("mock", wall_clock_ms=-9)["wall_clock_ms"] == 0

    def test_oversized_raw_is_truncated_with_a_marker(self):
        blob = {"prose": "z" * (RAW_MAX_BYTES * 2), "input_tokens": 12}
        manifest = build_manifest("claude-code", {"raw": blob})
        assert manifest["raw"]["_truncated"] is True
        assert manifest["raw"]["input_tokens"] == 12
        assert len(json.dumps(manifest["raw"]).encode()) <= RAW_MAX_BYTES

    def test_unserializable_raw_becomes_a_marker_not_an_exception(self):
        manifest = build_manifest("claude-code", {"raw": {"f": object()}})
        assert manifest["raw"]["_truncated"] is True

    def test_role_rides_through_from_the_agent_config(self):
        assert build_manifest("mock", role="planner")["role"] == "planner"


class TestScrapeFailureIsDurable:
    """F3.1: a scrape failure must survive all the way into the stored row.

    `raw` is the only free-form dict on the pinned wire, so the marker rides
    there: run.py forwards it verbatim and the server stores it, which is
    what lets `scripts/verify_executor.py` fail the push on it.
    """

    def test_a_failed_scrape_stamps_the_marker_into_raw(self):
        manifest = build_manifest("claude-code", scrape_claude_usage("no json here"))
        assert manifest["raw"][RAW_SCRAPE_FAILED] is True
        assert "claude CLI output" in manifest["raw"][RAW_SCRAPE_ERROR]

    def test_a_successful_scrape_carries_no_marker(self):
        manifest = build_manifest("claude-code", scrape_claude_usage(CLAUDE_STREAM))
        assert RAW_SCRAPE_FAILED not in manifest["raw"]

    def test_the_marker_does_not_widen_the_wire_shape(self):
        """The scrapers' internal bookkeeping keys must never reach the wire:
        run.py drops manifest keys it does not own."""
        manifest = build_manifest("claude-code", scrape_claude_usage(""))
        assert set(manifest) == WIRE_KEYS

    def test_no_usage_at_all_is_not_a_scrape_failure(self):
        """A SIGTERM kill or a crashed executor reports nothing; that is a
        legitimate unknown and must not be blamed on the vendor."""
        manifest = build_manifest("claude-code", None)
        assert manifest["raw"] is None

    def test_a_synthesized_usage_block_is_not_a_scrape_failure(self):
        """The mock agent (and any hand-written block) never scraped
        anything, so it can never be a scrape FAILURE."""
        manifest = build_manifest("mock", {"cost_source": "cli-reported"})
        assert manifest["raw"] is None

    def test_the_marker_survives_an_oversized_blob(self):
        """The marker is the load-bearing half: if both do not fit, the blob
        goes and the marker stays."""
        scraped = scrape_claude_usage("")
        scraped["raw"] = {"prose": "z" * (RAW_MAX_BYTES * 2)}
        manifest = build_manifest("claude-code", scraped)

        assert manifest["raw"][RAW_SCRAPE_FAILED] is True
        assert len(json.dumps(manifest["raw"]).encode()) <= RAW_MAX_BYTES

    def test_the_marker_reaches_the_written_manifest_file(self, tmp_path):
        target = tmp_path / "usage.exec-1.json"
        write_usage_manifest(str(target), "claude-code", scrape_claude_usage("hi"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["raw"][RAW_SCRAPE_FAILED] is True
        # ...and it never fails the step: the write still succeeded and the
        # record is still a complete, wire-valid manifest.
        assert set(payload) == WIRE_KEYS
        assert payload["cost_source"] in ("unknown", "cli-reported")


class TestWriteUsageManifest:
    def test_writes_a_json_object_with_every_wire_key(self, tmp_path):
        target = tmp_path / "usage.exec-1.json"
        assert write_usage_manifest(str(target), "mock", {"cost_source": "cli-reported"})
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert set(payload) == WIRE_KEYS
        assert payload["cost_source"] == "cli-reported"

    def test_no_path_is_a_no_op_not_a_failure(self, tmp_path):
        assert write_usage_manifest(None, "mock") is False
        assert write_usage_manifest("", "mock") is False

    def test_leaves_no_temp_files_behind(self, tmp_path):
        target = tmp_path / "usage.exec-1.json"
        write_usage_manifest(str(target), "mock")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["usage.exec-1.json"]

    def test_creates_the_directory_when_absent(self, tmp_path):
        target = tmp_path / "control" / "usage.exec-1.json"
        assert write_usage_manifest(str(target), "mock")
        assert target.exists()

    def test_an_unwritable_destination_warns_and_returns_false(
        self, tmp_path, monkeypatch, capsys
    ):
        """HARD RULE: this runs in the wrapper's finally and in its SIGTERM
        handler. It must never raise."""

        def boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(usage_module.tempfile, "mkstemp", boom)
        assert write_usage_manifest(str(tmp_path / "u.json"), "mock") is False
        assert "could not write usage manifest" in capsys.readouterr().err

    def test_overwrites_a_previous_manifest_atomically(self, tmp_path):
        target = tmp_path / "usage.exec-1.json"
        write_usage_manifest(str(target), "mock", {"cost_source": "unknown"})
        write_usage_manifest(str(target), "mock", {"cost_source": "cli-reported"})
        assert json.loads(target.read_text())["cost_source"] == "cli-reported"

    def test_money_never_travels_as_a_float(self, tmp_path):
        target = tmp_path / "usage.exec-1.json"
        write_usage_manifest(str(target), "claude-code", {"cost_usd": 0.1841})
        raw_text = target.read_text(encoding="utf-8")
        assert '"cost_usd": "0.1841"' in raw_text
        assert isinstance(json.loads(raw_text)["cost_usd"], str)
