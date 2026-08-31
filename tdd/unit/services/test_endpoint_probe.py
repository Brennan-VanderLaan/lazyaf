"""Unit tests for the capability probe (M14 wave8 s2; modalities M14.6).

One test per row of the section 2.2 decision table, driven against a STUB
transport so the whole probe - its requests, its judgements, the status
computation and the way an observation lands on a row - runs with no sockets
and no Docker. The same decision table is exercised END TO END against the
real mock server in `tdd/integration/api/test_model_endpoints_api.py`, so
nothing here is only ever checked against a stub written to match the code.

The properties these tests exist to protect:

- `supports_tools` is judged on the RESPONSE SHAPE, never on the server
  accepting the `tools` parameter. A probe that trusts the parameter is
  testing the server's advertising.
- **An unreachable endpoint leaves the previous capability booleans intact.**
  Nulling a good record because the box was rebooting is strictly worse than
  carrying a stale, timestamped one.
- The secret never reaches `probe_detail`, `last_error`, or a log line.
- **A modality is judged on the HTTP status and the TOKEN LEDGER, never on
  what the model says**, and a probe that could not tell records `None` -
  never `False`. The three ways that `None` arises (never asked / the asking
  broke / a 200 whose token count proves the attachment was discarded) are
  kept apart, because they tell an operator to do different things.
- **An absent ollama `capabilities` key is `None`, not "no vision".**
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
    MODALITY_BODY_SNIPPET_CHARS,
    MODALITY_MAX_TOKENS,
    MODALITY_REASONS,
    MODALITY_REFUSAL_STATUSES,
    PROBE_AUDIO_B64,
    PROBE_IMAGE_DATA_URL,
    PROBE_MIN_INTERVAL_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    PROBE_TOOL_NAME,
    PROBE_TOTAL_TIMEOUT_SECONDS,
    ProbeHTTP,
    ProbeResult,
    ProbeSpec,
    ProbeTransportError,
    apply_probe_result,
    compute_probe_status,
    judge_models,
    judge_modality,
    judge_modality_delta,
    judge_ollama_context,
    judge_ollama_vision,
    judge_streaming,
    judge_tools,
    judge_usage,
    modality_probe_body,
    ollama_capabilities,
    pick_context_window,
    probe_endpoint,
    prompt_tokens_of,
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


def ollama_show(context=32768, capabilities=None) -> ProbeHTTP:
    """`/api/show`. `capabilities=None` OMITS the key, which is what every
    ollama before v0.6 does - and is the case the probe must read as "we do
    not know", never as "no vision"."""
    payload = {
        "model_info": {
            "general.architecture": "qwen2",
            "qwen2.context_length": context,
            "qwen2.embedding_length": 5120,
        }
    }
    if capabilities is not None:
        payload["capabilities"] = list(capabilities)
    return http(200, payload)


#: The mock's own baseline, mirrored so the delta arithmetic here matches what
#: the integration lane sees against the real server.
CONTROL_PROMPT_TOKENS = 120


def modality_ok(prompt_tokens=CONTROL_PROMPT_TOKENS) -> ProbeHTTP:
    """A 200 that accepts the content-part shape, with a token ledger."""
    return http(
        200,
        {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 2},
        },
    )


def modality_refused(message="this model does not support image input") -> ProbeHTTP:
    """The shape a real server emits when it has no multimodal processor -
    a 400 raised BEFORE inference, which is why a refusal costs nothing."""
    return http(400, {"error": {"message": message, "type": "invalid_request_error"}})


def _parts_of(json_body) -> tuple:
    """Content-part types in a request body, or () for plain string content."""
    found = []
    for message in (json_body or {}).get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            found.extend(
                part.get("type") for part in content if isinstance(part, dict)
            )
    return tuple(found)


class StubTransport:
    """Routes by URL suffix and by CONTENT-PART SHAPE; an Exception is RAISED.

    The modality routing mirrors what a real server distinguishes: a request
    carrying an `image_url` part is a different question from the same request
    without one, and the control is the same request minus the attachment.
    `images`/`audio` default to a 400 refusal because a text-only server IS
    the common case, and a stub that accepted everything would let a broken
    delta check pass unnoticed.
    """

    def __init__(
        self,
        models=None,
        tools=None,
        stream=None,
        show=None,
        images=None,
        audio=None,
        control=None,
        delay=0.0,
    ):
        self.models = models if models is not None else models_ok()
        self.tools = tools if tools is not None else tools_ok()
        self.stream = stream if stream is not None else stream_ok()
        self.show = show if show is not None else ollama_show()
        self.images = images if images is not None else modality_refused()
        self.audio = audio if audio is not None else modality_refused(
            "invalid content type 'input_audio' for this model"
        )
        self.control = control if control is not None else modality_ok()
        self.delay = delay
        self.calls: list[tuple] = []

    async def request(
        self, method, url, *, json_body=None, timeout=None, stream=False, max_lines=64
    ):
        self.calls.append((method, url, stream, json_body))
        if self.delay:
            await asyncio.sleep(self.delay)
        parts = _parts_of(json_body)
        if url.endswith("/models"):
            value = self.models
        elif url.endswith("/api/show"):
            value = self.show
        elif stream:
            value = self.stream
        elif "image_url" in parts:
            value = self.images
        elif "input_audio" in parts:
            value = self.audio
        elif parts:
            value = self.control
        else:
            value = self.tools
        if isinstance(value, Exception):
            raise value
        return value

    def bodies(self) -> list:
        """Every `/chat/completions` body sent, in order."""
        return [
            call[3]
            for call in self.calls
            if call[1].endswith("/chat/completions") and call[3]
        ]

    def parts_sent(self) -> list:
        return [_parts_of(body) for body in self.bodies()]

    def count_with(self, part_type: str) -> int:
        return sum(1 for parts in self.parts_sent() if part_type in parts)

    def control_count(self) -> int:
        """Requests whose content is a part list of TEXT ONLY."""
        return sum(
            1 for parts in self.parts_sent() if parts and set(parts) == {"text"}
        )


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


# -----------------------------------------------------------------------------
# Modalities: the payloads (M14.6)
# -----------------------------------------------------------------------------

class TestModalityRequestBodies:
    def test_the_pair_differs_by_exactly_one_content_part(self):
        """THE design. Subtracting the control's `prompt_tokens` isolates the
        attachment only if nothing else about the two requests differs."""
        attached = modality_probe_body(MODEL, "images", attach=True)
        control = modality_probe_body(MODEL, "images", attach=False)

        assert _parts_of(control) == ("text",)
        assert _parts_of(attached) == ("text", "image_url")

        stripped = json.loads(json.dumps(attached))
        stripped["messages"][0]["content"] = [
            part
            for part in stripped["messages"][0]["content"]
            if part["type"] != "image_url"
        ]
        assert stripped == control

    def test_the_image_is_32x32_and_not_1x1(self):
        """A 1x1 is 36 bytes smaller and it is a TRAP: Qwen2-VL's image
        processor raises below its 28px patch factor, so a 1x1 probe would
        collect a 400 and record `unsupported` against a model that genuinely
        sees. A false negative manufactured by our own payload is worse than
        no probe at all."""
        import base64
        import struct

        assert PROBE_IMAGE_DATA_URL.startswith("data:image/png;base64,")
        png = base64.b64decode(PROBE_IMAGE_DATA_URL.split(",", 1)[1])
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert png[12:16] == b"IHDR"
        width, height = struct.unpack(">II", png[16:24])
        assert (width, height) == (32, 32)
        # Every patch factor in common use (14, 16, 28) fits inside it.
        assert min(width, height) >= 28

    def test_the_image_payload_stays_tiny(self):
        """It is sent to a real model and billed as prompt tokens. A server
        that FLATTENS content parts charges us for the base64 as prose, so
        every character here is a character that can be paid for twice."""
        import base64

        assert len(base64.b64decode(PROBE_IMAGE_DATA_URL.split(",", 1)[1])) < 256
        assert len(PROBE_IMAGE_DATA_URL) < 256

    def test_the_audio_payload_is_a_real_wav(self):
        import base64

        wav = base64.b64decode(PROBE_AUDIO_B64)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert len(wav) < 128

    def test_the_audio_part_uses_the_openai_input_audio_spelling(self):
        body = modality_probe_body(MODEL, "audio")
        part = body["messages"][0]["content"][1]
        assert part["type"] == "input_audio"
        assert part["input_audio"]["format"] == "wav"
        assert part["input_audio"]["data"] == PROBE_AUDIO_B64

    def test_the_reply_is_never_read_so_it_is_never_paid_for(self):
        body = modality_probe_body(MODEL, "images")
        assert body["max_tokens"] == MODALITY_MAX_TOKENS <= 4
        assert body["temperature"] == 0
        assert body["stream"] is False
        # No tools: this question is about content parts, nothing else.
        assert "tools" not in body

    def test_it_does_not_ask_the_model_to_describe_the_image(self):
        """Asking a 7B model to name the colour of a 32x32 square tests its
        COMPETENCE. A wrong answer would record False against a model that
        sees perfectly well, so the prompt asks for nothing about it."""
        text = modality_probe_body(MODEL, "images")["messages"][0]["content"][0]["text"]
        for word in ("describe", "colour", "color", "what", "see"):
            assert word not in text.lower(), text

    def test_there_is_no_video_body_to_build(self):
        """The wire format has no video content part, so there is nothing to
        send and the function refuses rather than inventing a spelling."""
        with pytest.raises(ValueError) as excinfo:
            modality_probe_body(MODEL, "video")
        assert "video" in str(excinfo.value)


class TestProbeBudget:
    def test_the_total_budget_leaves_room_for_the_modality_requests(self):
        """Four dispatch-critical requests at 20s each already exceed 60. At
        60 the modality probes would record `deadline_exhausted` BY
        CONSTRUCTION, which is a decoration rather than a capability."""
        assert PROBE_TOTAL_TIMEOUT_SECONDS == 90
        assert PROBE_TIMEOUT_SECONDS * 4 > 60

    def test_modality_bodies_are_quoted_half_as_far_as_other_bodies(self):
        """`set_probe_detail` REPLACES an oversized dict with a stub rather
        than trimming it, so two more 512-char snippets would take
        `tools_reason` down as collateral."""
        assert MODALITY_BODY_SNIPPET_CHARS == 256


# -----------------------------------------------------------------------------
# Modalities: the judgements
# -----------------------------------------------------------------------------

class TestOllamaVisionJudge:
    def test_the_free_answer_is_true_when_vision_is_in_the_array(self):
        supports, reason = judge_ollama_vision(
            ollama_show(capabilities=["completion", "tools", "vision"])
        )
        assert (supports, reason) == (True, None)

    def test_the_free_answer_is_false_when_the_array_omits_vision(self):
        """A PRESENT array that omits `vision` is a real negative: ollama
        computed it from the model's projector, not from its name."""
        supports, reason = judge_ollama_vision(
            ollama_show(capabilities=["completion", "tools"])
        )
        assert (supports, reason) == (False, "not_in_capabilities")

    def test_an_absent_capabilities_key_is_none_and_never_false(self):
        """THE assertion this path exists for. An absent key means "this
        ollama predates the field", not "this model cannot see". Recording
        False here would make every pre-0.6 ollama claim it is blind - and
        silently, because the wire probe that could correct it never runs."""
        supports, reason = judge_ollama_vision(ollama_show(capabilities=None))
        assert supports is None
        assert reason == "api_show_has_no_capabilities_field"

    @pytest.mark.parametrize(
        "response",
        [None, http(404, text="not found"), http(500, text="boom")],
    )
    def test_an_unavailable_api_show_is_none(self, response):
        supports, reason = judge_ollama_vision(response)
        assert supports is None
        assert reason == "api_show_unavailable"

    def test_a_non_list_capabilities_value_is_not_trusted(self):
        assert judge_ollama_vision(http(200, {"capabilities": "vision"}))[0] is None

    def test_the_raw_array_is_available_verbatim_as_evidence(self):
        """An operator asking "why does this say it sees" wants what ollama
        actually sent, not our reading of it."""
        array = ["completion", "tools", "vision", "thinking"]
        assert ollama_capabilities(ollama_show(capabilities=array)) == array
        assert ollama_capabilities(ollama_show(capabilities=None)) is None

    def test_it_answers_images_only_never_audio(self):
        """ollama's vocabulary has no `audio` member, so an array that omits
        audio says NOTHING about audio - there is deliberately no
        `judge_ollama_audio` to accidentally read one."""
        import app.services.model_endpoints.probe as probe_module

        assert not hasattr(probe_module, "judge_ollama_audio")


class TestModalityJudge:
    @pytest.mark.parametrize("status", MODALITY_REFUSAL_STATUSES)
    def test_a_shape_refusal_is_a_positive_false(self, status):
        """400/415/422 is the one modality answer an operator can act on -
        and it is free, because the server rejects before inference."""
        supports, reason = judge_modality(http(status, text='{"error":"nope"}'))
        assert supports is False
        assert reason == f"http_{status}"

    def test_a_5xx_is_unknown_and_deliberately_not_false(self):
        """`judge_tools` returns False on a 5xx and that is defensible there.
        For a modality a 500 is genuinely ambiguous between "no vision, and it
        crashed" and "vision, and the server is broken right now"."""
        supports, reason = judge_modality(http(503, text="overloaded"))
        assert supports is None
        assert reason == "http_5xx"
        # ...and the tools judge really does differ, so this is a choice:
        assert judge_tools(http(503, text="overloaded"))[0] is False

    @pytest.mark.parametrize("status", [401, 403, 404, 413, 429])
    def test_other_4xx_says_nothing_about_the_modality(self, status):
        supports, reason = judge_modality(http(status, text="no"))
        assert supports is None
        assert reason == "http_4xx"

    def test_a_timeout_is_unknown(self):
        assert judge_modality(None) == (None, "timeout")
        assert judge_modality(None, "transport_error") == (None, "transport_error")

    def test_a_mangled_envelope_is_unknown_not_false(self):
        """A broken envelope is not evidence about vision."""
        for payload in ({"nothing": "useful"}, {"choices": []}, {"choices": [{}]}):
            supports, reason = judge_modality(http(200, payload))
            assert supports is None
            assert reason == "bad_response_shape"

    def test_a_well_formed_200_accepts_the_shape(self):
        assert judge_modality(modality_ok()) == (True, None)

    def test_every_reason_it_emits_is_in_the_vocabulary(self):
        responses = [
            http(400, text="x"),
            http(415, text="x"),
            http(422, text="x"),
            http(401, text="x"),
            http(500, text="x"),
            http(200, {"nope": 1}),
            None,
        ]
        for response in responses:
            _, reason = judge_modality(response)
            assert reason in MODALITY_REASONS, reason


class TestModalityDelta:
    def test_a_positive_delta_proves_the_attachment_entered_the_prompt(self):
        assert judge_modality_delta(205, 120) == (True, None)

    def test_a_zero_delta_is_undetectable_and_never_true(self):
        """(C), the case that actually bites: llama.cpp-class shims flatten
        content parts by concatenating their text and return a clean 200. The
        request SUCCEEDS and the image went nowhere."""
        verdict, reason = judge_modality_delta(120, 120)
        assert verdict is None
        assert reason == "no_prompt_token_delta"

    def test_a_negative_delta_is_also_undetectable(self):
        assert judge_modality_delta(100, 120) == (None, "no_prompt_token_delta")

    def test_an_unavailable_comparison_decides_nothing_either_way(self):
        """Returns (None, None) - no verdict AND no reason - so the caller
        keeps its acceptance answer and records a caveat instead."""
        assert judge_modality_delta(None, 120) == (None, None)
        assert judge_modality_delta(205, None) == (None, None)

    def test_prompt_tokens_are_read_only_from_a_usage_block(self):
        assert prompt_tokens_of(modality_ok(77).payload) == 77
        assert prompt_tokens_of({"choices": []}) is None
        assert prompt_tokens_of(None) is None


# -----------------------------------------------------------------------------
# run_probe: sequencing, cost, and the deadline
# -----------------------------------------------------------------------------

class TestFreeOllamaPath:
    async def test_a_vision_capability_answers_for_free_and_skips_the_wire(self):
        """The whole value of `/api/show`: zero extra requests and zero
        tokens. The stub REFUSES on the wire, so a True here can only have
        come from the free path."""
        transport = StubTransport(
            show=ollama_show(capabilities=["completion", "tools", "vision"]),
            images=modality_refused(),
        )
        result = await run_probe(spec(server_kind="ollama"), transport)

        assert result.supports_images is True
        assert result.detail["images_source"] == "ollama_capabilities"
        assert result.detail["ollama_capabilities"] == ["completion", "tools", "vision"]
        assert transport.count_with("image_url") == 0, "no paid image request"

    async def test_an_array_without_vision_answers_false_and_skips_the_wire(self):
        """The stub ACCEPTS on the wire, so a False here can only have come
        from the free path."""
        transport = StubTransport(
            show=ollama_show(capabilities=["completion", "tools"]),
            images=modality_ok(CONTROL_PROMPT_TOKENS + 85),
        )
        result = await run_probe(spec(server_kind="ollama"), transport)

        assert result.supports_images is False
        assert result.detail["images_reason"] == "not_in_capabilities"
        assert transport.count_with("image_url") == 0

    async def test_an_absent_capabilities_key_falls_through_to_the_wire(self):
        """ollama < 0.6. `None` from the free path must lead to a real
        question, not to a recorded False."""
        transport = StubTransport(
            show=ollama_show(capabilities=None),
            images=modality_ok(CONTROL_PROMPT_TOKENS + 85),
        )
        result = await run_probe(spec(server_kind="ollama"), transport)

        assert result.supports_images is True
        assert result.detail["images_source"] == "wire_probe"
        assert result.detail["images_free_path_reason"] == (
            "api_show_has_no_capabilities_field"
        )
        assert transport.count_with("image_url") == 1

    async def test_a_non_ollama_server_never_attempts_api_show(self):
        transport = StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert not any("/api/show" in call[1] for call in transport.calls)
        assert result.detail["images_source"] == "wire_probe"

    async def test_audio_is_always_asked_on_the_wire_even_on_ollama(self):
        """ollama's capability vocabulary has no `audio` member, so there is
        no free answer to inherit."""
        transport = StubTransport(
            show=ollama_show(capabilities=["completion", "tools", "vision"])
        )
        result = await run_probe(spec(server_kind="ollama"), transport)

        assert transport.count_with("input_audio") == 1
        assert result.supports_audio is False
        assert result.detail["audio_source"] == "wire_probe"


class TestWireModalityProbe:
    async def test_a_refusal_records_false_with_the_upstream_body(self):
        transport = StubTransport()  # the default policy is a text-only server
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is False
        assert result.detail["images_reason"] == "http_400"
        assert result.detail["images_status"] == 400
        assert "does not support image input" in result.detail["images_body"]
        # A refusal precedes inference, so no control is worth spending.
        assert transport.control_count() == 0

    async def test_acceptance_with_a_token_delta_records_true(self):
        transport = StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is True
        assert result.detail["images_prompt_tokens"] == CONTROL_PROMPT_TOKENS + 85
        assert result.detail["images_control_tokens"] == CONTROL_PROMPT_TOKENS
        assert transport.control_count() == 1, "one matched control, for images"

    async def test_acceptance_with_no_token_delta_is_undetectable(self):
        """The nastiest row in the table: 200 OK, and the image went
        nowhere. Recording True here would claim a capability that was
        demonstrably not exercised."""
        transport = StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is None
        assert result.detail["images_reason"] == "no_prompt_token_delta"
        assert result.detail["images_prompt_tokens"] == CONTROL_PROMPT_TOKENS
        assert result.detail["images_control_tokens"] == CONTROL_PROMPT_TOKENS

    async def test_a_5xx_leaves_the_answer_unknown(self):
        transport = StubTransport(images=http(503, text="overloaded"))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is None
        assert result.detail["images_reason"] == "http_5xx"

    async def test_a_transport_failure_leaves_the_answer_unknown(self):
        transport = StubTransport(images=ProbeTransportError("read timeout"))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is None
        assert result.detail["images_reason"] == "transport_error"
        assert "read timeout" in result.detail["images_transport_error"]

    async def test_no_usage_reporting_means_no_control_is_spent(self):
        """Without a token ledger the control proves nothing, so it is not
        sent. The claim narrows to "it accepted the shape" and the CAVEAT
        says exactly that."""
        transport = StubTransport(
            tools=tools_ok(usage=False),
            stream=stream_ok(usage=False),
            images=http(
                200,
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            ),
        )
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.reports_usage is False
        assert result.supports_images is True
        assert result.detail["images_caveat"] == "no_usage_no_control"
        assert transport.control_count() == 0

    async def test_audio_and_images_are_judged_independently(self):
        transport = StubTransport(
            images=modality_refused(),
            audio=modality_ok(CONTROL_PROMPT_TOKENS + 1500),
        )
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert result.supports_images is False
        assert result.supports_audio is True
        assert result.detail["audio_prompt_tokens"] == CONTROL_PROMPT_TOKENS + 1500

    async def test_a_text_only_server_costs_two_requests_and_no_tokens(self):
        """The common case, priced. Both refusals precede inference."""
        transport = StubTransport()
        await run_probe(spec(server_kind="vllm"), transport)

        assert transport.count_with("image_url") == 1
        assert transport.count_with("input_audio") == 1
        assert transport.control_count() == 0

    async def test_modalities_never_change_probe_status(self):
        """A model with no vision is not a DEGRADED endpoint - it is a text
        model, which is what almost every endpoint here is."""
        blind = await run_probe(spec(server_kind="vllm"), StubTransport())
        seeing = await run_probe(
            spec(server_kind="vllm"),
            StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85)),
        )
        assert blind.probe_status == seeing.probe_status == "ok"

    async def test_the_secret_never_reaches_a_modality_body(self):
        sentinel = "sk-modality-sentinel-9876543210"
        transport = StubTransport(
            images=http(400, text=f'{{"error":"bad key {sentinel}"}}')
        )
        result = await run_probe(spec(server_kind="vllm", secret=sentinel), transport)

        blob = json.dumps(result.to_dict())
        assert sentinel not in blob
        assert result.detail["images_body"]

    async def test_an_enormous_refusal_body_is_capped_at_the_modality_limit(self):
        transport = StubTransport(images=http(400, text="x" * 5000))
        result = await run_probe(spec(server_kind="vllm"), transport)

        assert len(result.detail["images_body"]) == MODALITY_BODY_SNIPPET_CHARS
        # ...and the whole detail dict still fits, so `tools_reason` and
        # friends are not lost to the truncation stub.
        endpoint = make_endpoint()
        apply_probe_result(endpoint, result)
        assert "truncated" not in endpoint.get_probe_detail()


class TestModalityDeadline:
    async def test_a_spent_budget_records_deadline_exhausted_not_a_timeout(self):
        """Starvation is honest, and it is NOT a `False`. The modality probes
        run last precisely so that this is what gets starved."""
        transport = StubTransport(delay=0.3)
        result = await run_probe(
            ProbeSpec(
                base_url=BASE,
                model=MODEL,
                server_kind="ollama",
                total_timeout_seconds=1,
            ),
            transport,
        )

        assert result.supports_images is None
        assert result.supports_audio is None
        assert result.detail["images_reason"] == "deadline_exhausted"
        assert result.detail["audio_reason"] == "deadline_exhausted"
        # And nothing was SENT after the budget ran out.
        assert transport.count_with("image_url") == 0
        assert transport.count_with("input_audio") == 0
        # ...so no source is claimed: `wire_probe` would imply a request that
        # never left the building.
        assert "images_source" not in result.detail
        # The dispatch-critical answers survived; the modalities are the right
        # thing to starve, which is why they run last.
        assert result.supports_tools is True

    async def test_a_starved_modality_reads_probe_failed_not_unprobed(self):
        """`probe_failed` says the asking broke; `unprobed` says press Probe.
        Collapsing them would send an operator to the wrong fix."""
        from app.schemas.model_endpoint import modalities_of

        transport = StubTransport(delay=0.3)
        result = await run_probe(
            ProbeSpec(
                base_url=BASE, model=MODEL, server_kind="ollama", total_timeout_seconds=1
            ),
            transport,
        )
        endpoint = make_endpoint()
        apply_probe_result(endpoint, result)

        states = {m.modality: m.state for m in modalities_of(endpoint)}
        assert states["images"] == "probe_failed"
        assert states["audio"] == "probe_failed"


class TestModalityCaveatVocabulary:
    async def test_every_caveat_the_probe_emits_is_declared(self):
        from app.services.model_endpoints.probe import MODALITY_CAVEATS

        no_usage = await run_probe(
            spec(server_kind="vllm"),
            StubTransport(
                tools=tools_ok(usage=False),
                stream=stream_ok(usage=False),
                images=http(
                    200,
                    {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
                ),
            ),
        )
        no_control = await run_probe(
            spec(server_kind="vllm"),
            StubTransport(
                images=modality_ok(CONTROL_PROMPT_TOKENS + 85),
                control=http(500, text="control blew up"),
            ),
        )
        emitted = {
            no_usage.detail.get("images_caveat"),
            no_control.detail.get("images_caveat"),
        }
        assert emitted == {"no_usage_no_control", "control_unavailable"}
        for caveat in emitted:
            assert caveat in MODALITY_CAVEATS, caveat

    async def test_a_broken_control_keeps_the_acceptance_answer(self):
        """The endpoint DID accept the shape. Discarding that because the
        second request failed would throw away a real observation."""
        result = await run_probe(
            spec(server_kind="vllm"),
            StubTransport(
                images=modality_ok(CONTROL_PROMPT_TOKENS + 85),
                control=ProbeTransportError("control timed out"),
            ),
        )
        assert result.supports_images is True
        assert result.detail["images_caveat"] == "control_unavailable"


class TestApplyModalityResult:
    async def test_an_unreachable_probe_leaves_a_good_modality_record_intact(self):
        """Same rule as the other capabilities: a rebooting box must not
        erase what we know about it."""
        endpoint = make_endpoint()
        good = StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85))
        apply_probe_result(endpoint, await run_probe(spec(server_kind="vllm"), good))
        assert endpoint.supports_images is True

        down = StubTransport(models=ProbeTransportError("Connection refused"))
        apply_probe_result(endpoint, await run_probe(spec(server_kind="vllm"), down))

        assert endpoint.probe_status == "unreachable"
        assert endpoint.supports_images is True

    async def test_a_reachable_probe_that_could_not_tell_clears_a_stale_true(self):
        """The opposite case, and the opposite rule: we reached the server,
        asked, and could not settle it. Leaving the old True standing behind a
        fresh `probed_at` would date a claim this probe never made."""
        endpoint = make_endpoint()
        good = StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85))
        apply_probe_result(endpoint, await run_probe(spec(server_kind="vllm"), good))
        assert endpoint.supports_images is True

        murky = StubTransport(images=http(503, text="overloaded"))
        apply_probe_result(endpoint, await run_probe(spec(server_kind="vllm"), murky))

        assert endpoint.supports_images is None
        assert endpoint.get_probe_detail()["images_reason"] == "http_5xx"

    async def test_a_never_probed_endpoint_that_is_down_keeps_both_columns_none(self):
        endpoint = make_endpoint()
        down = StubTransport(models=ProbeTransportError("refused"))
        apply_probe_result(endpoint, await run_probe(spec(), down))

        assert endpoint.supports_images is None
        assert endpoint.supports_audio is None

    async def test_the_wire_result_carries_both_columns(self):
        result = await run_probe(
            spec(server_kind="vllm"),
            StubTransport(images=modality_ok(CONTROL_PROMPT_TOKENS + 85)),
        )
        payload = result.to_dict()
        assert payload["supports_images"] is True
        assert payload["supports_audio"] is False

    def test_an_older_runner_reporting_no_modality_keys_reads_as_not_asked(self):
        """A runner-local probe from an image that predates M14.6 must report
        "we did not ask", never False."""
        from app.schemas.model_endpoint import coerce_probe_result

        parsed = coerce_probe_result(
            {
                "reachable": True,
                "probe_status": "ok",
                "supports_tools": True,
                "supports_streaming": True,
                "reports_usage": True,
            }
        )
        assert parsed.supports_images is None
        assert parsed.supports_audio is None


class TestUncorroboratedAcceptanceIsItsOwnState:
    """FP-1: a server that ACCEPTS an image is not a server that SAW it.

    The matched-pair control asks whether prompt_tokens moved. A shim that
    flattens content parts into the prompt as prose moves them exactly like a
    real vision encoder does - so the control votes yes for a model that never
    saw the image, and the endpoint rendered a plain green check. An adversarial
    verifier built that shim and it scored `supported` with no caveat visible.

    The fix is not a third probe request: it is refusing to collapse two
    different facts into one word.
    """

    def test_an_acceptance_with_a_caveat_is_not_plain_supported(self):
        from app.services.model_endpoints.probe import modality_state

        assert modality_state(True, None, None) == "supported"
        assert (
            modality_state(True, None, "no_usage_no_control")
            == "supported_unverified"
        )

    def test_every_caveat_downgrades(self):
        from app.services.model_endpoints.probe import (
            MODALITY_CAVEATS,
            modality_state,
        )

        assert MODALITY_CAVEATS, "no caveats defined - has the vocabulary moved?"
        for caveat in MODALITY_CAVEATS:
            assert modality_state(True, None, caveat) == "supported_unverified", (
                f"caveat {caveat!r} still renders as a proven capability"
            )

    def test_a_caveat_does_not_change_a_negative_or_an_unknown(self):
        from app.services.model_endpoints.probe import modality_state

        # A caveat narrows an ACCEPTANCE. It must not turn a refusal into
        # something softer, or an unknown into something knowable.
        assert modality_state(False, "http_400", "no_usage_no_control") == "unsupported"
        assert modality_state(None, None, "no_usage_no_control") == "unprobed"

    def test_the_new_state_is_in_the_model_vocabulary(self):
        from app.models.model_endpoint import MODALITY_STATES

        assert "supported_unverified" in MODALITY_STATES
