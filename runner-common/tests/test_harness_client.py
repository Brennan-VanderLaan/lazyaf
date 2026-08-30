"""
The OpenAI-compatible client the harness and the runner-local probe SHARE
(Milestone 14.2, design section 2.3: one client, one bug surface).

The retry policy is the part worth pinning, because it decides how a cold
ollama loading a 32B model is treated (a retry, not a dead step) versus how a
wrong model id is treated (fatal on the first response, not four times the
operator's wall clock for the same message).
"""
import json

import pytest

from runner_common.harness.client import (
    ChatResponse,
    EndpointFatal,
    OpenAICompatClient,
    ToolCall,
    ToolsRejected,
    normalize_base_url,
)
from runner_common.harness.constants import (
    ENDPOINT_RETRY_MAX_SECONDS,
    MAX_ENDPOINT_RETRIES,
)
from tests.fixtures.openai import (
    FakeResponse,
    FakeSession,
    chat_payload,
    chat_response,
    sse_response,
    tool_call,
)


def client(session, **kwargs):
    slept = kwargs.pop("slept", None)
    return OpenAICompatClient(
        base_url=kwargs.pop("base_url", "http://gpu.lan:11434/v1"),
        model=kwargs.pop("model", "qwen2.5-coder:32b"),
        session=session,
        sleep=(slept.append if slept is not None else (lambda seconds: None)),
        rand=lambda: 1.0,
        **kwargs,
    )


# --------------------------------------------------------------------------
# URLs and bodies
# --------------------------------------------------------------------------

class TestRequestShape:
    def test_the_base_url_is_normalized_but_never_rewritten(self):
        assert normalize_base_url("http://x:11434/v1/") == "http://x:11434/v1"
        # A URL that does not end in /v1 is accepted AS WRITTEN: silently
        # appending it to a broker that does not use it produces a 404 the
        # operator cannot explain.
        assert normalize_base_url("http://x:8000/openai") == "http://x:8000/openai"

    def test_the_chat_url_is_the_base_plus_chat_completions(self):
        session = FakeSession([chat_response(content="hi")])
        client(session).chat([{"role": "user", "content": "hi"}])
        assert session.requests[0]["url"] == (
            "http://gpu.lan:11434/v1/chat/completions"
        )

    def test_determinism_parameters_travel_when_set(self):
        session = FakeSession([chat_response(content="hi")])
        client(session, temperature=0.3, top_p=0.9, seed=11, max_output_tokens=512).chat(
            [{"role": "user", "content": "hi"}]
        )
        body = session.bodies[0]
        assert body["temperature"] == 0.3
        assert body["top_p"] == 0.9
        assert body["seed"] == 11
        assert body["max_tokens"] == 512

    def test_tools_travel_with_tool_choice_auto_never_required(self):
        session = FakeSession([chat_response(content="hi")])
        client(session).chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "probe"}}],
        )
        body = session.bodies[0]
        assert body["tool_choice"] == "auto", (
            "several servers accept `required` and emit prose anyway; trusting "
            "the parameter tests the ADVERTISING, not the behaviour"
        )

    def test_streaming_asks_for_usage_on_the_final_frame(self):
        session = FakeSession([sse_response(["hi"], usage={"prompt_tokens": 3})])
        client(session).chat([{"role": "user", "content": "hi"}], stream=True)
        assert session.bodies[0]["stream_options"] == {"include_usage": True}
        assert session.requests[0]["stream"] is True


# --------------------------------------------------------------------------
# the retry policy
# --------------------------------------------------------------------------

class TestRetryPolicy:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_statuses_are_retried_then_succeed(self, status):
        session = FakeSession(
            [FakeResponse(status, text="busy"), chat_response(content="ok")]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.content == "ok"
        assert len(session.requests) == 2
        assert response.retries == 1

    def test_the_retry_budget_is_finite_and_then_fatal(self):
        session = FakeSession([FakeResponse(503, text="busy")])
        with pytest.raises(EndpointFatal) as caught:
            client(session).chat([{"role": "user", "content": "hi"}])
        assert len(session.requests) == MAX_ENDPOINT_RETRIES + 1
        assert "503" in caught.value.reason
        assert f"after {MAX_ENDPOINT_RETRIES + 1} attempts" in caught.value.reason

    def test_a_transport_error_is_retried_too_because_a_cold_model_is_slow(self):
        class Flaky(FakeSession):
            def post(self, *args, **kwargs):
                if not self.requests:
                    self.requests.append({"url": "", "headers": {}, "body": None, "stream": False})
                    raise OSError("connection reset")
                return super().post(*args, **kwargs)

        session = Flaky([chat_response(content="warm now")])
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.content == "warm now"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_any_other_4xx_is_fatal_on_the_first_response(self, status):
        session = FakeSession([FakeResponse(status, text="nope")])
        with pytest.raises(EndpointFatal):
            client(session).chat([{"role": "user", "content": "hi"}])
        assert len(session.requests) == 1

    def test_the_backoff_is_full_jitter_and_capped(self):
        slept = []
        session = FakeSession([FakeResponse(503, text="busy")])
        with pytest.raises(EndpointFatal):
            client(session, slept=slept).chat([{"role": "user", "content": "hi"}])
        assert slept == [1.5, 3.0, 6.0]
        assert all(delay <= ENDPOINT_RETRY_MAX_SECONDS for delay in slept)

    def test_http_errors_are_counted_for_the_usage_record(self):
        session = FakeSession(
            [FakeResponse(503, text="busy"), FakeResponse(503, text="busy"),
             chat_response(content="ok")]
        )
        instance = client(session)
        instance.chat([{"role": "user", "content": "hi"}])
        assert instance.http_errors == 2


# --------------------------------------------------------------------------
# the tools demotion signal
# --------------------------------------------------------------------------

class TestToolsRejected:
    @pytest.mark.parametrize(
        "body",
        [
            '{"error": "this model does not support tools"}',
            '{"error": {"message": "function calling is not enabled"}}',
            "Tool use unsupported for this template",
        ],
    )
    def test_a_400_mentioning_tools_or_functions_is_a_demotion_not_a_death(self, body):
        session = FakeSession([FakeResponse(400, text=body)])
        with pytest.raises(ToolsRejected):
            client(session).chat([{"role": "user", "content": "hi"}])

    def test_an_unrelated_400_stays_fatal(self):
        session = FakeSession([FakeResponse(400, text='{"error": "bad model id"}')])
        with pytest.raises(EndpointFatal):
            client(session).chat([{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

class TestParsing:
    def test_a_plain_completion(self):
        session = FakeSession(
            [chat_response(content="hello", usage={"prompt_tokens": 9, "completion_tokens": 2})]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.content == "hello"
        assert response.prompt_tokens == 9
        assert response.completion_tokens == 2
        assert response.reports_usage is True
        assert response.model == "test-model"

    def test_a_missing_usage_block_is_an_absence_not_a_zero(self):
        session = FakeSession([chat_response(content="hello")])
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.usage is None
        assert response.prompt_tokens is None
        assert response.reports_usage is False

    def test_cached_tokens_are_read_from_prompt_tokens_details(self):
        session = FakeSession(
            [
                chat_response(
                    content="x",
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 4},
                    },
                )
            ]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.cached_tokens == 4

    def test_tool_calls_are_normalized_from_the_string_arguments_form(self):
        session = FakeSession(
            [chat_response(tool_calls=[tool_call("read_file", {"path": "a.py"})])]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert len(response.tool_calls) == 1
        args, error = response.tool_calls[0].arguments()
        assert error is None
        assert args == {"path": "a.py"}

    def test_tool_calls_are_normalized_from_the_object_arguments_form(self):
        """Some servers hand back a dict where the spec says a JSON string."""
        payload = chat_payload()
        payload["choices"][0]["message"]["tool_calls"] = [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": {"path": "a.py"}}}
        ]
        session = FakeSession([FakeResponse(200, payload)])
        response = client(session).chat([{"role": "user", "content": "hi"}])
        args, error = response.tool_calls[0].arguments()
        assert (args, error) == ({"path": "a.py"}, None)

    def test_unparseable_tool_arguments_are_reported_not_raised(self):
        call = ToolCall(name="read_file", arguments_raw="{not json")
        args, error = call.arguments()
        assert args is None
        assert "not valid JSON" in error

    def test_content_blocks_are_flattened(self):
        payload = chat_payload()
        payload["choices"][0]["message"]["content"] = [
            {"type": "text", "text": "part one "},
            {"type": "text", "text": "part two"},
        ]
        session = FakeSession([FakeResponse(200, payload)])
        response = client(session).chat([{"role": "user", "content": "hi"}])
        assert response.content == "part one part two"

    def test_a_non_json_body_is_a_readable_endpoint_failure(self):
        session = FakeSession([FakeResponse(200, text="<html>proxy error</html>")])
        with pytest.raises(EndpointFatal) as caught:
            client(session).chat([{"role": "user", "content": "hi"}])
        assert "not JSON" in caught.value.reason


class TestStreamParsing:
    def test_content_deltas_are_concatenated(self):
        session = FakeSession([sse_response(["Hello", ", ", "world"])])
        response = client(session).chat([{"role": "user", "content": "hi"}], stream=True)
        assert response.content == "Hello, world"
        assert response.streamed is True

    def test_fragmented_tool_calls_are_merged_by_index(self):
        session = FakeSession(
            [
                sse_response(
                    ["thinking "],
                    tool_calls=[tool_call("write_file", {"path": "a.py", "content": "x"})],
                    usage={"prompt_tokens": 12, "completion_tokens": 3},
                )
            ]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}], stream=True)
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "write_file"
        args, error = response.tool_calls[0].arguments()
        assert (args, error) == ({"path": "a.py", "content": "x"}, None)
        assert response.prompt_tokens == 12

    def test_usage_on_the_final_frame_is_captured(self):
        session = FakeSession(
            [sse_response(["hi"], usage={"prompt_tokens": 7, "completion_tokens": 1})]
        )
        response = client(session).chat([{"role": "user", "content": "hi"}], stream=True)
        assert response.usage == {"prompt_tokens": 7, "completion_tokens": 1}

    def test_bytes_lines_and_junk_frames_are_tolerated(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"a"}}]}',
            b"",
            b": keepalive",
            b"data: not json at all",
            b'data: {"choices":[{"delta":{"content":"b"}}]}',
            b"data: [DONE]",
        ]
        session = FakeSession([FakeResponse(200, lines=lines)])
        response = client(session).chat([{"role": "user", "content": "hi"}], stream=True)
        assert response.content == "ab"


# --------------------------------------------------------------------------
# get_json, used by the probe
# --------------------------------------------------------------------------

class TestGetJson:
    def test_it_returns_status_and_payload(self):
        session = FakeSession(get_script=[FakeResponse(200, {"data": [{"id": "m"}]})])
        status, payload, error = client(session).get_json("models")
        assert (status, error) == (200, None)
        assert payload == {"data": [{"id": "m"}]}
        assert session.gets[0]["url"] == "http://gpu.lan:11434/v1/models"

    def test_a_transport_failure_is_data_not_an_exception(self):
        class Dead(FakeSession):
            def get(self, *args, **kwargs):
                raise OSError("no route to host")

        status, payload, error = client(Dead()).get_json("models")
        assert status is None and payload is None
        assert "OSError" in error
