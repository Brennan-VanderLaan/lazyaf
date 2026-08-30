"""
ONE TEST PER STOP CONDITION (Milestone 14.2, design section 3.2's table).

An inference server has no notion of being done, so the loop supplies ten
named ways to stop and this file walks every one of them. Each asserts the
EXIT CODE and that a usage record was produced carrying the tokens actually
spent — because a step that dies without telemetry is a step whose GPU time
vanished from the cost axis M14 exists to build.

The most opinionated assertion in the file is
``test_finish_success_with_no_file_change_is_a_failure``: in a benchmark, a
no-op that reports success is the most expensive possible failure, because it
looks like a cheap win.
"""
import pytest

from runner_common.harness.constants import (
    EXIT_BUDGET,
    EXIT_CONTEXT,
    EXIT_ENDPOINT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_SIGTERM,
    EXIT_UNPARSEABLE,
    MAX_CONSECUTIVE_TOOL_ERRORS,
    NO_TOOL_PATIENCE,
)
from runner_common.harness.executor import HarnessExecutor, looks_final
from runner_common.harness.loop import run_loop, soft_deadline_seconds
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    FakeResponse,
    chat_response,
    endpoint_block,
    harness_block,
    lazyaf_block,
    make_context,
    make_repo,
    run_harness,
    tool_call,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def usage(index=0):
    return dict(DEFAULT_USAGE_SERIES[index % len(DEFAULT_USAGE_SERIES)])


def call(name, args, index=0):
    return chat_response(tool_calls=[tool_call(name, args)], usage=usage(index))


def finish(status="success", summary="done", index=0):
    return call("finish", {"status": status, "summary": summary}, index)


# --------------------------------------------------------------------------
# 1 — finish
# --------------------------------------------------------------------------

class TestFinish:
    def test_finish_success_with_a_file_change_is_green(self, repo):
        result, logs, session, _ = run_harness(
            repo,
            [
                call("write_file", {"path": "done.txt", "content": "done"}, 0),
                finish("success", "created done.txt", 1),
            ],
        )
        assert result.success is True
        assert result.exit_code == EXIT_OK
        assert result.error is None
        assert (repo / "done.txt").read_text(encoding="utf-8") == "done"
        assert result.usage["raw"]["harness"]["stop_reason"] == "finish"
        assert result.usage["raw"]["harness"]["finish_status"] == "success"
        assert result.usage["raw"]["harness"]["files_changed"] == 1
        assert any("stop: finish(status=success)" in line for line in logs)

    def test_finish_success_with_no_file_change_is_a_failure(self, repo):
        """THE most expensive failure mode in a benchmark, made red."""
        result, _, _, _ = run_harness(
            repo,
            [
                call("read_file", {"path": "README.md"}, 0),
                finish("success", "looks fine to me", 1),
            ],
        )
        assert result.success is False
        assert result.exit_code == EXIT_FAILED
        assert result.error == "the agent reported success but changed no files"
        # The tokens still land: a red step is still a step that cost GPU time.
        assert result.usage["input_tokens"] == 101 + 203

    def test_require_changes_false_lets_an_analysis_step_finish_green(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [finish("success", "reviewed, nothing to change", 0)],
            harness=harness_block(require_changes=False),
        )
        assert result.success is True
        assert result.exit_code == EXIT_OK

    @pytest.mark.parametrize("status", ["failed", "blocked"])
    def test_finish_failed_or_blocked_reports_the_models_own_summary(self, repo, status):
        result, _, _, _ = run_harness(
            repo, [finish(status, "the spec contradicts itself", 0)]
        )
        assert result.success is False
        assert result.exit_code == EXIT_FAILED
        assert result.error == "the spec contradicts itself"
        assert result.usage["raw"]["harness"]["finish_status"] == status


# --------------------------------------------------------------------------
# 2, 3, 4 — the budgets
# --------------------------------------------------------------------------

class TestBudgets:
    def test_iteration_budget_stops_a_model_that_never_finishes(self, repo):
        """The `never_finishes` scenario: echo read_file forever."""
        result, _, session, _ = run_harness(
            repo,
            [call("read_file", {"path": "README.md"}, 0)],
            harness=harness_block(max_iterations=4),
        )
        assert result.success is False
        assert result.exit_code == EXIT_BUDGET
        assert "iteration budget" in result.error
        assert len(session.requests) == 4
        assert result.usage["raw"]["harness"]["turns"] == 4
        assert result.usage["input_tokens"] == 101 * 4

    def test_token_budget_stops_the_loop_and_names_the_counters(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [call("read_file", {"path": "README.md"}, 0)],
            harness=harness_block(max_iterations=40, max_total_tokens=300),
        )
        assert result.exit_code == EXIT_BUDGET
        assert "token budget" in result.error
        # 3 turns of 112 tokens each crosses 300 at the top of turn 4.
        assert result.usage["raw"]["harness"]["turns"] == 3
        assert result.usage["raw"]["harness"]["stop_reason"] == "token_budget"

    def test_soft_deadline_stops_inside_the_watchdogs_hard_one(self, repo):
        """Stop condition 4, the load-bearing one, on an INJECTED clock.

        A wall-clock deadline tested against the wall clock is a flake; the
        loop takes its clock as a seam precisely so this assertion can be
        exact.
        """
        ticks = iter(range(0, 100))
        ctx, logs, session = make_context(
            repo,
            [call("read_file", {"path": "README.md"}, 0)],
            clock=lambda: float(next(ticks)),
            harness=harness_block(time_budget_seconds=5),
        )
        outcome = run_loop(ctx)
        assert outcome.stop_reason == "time_budget"
        assert "time budget (5s) was spent" in outcome.error
        # It stopped ITSELF, which is the whole point: it is still alive to
        # commit and write telemetry.
        assert ctx.accumulator.input_tokens == 101 * len(session.requests)
        assert len(session.requests) < ctx.max_iterations

    def test_the_executor_surfaces_the_time_budget_as_exit_3(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [call("read_file", {"path": "README.md"}, 0)],
            harness=harness_block(time_budget_seconds=0.001, max_iterations=60),
        )
        assert result.exit_code == EXIT_BUDGET
        assert "time budget" in result.error
        assert result.usage["raw"]["harness"]["stop_reason"] == "time_budget"
        assert result.usage["input_tokens"] > 0

    def test_soft_deadline_is_derived_from_the_step_timeout_by_one_rule(self):
        assert soft_deadline_seconds(1800) == 1740
        assert soft_deadline_seconds(300) == 240
        # Under 2x the reserve, half the timeout (and the caller warns).
        assert soft_deadline_seconds(90) == 45
        assert soft_deadline_seconds(None) is None
        assert soft_deadline_seconds(0) is None


# --------------------------------------------------------------------------
# 5 — the model stops calling tools
# --------------------------------------------------------------------------

class TestModelStopsCallingTools:
    def test_two_prose_only_turns_stop_the_loop(self, repo):
        result, logs, session, _ = run_harness(
            repo, [chat_response(content="Let me think about this.", usage=usage(0))]
        )
        assert len(session.requests) == NO_TOOL_PATIENCE
        assert result.success is False
        assert result.exit_code == EXIT_FAILED
        assert result.error == (
            "the agent stopped without calling finish and changed no files"
        )
        assert any("Let me think about this." in line for line in logs)

    def test_final_sounding_prose_plus_a_real_change_is_green(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                call("write_file", {"path": "done.txt", "content": "x"}, 0),
                chat_response(content="I have implemented the change.", usage=usage(1)),
                chat_response(content="I have implemented the change.", usage=usage(2)),
            ],
        )
        assert result.success is True
        assert result.exit_code == EXIT_OK

    def test_non_final_prose_with_a_change_is_still_a_failure(self, repo):
        result, _, _, _ = run_harness(
            repo,
            [
                call("write_file", {"path": "done.txt", "content": "x"}, 0),
                chat_response(content="Hmm, what next?", usage=usage(1)),
                chat_response(content="Hmm, what next?", usage=usage(2)),
            ],
        )
        assert result.success is False
        assert result.error == (
            "the agent stopped calling tools without calling finish"
        )

    def test_looks_final_is_a_small_stated_phrase_list(self):
        assert looks_final("The task is complete.") is True
        assert looks_final("I have implemented the rate limiter") is True
        assert looks_final("Next I will read the router") is False
        assert looks_final("") is False


# --------------------------------------------------------------------------
# 6 — the tool-error loop
# --------------------------------------------------------------------------

def test_five_consecutive_tool_errors_stop_the_loop(repo):
    """The specific small-model pathology: retrying an identical failing
    apply_patch until the container watchdog kills it."""
    result, logs, session, _ = run_harness(
        repo,
        [
            call(
                "apply_patch",
                {"path": "src/main.py", "find": "NOT PRESENT", "replace": "x"},
                0,
            )
        ],
    )
    assert result.exit_code == EXIT_FAILED
    assert result.usage["raw"]["harness"]["stop_reason"] == "tool_error_loop"
    assert result.usage["raw"]["harness"]["tool_errors"] == MAX_CONSECUTIVE_TOOL_ERRORS
    assert len(session.requests) == MAX_CONSECUTIVE_TOOL_ERRORS
    assert any("matched 0 occurrences" in line for line in logs)


def test_a_successful_tool_resets_the_consecutive_error_counter(repo):
    script = [
        call("apply_patch", {"path": "src/main.py", "find": "NOPE", "replace": "x"}, 0),
        call("apply_patch", {"path": "src/main.py", "find": "NOPE", "replace": "x"}, 1),
        call("read_file", {"path": "README.md"}, 2),
        call("apply_patch", {"path": "src/main.py", "find": "NOPE", "replace": "x"}, 3),
        call("apply_patch", {"path": "src/main.py", "find": "NOPE", "replace": "x"}, 4),
        finish("blocked", "cannot find the code", 5),
    ]
    result, _, session, _ = run_harness(repo, script)
    assert result.usage["raw"]["harness"]["stop_reason"] == "finish"
    assert len(session.requests) == 6


# --------------------------------------------------------------------------
# 7 — unparseable (fallback mode); see test_harness_fallback for the parser
# --------------------------------------------------------------------------

def test_four_consecutive_unparseable_replies_exit_5(repo):
    result, logs, session, _ = run_harness(
        repo,
        [chat_response(content="I would edit main.py.", usage=usage(0))],
        harness=harness_block(mode="text"),
    )
    assert result.exit_code == EXIT_UNPARSEABLE
    assert "no parseable action in 4 consecutive turns" in result.error
    assert "last reason: no_block" in result.error
    assert result.usage["raw"]["harness"]["malformed_responses"] == 4
    assert any("last raw response:" in line for line in logs)
    # It NEVER silently passes: the usage row still lands with every token.
    assert result.usage["input_tokens"] == 101 * 4


# --------------------------------------------------------------------------
# 8 — endpoint fatal
# --------------------------------------------------------------------------

def test_a_non_retryable_http_error_is_fatal_on_the_first_response(repo):
    result, _, session, _ = run_harness(
        repo, [FakeResponse(404, text='{"error":"model not found"}')]
    )
    assert result.exit_code == EXIT_ENDPOINT
    assert "404" in result.error
    assert len(session.requests) == 1, "a 404 must not be retried"
    assert result.usage["raw"]["harness"]["stop_reason"] == "endpoint"


def test_a_503_is_retried_then_succeeds(repo):
    """The `flaky_5xx` scenario: two 503s then a real answer."""
    result, _, session, _ = run_harness(
        repo,
        [
            FakeResponse(503, text="overloaded"),
            FakeResponse(503, text="overloaded"),
            call("write_file", {"path": "done.txt", "content": "x"}, 0),
            finish("success", "done", 1),
        ],
    )
    assert result.success is True
    assert len(session.requests) == 4
    assert result.usage["raw"]["harness"]["endpoint_http_errors"] == 2


# --------------------------------------------------------------------------
# 9 — SIGTERM
# --------------------------------------------------------------------------

class TestCancellation:
    def test_a_cancelled_context_stops_at_the_top_of_the_next_turn(self, repo):
        ctx, logs, session = make_context(
            repo, [call("read_file", {"path": "README.md"}, 0)]
        )
        ctx.cancelled = True
        outcome = run_loop(ctx)
        assert outcome.stop_reason == "cancelled"
        assert session.requests == []

    def test_the_signal_handler_sets_cancelled_and_restores_the_previous(self, repo):
        import signal

        from runner_common.harness.executor import _install_cancel_handler

        ctx, logs, _ = make_context(repo, [])
        previous = signal.getsignal(signal.SIGTERM)
        restore = _install_cancel_handler(ctx, logs.append)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert handler is not previous
            handler(signal.SIGTERM, None)
            assert ctx.cancelled is True
            assert any("stopping after the current tool call" in l for l in logs)
        finally:
            restore()
        assert signal.getsignal(signal.SIGTERM) is previous

    def test_cancelled_maps_to_exit_143(self, repo):
        from runner_common.harness.loop import HarnessOutcome

        executor = HarnessExecutor(endpoint_block(), harness_block())
        success, code, error = executor._verdict(
            HarnessOutcome("cancelled", 3, error="cancelled"),
            files_changed=0,
            require_changes=True,
        )
        assert (success, code, error) == (False, EXIT_SIGTERM, "cancelled")


# --------------------------------------------------------------------------
# 10 — the context floor, at turn 0, before any spend
# --------------------------------------------------------------------------

def test_a_prompt_that_cannot_fit_fails_before_any_request(repo):
    endpoint = endpoint_block(
        capabilities={"context_window": 512, "max_output_tokens": 256}
    )
    result, logs, session, _ = run_harness(
        repo,
        [finish("success", "never reached", 0)],
        endpoint=endpoint,
        prompt="x" * 40_000,
    )
    assert result.exit_code == EXIT_CONTEXT
    assert session.requests == [], "not one token may be spent"
    assert "exceeds endpoint local-4090's context window" in result.error
    assert result.usage["input_tokens"] is None
    assert result.usage["raw"]["harness"]["stop_reason"] == "context_floor"


# --------------------------------------------------------------------------
# the loop's own guard rails
# --------------------------------------------------------------------------

def test_more_tool_calls_than_the_per_turn_cap_are_dropped_loudly(repo):
    many = [tool_call("read_file", {"path": "README.md"}, f"c{i}") for i in range(7)]
    result, logs, _, _ = run_harness(
        repo,
        [
            chat_response(tool_calls=many, usage=usage(0)),
            finish("blocked", "stopping", 1),
        ],
        harness=harness_block(max_tool_calls_per_turn=2),
    )
    assert any("honoring the first 2" in line for line in logs)
    assert result.usage["raw"]["harness"]["tool_calls"]["read_file"] == 2


def test_every_turn_records_usage_even_when_the_tool_errors(repo):
    result, _, _, _ = run_harness(
        repo,
        [
            call("read_file", {"path": "../../etc/passwd"}, 0),
            finish("blocked", "cannot read outside the workspace", 1),
        ],
    )
    assert result.usage["raw"]["harness"]["tool_errors"] == 1
    assert result.usage["input_tokens"] == 101 + 203


def test_the_tools_mode_transcript_stays_server_legal(repo):
    """Every ``tool`` message answers a tool_call id the assistant actually
    emitted, and every emitted id is answered. A server rejects the next
    request outright when that pairing breaks, which is the failure this
    assertion exists to catch before a real endpoint does."""
    from tests.fixtures.openai import chat_payload

    unnamed = chat_payload()
    unnamed["choices"][0]["message"]["tool_calls"] = [
        # No `id` at all — some servers omit it, and the harness must still
        # produce a legal pairing.
        {"type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "README.md"}'}}
    ]
    unnamed["usage"] = usage(0)
    result, _, session, _ = run_harness(
        repo,
        [
            FakeResponse(200, unnamed),
            call("write_file", {"path": "done.txt", "content": "x"}, 1),
            finish("success", "done", 2),
        ],
    )
    assert result.success is True
    final = session.bodies[-1]["messages"]
    emitted = [
        entry["id"]
        for message in final
        for entry in (message.get("tool_calls") or [])
    ]
    answered = [
        message["tool_call_id"] for message in final if message["role"] == "tool"
    ]
    assert emitted and sorted(emitted) == sorted(answered)
    assert all(identifier for identifier in emitted), "no null tool_call ids"


def test_a_finish_block_in_text_mode_terminates_too(repo):
    result, _, session, _ = run_harness(
        repo,
        [
            chat_response(
                content=lazyaf_block(
                    "write_file", {"path": "done.txt", "content": "x"}
                ),
                usage=usage(0),
            ),
            chat_response(
                content=lazyaf_block(
                    "finish", {"status": "success", "summary": "wrote it"}
                ),
                usage=usage(1),
            ),
        ],
        harness=harness_block(mode="text"),
    )
    assert result.success is True
    assert result.usage["raw"]["harness"]["mode"] == "text"
    assert len(session.requests) == 2
    # The tools schema is never sent in text mode: that is the whole saving.
    assert all("tools" not in (body or {}) for body in session.bodies)
