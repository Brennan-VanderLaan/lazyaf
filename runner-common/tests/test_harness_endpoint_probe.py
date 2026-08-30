"""
The runner-local capability probe (Milestone 14.1, design section 2.3).

A ``reach=runner-local`` endpoint is unreachable from the backend BY
DEFINITION, so probing it uses the machinery that already reaches that host: a
one-step pipeline run pinned by ``requires: {has: ["endpoint:<name>"]}``. This
module is what that step runs, and it shares ``harness.client`` with the
harness — one client, one bug surface.

The decision table below is a deliberate TWIN of the backend's pure judges
(``app.services.model_endpoints.probe``). It cannot import them: a step
container has no ``backend/app`` and no ``httpx``. The reason vocabulary is
pinned on both sides so a divergence shows up in ``probe_detail`` rather than
in nobody's output.
"""
import json

import pytest

from runner_common import endpoint_probe as probe
from runner_common.harness.client import OpenAICompatClient
from tests.fixtures.openai import FakeResponse, FakeSession, endpoint_block


def models_payload(model="qwen2.5-coder:32b", max_model_len=None):
    entry = {"id": model, "object": "model"}
    if max_model_len:
        entry["max_model_len"] = max_model_len
    return {"object": "list", "data": [entry]}


def tools_ok_payload(usage=True):
    payload = {
        "model": "qwen2.5-coder:32b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "probe",
                                "arguments": json.dumps({"value": 7}),
                            },
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 31, "completion_tokens": 9}
    return payload


def stream_lines(with_delta=True):
    lines = []
    if with_delta:
        lines.append('data: {"choices":[{"index":0,"delta":{"content":"h"}}]}')
    lines.append("data: [DONE]")
    return lines


def run(endpoint=None, *, get=None, posts=None):
    endpoint = endpoint or endpoint_block(server_kind="vllm")
    session = FakeSession(script=posts or [], get_script=get or [])
    client = OpenAICompatClient(
        base_url=endpoint["base_url"], model=endpoint["model"], session=session
    )
    return probe.run_probe(endpoint, client=client), session


# --------------------------------------------------------------------------
# request 1
# --------------------------------------------------------------------------

class TestLiveness:
    def test_a_transport_failure_is_unreachable_and_stops_the_probe(self):
        class Dead(FakeSession):
            def get(self, *args, **kwargs):
                raise OSError("no route to host")

        endpoint = endpoint_block()
        client = OpenAICompatClient(
            base_url=endpoint["base_url"], model=endpoint["model"], session=Dead()
        )
        result = probe.run_probe(endpoint, client=client)
        assert result["reachable"] is False
        assert result["probe_status"] == "unreachable"
        assert "OSError" in result["error"]
        assert result["detail"]["unreachable_reason"]
        # Requests 2-4 against a box that is not there would triple the
        # operator's wait for the same answer.
        assert result["supports_tools"] is None

    def test_a_404_on_models_is_not_a_failure(self):
        """Some brokers simply do not implement the listing."""
        result, _ = run(
            get=[FakeResponse(404, text="nope")],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["reachable"] is True
        assert result["model_listed"] is None
        assert result["detail"]["models_reason"] == "models_not_implemented"

    def test_a_missing_model_is_recorded_and_degrades(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload(model="something-else"))],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["model_listed"] is False
        assert result["detail"]["models_reason"] == "model_not_listed"
        assert result["probe_status"] == "degraded"


# --------------------------------------------------------------------------
# request 2 — the decision table, one row per outcome
# --------------------------------------------------------------------------

class TestToolJudgement:
    def test_a_real_tool_call_is_the_only_thing_that_counts(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["supports_tools"] is True
        assert result["probe_status"] == "ok"

    @pytest.mark.parametrize(
        "status,expected",
        [(400, "http_400"), (500, "http_5xx"), (503, "http_5xx"), (403, "http_4xx")],
    )
    def test_http_failures_are_recorded_by_class(self, status, expected):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(status, text="no"), FakeResponse(200, lines=stream_lines())],
        )
        assert result["supports_tools"] is False
        assert result["detail"]["tools_reason"] == expected

    def test_a_200_with_prose_and_no_tool_calls_is_no_tool_calls(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "sure!"}}]}
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, payload), FakeResponse(200, lines=stream_lines())],
        )
        assert result["supports_tools"] is False
        assert result["detail"]["tools_reason"] == "no_tool_calls"

    def test_the_wrong_tool_name_is_wrong_tool(self):
        payload = tools_ok_payload()
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "echo"
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, payload), FakeResponse(200, lines=stream_lines())],
        )
        assert result["detail"]["tools_reason"] == "wrong_tool"

    def test_arguments_that_are_not_json_are_bad_arguments_json(self):
        payload = tools_ok_payload()
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "7"
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, payload), FakeResponse(200, lines=stream_lines())],
        )
        assert result["detail"]["tools_reason"] == "bad_arguments_json"

    def test_the_probe_asks_with_tool_choice_auto(self):
        _, session = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert session.bodies[0]["tool_choice"] == "auto"
        assert session.bodies[0]["max_tokens"] == 64


# --------------------------------------------------------------------------
# request 3 and usage
# --------------------------------------------------------------------------

class TestStreamingAndUsage:
    def test_one_delta_frame_before_done_is_enough(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["supports_streaming"] is True

    def test_no_delta_frames_is_recorded(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[
                FakeResponse(200, tools_ok_payload()),
                FakeResponse(200, lines=stream_lines(with_delta=False)),
            ],
        )
        assert result["supports_streaming"] is False
        assert result["detail"]["stream_reason"] == "no_delta_frames"

    def test_reports_usage_decides_whether_any_cost_number_is_possible(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[
                FakeResponse(200, tools_ok_payload(usage=False)),
                FakeResponse(200, lines=stream_lines()),
            ],
        )
        assert result["reports_usage"] is False
        assert result["detail"]["usage_reason"] == "no_usage_block"
        assert result["probe_status"] == "degraded"

    def test_the_stream_request_asks_for_usage(self):
        _, session = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert session.bodies[1]["stream_options"] == {"include_usage": True}
        assert "tools" not in session.bodies[1]


# --------------------------------------------------------------------------
# context-window discovery
# --------------------------------------------------------------------------

class TestContextWindowDiscovery:
    def test_the_operator_override_beats_everything(self):
        endpoint = endpoint_block(server_kind="vllm", context_window=12345)
        result, _ = run(
            endpoint,
            get=[FakeResponse(200, models_payload(max_model_len=8192))],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["context_window"] == 12345
        assert result["context_window_source"] == "override"

    def test_vllm_max_model_len_is_harvested_from_the_listing(self):
        result, _ = run(
            endpoint_block(server_kind="vllm"),
            get=[FakeResponse(200, models_payload(max_model_len=8192))],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["context_window"] == 8192
        assert result["context_window_source"] == "max_model_len"

    def test_ollama_api_show_is_attempted_for_exactly_one_server_kind(self):
        show = FakeResponse(
            200, {"model_info": {"qwen2.context_length": 32768, "general.x": 1}}
        )
        result, session = run(
            endpoint_block(server_kind="ollama"),
            get=[FakeResponse(200, models_payload())],
            posts=[
                FakeResponse(200, tools_ok_payload()),
                FakeResponse(200, lines=stream_lines()),
                show,
            ],
        )
        assert result["context_window"] == 32768
        assert result["context_window_source"] == "ollama"
        assert session.requests[2]["url"].endswith("/api/show")
        assert "/v1/" not in session.requests[2]["url"]

    def test_a_non_ollama_endpoint_never_calls_the_vendor_extension(self):
        _, session = run(
            endpoint_block(server_kind="lmstudio"),
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert len(session.requests) == 2

    def test_no_discovery_at_all_leaves_it_null(self):
        result, _ = run(
            endpoint_block(server_kind="other"),
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert result["context_window"] is None
        assert result["context_window_source"] is None


# --------------------------------------------------------------------------
# status computation and the wire shape
# --------------------------------------------------------------------------

class TestProbeStatus:
    @pytest.mark.parametrize(
        "reachable,tools,usage,listed,expected",
        [
            (False, None, None, None, "unreachable"),
            (True, True, True, True, "ok"),
            (True, True, True, None, "ok"),
            (True, False, True, True, "degraded"),
            (True, True, False, True, "degraded"),
            (True, True, True, False, "degraded"),
        ],
    )
    def test_the_status_table(self, reachable, tools, usage, listed, expected):
        assert (
            probe.compute_probe_status(reachable, tools, True, usage, listed) == expected
        )

    def test_the_result_carries_exactly_the_probe_result_wire_keys(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload())],
            posts=[FakeResponse(200, tools_ok_payload()), FakeResponse(200, lines=stream_lines())],
        )
        assert set(result) == {
            "reachable",
            "probe_status",
            "model_listed",
            "supports_tools",
            "supports_streaming",
            "reports_usage",
            "context_window",
            "context_window_source",
            "detail",
            "error",
            "elapsed_ms",
        }
        assert isinstance(result["elapsed_ms"], int)

    def test_every_recorded_reason_is_in_the_pinned_vocabulary(self):
        result, _ = run(
            get=[FakeResponse(200, models_payload(model="other"))],
            posts=[
                FakeResponse(400, text="no tools here"),
                FakeResponse(200, lines=stream_lines(with_delta=False)),
            ],
        )
        for key in ("models_reason", "tools_reason", "stream_reason", "usage_reason"):
            reason = result["detail"].get(key)
            if reason:
                assert reason in probe.PROBE_REASONS, f"{key}={reason} is not pinned"


# --------------------------------------------------------------------------
# the entrypoint
# --------------------------------------------------------------------------

class TestEntrypoint:
    def test_it_refuses_without_the_endpoint_id_rather_than_guessing(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv(probe.PROBE_ENDPOINT_ID_ENV, raising=False)
        assert probe.main() == 1
        assert probe.PROBE_ENDPOINT_ID_ENV in capsys.readouterr().err

    def test_an_inline_endpoint_spec_avoids_a_backend_round_trip(self, monkeypatch):
        monkeypatch.setenv(
            probe.PROBE_ENDPOINT_JSON_ENV, json.dumps(endpoint_block())
        )
        loaded, reason = probe._load_endpoint("e1", "", None)
        assert reason is None
        assert loaded["name"] == "local-4090"

    def test_a_broken_inline_spec_says_why(self, monkeypatch):
        monkeypatch.setenv(probe.PROBE_ENDPOINT_JSON_ENV, "{not json")
        loaded, reason = probe._load_endpoint("e1", "", None)
        assert loaded is None
        assert "not valid JSON" in reason

    def test_with_no_backend_url_and_no_inline_spec_it_says_so(self, monkeypatch):
        monkeypatch.delenv(probe.PROBE_ENDPOINT_JSON_ENV, raising=False)
        loaded, reason = probe._load_endpoint("e1", "", None)
        assert loaded is None
        assert "no way to learn what to probe" in reason
