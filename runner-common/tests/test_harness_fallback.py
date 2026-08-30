"""
THE NO-TOOLS FALLBACK PROTOCOL (Milestone 14.2, design section 3.8).

Plenty of genuinely useful self-hosted models either do not implement the
OpenAI ``tools`` parameter or implement it badly enough that the server
accepts it and the model emits prose anyway. Refusing them would remove most
of the hardware this milestone exists to support, so the harness asks for one
fenced ``lazyaf`` block, parses it with six ORDERED rules, and corrects the
model at most three consecutive times.

The two things this file exists to prove: it NEVER hangs, and it NEVER
silently passes. A model that only ever emits prose burns its budget and dies
with a reason and its last raw response in the step log.
"""
import json

import pytest

from runner_common.harness.constants import EXIT_BUDGET, EXIT_UNPARSEABLE
from runner_common.harness.fallback import (
    Action,
    Malformed,
    correction_for,
    parse_action,
    system_prompt,
    tool_menu,
)
from runner_common.harness.tools import TOOL_ORDER
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    FakeResponse,
    chat_response,
    endpoint_block,
    harness_block,
    lazyaf_block,
    make_repo,
    run_harness,
    tool_call,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def usage(index=0):
    return dict(DEFAULT_USAGE_SERIES[index % len(DEFAULT_USAGE_SERIES)])


def block(body: str) -> str:
    return f"```lazyaf\n{body}\n```"


# --------------------------------------------------------------------------
# the six ordered rules
# --------------------------------------------------------------------------

class TestParserTable:
    def test_a_clean_block_parses_with_defaults_filled(self):
        parsed = parse_action(
            block('{"tool": "read_file", "args": {"path": "src/main.py"}}')
        )
        assert isinstance(parsed, Action)
        assert parsed.tool == "read_file"
        assert parsed.args == {"path": "src/main.py", "start_line": 1, "max_lines": 400}
        assert parsed.warn is None

    def test_prose_before_the_block_is_allowed(self):
        text = "I'll read the router first.\n\n" + block(
            '{"tool": "read_file", "args": {"path": "a.py"}}'
        )
        assert isinstance(parse_action(text), Action)

    def test_rule_2_zero_blocks_is_no_block(self):
        parsed = parse_action("I would edit main.py to add the limiter.")
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "no_block"

    def test_rule_3_two_blocks_take_the_first_and_warn(self):
        """Models routinely emit a plan block AND a call block. Refusing that
        would burn a turn on a reply containing a perfectly good action."""
        text = (
            block('{"tool": "list_files", "args": {"path": "."}}')
            + "\n\n"
            + block('{"tool": "read_file", "args": {"path": "a.py"}}')
        )
        parsed = parse_action(text)
        assert isinstance(parsed, Action)
        assert parsed.tool == "list_files"
        assert parsed.warn == "multiple_blocks"

    def test_rule_4_trailing_prose_inside_the_fence_is_repaired(self):
        text = block(
            '{"tool": "read_file", "args": {"path": "a.py"}}\n'
            "Now I will look at the tests."
        )
        parsed = parse_action(text)
        assert isinstance(parsed, Action)
        assert parsed.tool == "read_file"

    def test_rule_4_unrepairable_json_is_bad_json(self):
        parsed = parse_action(block('{"tool": "read_file", "args": {'))
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "bad_json"
        assert parsed.detail

    def test_rule_4_a_json_list_is_bad_json_with_a_reason(self):
        parsed = parse_action(block('["read_file", "a.py"]'))
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "bad_json"
        assert "expected an object" in parsed.detail

    def test_rule_5_an_unknown_tool_names_it(self):
        parsed = parse_action(block('{"tool": "delete_everything", "args": {}}'))
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "unknown_tool: delete_everything"

    def test_rule_5_a_missing_tool_key_is_unknown_tool(self):
        parsed = parse_action(block('{"args": {"path": "a.py"}}'))
        assert isinstance(parsed, Malformed)
        assert parsed.reason.startswith("unknown_tool:")

    def test_rule_6_a_missing_required_argument_names_it(self):
        parsed = parse_action(block('{"tool": "read_file", "args": {}}'))
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "missing_arg: path"

    def test_rule_6_a_wrong_argument_type_names_the_expected_type(self):
        parsed = parse_action(
            block('{"tool": "read_file", "args": {"path": "a.py", "max_lines": "200"}}')
        )
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "bad_arg_type: max_lines expected integer"

    def test_rule_6_absent_args_are_treated_as_an_empty_object(self):
        parsed = parse_action(block('{"tool": "list_files"}'))
        assert isinstance(parsed, Action)
        assert parsed.args["path"] == "."

    def test_the_info_string_is_case_insensitive(self):
        text = '```LAZYAF\n{"tool": "list_files", "args": {}}\n```'
        assert isinstance(parse_action(text), Action)

    def test_a_non_string_response_is_no_block_not_a_crash(self):
        parsed = parse_action(None)
        assert isinstance(parsed, Malformed)
        assert parsed.reason == "no_block"


# --------------------------------------------------------------------------
# corrections
# --------------------------------------------------------------------------

class TestCorrections:
    def test_every_reason_produces_a_quotable_phrase_and_an_example(self):
        for malformed in (
            Malformed("no_block"),
            Malformed("bad_json", detail="Expecting value"),
            Malformed("unknown_tool: nope"),
            Malformed("missing_arg: path"),
            Malformed("bad_arg_type: max_lines expected integer"),
        ):
            phrase, example = correction_for(malformed)
            assert phrase and "```lazyaf" in example

    def test_the_no_block_phrase_is_the_one_the_design_names(self):
        phrase, example = correction_for(Malformed("no_block"))
        assert phrase == "no ```lazyaf block found"
        assert 'keys "tool" ' in example

    def test_the_multiple_block_warning_rides_on_the_next_correction(self):
        phrase, _ = correction_for(Malformed("missing_arg: path", warn="multiple_blocks"))
        assert "more than one ```lazyaf block" in phrase


# --------------------------------------------------------------------------
# the prompts
# --------------------------------------------------------------------------

class TestPrompts:
    def test_the_text_menu_is_generated_from_the_one_tool_table(self):
        menu = tool_menu()
        for name in TOOL_ORDER:
            assert name in menu
        assert '"status": "success"|"failed"|"blocked"' in menu

    def test_the_text_prompt_demands_exactly_one_block(self):
        prompt = system_prompt("text", "/workspace/repo", 40)
        assert "EXACTLY ONE fenced block" in prompt
        assert "```lazyaf" in prompt
        assert "at most 40 turns" in prompt
        assert "Do not commit or push" in prompt

    def test_the_tools_prompt_is_short_and_names_finish(self):
        prompt = system_prompt("tools", "/workspace/repo", 40)
        assert "```lazyaf" not in prompt
        assert 'finish(status="success"' in prompt
        assert len(prompt) < 900, "every token here is paid on EVERY turn"


# --------------------------------------------------------------------------
# the retry counter, end to end through the real loop
# --------------------------------------------------------------------------

class TestMalformedRetries:
    def test_three_malformed_replies_recover_when_the_fourth_parses(self, repo):
        script = [
            chat_response(content="thinking...", usage=usage(0)),
            chat_response(content="still thinking", usage=usage(1)),
            chat_response(content="nearly there", usage=usage(2)),
            chat_response(
                content=lazyaf_block("write_file", {"path": "done.txt", "content": "x"}),
                usage=usage(3),
            ),
            chat_response(
                content=lazyaf_block("finish", {"status": "success", "summary": "ok"}),
                usage=usage(4),
            ),
        ]
        result, logs, session, _ = run_harness(
            repo, script, harness=harness_block(mode="text")
        )
        assert result.success is True
        assert result.usage["raw"]["harness"]["malformed_responses"] == 3
        assert len(session.requests) == 5

    def test_the_counter_resets_so_an_occasional_stumble_is_not_punished(self, repo):
        good_read = lazyaf_block("read_file", {"path": "README.md"})
        script = [
            chat_response(content="hmm", usage=usage(0)),
            chat_response(content=good_read, usage=usage(1)),
            chat_response(content="hmm", usage=usage(2)),
            chat_response(content=good_read, usage=usage(3)),
            chat_response(content="hmm", usage=usage(4)),
            chat_response(
                content=lazyaf_block("finish", {"status": "blocked", "summary": "no"}),
                usage=usage(5),
            ),
        ]
        result, _, session, _ = run_harness(
            repo, script, harness=harness_block(mode="text")
        )
        assert result.usage["raw"]["harness"]["stop_reason"] == "finish"
        assert result.usage["raw"]["harness"]["malformed_responses"] == 3
        assert len(session.requests) == 6

    def test_the_fourth_consecutive_malformed_reply_exits_5_with_the_raw_response(
        self, repo
    ):
        marker = "I-WOULD-EDIT-MAIN-PY"
        result, logs, session, _ = run_harness(
            repo,
            [chat_response(content=marker, usage=usage(0))],
            harness=harness_block(mode="text"),
        )
        assert result.exit_code == EXIT_UNPARSEABLE
        assert "produced no parseable action in 4 consecutive turns" in result.error
        assert "last reason: no_block" in result.error
        assert len(session.requests) == 4
        raw_lines = [line for line in logs if "last raw response:" in line]
        assert raw_lines and marker in raw_lines[0]

    def test_a_correction_is_appended_once_per_malformed_reply(self, repo):
        result, logs, session, _ = run_harness(
            repo,
            [chat_response(content="nope", usage=usage(0))],
            harness=harness_block(mode="text"),
        )
        corrections = [
            message
            for body in session.bodies
            for message in body["messages"]
            if message["role"] == "user"
            and "could not be used" in (message.get("content") or "")
        ]
        # turns 2, 3 and 4 each carry one more correction than the last.
        assert len(corrections) == 1 + 2 + 3

    def test_malformed_turns_count_against_max_iterations(self, repo):
        """A model that only ever emits prose cannot loop forever."""
        result, _, session, _ = run_harness(
            repo,
            [chat_response(content="nope", usage=usage(0))],
            harness=harness_block(mode="text", max_iterations=2),
        )
        assert result.exit_code == EXIT_BUDGET
        assert len(session.requests) == 2
        assert result.usage["raw"]["harness"]["malformed_responses"] == 2

    def test_the_endpoints_health_is_not_blamed_for_a_model_failure(self, repo):
        """A model-capability failure must not make a working endpoint look
        down: `endpoint_http_errors` stays 0 and the stop reason is not
        `endpoint`."""
        result, _, _, _ = run_harness(
            repo,
            [chat_response(content="nope", usage=usage(0))],
            harness=harness_block(mode="text"),
        )
        assert result.usage["raw"]["harness"]["endpoint_http_errors"] == 0
        assert result.usage["raw"]["harness"]["stop_reason"] == "unparseable"


# --------------------------------------------------------------------------
# tool results in fallback mode
# --------------------------------------------------------------------------

class TestModeParity:
    def test_the_same_actions_produce_the_same_log_lines_in_both_modes(self, repo):
        """Design section 3.8: the two modes produce the same observable
        transcript shape and the same log lines — which is exactly what lets
        an experiment vary ONLY ``harness.mode`` and attribute the difference
        to the loop shape rather than to the instrumentation."""
        actions = [
            ("read_file", {"path": "README.md"}),
            ("write_file", {"path": "done.txt", "content": "x"}),
            ("finish", {"status": "success", "summary": "ok"}),
        ]
        tools_script = [
            chat_response(tool_calls=[tool_call(name, args)], usage=usage(index))
            for index, (name, args) in enumerate(actions)
        ]
        text_script = [
            chat_response(content=lazyaf_block(name, args), usage=usage(index))
            for index, (name, args) in enumerate(actions)
        ]

        _, tools_logs, _, _ = run_harness(repo, tools_script)
        (repo / "done.txt").unlink()
        _, text_logs, _, _ = run_harness(
            repo, text_script, harness=harness_block(mode="text")
        )

        def tool_lines(logs):
            return [line for line in logs if line.startswith("[agent]   tool ")]

        assert tool_lines(tools_logs) == tool_lines(text_logs)
        assert len(tool_lines(tools_logs)) == 2


class TestFallbackTranscriptShape:
    def test_results_come_back_as_user_messages_with_the_designed_headers(self, repo):
        script = [
            chat_response(
                content=lazyaf_block("read_file", {"path": "README.md"}), usage=usage(0)
            ),
            chat_response(
                content=lazyaf_block(
                    "apply_patch", {"path": "README.md", "find": "NOPE", "replace": "x"}
                ),
                usage=usage(1),
            ),
            chat_response(
                content=lazyaf_block("finish", {"status": "blocked", "summary": "no"}),
                usage=usage(2),
            ),
        ]
        _, _, session, _ = run_harness(repo, script, harness=harness_block(mode="text"))
        final_messages = session.bodies[-1]["messages"]
        roles = {message["role"] for message in final_messages}
        assert roles <= {"system", "user", "assistant"}, "no tool role in text mode"
        joined = "\n".join(m.get("content") or "" for m in final_messages)
        assert "TOOL RESULT read_file (ok)" in joined
        assert "TOOL ERROR apply_patch:" in joined


# --------------------------------------------------------------------------
# the bridge for a probe that lied
# --------------------------------------------------------------------------

class TestProbeDrift:
    def test_a_tools_mode_turn_that_emits_a_block_in_content_still_works(self, repo):
        """The `lying_tools` scenario: the step SUCCEEDS and probe_drift is
        recorded, so the platform can correct the capability record."""
        script = [
            chat_response(
                content=lazyaf_block("write_file", {"path": "done.txt", "content": "x"}),
                usage=usage(0),
            ),
            chat_response(
                tool_calls=[tool_call("finish", {"status": "success", "summary": "ok"})],
                usage=usage(1),
            ),
        ]
        result, logs, _, _ = run_harness(repo, script)
        assert result.success is True
        assert result.usage["raw"]["harness"]["probe_drift"] is True
        assert any("emitted a ```lazyaf block in content" in line for line in logs)

    def test_two_drifting_turns_switch_the_harness_to_fallback_mode(self, repo):
        script = [
            chat_response(
                content=lazyaf_block("read_file", {"path": "README.md"}), usage=usage(0)
            ),
            chat_response(
                content=lazyaf_block("write_file", {"path": "done.txt", "content": "x"}),
                usage=usage(1),
            ),
            chat_response(
                content=lazyaf_block("finish", {"status": "success", "summary": "ok"}),
                usage=usage(2),
            ),
        ]
        result, logs, session, _ = run_harness(repo, script)
        assert result.usage["raw"]["harness"]["mode"] == "text"
        assert any("switching to the no-tools fallback protocol" in l for l in logs)
        # The third request no longer pays the tools-schema tax.
        assert "tools" in session.bodies[0]
        assert "tools" not in session.bodies[2]

    def test_a_400_mentioning_tools_demotes_at_the_request_layer(self, repo):
        script = [
            FakeResponse(400, text='{"error":"this model does not support tools"}'),
            chat_response(
                content=lazyaf_block("write_file", {"path": "done.txt", "content": "x"}),
                usage=usage(0),
            ),
            chat_response(
                content=lazyaf_block("finish", {"status": "success", "summary": "ok"}),
                usage=usage(1),
            ),
        ]
        result, logs, session, _ = run_harness(repo, script)
        assert result.success is True
        assert result.usage["raw"]["harness"]["probe_drift"] is True
        assert result.usage["raw"]["harness"]["mode"] == "text"
        assert any("rejected the tools parameter" in line for line in logs)
        assert "tools" not in session.bodies[1]

    def test_a_400_mentioning_tools_is_not_retried_forever_in_text_mode(self, repo):
        result, _, session, _ = run_harness(
            repo,
            [FakeResponse(400, text='{"error":"function calling unsupported"}')],
            harness=harness_block(mode="text"),
        )
        assert result.exit_code == 4
        assert len(session.requests) == 1
