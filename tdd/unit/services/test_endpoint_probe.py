"""Unit tests for the capability probe (M14, wave8 section 2).

One test per row of the section 2.2 decision table, driven against a STUB
transport so the whole probe - four requests, five judgements, the status
computation and the way an observation lands on a row - runs with no sockets
and no Docker.

The properties these tests exist to protect:

- `supports_tools` is judged on the RESPONSE SHAPE, never on the server
  accepting the `tools` parameter. A probe that trusts the parameter is
  testing the server's advertising.
- **An unreachable endpoint leaves the previous capability booleans intact.**
  Nulling a good record because the box was rebooting is strictly worse than
  carrying a stale, timestamped one.
- The secret never reaches `probe_detail`, `last_error`, or a log line.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.model_endpoint import ModelEndpoint  # noqa: E402
from app.services.model_endpoints.probe import (  # noqa: E402
    PROBE_MIN_INTERVAL_SECONDS,
    PROBE_TOOL_NAME,
    ProbeHTTP,
    ProbeResult,
    ProbeSpec,
    ProbeTransportError,
    apply_probe_result,
    compute_probe_status,
    judge_models,
    judge_ollama_context,
    judge_streaming,
    judge_tools,
    judge_usage,
    pick_context_window,
    probe_endpoint,
    run_probe,
    spec_for_endpoint,
    stream_probe_body,
    tool_probe_body,
)
from app.services.model_endpoints.secrets import (  # noqa: E402
    ENDPOINT_SECRET_PREFIX,
    ENDPOINT_SECRET_REF_RE,
    HARNESS_API_KEY_ENV,
    EndpointSecretMissing,
    auth_headers,
    endpoint_secret_value,
    is_valid_secret_ref,
    scrub_secrets,
    secret_present,
)

MODEL = "qwen2.5-coder:32b"
BASE = "http://192.168.1.50:11434/v1"


# -----------------------------------------------------------------------------
# Canned upstream bodies
# -----------------------------------------------------------------------------

def http(status=200, payload=None, text=None, lines=None) -> ProbeHTTP:
    body = text if text is not None else (json.dumps(payload) if payload is not None else "")
    return ProbeHTTP(status=status, text=body, payload=payload, lines=list(lines or []))


def models_ok(max_model_len=None) -> ProbeHTTP:
    entry = {"id": MODEL, "object": "model"}
    if max_model_len is not None:
        entry["max_model_len"] = max_model_len
    return http(200, {"object": "list", "data": [entry, {"id": "llama3.1:8b"}]})


def tools_ok(usage=True, arguments='{"value": 7}') -> ProbeHTTP:
    payload = {
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": PROBE_TOOL_NAME, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 61, "completion_tokens": 12}
    return http(200, payload)


def tools_prose(usage=True) -> ProbeHTTP:
    payload = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "Sure! Here you go."}}
        ]
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 61, "completion_tokens": 12}
    return http(200, payload)


def stream_ok(usage=True) -> ProbeHTTP:
    frames = [
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": "hi"}}]}',
    ]
    if usage:
        frames.append(
            'data: {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 3}}'
        )
    frames.append("data: [DONE]")
    return ProbeHTTP(status=200, lines=frames)


def ollama_show(context=32768) -> ProbeHTTP:
    return http(
        200,
        {
            "model_info": {
                "general.architecture": "qwen2",
                "qwen2.context_length": context,
                "qwen2.embedding_length": 5120,
            }
        },
    )


class StubTransport:
    """Routes by URL suffix; a value that is an Exception is RAISED."""

    def __init__(self, models=None, tools=None, stream=None, show=None):
        self.models = models if models is not None else models_ok()
        self.tools = tools if tools is not None else tools_ok()
        self.stream = stream if stream is not None else stream_ok()
        self.show = show if show is not None else ollama_show()
        self.calls: list[tuple] = []

    async def request(
        self, method, url, *, json_body=None, timeout=None, stream=False, max_lines=64
    ):
        self.calls.append((method, url, stream, json_body))
        if url.endswith("/models"):
            value = self.models
        elif url.endswith("/api/show"):
            value = self.show
        elif stream:
            value = self.stream
        else:
            value = self.tools
        if isinstance(value, Exception):
            raise value
        return value


class FakeDB:
    """Enough AsyncSession for `probe_endpoint`; the row is mutated in place
    and the persistence half is covered by the API integration tests."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


def make_endpoint(**overrides) -> ModelEndpoint:
    values = dict(
        id="e1",
        name="local-4090",
        base_url=BASE,
        model=MODEL,
        server_kind="ollama",
        auth_style="none",
        reach="direct",
        gpu_node_id="endpoint:local-4090",
        max_concurrency=1,
        request_timeout_seconds=300,
        probe_status="unprobed",
        probe_detail="{}",
        consecutive_failures=0,
        enabled=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    values.update(overrides)
    return ModelEndpoint(**values)


# -----------------------------------------------------------------------------
# The request bodies
# -----------------------------------------------------------------------------

class TestRequestBodies:
    def test_tool_probe_uses_auto_not_required(self):
        """Several servers accept `required` and emit prose anyway; trusting
        the parameter tests the ADVERTISING, not the behavior."""
        body = tool_probe_body(MODEL)
        assert body["tool_choice"] == "auto"
        assert body["tools"][0]["function"]["name"] == PROBE_TOOL_NAME
        assert body["temperature"] == 0
        assert body["stream"] is False

    def test_stream_probe_asks_for_usage_on_the_final_frame(self):
        body = stream_probe_body(MODEL)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert "tools" not in body


# -----------------------------------------------------------------------------
# The decision table (pure judgements)
# -----------------------------------------------------------------------------

class TestJudgements:
    def test_models_lists_the_model(self):
        listed, max_len, reason = judge_models(models_ok(), MODEL)
        assert (listed, max_len, reason) == (True, None, None)

    def test_models_harvests_vllm_max_model_len(self):
        listed, max_len, _ = judge_models(models_ok(max_model_len=32768), MODEL)
        assert (listed, max_len) == (True, 32768)

    def test_models_404_is_not_a_failure(self):
        """Some brokers simply do not implement the listing."""
        listed, _, reason = judge_models(http(404, text="nope"), MODEL)
        assert listed is None
        assert reason == "models_not_implemented"

    def test_model_absent_from_the_listing(self):
        listed, _, reason = judge_models(
            http(200, {"data": [{"id": "something-else"}]}), MODEL
        )
        assert listed is False
        assert reason == "model_not_listed"

    @pytest.mark.parametrize(
        "response,expected_reason",
        [
            (http(400, text='{"error":"tools not supported"}'), "http_400"),
            (http(403, text="denied"), "http_4xx"),
            (http(503, text="overloaded"), "http_5xx"),
            (tools_prose(), "no_tool_calls"),
            (
                http(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {"function": {"name": "not_probe", "arguments": "{}"}}
                                    ]
                                }
                            }
                        ]
                    },
                ),
                "wrong_tool",
            ),
            (tools_ok(arguments="value=7, definitely not json"), "bad_arguments_json"),
            (http(200, {"nothing": "useful"}), "bad_response_shape"),
        ],
    )
    def test_tools_failure_reasons(self, response, expected_reason):
        supports, reason = judge_tools(response)
        assert supports is False
        assert reason == expected_reason

    def test_tools_success_requires_the_right_name_and_json_arguments(self):
        assert judge_tools(tools_ok()) == (True, None)

    def test_tools_timeout(self):
        assert judge_tools(None, "timeout") == (False, "timeout")

    def test_streaming_ok(self):
        assert judge_streaming(stream_ok()) == (True, None)

    def test_streaming_unsupported(self):
        supports, reason = judge_streaming(
            ProbeHTTP(status=200, lines=["data: [DONE]"])
        )
        assert supports is False
        assert reason == "no_delta_frames"

    def test_streaming_http_error(self):
        supports, reason = judge_streaming(http(400, text="no streaming"))
        assert supports is False
        assert reason == "http_400"

    def test_usage_present_and_absent(self):
        assert judge_usage([tools_ok().payload]) == (True, None)
        supports, reason = judge_usage([tools_ok(usage=False).payload, None])
        assert supports is False
        assert reason == "no_usage_block"

    def test_ollama_context_scans_for_the_family_prefixed_key(self):
        assert judge_ollama_context(ollama_show(32768)) == 32768
        assert judge_ollama_context(http(404, text="")) is None
        assert judge_ollama_context(None) is None

    @pytest.mark.parametrize(
        "override,ollama,vllm,expected",
        [
            (4096, 32768, 16384, (4096, "override")),
            (None, 32768, 16384, (32768, "ollama")),
            (None, None, 16384, (16384, "max_model_len")),
            (None, None, None, (None, None)),
        ],
    )
    def test_context_precedence(self, override, ollama, vllm, expected):
        assert pick_context_window(override, ollama, vllm) == expected

    @pytest.mark.parametrize(
        "reachable,tools,stream,usage,listed,expected",
        [
            (False, None, None, None, None, "unreachable"),
            (True, True, True, True, True, "ok"),
            (True, True, True, True, None, "ok"),
            (True, False, True, True, True, "degraded"),
            (True, True, True, False, True, "degraded"),
            (True, True, True, True, False, "degraded"),
        ],
    )
    def test_probe_status_table(self, reachable, tools, stream, usage, listed, expected):
        assert compute_probe_status(reachable, tools, stream, usage, listed) == expected


# -----------------------------------------------------------------------------
# run_probe end to end against the stub
# -----------------------------------------------------------------------------

def spec(server_kind="ollama", secret=None) -> ProbeSpec:
    return ProbeSpec(
        base_url=BASE,
        model=MODEL,
        server_kind=server_kind,
        secret_values=(secret,) if secret else (),
    )


class TestRunProbe:
    async def test_happy_path_ollama(self):
        transport = StubTransport()
        result = await run_probe(spec(), transport)

        assert result.reachable is True
        assert result.probe_status == "ok"
        assert result.supports_tools is True
        assert result.supports_streaming is True
        assert result.reports_usage is True
        assert result.context_window == 32768
        assert result.context_window_source == "ollama"
        assert result.model_listed is True

    async def test_vllm_discovers_max_model_len_and_skips_api_show(self):
        transport = StubTransport(models=models_ok(max_model_len=16384))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.context_window == 16384
        assert result.context_window_source == "max_model_len"
        assert not any("/api/show" in call[1] for call in transport.calls), (
            "the ollama vendor extension is attempted for exactly one "
            "server_kind, not guessed everywhere"
        )

    async def test_unreachable_stops_after_request_one(self):
        """Requests 2-4 against a box that is not there are pointless and
        would triple the operator's wait for the same answer."""
        transport = StubTransport(models=ProbeTransportError("Connection refused"))
        result = await run_probe(spec(), transport)

        assert result.reachable is False
        assert result.probe_status == "unreachable"
        assert "Connection refused" in result.error
        assert len(transport.calls) == 1

    async def test_tools_unsupported_is_degraded_but_usable(self):
        transport = StubTransport(tools=http(400, text='{"error":"no tool support"}'))
        result = await run_probe(spec(), transport)

        assert result.probe_status == "degraded"
        assert result.supports_tools is False
        assert result.detail["tools_reason"] == "http_400"
        # Still streams, still reports usage - the fallback protocol works.
        assert result.supports_streaming is True
        assert result.reports_usage is True

    async def test_no_usage_anywhere_is_degraded(self):
        transport = StubTransport(
            tools=tools_ok(usage=False), stream=stream_ok(usage=False)
        )
        result = await run_probe(spec(), transport)

        assert result.reports_usage is False
        assert result.probe_status == "degraded"
        assert result.detail["usage_reason"] == "no_usage_block"

    async def test_usage_harvested_from_the_streaming_frame_alone(self):
        transport = StubTransport(tools=tools_ok(usage=False), stream=stream_ok(usage=True))
        result = await run_probe(spec(), transport)
        assert result.reports_usage is True

    async def test_a_transport_failure_on_request_two_is_not_unreachable(self):
        """An endpoint that answers /models is REACHABLE; a later timeout is
        a capability observation, not a liveness one."""
        transport = StubTransport(tools=ProbeTransportError("read timeout"))
        result = await run_probe(spec(), transport)

        assert result.reachable is True
        assert result.supports_tools is False
        assert result.detail["tools_reason"] == "timeout"

    async def test_never_raises_on_a_broken_transport(self):
        class Exploding:
            async def request(self, *a, **k):
                raise RuntimeError("boom")

        result = await run_probe(spec(), Exploding())
        assert result.reachable is False
        assert "boom" in result.error

    async def test_the_secret_never_reaches_the_recorded_detail(self):
        sentinel = "sk-probe-sentinel-value-123456"
        transport = StubTransport(
            tools=http(401, text=f'{{"error":"bad key {sentinel}"}}')
        )
        result = await run_probe(spec(secret=sentinel), transport)

        blob = json.dumps(result.to_dict())
        assert sentinel not in blob
        assert "***" in blob


# -----------------------------------------------------------------------------
# Applying an observation to a row
# -----------------------------------------------------------------------------

class TestApplyProbeResult:
    async def test_success_populates_and_zeroes_failures(self):
        endpoint = make_endpoint(consecutive_failures=2)
        result = await run_probe(spec(), StubTransport())

        apply_probe_result(endpoint, result)

        assert endpoint.probe_status == "ok"
        assert endpoint.supports_tools is True
        assert endpoint.consecutive_failures == 0
        assert endpoint.last_error is None
        assert endpoint.probed_from == "backend"
        assert endpoint.effective_context_window == 32768

    async def test_unreachable_leaves_the_previous_capabilities_intact(self):
        """THE rule: a rebooting box must not erase what we know about it."""
        endpoint = make_endpoint()
        apply_probe_result(endpoint, await run_probe(spec(), StubTransport()))
        assert endpoint.supports_tools is True

        down = StubTransport(models=ProbeTransportError("Connection refused"))
        apply_probe_result(endpoint, await run_probe(spec(), down))

        assert endpoint.probe_status == "unreachable"
        assert endpoint.consecutive_failures == 1
        assert "Connection refused" in endpoint.last_error
        # UNCHANGED:
        assert endpoint.supports_tools is True
        assert endpoint.supports_streaming is True
        assert endpoint.reports_usage is True
        assert endpoint.effective_context_window == 32768

    async def test_failures_accumulate_across_consecutive_outages(self):
        endpoint = make_endpoint()
        down = StubTransport(models=ProbeTransportError("no route to host"))
        for expected in (1, 2, 3):
            apply_probe_result(endpoint, await run_probe(spec(), down))
            assert endpoint.consecutive_failures == expected
        assert endpoint.health == "unhealthy"

    async def test_a_never_probed_endpoint_that_is_down_keeps_supports_tools_none(self):
        """The correct outcome: we do not know how to drive it, and dispatch
        refuses on exactly that."""
        endpoint = make_endpoint()
        down = StubTransport(models=ProbeTransportError("refused"))
        apply_probe_result(endpoint, await run_probe(spec(), down))
        assert endpoint.supports_tools is None

    async def test_error_text_is_scrubbed_and_capped(self):
        endpoint = make_endpoint()
        result = ProbeResult(
            reachable=False,
            probe_status="unreachable",
            error="Bearer hunter2-hunter2 " + "x" * 900,
        )
        apply_probe_result(endpoint, result)
        assert len(endpoint.last_error) <= 512
        assert "hunter2" not in endpoint.last_error


# -----------------------------------------------------------------------------
# Rate limiting
# -----------------------------------------------------------------------------

class TestProbeRateLimit:
    async def test_second_probe_inside_the_floor_is_cached(self):
        endpoint = make_endpoint()
        db = FakeDB()
        transport = StubTransport()

        probed, _ = await probe_endpoint(db, endpoint, transport=transport)
        assert probed is True
        first_calls = len(transport.calls)

        probed_again, detail = await probe_endpoint(db, endpoint, transport=transport)

        assert probed_again is False
        assert str(PROBE_MIN_INTERVAL_SECONDS) in detail
        assert len(transport.calls) == first_calls, "no second upstream call"

    async def test_force_bypasses_the_floor(self):
        endpoint = make_endpoint()
        db = FakeDB()
        transport = StubTransport()

        await probe_endpoint(db, endpoint, transport=transport)
        calls = len(transport.calls)
        probed, _ = await probe_endpoint(db, endpoint, force=True, transport=transport)

        assert probed is True
        assert len(transport.calls) > calls

    async def test_an_old_record_is_re_probed_without_force(self):
        endpoint = make_endpoint(
            probed_at=datetime.utcnow() - timedelta(seconds=PROBE_MIN_INTERVAL_SECONDS + 5)
        )
        db = FakeDB()
        probed, _ = await probe_endpoint(db, endpoint, transport=StubTransport())
        assert probed is True

    async def test_concurrent_probes_do_not_double_call_upstream(self):
        endpoint = make_endpoint(id="race-1")
        db = FakeDB()
        transport = StubTransport()

        results = await asyncio.gather(
            probe_endpoint(db, endpoint, transport=transport),
            probe_endpoint(db, endpoint, transport=transport),
        )

        assert sorted(probed for probed, _ in results) == [False, True]


# -----------------------------------------------------------------------------
# Secrets
# -----------------------------------------------------------------------------

class TestSecrets:
    def test_the_container_side_variable_has_exactly_one_spelling(self):
        """Cross-agent contract #3."""
        assert HARNESS_API_KEY_ENV == "LAZYAF_ENDPOINT_API_KEY"
        assert ENDPOINT_SECRET_PREFIX == "LAZYAF_ENDPOINT_"
        assert ENDPOINT_SECRET_REF_RE.match(HARNESS_API_KEY_ENV)

    @pytest.mark.parametrize(
        "ref,valid",
        [
            ("LAZYAF_ENDPOINT_LOCAL_4090", True),
            ("LAZYAF_ENDPOINT_API_KEY", True),
            ("ANTHROPIC_API_KEY", False),
            ("LAZYAF_STEP_AUTH_SECRET", False),
            ("LAZYAF_RUNNER_AUTH_SECRET", False),
            ("GEMINI_API_KEY", False),
            ("LAZYAF_ENDPOINT_", False),
            ("lazyaf_endpoint_x", False),
            ("LAZYAF_ENDPOINT_X;rm -rf /", False),
            (None, False),
        ],
    )
    def test_allowlist(self, ref, valid):
        assert is_valid_secret_ref(ref) is valid

    def test_no_auth_resolves_to_no_secret_and_no_headers(self):
        endpoint = make_endpoint(auth_style="none")
        assert endpoint_secret_value(endpoint) is None
        assert auth_headers(endpoint, None) == {}
        assert secret_present(endpoint) is True

    def test_bearer_headers(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_ENDPOINT_DEMO", "hunter2hunter2")
        endpoint = make_endpoint(
            auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_DEMO"
        )
        value = endpoint_secret_value(endpoint)
        assert value == "hunter2hunter2"
        assert auth_headers(endpoint, value) == {"Authorization": "Bearer hunter2hunter2"}
        assert secret_present(endpoint) is True

    def test_custom_header_style(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_ENDPOINT_DEMO", "abc12345")
        endpoint = make_endpoint(
            auth_style="header",
            auth_secret_ref="LAZYAF_ENDPOINT_DEMO",
            auth_header_name="x-api-key",
        )
        assert auth_headers(endpoint, endpoint_secret_value(endpoint)) == {
            "x-api-key": "abc12345"
        }

    def test_missing_variable_fails_naming_the_variable_not_the_value(self, monkeypatch):
        monkeypatch.delenv("LAZYAF_ENDPOINT_ABSENT", raising=False)
        endpoint = make_endpoint(
            auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_ABSENT"
        )
        assert secret_present(endpoint) is False
        with pytest.raises(EndpointSecretMissing) as excinfo:
            endpoint_secret_value(endpoint)
        assert "LAZYAF_ENDPOINT_ABSENT" in str(excinfo.value)

    def test_file_variable_wins(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "key"
        secret_file.write_text("from-a-file\n", encoding="utf-8")
        monkeypatch.setenv("LAZYAF_ENDPOINT_DEMO", "inline-value")
        monkeypatch.setenv("LAZYAF_ENDPOINT_DEMO_FILE", str(secret_file))
        endpoint = make_endpoint(
            auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_DEMO"
        )
        assert endpoint_secret_value(endpoint) == "from-a-file"

    @pytest.mark.parametrize(
        "text,known,expected_absent",
        [
            ("key is hunter2hunter2 here", ("hunter2hunter2",), "hunter2hunter2"),
            ("Authorization: Bearer abc.def.ghi", (), "abc.def.ghi"),
            ("token sk-abcdefgh12345 rejected", (), "sk-abcdefgh12345"),
        ],
    )
    def test_scrub_removes_every_secret_shape(self, text, known, expected_absent):
        scrubbed = scrub_secrets(text, known)
        assert expected_absent not in scrubbed
        assert "***" in scrubbed

    def test_scrub_is_total_on_none_and_non_strings(self):
        assert scrub_secrets(None) == ""
        assert scrub_secrets(1234) == "1234"

    def test_spec_headers_come_from_the_endpoint(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_ENDPOINT_DEMO", "hunter2hunter2")
        endpoint = make_endpoint(
            auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_DEMO"
        )
        built = spec_for_endpoint(endpoint, endpoint_secret_value(endpoint))
        assert built.headers["Authorization"].startswith("Bearer ")
        assert built.secret_values == ("hunter2hunter2",)
        assert built.root_url == "http://192.168.1.50:11434"


# -----------------------------------------------------------------------------
# Health from real step outcomes (wave8 s5.4)
# -----------------------------------------------------------------------------

class _OneRowDB(FakeDB):
    """`record_step_outcome` looks the row up; this hands back the one we
    already have so the demotion logic can be exercised without a session."""

    def __init__(self, endpoint=None):
        super().__init__()
        self.endpoint = endpoint

    async def execute(self, _statement):
        endpoint = self.endpoint

        class _Result:
            def scalar_one_or_none(self):
                return endpoint

        return _Result()


class TestHealthFromStepOutcomes:
    async def test_a_clean_step_zeroes_the_failure_counter(self):
        from app.services.model_endpoints.health import record_step_outcome

        endpoint = make_endpoint(
            probe_status="ok", supports_tools=True, consecutive_failures=2,
            last_error="Connection refused",
        )
        db = _OneRowDB(endpoint)

        await record_step_outcome(db, endpoint.id, {"endpoint_http_errors": 0})

        assert endpoint.consecutive_failures == 0
        assert endpoint.last_error is None
        assert endpoint.last_success_at is not None

    async def test_an_endpoint_fatal_step_bumps_the_counter(self):
        from app.services.model_endpoints.health import record_step_outcome

        endpoint = make_endpoint(probe_status="ok", supports_tools=True)
        db = _OneRowDB(endpoint)

        await record_step_outcome(
            db,
            endpoint.id,
            {"stop_reason": "endpoint", "stop_error": "503 from Bearer abc.def"},
        )

        assert endpoint.consecutive_failures == 1
        assert "abc.def" not in endpoint.last_error

    async def test_a_model_capability_failure_is_not_an_endpoint_failure(self):
        """Conflating them would make a perfectly working endpoint look down."""
        from app.services.model_endpoints.health import record_step_outcome

        endpoint = make_endpoint(probe_status="ok", supports_tools=True)
        db = _OneRowDB(endpoint)

        await record_step_outcome(
            db, endpoint.id, {"stop_reason": "unparseable", "endpoint_http_errors": 0}
        )

        assert endpoint.consecutive_failures == 0

    async def test_two_consecutive_drifting_steps_demote_supports_tools(self):
        """The teeth behind "a probe that lies": the WORK corrects the record,
        visibly, within two steps."""
        from app.services.model_endpoints.health import record_step_outcome

        endpoint = make_endpoint(probe_status="ok", supports_tools=True)
        db = _OneRowDB(endpoint)
        drift = {"probe_drift": True, "endpoint_http_errors": 0}

        await record_step_outcome(db, endpoint.id, drift)
        assert endpoint.supports_tools is True, "one step is not evidence"

        await record_step_outcome(db, endpoint.id, drift)

        assert endpoint.supports_tools is False
        assert endpoint.probe_status == "degraded"
        assert (
            endpoint.get_probe_detail()["demoted_reason"]
            == "tools advertised but never emitted"
        )

    async def test_a_clean_step_resets_the_drift_counter(self):
        from app.services.model_endpoints.health import record_step_outcome

        endpoint = make_endpoint(probe_status="ok", supports_tools=True)
        db = _OneRowDB(endpoint)

        await record_step_outcome(
            db, endpoint.id, {"probe_drift": True, "endpoint_http_errors": 0}
        )
        await record_step_outcome(db, endpoint.id, {"endpoint_http_errors": 0})
        await record_step_outcome(
            db, endpoint.id, {"probe_drift": True, "endpoint_http_errors": 0}
        )

        assert endpoint.supports_tools is True

    async def test_a_deleted_endpoint_does_not_raise(self):
        """The never-fail-a-step rule reaches here: a health update is not
        worth a 500 on a telemetry POST."""
        from app.services.model_endpoints.health import record_step_outcome

        await record_step_outcome(_OneRowDB(None), "gone", {"endpoint_http_errors": 0})
