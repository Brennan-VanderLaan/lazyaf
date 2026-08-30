"""The mock OpenAI server serves every wave8 s8.1 scenario, and the REAL probe
agrees with it.

Two kinds of assertion live here and the second kind is the point:

1. **Shape** - each scenario's response is the OpenAI wire shape the harness
   parses (`choices[0].message.tool_calls`, SSE `data:` frames with
   `choices[].delta`, a `usage` object with integer counts).

2. **Agreement with the real judge** - the tests import
   `app.services.model_endpoints.probe`'s ACTUAL decision functions and run
   them over the mock's ACTUAL bytes. A mock that only satisfies a
   hand-written idea of the format is a mock that will drift; one that
   satisfies the shipping judge cannot (R3, R6).

The token constants are pinned here too, because
`scripts/verify_executor.py` assertion 13 carries a stdlib-only copy of them
(it runs in a bare step container that cannot import `tdd`) and a silent drift
would turn the strongest assertion in the gate into a tautology.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# runner-common lives OUTSIDE tdd/, so tdd/conftest.py's sys.path work does not
# cover it. The harness's own parser is imported below to prove the mock and
# the consumer agree; adding the path here is the same thing
# tdd/unit/control_runtime/conftest.py does for the control runtime.
_RUNNER_COMMON = Path(__file__).resolve().parents[3] / "runner-common"
if str(_RUNNER_COMMON) not in sys.path:
    sys.path.insert(0, str(_RUNNER_COMMON))

from tdd.shared.mock_openai import (
    ACTION_SCRIPT_LENGTH,
    MOCK_COMPLETION_TOKENS_PER_TURN,
    MOCK_MODELS,
    MOCK_MODEL_CONTEXT_WINDOW,
    MOCK_PROMPT_TOKENS_PER_TURN,
    MockOpenAIServer,
    SCENARIOS,
    expected_summed_tokens,
    largest_single_turn_tokens,
)
from tdd.shared.mock_openai.scenarios import (
    DEFAULT_TARGET_PATH,
    FLAKY_FAILURES,
    action_script,
    reset_state,
    target_path,
    turn_number,
)

pytestmark = pytest.mark.unit

MODEL = MOCK_MODELS[0]


# -----------------------------------------------------------------------------
# Fixtures + tiny stdlib client
# -----------------------------------------------------------------------------

@pytest.fixture()
def server():
    reset_state()
    with MockOpenAIServer(host="127.0.0.1") as srv:
        yield srv


def _post(url: str, body: dict, raw: bool = False):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read()
    return payload.decode() if raw else json.loads(payload)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def _chat(server, scenario: str, messages: list[dict], **extra) -> dict:
    body = {"model": MODEL, "messages": messages, **extra}
    return _post(server.base_url(scenario, host="127.0.0.1") + "/chat/completions", body)


def _task(text: str = f"Create {DEFAULT_TARGET_PATH} naming the endpoint") -> list[dict]:
    return [
        {"role": "system", "content": "You are a software engineer."},
        {"role": "user", "content": text},
    ]


def _advance(messages: list[dict], reply: dict, tool_role: str = "tool") -> list[dict]:
    """Append one assistant reply plus its result, as a real client would."""
    message = reply["choices"][0]["message"]
    out = list(messages) + [message]
    if message.get("tool_calls"):
        out.append({"role": tool_role, "tool_call_id": "call_0", "content": "ok"})
    else:
        out.append({"role": "user", "content": "TOOL RESULT (ok)"})
    return out


def _drive(server, scenario: str, turns: int, messages=None) -> list[dict]:
    """Run `turns` turns and return every reply, in order."""
    messages = messages if messages is not None else _task()
    replies = []
    for _ in range(turns):
        reply = _chat(server, scenario, messages)
        replies.append(reply)
        messages = _advance(messages, reply)
    return replies


# -----------------------------------------------------------------------------
# Contract: the scenario set
# -----------------------------------------------------------------------------

class TestScenarioSet:
    def test_all_nine_named_scenarios_are_served(self):
        assert set(SCENARIOS) == {
            "happy_tools",
            "happy_text",
            "never_finishes",
            "malformed",
            "malformed_forever",
            "no_usage",
            "lying_tools",
            "slow",
            "flaky_5xx",
        }

    def test_control_endpoint_lists_them(self, server):
        payload = _get(f"http://127.0.0.1:{server.port}/_control/scenarios")
        assert sorted(payload["scenarios"]) == sorted(SCENARIOS)
        assert payload["default"] == "happy_tools"

    def test_health(self, server):
        assert _get(f"http://127.0.0.1:{server.port}/health")["status"] == "ok"

    def test_unknown_route_names_the_scenarios(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"http://127.0.0.1:{server.port}/v1/nope")
        assert exc.value.code == 404
        assert "happy_tools" in exc.value.read().decode()


# -----------------------------------------------------------------------------
# The turn counter is derived from the transcript (statelessness)
# -----------------------------------------------------------------------------

class TestTurnNumbering:
    def test_turn_number_counts_assistant_messages(self):
        assert turn_number([]) == 1
        assert turn_number(_task()) == 1
        assert turn_number(_task() + [{"role": "assistant", "content": "x"}]) == 2

    def test_a_replayed_request_returns_the_same_action(self, server):
        messages = _task()
        first = _chat(server, "happy_tools", messages)
        again = _chat(server, "happy_tools", messages)
        assert (
            first["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == again["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        )

    def test_target_path_is_read_out_of_the_task_text(self):
        assert target_path(_task("Create .lazyaf-dogfood/x-ran now")) == (
            ".lazyaf-dogfood/x-ran"
        )
        assert target_path(_task("do something vague")) == DEFAULT_TARGET_PATH


# -----------------------------------------------------------------------------
# happy_tools / happy_text: the six-action script
# -----------------------------------------------------------------------------

class TestHappyScenarios:
    def test_tools_mode_walks_the_script_and_finishes(self, server):
        replies = _drive(server, "happy_tools", ACTION_SCRIPT_LENGTH)
        names = [
            r["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            for r in replies
        ]
        assert names == [a["tool"] for a in action_script(DEFAULT_TARGET_PATH, MODEL)]
        assert names[-1] == "finish"
        assert replies[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_tool_arguments_are_a_json_string_like_openai(self, server):
        reply = _chat(server, "happy_tools", _task())
        arguments = reply["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"path": ".", "depth": 1, "max_entries": 50}

    def test_text_mode_emits_one_lazyaf_block_and_no_tool_calls(self, server):
        replies = _drive(server, "happy_text", ACTION_SCRIPT_LENGTH)
        for reply in replies:
            message = reply["choices"][0]["message"]
            assert "tool_calls" not in message or not message["tool_calls"]
            assert message["content"].count("```lazyaf") == 1
        last = replies[-1]["choices"][0]["message"]["content"]
        assert json.loads(last.split("```lazyaf\n")[1].split("\n```")[0])["tool"] == (
            "finish"
        )

    def test_the_script_writes_the_path_the_task_named(self, server):
        messages = _task("Create .lazyaf-dogfood/harness-fallback-ran please")
        replies = _drive(server, "happy_text", ACTION_SCRIPT_LENGTH, messages)
        written = [
            json.loads(r["choices"][0]["message"]["content"].split("```lazyaf\n")[1].split("\n```")[0])
            for r in replies
        ]
        write = next(a for a in written if a["tool"] == "write_file")
        assert write["args"]["path"] == ".lazyaf-dogfood/harness-fallback-ran"

    def test_the_script_touches_five_tools_plus_finish(self):
        names = {a["tool"] for a in action_script(DEFAULT_TARGET_PATH, MODEL)}
        assert names == {
            "list_files",
            "run_shell",
            "write_file",
            "apply_patch",
            "read_file",
            "finish",
        }


# -----------------------------------------------------------------------------
# Token accounting - the assertion-13 contract
# -----------------------------------------------------------------------------

class TestTokenAccounting:
    def test_usage_grows_with_the_turn(self, server):
        replies = _drive(server, "happy_tools", 3)
        assert [r["usage"]["prompt_tokens"] for r in replies] == [
            MOCK_PROMPT_TOKENS_PER_TURN * n for n in (1, 2, 3)
        ]
        assert [r["usage"]["completion_tokens"] for r in replies] == [
            MOCK_COMPLETION_TOKENS_PER_TURN * n for n in (1, 2, 3)
        ]

    def test_summed_strictly_exceeds_the_largest_single_turn(self):
        for turns in range(2, 12):
            summed = expected_summed_tokens(turns)
            largest = largest_single_turn_tokens(turns)
            assert summed[0] > largest[0]
            assert summed[1] > largest[1]

    def test_one_turn_is_the_degenerate_case_and_is_documented(self):
        # With a single turn "summed" and "last response" are the same number,
        # which is exactly why assertion 13 also requires turns >= 2.
        assert expected_summed_tokens(1) == largest_single_turn_tokens(1)

    def test_a_real_six_turn_run_matches_the_predicted_sum(self, server):
        replies = _drive(server, "happy_tools", ACTION_SCRIPT_LENGTH)
        summed_prompt = sum(r["usage"]["prompt_tokens"] for r in replies)
        summed_completion = sum(r["usage"]["completion_tokens"] for r in replies)
        assert (summed_prompt, summed_completion) == expected_summed_tokens(
            ACTION_SCRIPT_LENGTH
        )

    def test_no_usage_scenario_omits_the_block_entirely(self, server):
        for reply in _drive(server, "no_usage", 3):
            assert "usage" not in reply


# -----------------------------------------------------------------------------
# The failure scenarios
# -----------------------------------------------------------------------------

class TestFailureScenarios:
    def test_never_finishes_never_calls_finish(self, server):
        for reply in _drive(server, "never_finishes", 12):
            name = reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            assert name == "list_files"

    def test_malformed_is_three_prose_turns_then_parseable_blocks(self, server):
        replies = _drive(server, "malformed", 5)
        for reply in replies[:3]:
            content = reply["choices"][0]["message"]["content"]
            assert "```lazyaf" not in content
            assert content
        for reply in replies[3:]:
            assert "```lazyaf" in reply["choices"][0]["message"]["content"]

    def test_malformed_recovers_at_the_first_action_of_the_script(self, server):
        replies = _drive(server, "malformed", 4)
        block = replies[3]["choices"][0]["message"]["content"]
        action = json.loads(block.split("```lazyaf\n")[1].split("\n```")[0])
        assert action["tool"] == "list_files"

    def test_malformed_forever_never_parses(self, server):
        for reply in _drive(server, "malformed_forever", 10):
            assert "```lazyaf" not in reply["choices"][0]["message"]["content"]

    def test_flaky_5xx_fails_twice_then_succeeds(self, server):
        messages = _task()
        for attempt in range(FLAKY_FAILURES):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _chat(server, "flaky_5xx", messages)
            assert exc.value.code == 503, f"attempt {attempt}"
        reply = _chat(server, "flaky_5xx", messages)
        assert reply["choices"][0]["message"]["tool_calls"]

    def test_flaky_5xx_counter_is_resettable(self, server):
        messages = _task()
        for _ in range(FLAKY_FAILURES):
            with pytest.raises(urllib.error.HTTPError):
                _chat(server, "flaky_5xx", messages)
        _post(f"http://127.0.0.1:{server.port}/_control/reset", {})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _chat(server, "flaky_5xx", messages)
        assert exc.value.code == 503

    def test_slow_takes_measurable_time(self, server, monkeypatch):
        import time

        monkeypatch.setenv("LAZYAF_MOCK_SLOW_SECONDS", "0.4")
        started = time.monotonic()
        _chat(server, "slow", _task())
        assert time.monotonic() - started >= 0.35

    def test_lying_tools_answers_the_probe_but_never_the_harness(self, server):
        probe_body = {
            "model": MODEL,
            "messages": _task(),
            "tools": [{"type": "function", "function": {"name": "probe"}}],
            "tool_choice": "auto",
        }
        probe_reply = _post(
            server.base_url("lying_tools", host="127.0.0.1") + "/chat/completions",
            probe_body,
        )
        assert (
            probe_reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == "probe"
        )
        harness_reply = _chat(
            server,
            "lying_tools",
            _task(),
            tools=[{"type": "function", "function": {"name": "list_files"}}],
        )
        message = harness_reply["choices"][0]["message"]
        assert not message.get("tool_calls")
        assert "```lazyaf" in message["content"]


# -----------------------------------------------------------------------------
# Streaming
# -----------------------------------------------------------------------------

class TestStreaming:
    def _sse(self, server, scenario: str, **extra) -> list[str]:
        raw = _post(
            server.base_url(scenario, host="127.0.0.1") + "/chat/completions",
            {"model": MODEL, "messages": _task(), "stream": True, **extra},
            raw=True,
        )
        return [
            line[len("data: "):]
            for line in raw.splitlines()
            if line.startswith("data: ")
        ]

    def test_stream_ends_with_done(self, server):
        assert self._sse(server, "happy_tools")[-1] == "[DONE]"

    def test_stream_carries_delta_frames(self, server):
        frames = [json.loads(f) for f in self._sse(server, "happy_text")[:-1]]
        assert any(
            frame["choices"] and "delta" in frame["choices"][0] for frame in frames
        )

    def test_prose_is_split_across_more_than_one_content_delta(self, server):
        frames = [json.loads(f) for f in self._sse(server, "happy_text")[:-1]]
        content_frames = [
            f
            for f in frames
            if f["choices"] and f["choices"][0]["delta"].get("content")
        ]
        assert len(content_frames) >= 2

    def test_include_usage_puts_usage_on_the_final_frame(self, server):
        frames = [json.loads(f) for f in self._sse(
            server, "happy_tools", stream_options={"include_usage": True}
        )[:-1]]
        assert frames[-1]["usage"]["prompt_tokens"] == MOCK_PROMPT_TOKENS_PER_TURN

    def test_include_usage_false_omits_it(self, server):
        frames = [json.loads(f) for f in self._sse(
            server, "happy_tools", stream_options={"include_usage": False}
        )[:-1]]
        assert all("usage" not in frame for frame in frames)

    def test_tool_calls_stream_as_deltas(self, server):
        frames = [json.loads(f) for f in self._sse(
            server, "happy_tools", tools=[{"type": "function", "function": {"name": "x"}}]
        )[:-1]]
        calls = [
            f
            for f in frames
            if f["choices"] and f["choices"][0]["delta"].get("tool_calls")
        ]
        assert calls
        assert calls[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"]


# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------

class TestAuth:
    def test_no_key_configured_means_no_auth_required(self, server):
        assert _get(server.base_url("happy_tools", host="127.0.0.1") + "/models")

    def test_a_configured_key_is_enforced(self):
        with MockOpenAIServer(host="127.0.0.1", api_key="sentinel-key-123") as srv:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(srv.base_url("happy_tools", host="127.0.0.1") + "/models")
            assert exc.value.code == 401
            request = urllib.request.Request(
                srv.base_url("happy_tools", host="127.0.0.1") + "/models",
                headers={"Authorization": "Bearer sentinel-key-123"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert json.load(response)["data"]

    def test_a_custom_header_style_is_enforced(self):
        with MockOpenAIServer(
            host="127.0.0.1", api_key="k9", auth_header="x-api-key"
        ) as srv:
            request = urllib.request.Request(
                srv.base_url("happy_tools", host="127.0.0.1") + "/models",
                headers={"x-api-key": "k9"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert json.load(response)["data"]


# -----------------------------------------------------------------------------
# THE REAL PROBE AGREES (R3/R6): the shipping judges, over the mock's bytes
# -----------------------------------------------------------------------------

class TestTheRealProbeAgrees:
    """These import `app.services.model_endpoints.probe` and run its ACTUAL
    decision functions. A mock that satisfies a hand-written idea of the
    OpenAI format drifts; a mock that satisfies the shipping judge cannot."""

    def _http(self, status: int, payload=None, lines=None):
        from app.services.model_endpoints.probe import ProbeHTTP

        return ProbeHTTP(
            status=status, payload=payload, text=json.dumps(payload or {}),
            lines=lines or [],
        )

    def test_models_listing_satisfies_judge_models(self, server):
        from app.services.model_endpoints.probe import judge_models

        payload = _get(server.base_url("happy_tools", host="127.0.0.1") + "/models")
        listed, max_model_len, reason = judge_models(
            self._http(200, payload), MOCK_MODELS[0]
        )
        assert listed is True
        assert max_model_len == MOCK_MODEL_CONTEXT_WINDOW
        assert reason is None

    def test_an_unlisted_model_is_reported_as_such(self, server):
        from app.services.model_endpoints.probe import judge_models

        payload = _get(server.base_url("happy_tools", host="127.0.0.1") + "/models")
        listed, _, reason = judge_models(self._http(200, payload), "not-a-mock-model")
        assert listed is False
        assert reason == "model_not_listed"

    def test_tool_probe_satisfies_judge_tools(self, server):
        from app.services.model_endpoints.probe import judge_tools, tool_probe_body

        payload = _post(
            server.base_url("happy_tools", host="127.0.0.1") + "/chat/completions",
            tool_probe_body(MOCK_MODELS[0]),
        )
        supports, reason = judge_tools(self._http(200, payload))
        assert supports is True
        assert reason is None

    def test_happy_text_is_judged_as_no_tool_calls(self, server):
        from app.services.model_endpoints.probe import judge_tools, tool_probe_body

        payload = _post(
            server.base_url("happy_text", host="127.0.0.1") + "/chat/completions",
            tool_probe_body(MOCK_MODELS[0]),
        )
        supports, reason = judge_tools(self._http(200, payload))
        assert supports is False
        assert reason == "no_tool_calls"

    def test_stream_probe_satisfies_judge_streaming(self, server):
        from app.services.model_endpoints.probe import judge_streaming, stream_probe_body

        raw = _post(
            server.base_url("happy_tools", host="127.0.0.1") + "/chat/completions",
            stream_probe_body(MOCK_MODELS[0]),
            raw=True,
        )
        supports, reason = judge_streaming(self._http(200, {}, raw.splitlines()))
        assert supports is True
        assert reason is None

    def test_usage_block_satisfies_judge_usage(self, server):
        from app.services.model_endpoints.probe import judge_usage, tool_probe_body

        payload = _post(
            server.base_url("happy_tools", host="127.0.0.1") + "/chat/completions",
            tool_probe_body(MOCK_MODELS[0]),
        )
        reports, reason = judge_usage([payload])
        assert reports is True
        assert reason is None

    def test_no_usage_scenario_is_judged_as_reporting_nothing(self, server):
        from app.services.model_endpoints.probe import judge_usage, tool_probe_body

        payload = _post(
            server.base_url("no_usage", host="127.0.0.1") + "/chat/completions",
            tool_probe_body(MOCK_MODELS[0]),
        )
        reports, reason = judge_usage([payload])
        assert reports is False
        assert reason == "no_usage_block"

    def test_ollama_api_show_satisfies_judge_ollama_context(self, server):
        from app.services.model_endpoints.probe import judge_ollama_context

        payload = _post(
            f"http://127.0.0.1:{server.port}/happy_tools/api/show",
            {"model": MOCK_MODELS[0]},
        )
        assert judge_ollama_context(self._http(200, payload)) == (
            MOCK_MODEL_CONTEXT_WINDOW
        )

    async def test_run_probe_end_to_end_reports_ok(self, server):
        """The whole probe, over the wire, against the mock: `ok`, tools,
        streaming, usage and a real context window."""
        from app.services.model_endpoints.probe import ProbeSpec, run_probe

        result = await run_probe(
            ProbeSpec(
                base_url=server.base_url("happy_tools", host="127.0.0.1"),
                model=MOCK_MODELS[0],
                server_kind="vllm",
            )
        )
        assert result.reachable is True
        assert result.probe_status == "ok"
        assert result.supports_tools is True
        assert result.supports_streaming is True
        assert result.reports_usage is True
        assert result.context_window == MOCK_MODEL_CONTEXT_WINDOW

    async def test_run_probe_against_the_no_tools_scenario_is_degraded(self, server):
        """`degraded` is USABLE - it routes the fallback protocol. The status
        exists so the UI can say why the endpoint will behave as it will."""
        from app.services.model_endpoints.probe import ProbeSpec, run_probe

        result = await run_probe(
            ProbeSpec(
                base_url=server.base_url("happy_text", host="127.0.0.1"),
                model=MOCK_MODELS[0],
                server_kind="vllm",
            )
        )
        assert result.reachable is True
        assert result.supports_tools is False
        assert result.probe_status == "degraded"
        assert result.reports_usage is True

    async def test_run_probe_discovers_the_window_via_ollama_when_asked(self, server):
        from app.services.model_endpoints.probe import ProbeSpec, run_probe

        result = await run_probe(
            ProbeSpec(
                base_url=server.base_url("happy_tools", host="127.0.0.1"),
                model=MOCK_MODELS[0],
                server_kind="ollama",
            )
        )
        assert result.context_window == MOCK_MODEL_CONTEXT_WINDOW
        assert result.context_window_source == "ollama"


# -----------------------------------------------------------------------------
# THE HARNESS'S OWN PARSER AGREES (R3/R6)
# -----------------------------------------------------------------------------

class TestTheHarnessParserAgrees:
    """The fallback scenarios emit ```lazyaf blocks for a consumer that has to
    parse them. These import that consumer - `runner_common.harness.fallback` -
    and run its REAL parser over the mock's REAL output.

    Without this, the mock and the harness could each be internally consistent
    and mutually useless: the mock emitting `{"name": ...}` where the parser
    wants `{"tool": ...}`, or a tool named `shell` where the table says
    `run_shell`, would sail through every other test in this file and only
    surface as a mysterious `unknown_tool` in a container.
    """

    def _fallback(self):
        from runner_common.harness import fallback

        return fallback

    def test_every_scripted_block_parses_as_an_action(self):
        fallback = self._fallback()
        from tdd.shared.mock_openai.scenarios import render_block

        for action in action_script(DEFAULT_TARGET_PATH, MODEL):
            parsed = fallback.parse_action(render_block(action))
            assert not isinstance(parsed, fallback.Malformed), (
                f"the harness parser rejected the mock's {action['tool']} block: "
                f"{getattr(parsed, 'reason', parsed)!r}"
            )
            assert parsed.name == action["tool"]
            assert parsed.args == action["args"]

    def test_the_script_uses_only_tools_the_harness_has(self):
        fallback = self._fallback()
        names = {a["tool"] for a in action_script(DEFAULT_TARGET_PATH, MODEL)}
        assert names <= set(fallback.TOOL_ORDER), (
            f"the mock scripts tools the harness does not implement: "
            f"{sorted(names - set(fallback.TOOL_ORDER))}"
        )

    def test_prose_turns_are_judged_malformed(self):
        """The `malformed` and `malformed_forever` scenarios are only useful if
        the harness genuinely cannot parse them."""
        fallback = self._fallback()
        parsed = fallback.parse_action(
            "Let me think about this task before I do anything."
        )
        assert isinstance(parsed, fallback.Malformed)
        assert parsed.reason == "no_block"

    def test_a_live_fallback_turn_parses(self, server):
        fallback = self._fallback()
        reply = _chat(server, "happy_text", _task())
        parsed = fallback.parse_action(reply["choices"][0]["message"]["content"])
        assert not isinstance(parsed, fallback.Malformed)
        assert parsed.name == "list_files"


class TestTheHarnessClientAgrees:
    """The tools path, checked the same way: the harness's REAL HTTP client
    (`runner_common.harness.client.OpenAICompatClient`) against the mock's REAL
    bytes, non-streaming AND streaming.

    Streaming matters separately because tool calls arrive as `delta.tool_calls`
    fragments rather than a finished `message.tool_calls`, and a mock that only
    got the non-streaming shape right would pass every other test here and then
    hand the harness an empty turn the moment `stream: true` was set.
    """

    def _client(self, server, scenario: str, model: str = MODEL):
        from runner_common.harness.client import OpenAICompatClient

        return OpenAICompatClient(
            server.base_url(scenario, host="127.0.0.1"), model, timeout=20
        )

    def _schemas(self):
        from runner_common.harness.tools import tool_schemas

        return tool_schemas()

    @pytest.mark.parametrize("stream", [False, True])
    def test_the_client_reads_the_mocks_tool_call(self, server, stream):
        response = self._client(server, "happy_tools").chat(
            _task(), tools=self._schemas(), stream=stream
        )
        assert len(response.tool_calls) == 1
        call = response.tool_calls[0]
        assert call.name == "list_files"
        # `arguments()` returns (parsed, error) - the error half is how the
        # harness reports a model that emitted unparseable JSON.
        parsed, error = call.arguments()
        assert error is None
        assert parsed == {"path": ".", "depth": 1, "max_entries": 50}

    @pytest.mark.parametrize("stream", [False, True])
    def test_the_client_reads_the_mocks_usage(self, server, stream):
        response = self._client(server, "happy_tools").chat(
            _task(), tools=self._schemas(), stream=stream
        )
        assert response.usage["prompt_tokens"] == MOCK_PROMPT_TOKENS_PER_TURN
        assert response.usage["completion_tokens"] == MOCK_COMPLETION_TOKENS_PER_TURN
        assert response.model == MODEL

    def test_the_no_usage_scenario_leaves_the_client_with_none(self, server):
        response = self._client(server, "no_usage").chat(
            _task(), tools=self._schemas()
        )
        assert not response.usage

    def test_the_clients_content_carries_the_fallback_block(self, server):
        response = self._client(server, "happy_text").chat(_task())
        assert not response.tool_calls
        assert "```lazyaf" in (response.content or "")

    def test_the_mocks_tool_names_are_the_harnesss_tool_names(self):
        """One table, two spellings. If the harness renames a tool and the
        mock does not follow, every scripted turn becomes `unknown_tool` in a
        container and nothing here would have said so."""
        from runner_common.harness.tools import tool_schemas

        harness_names = {s["function"]["name"] for s in tool_schemas()}
        scripted = {a["tool"] for a in action_script(DEFAULT_TARGET_PATH, MODEL)}
        assert scripted <= harness_names
