"""
Context-window management: BUDGET AND ELIDE, NEVER SUMMARIZE
(Milestone 14.2, design section 3.7).

The failure this file exists to prevent: turn 12 returns an opaque 400 and the
step dies having spent everything, or — worse — the server silently truncates
the transcript and the model starts answering a question it can no longer see.

Summarization is rejected and its absence is asserted: it costs an extra
generation on the slowest, scarcest resource in the system, it burns output
tokens against the very budget it is protecting, and a small model summarizing
its own transcript is the model least able to do it faithfully.
"""
import pytest

from runner_common.harness.constants import (
    CONTEXT_RESERVE_FRACTION,
    DEFAULT_ASSUMED_CONTEXT,
    DEFAULT_CHARS_PER_TOKEN,
    EXIT_CONTEXT,
    KEEP_RECENT_TURNS,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    TOOL_OUTPUT_MAX_BYTES,
)
from runner_common.harness.tools import ToolResult
from runner_common.harness.transcript import (
    ContextFloorUnmeetable,
    Transcript,
)
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    chat_response,
    endpoint_block,
    harness_block,
    make_repo,
    run_harness,
    tool_call,
)


class FakeCall:
    def __init__(self, name="read_file", call_id="c1"):
        self.name = name
        self.id = call_id
        self.arguments_raw = "{}"


def transcript(system="SYSTEM", task="TASK", window=4096, output=256, logs=None):
    return Transcript(
        system=system,
        task=task,
        context_window=window,
        max_output_tokens=output,
        endpoint_name="local-4090",
        log=(logs.append if logs is not None else None),
    )


# --------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------

class TestBudget:
    def test_the_working_budget_reserves_output_and_slack(self):
        tx = transcript(window=32768, output=4096)
        assert tx.working_budget == 32768 - 4096 - int(CONTEXT_RESERVE_FRACTION * 32768)

    def test_a_null_context_window_assumes_the_stated_default(self):
        tx = transcript(window=None)
        assert tx.context_window == DEFAULT_ASSUMED_CONTEXT
        assert tx.context_assumed is True

    def test_the_executor_says_so_loudly_when_it_assumes(self, tmp_path):
        repo = make_repo(tmp_path)
        result, logs, _, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[tool_call("finish", {"status": "blocked", "summary": "x"})],
                    usage=dict(DEFAULT_USAGE_SERIES[0]),
                )
            ],
            endpoint=endpoint_block(capabilities={"context_window": None}),
        )
        assert any(
            "endpoint declares no context window; assuming 8192 tokens" in line
            for line in logs
        )
        assert any("ctx=unknown" in line for line in logs)


# --------------------------------------------------------------------------
# the live chars-per-token correction
# --------------------------------------------------------------------------

class TestCharsPerToken:
    def test_it_starts_crude(self):
        tx = transcript()
        assert tx.chars_per_token == DEFAULT_CHARS_PER_TOKEN
        assert tx.chars_per_token_observed is False

    def test_it_is_corrected_from_the_servers_own_prompt_tokens(self):
        tx = transcript()
        tx.observe_usage(request_chars=1200, prompt_tokens=200)
        assert tx.chars_per_token == 6.0
        assert tx.chars_per_token_observed is True
        # And the estimate moves with it — that IS the feedback loop.
        assert tx.estimate_chars(600) == 101

    def test_a_nonsense_ratio_is_clamped_rather_than_believed(self):
        tx = transcript()
        tx.observe_usage(request_chars=100_000, prompt_tokens=1)
        assert tx.chars_per_token == MAX_CHARS_PER_TOKEN
        tx.observe_usage(request_chars=10, prompt_tokens=10_000)
        assert tx.chars_per_token == MIN_CHARS_PER_TOKEN

    def test_a_missing_usage_block_leaves_the_ratio_alone(self):
        tx = transcript()
        tx.observe_usage(request_chars=1200, prompt_tokens=None)
        assert tx.chars_per_token == DEFAULT_CHARS_PER_TOKEN

    def test_the_correction_happens_through_the_real_loop(self, tmp_path):
        repo = make_repo(tmp_path)
        _, _, session, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[tool_call("read_file", {"path": "README.md"})],
                    usage={"prompt_tokens": 40, "completion_tokens": 5},
                ),
                chat_response(
                    tool_calls=[tool_call("finish", {"status": "blocked", "summary": "x"})],
                    usage={"prompt_tokens": 60, "completion_tokens": 5},
                ),
            ],
        )
        assert len(session.requests) == 2


# --------------------------------------------------------------------------
# elision
# --------------------------------------------------------------------------

class TestElision:
    def build_long_transcript(self, tx, turns=12):
        for index in range(turns):
            tx.append_assistant("x" * 400)
            tx.append_tool_result(
                FakeCall(call_id=f"c{index}"),
                ToolResult("y" * 400),
                "text",
            )
        return tx

    def test_it_drops_the_middle_and_keeps_the_ends(self):
        logs = []
        tx = transcript(system="S" * 200, task="T" * 200, window=2048, output=128, logs=logs)
        self.build_long_transcript(tx)
        before = tx.estimate()
        assert before > tx.working_budget

        record = tx.fit()

        assert record is not None
        assert tx.estimate() <= tx.working_budget
        assert tx.entries[0].content == "S" * 200, "the system message is never dropped"
        assert tx.entries[1].content == "T" * 200, "the task message is never dropped"
        assert any("context: elided" in line for line in logs)

    def test_it_installs_exactly_one_honest_marker(self):
        tx = transcript(window=2048, output=128)
        self.build_long_transcript(tx)
        tx.fit()
        markers = [e for e in tx.entries if e.kind == "marker"]
        assert len(markers) == 1
        assert "earlier messages elided to fit the context window" in markers[0].content
        assert "Re-read files if you need them." in markers[0].content
        assert markers[0].role == "user"

    def test_repeated_elisions_accumulate_into_the_same_marker(self):
        tx = transcript(window=2048, output=128)
        self.build_long_transcript(tx, turns=10)
        tx.fit()
        first = [e for e in tx.entries if e.kind == "marker"][0].content
        self.build_long_transcript(tx, turns=10)
        tx.fit()
        markers = [e for e in tx.entries if e.kind == "marker"]
        assert len(markers) == 1
        assert markers[0].content != first
        assert tx.elided.events == 2

    def test_the_recent_tail_is_never_dropped(self):
        tx = transcript(window=2048, output=128)
        self.build_long_transcript(tx, turns=20)
        tail = [entry.content for entry in tx.entries[-KEEP_RECENT_TURNS:]]
        tx.fit()
        assert [entry.content for entry in tx.entries[-KEEP_RECENT_TURNS:]] == tail

    def test_it_never_summarizes(self):
        """No extra generation, no model call: elision is pure bookkeeping."""
        tx = transcript(window=2048, output=128)
        self.build_long_transcript(tx)
        assert not hasattr(tx, "client")
        tx.fit()
        marker = [e for e in tx.entries if e.kind == "marker"][0]
        assert "summar" not in marker.content.lower()

    def test_fit_is_a_no_op_under_budget(self):
        tx = transcript(window=32768, output=1024)
        tx.append_assistant("short")
        assert tx.fit() is None

    def test_an_assistant_turn_and_its_tool_results_are_dropped_together(self):
        """In tools mode an assistant message carrying tool_calls MUST be
        followed by its tool results, or the server rejects the request."""
        tx = transcript(window=1536, output=64)
        for index in range(10):
            call = FakeCall(call_id=f"c{index}")
            tx.append_assistant("z" * 300, [_ToolCallStub(call.name, call.id)])
            tx.append_tool_result(call, ToolResult("w" * 300), "tools")
        tx.fit()
        for position, entry in enumerate(tx.entries):
            if entry.role == "tool":
                previous = tx.entries[position - 1]
                assert previous.role in ("assistant", "tool"), (
                    "a tool result was orphaned by elision"
                )

    def test_the_elision_is_counted_on_the_usage_record(self, tmp_path):
        repo = make_repo(tmp_path)
        wall = "Q" * 6000
        result, logs, _, _ = run_harness(
            repo,
            [
                chat_response(
                    content=wall,
                    tool_calls=[tool_call("read_file", {"path": "README.md"})],
                    usage=dict(DEFAULT_USAGE_SERIES[0]),
                )
            ],
            endpoint=endpoint_block(
                capabilities={"context_window": 2048, "max_output_tokens": 128}
            ),
            harness=harness_block(max_iterations=6),
        )
        assert result.usage["raw"]["harness"]["context_elisions"] >= 1
        assert any("context: elided" in line for line in logs)


class _ToolCallStub:
    def __init__(self, name, call_id):
        self.name = name
        self.id = call_id
        self.arguments_raw = "{}"


# --------------------------------------------------------------------------
# tool results are always truncatable; the transcript is not
# --------------------------------------------------------------------------

def test_a_giant_tool_result_is_truncated_before_it_enters_the_transcript():
    tx = transcript(window=2048, output=128)
    cap = tx.tool_result_char_cap()
    assert cap <= TOOL_OUTPUT_MAX_BYTES
    tx.append_tool_result(FakeCall(), ToolResult("R" * 100_000), "text")
    entry = tx.entries[-1]
    assert len(entry.content) < 100_000
    assert "bytes elided" in entry.content


# --------------------------------------------------------------------------
# the turn-0 refusal
# --------------------------------------------------------------------------

class TestContextFloor:
    def test_a_prompt_that_cannot_fit_raises_before_any_request(self):
        tx = transcript(system="S" * 100, task="T" * 40_000, window=512, output=256)
        with pytest.raises(ContextFloorUnmeetable) as caught:
            tx.check_floor()
        message = str(caught.value)
        assert "exceeds endpoint local-4090's context window" in message
        assert "use a larger model, trim the spec context" in message

    def test_a_prompt_that_fits_does_not_raise(self):
        tx = transcript(window=32768, output=1024)
        tx.check_floor()

    def test_the_executor_maps_it_to_exit_6_with_zero_spend(self, tmp_path):
        repo = make_repo(tmp_path)
        result, _, session, _ = run_harness(
            repo,
            [chat_response(content="never reached")],
            endpoint=endpoint_block(
                capabilities={"context_window": 512, "max_output_tokens": 256}
            ),
            prompt="y" * 40_000,
        )
        assert result.exit_code == EXIT_CONTEXT
        assert session.requests == []
