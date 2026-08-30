"""
Token accumulation ACROSS TURNS, and the honesty rules around it
(Milestone 14.2, design section 5.1).

This is the only genuinely new accounting logic in Milestone 14. The three CLI
executors scrape ONE final report; the harness makes N requests and must add
them up. The fixtures' per-turn numbers are all DISTINCT and none is the sum
of the others, so a last-response-wins bug cannot pass by coincidence.

Three rules are asserted rather than assumed:

- a PARTIAL is never read as a TOTAL (``turns_without_usage`` says so);
- a ZERO is never substituted for a NULL (a zero is a claim, a null is an
  absence);
- the harness NEVER prices, and a harness row may never claim ``cli-reported``.
"""
import json

import pytest

from runner_common import agent_wrapper
from runner_common.agent_config import AGENT_CONFIG_PATH_ENV
from runner_common.harness import HarnessExecutor
from runner_common.harness.client import OpenAICompatClient
from runner_common.harness.constants import HARNESS_COST_SOURCE, HARNESS_PROVIDER
from runner_common.usage import (
    PROVIDER_BY_AGENT,
    SCRAPE_FAILED_LOG_MARKER,
    USAGE_PATH_ENV,
    TokenAccumulator,
    build_manifest,
    scrape_failure_reason,
)
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    FakeSession,
    chat_response,
    endpoint_block,
    harness_block,
    make_repo,
    run_harness,
    sse_response,
    tool_call,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def usage(index=0):
    return dict(DEFAULT_USAGE_SERIES[index % len(DEFAULT_USAGE_SERIES)])


def call(name, args, index=0, **kwargs):
    return chat_response(tool_calls=[tool_call(name, args)], usage=usage(index), **kwargs)


# --------------------------------------------------------------------------
# the accumulator itself
# --------------------------------------------------------------------------

class TestTokenAccumulator:
    def test_it_sums_rather_than_overwrites(self):
        accumulator = TokenAccumulator()
        for index in range(4):
            accumulator.add(usage(index))
        assert accumulator.input_tokens == 101 + 203 + 307 + 409
        assert accumulator.output_tokens == 11 + 23 + 37 + 41
        assert accumulator.turns == 4
        assert accumulator.turns_without_usage == 0

    def test_no_reported_usage_yields_null_not_zero(self):
        accumulator = TokenAccumulator()
        for _ in range(3):
            accumulator.add(None)
        assert accumulator.input_tokens is None
        assert accumulator.output_tokens is None
        assert accumulator.total_tokens == 0
        assert accumulator.turns_without_usage == 3
        assert accumulator.no_usage_reason() == (
            "endpoint reported no usage block in any of 3 turns"
        )

    def test_a_partial_is_the_sum_of_the_reporting_turns(self):
        accumulator = TokenAccumulator()
        accumulator.add(usage(0))
        accumulator.add(None)
        accumulator.add(usage(1))
        assert accumulator.input_tokens == 101 + 203
        assert accumulator.turns_without_usage == 1
        assert accumulator.no_usage_reason() is None

    def test_cached_tokens_are_summed_only_when_reported(self):
        accumulator = TokenAccumulator()
        accumulator.add({"prompt_tokens": 10, "completion_tokens": 1})
        assert accumulator.cache_read_tokens is None
        accumulator.add(
            {
                "prompt_tokens": 20,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 7},
            }
        )
        assert accumulator.cache_read_tokens == 7

    def test_a_junk_usage_block_counts_as_a_non_reporting_turn(self):
        accumulator = TokenAccumulator()
        accumulator.add({"nonsense": True})
        assert accumulator.turns_without_usage == 1
        assert accumulator.input_tokens is None

    def test_the_provider_map_gained_exactly_one_entry(self):
        assert PROVIDER_BY_AGENT["openai-harness"] == "openai-compatible"
        assert PROVIDER_BY_AGENT["claude-code"] == "anthropic"
        assert set(PROVIDER_BY_AGENT) == {
            "claude-code",
            "gemini",
            "mock",
            "openai-harness",
        }


# --------------------------------------------------------------------------
# through the real executor
# --------------------------------------------------------------------------

class TestHarnessUsageRecord:
    def test_tokens_sum_across_every_turn(self, repo):
        script = [
            call("read_file", {"path": "README.md"}, 0),
            call("read_file", {"path": "src/main.py"}, 1),
            call("write_file", {"path": "done.txt", "content": "x"}, 2),
            call("finish", {"status": "success", "summary": "ok"}, 3),
        ]
        result, _, _, _ = run_harness(repo, script)
        assert result.usage["input_tokens"] == 101 + 203 + 307 + 409
        assert result.usage["output_tokens"] == 11 + 23 + 37 + 41
        # The sum is strictly larger than any single turn, which is the
        # assertion a last-response-wins bug cannot satisfy.
        assert result.usage["input_tokens"] > max(
            u["prompt_tokens"] for u in DEFAULT_USAGE_SERIES[:4]
        )
        assert result.usage["raw"]["harness"]["turns_without_usage"] == 0

    def test_a_mix_of_reporting_and_silent_turns_is_recorded_as_partial(self, repo):
        script = [
            call("read_file", {"path": "README.md"}, 0),
            chat_response(
                tool_calls=[tool_call("write_file", {"path": "d.txt", "content": "x"})]
            ),  # no usage block at all
            call("finish", {"status": "success", "summary": "ok"}, 1),
        ]
        result, _, _, _ = run_harness(repo, script)
        assert result.usage["input_tokens"] == 101 + 203
        assert result.usage["raw"]["harness"]["turns"] == 3
        assert result.usage["raw"]["harness"]["turns_without_usage"] == 1
        assert scrape_failure_reason(result.usage) is None

    def test_no_reporting_turn_yields_null_tokens_and_flags_a_scrape_failure(
        self, repo
    ):
        """The `no_usage` scenario. Reusing the 12.5 marker means
        verify_executor's existing grep catches it on the dogfood lane."""
        script = [
            chat_response(
                tool_calls=[tool_call("write_file", {"path": "d.txt", "content": "x"})]
            ),
            chat_response(
                tool_calls=[tool_call("finish", {"status": "success", "summary": "ok"})]
            ),
        ]
        result, _, _, _ = run_harness(repo, script)
        assert result.usage["input_tokens"] is None
        assert result.usage["output_tokens"] is None
        assert scrape_failure_reason(result.usage) == (
            "endpoint reported no usage block in any of 2 turns"
        )

    def test_the_scrape_marker_reaches_the_step_log_through_the_wrapper(
        self, repo, tmp_path, monkeypatch, capsys
    ):
        """END TO END: the wrapper is the ONE printer of the marker, so this
        drives the real ``agent_wrapper.main()`` rather than asserting on a
        string the executor might have printed itself."""
        session = FakeSession(
            [
                chat_response(
                    tool_calls=[
                        tool_call("write_file", {"path": "d.txt", "content": "x"})
                    ]
                ),
                chat_response(
                    tool_calls=[
                        tool_call("finish", {"status": "success", "summary": "ok"})
                    ]
                ),
            ]
        )

        def build(**kwargs):
            return HarnessExecutor(
                client_factory=lambda **client_kwargs: OpenAICompatClient(
                    session=session, sleep=lambda s: None, **client_kwargs
                ),
                **kwargs,
            )

        monkeypatch.setattr(agent_wrapper, "HarnessExecutor", build)

        config_path = tmp_path / "agent.json"
        usage_path = tmp_path / "usage.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agent": "openai-harness",
                    "model": "qwen2.5-coder:32b",
                    "prompt": "Create d.txt",
                    "stream": False,
                    "repo": {"workdir": str(repo), "branch": "b"},
                    "commit": {"enabled": False},
                    "endpoint": endpoint_block(),
                    "harness": harness_block(),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(AGENT_CONFIG_PATH_ENV, str(config_path))
        monkeypatch.setenv(USAGE_PATH_ENV, str(usage_path))

        exit_code = agent_wrapper.main()
        printed = capsys.readouterr().out
        assert SCRAPE_FAILED_LOG_MARKER in printed
        assert "endpoint reported no usage block in any of 2 turns" in printed
        assert exit_code == 0

        manifest = json.loads(usage_path.read_text(encoding="utf-8"))
        assert manifest["provider"] == "openai-compatible"
        assert manifest["input_tokens"] is None
        assert manifest["cost_usd"] is None
        assert manifest["cost_source"] == "unknown"
        assert manifest["raw"]["_scrape_failed"] is True

    def test_cost_is_always_null_and_never_cli_reported(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                call("write_file", {"path": "d.txt", "content": "x"}, 0),
                call("finish", {"status": "success", "summary": "ok"}, 1),
            ],
        )
        assert result.usage["cost_usd"] is None
        assert result.usage["cost_source"] == HARNESS_COST_SOURCE == "unknown"
        assert result.usage["provider"] == HARNESS_PROVIDER == "openai-compatible"
        manifest = build_manifest("openai-harness", result.usage)
        assert manifest["cost_source"] != "cli-reported"
        assert manifest["provider"] == "openai-compatible"

    def test_determinism_is_finally_non_empty(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [call("finish", {"status": "blocked", "summary": "x"}, 0)],
            harness=harness_block(temperature=0.2, top_p=0.9, seed=7),
        )
        assert result.usage["determinism"] == {
            "temperature": 0.2,
            "top_p": 0.9,
            "seed": 7,
        }
        manifest = build_manifest("openai-harness", result.usage)
        assert manifest["determinism"] == {"temperature": 0.2, "top_p": 0.9, "seed": 7}

    def test_model_version_records_the_tag_the_server_actually_served(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[
                        tool_call("finish", {"status": "blocked", "summary": "x"})
                    ],
                    usage=usage(0),
                    model="qwen2.5-coder:32b-instruct-q4_K_M",
                )
            ],
        )
        assert result.usage["model"] == "qwen2.5-coder:32b"
        assert result.usage["model_version"] == "qwen2.5-coder:32b-instruct-q4_K_M"

    def test_model_version_is_null_when_the_server_echoes_the_same_id(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[
                        tool_call("finish", {"status": "blocked", "summary": "x"})
                    ],
                    usage=usage(0),
                    model="qwen2.5-coder:32b",
                )
            ],
        )
        assert result.usage["model_version"] is None

    def test_the_raw_record_carries_the_designed_accounting_fields(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                call("read_file", {"path": "README.md"}, 0),
                call("write_file", {"path": "d.txt", "content": "x"}, 1),
                call("finish", {"status": "success", "summary": "ok"}, 2),
            ],
        )
        record = result.usage["raw"]["harness"]
        assert set(record) == {
            "endpoint_id",
            "endpoint_name",
            "endpoint_reach",
            "endpoint_max_concurrency",
            "endpoint_probe_age_s",
            "mode",
            "turns",
            "turns_without_usage",
            "stop_reason",
            "finish_status",
            "tool_calls",
            "tool_errors",
            "malformed_responses",
            "context_elisions",
            "endpoint_http_errors",
            "probe_drift",
            "files_changed",
        }
        assert record["tool_calls"] == {"read_file": 1, "write_file": 1, "finish": 1}
        assert isinstance(record["tool_calls"], dict), "a COUNT MAP, never a list"
        assert record["endpoint_probe_age_s"] == 3821

    def test_max_concurrency_is_derived_from_the_gpu_fraction_on_the_wire(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [call("finish", {"status": "blocked", "summary": "x"}, 0)],
            endpoint=endpoint_block(pricing={"gpu_fraction": 0.25}),
        )
        assert result.usage["raw"]["harness"]["endpoint_max_concurrency"] == 4

    def test_the_raw_record_survives_the_8_kib_cap(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [call("read_file", {"path": "README.md"}, 0)],
            harness=harness_block(max_iterations=30),
        )
        manifest = build_manifest("openai-harness", result.usage)
        encoded = json.dumps(manifest["raw"])
        assert len(encoded.encode("utf-8")) <= 8192
        assert manifest["raw"]["harness"]["turns"] == 30


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

class TestStreamedUsage:
    def test_usage_arrives_on_the_final_sse_frame_and_is_counted(self, repo):
        script = [
            sse_response(
                ["Writing the file. "],
                usage={"prompt_tokens": 111, "completion_tokens": 22},
                tool_calls=[tool_call("write_file", {"path": "d.txt", "content": "x"})],
            ),
            sse_response(
                [""],
                usage={"prompt_tokens": 222, "completion_tokens": 33},
                tool_calls=[
                    tool_call("finish", {"status": "success", "summary": "ok"})
                ],
            ),
        ]
        result, _, session, _ = run_harness(repo, script, streaming=True)
        assert result.success is True
        assert result.usage["input_tokens"] == 333
        assert result.usage["output_tokens"] == 55
        assert all(request["stream"] for request in session.requests)
        assert all(
            body["stream_options"] == {"include_usage": True} for body in session.bodies
        )

    def test_streaming_is_not_used_when_the_endpoint_cannot_do_it(self, repo):
        result, _, session, _ = run_harness(
            repo,
            [call("finish", {"status": "blocked", "summary": "x"}, 0)],
            endpoint=endpoint_block(capabilities={"supports_streaming": False}),
            streaming=True,
        )
        assert session.requests[0]["stream"] is False
        assert result.usage["raw"]["harness"]["turns"] == 1
