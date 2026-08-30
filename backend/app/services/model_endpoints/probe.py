"""The capability probe (M14, wave8 section 2).

Four requests, no side effects, never raises. The probe answers the only four
questions the platform actually needs before it dares spend GPU time on a
model it has never driven:

    1. is it there, and does it have the model?   GET  {base}/models
    2. can it TOOL CALL?                          POST {base}/chat/completions
    3. can it STREAM?                             POST {base}/chat/completions
    4. how big is its context window?             POST {root}/api/show (ollama)

...plus `reports_usage`, harvested from 2 and 3, because the whole cost story
depends on whether the server returns a `usage` block at all.

THE DECISION THAT MAKES THIS PROBE WORTH RUNNING: `supports_tools` is judged on
the **RESPONSE SHAPE**, not on the server accepting the `tools` parameter, and
the probe sends `tool_choice: "auto"` rather than `"required"`. Several servers
accept `required` and then emit prose anyway; a probe that trusts the parameter
is testing the server's ADVERTISING. This one checks
`choices[0].message.tool_calls[0].function.name == "probe"` and that its
arguments parse as a JSON object - the only thing the harness can rely on.

AN UNREACHABLE PROBE IS A SUCCESSFUL OBSERVATION. `probe_status` becomes
`unreachable`, `consecutive_failures` is bumped, `last_error` is set - and the
**capability booleans are LEFT AT THEIR PREVIOUS VALUES**, because nulling a
good record because the box was rebooting is strictly worse than carrying a
stale, timestamped one. The API returns 200: a probe is an observation, and
"it is down" is one. Returning 502 would make the operator's UI show a request
error where it should show a red endpoint.

Transport is injected (`ProbeTransport`) for two reasons: the unit tests drive
the whole decision table against a stub with no sockets, and the same decision
logic is what the runner-local probe runs inside a container with nothing but
`requests`.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# -- budgets, named, no inline literals ---------------------------------------

#: Per request.
PROBE_TIMEOUT_SECONDS = 20
#: All four requests together. The operator is watching a spinner.
PROBE_TOTAL_TIMEOUT_SECONDS = 60
#: 24h. After this the capability record is STALE - which still WORKS: dispatch
#: runs, warns, and schedules a background re-probe.
PROBE_TTL_SECONDS = 86_400
#: Two probes inside this window: the second returns the cached record.
PROBE_MIN_INTERVAL_SECONDS = 30
#: Used ONLY with a loud log line, when nothing discovered a real window.
DEFAULT_ASSUMED_CONTEXT = 8192
DEFAULT_MAX_OUTPUT_TOKENS = 1024
#: `probe_detail` cap (wave8 s1.1).
PROBE_DETAIL_MAX_BYTES = 4096
#: `last_error` cap (wave8 s1.1).
LAST_ERROR_MAX_CHARS = 512
#: Upstream bodies are quoted into `probe_detail` this far and no further.
BODY_SNIPPET_CHARS = 512

#: The tool the probe asks for, and the value it asks for.
PROBE_TOOL_NAME = "probe"
PROBE_TOOL_VALUE = 7

#: Reason vocabularies recorded in `probe_detail` when a capability is False.
TOOLS_REASONS = (
    "http_400",
    "http_4xx",
    "http_5xx",
    "no_tool_calls",
    "wrong_tool",
    "bad_arguments_json",
    "bad_response_shape",
    "timeout",
)


class ProbeTransportError(Exception):
    """A connection/TLS/timeout failure - i.e. we never got an HTTP status."""


@dataclass
class ProbeHTTP:
    """One transport-level outcome. `lines` is populated for streamed calls."""

    status: int
    text: str = ""
    payload: Any = None
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class ProbeSpec:
    """Everything the probe needs and nothing about the database.

    `secret_values` is what `scrub_secrets` redacts out of anything the probe
    records: the resolved key, if there is one. It is never logged, never
    returned and never persisted.
    """

    base_url: str
    model: str
    server_kind: str = "other"
    headers: dict = field(default_factory=dict)
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS
    total_timeout_seconds: int = PROBE_TOTAL_TIMEOUT_SECONDS
    secret_values: tuple = ()

    @property
    def root_url(self) -> str:
        """`base_url` with the trailing `/v1` removed - the vendor-extension
        root (`/api/show`). Only ollama's discovery uses it."""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base[: -len("/v1")]
        return base


@dataclass
class ProbeResult:
    """One observation. `to_dict()` is the wire shape the runner-local probe
    POSTs back, so this dataclass and `schemas.model_endpoint.ProbeResult`
    are two spellings of ONE contract."""

    reachable: bool
    probe_status: str
    model_listed: bool | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    reports_usage: bool | None = None
    context_window: int | None = None
    context_window_source: str | None = None
    detail: dict = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "probe_status": self.probe_status,
            "model_listed": self.model_listed,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "reports_usage": self.reports_usage,
            "context_window": self.context_window,
            "context_window_source": self.context_window_source,
            "detail": self.detail,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


# -----------------------------------------------------------------------------
# Transport
# -----------------------------------------------------------------------------

class HttpxProbeTransport:
    """The backend-side transport. One `httpx.AsyncClient` per probe.

    R5: everything here is awaited; nothing blocks the loop. A connect, TLS or
    read failure becomes `ProbeTransportError`, which is the ONLY thing that
    makes an endpoint `unreachable` - an HTTP 500 is a reachable server with an
    opinion, and is recorded as such.
    """

    def __init__(self, headers: dict | None = None) -> None:
        self._headers = dict(headers or {})

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        stream: bool = False,
        max_lines: int = 64,
    ) -> ProbeHTTP:
        import httpx

        headers = dict(self._headers)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                if not stream:
                    response = await client.request(
                        method, url, json=json_body, headers=headers
                    )
                    text = response.text or ""
                    payload = None
                    try:
                        payload = json.loads(text) if text else None
                    except ValueError:
                        payload = None
                    return ProbeHTTP(
                        status=response.status_code, text=text, payload=payload
                    )

                lines: list[str] = []
                async with client.stream(
                    method, url, json=json_body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        return ProbeHTTP(status=response.status_code, text=body)
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        lines.append(line)
                        if len(lines) >= max_lines or line.strip() == "data: [DONE]":
                            break
                    return ProbeHTTP(
                        status=response.status_code, text="", lines=lines
                    )
        except Exception as exc:  # httpx.* and anything a proxy throws
            raise ProbeTransportError(f"{type(exc).__name__}: {exc}") from exc


# -----------------------------------------------------------------------------
# The four request bodies (data, so a test can assert on them)
# -----------------------------------------------------------------------------

TOOL_PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": PROBE_TOOL_NAME,
            "description": "Echo a number back to the caller.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "description": "The number to echo."}
                },
                "required": ["value"],
            },
        },
    }
]

_TOOL_MESSAGES = [
    {"role": "system", "content": "You call tools. Never answer in prose."},
    {
        "role": "user",
        "content": (
            f"Call the tool `{PROBE_TOOL_NAME}` with value {PROBE_TOOL_VALUE}. "
            "Do not reply in text."
        ),
    },
]


def tool_probe_body(model: str) -> dict:
    return {
        "model": model,
        "messages": _TOOL_MESSAGES,
        "tools": TOOL_PROBE_TOOLS,
        # "auto", NOT "required": several servers accept `required` and emit
        # prose anyway, so trusting the parameter tests the ADVERTISING.
        "tool_choice": "auto",
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }


def stream_probe_body(model: str) -> dict:
    return {
        "model": model,
        "messages": _TOOL_MESSAGES,
        "max_tokens": 8,
        "temperature": 0,
        "stream": True,
        # Servers that honor this put `usage` on the final frame; servers that
        # ignore it produce a turn with no usage, which the harness COUNTS.
        "stream_options": {"include_usage": True},
    }


# -----------------------------------------------------------------------------
# The decision table (pure - one unit test per row of wave8 s2.2)
# -----------------------------------------------------------------------------

def judge_models(response: ProbeHTTP, model: str) -> tuple[bool | None, int | None, str | None]:
    """Request 1. Returns (model_listed, max_model_len, reason).

    404/501 -> `model_listed = None` and NOT a failure: some brokers simply do
    not implement the listing. Only a transport failure is unreachable.
    """
    if response.status in (404, 501):
        return None, None, "models_not_implemented"
    if not response.ok:
        return None, None, f"http_{response.status}"

    data = None
    if isinstance(response.payload, dict):
        data = response.payload.get("data")
    elif isinstance(response.payload, list):
        data = response.payload
    if not isinstance(data, list):
        return None, None, "bad_response_shape"

    listed = False
    max_model_len: int | None = None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == model:
            listed = True
        value = entry.get("max_model_len")
        if isinstance(value, int) and value > 0:
            if entry.get("id") == model:
                max_model_len = value
            elif max_model_len is None:
                max_model_len = value
    return listed, max_model_len, None if listed else "model_not_listed"


def judge_tools(response: ProbeHTTP | None, error: str | None = None) -> tuple[bool, str | None]:
    """Request 2. True ONLY when the response SHAPE proves a real tool call."""
    if error is not None:
        return False, error
    if response is None:
        return False, "timeout"
    if response.status == 400:
        return False, "http_400"
    if response.status >= 500:
        return False, "http_5xx"
    if not response.ok:
        return False, "http_4xx"

    payload = response.payload
    if not isinstance(payload, dict):
        return False, "bad_response_shape"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, "bad_response_shape"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return False, "bad_response_shape"

    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return False, "no_tool_calls"
    first = calls[0]
    function = first.get("function") if isinstance(first, dict) else None
    if not isinstance(function, dict):
        return False, "bad_response_shape"
    if function.get("name") != PROBE_TOOL_NAME:
        return False, "wrong_tool"

    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return True, None
    if not isinstance(arguments, str):
        return False, "bad_arguments_json"
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return False, "bad_arguments_json"
    if not isinstance(parsed, dict):
        return False, "bad_arguments_json"
    return True, None


def judge_streaming(response: ProbeHTTP | None, error: str | None = None) -> tuple[bool, str | None]:
    """Request 3. At least one SSE `data:` frame carrying `choices[].delta`
    before `[DONE]`."""
    if error is not None:
        return False, error
    if response is None:
        return False, "timeout"
    if not response.ok:
        return False, f"http_{response.status}"
    for line in response.lines:
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        body = stripped[len("data:"):].strip()
        if body == "[DONE]":
            break
        try:
            frame = json.loads(body)
        except ValueError:
            continue
        choices = frame.get("choices") if isinstance(frame, dict) else None
        if isinstance(choices, list) and choices:
            if isinstance(choices[0], dict) and "delta" in choices[0]:
                return True, None
    return False, "no_delta_frames"


def _usage_from(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "completion_tokens"):
        if isinstance(usage.get(key), int):
            return usage
    return None


def judge_usage(payloads) -> tuple[bool, str | None]:
    """`reports_usage` - the capability that decides whether ANY cost number
    is possible. True when any probed response carried a `usage` object with
    an integer `prompt_tokens` or `completion_tokens`."""
    for payload in payloads:
        if _usage_from(payload) is not None:
            return True, None
    return False, "no_usage_block"


def judge_ollama_context(response: ProbeHTTP | None) -> int | None:
    """Request 4. ollama's `/api/show` reports the window under a
    FAMILY-PREFIXED key (`qwen2.model_info.context_length`), so scan for any
    key ENDING in `.context_length` and take the max. A NAMED vendor
    extension attempted for exactly one `server_kind` - not a guess applied
    everywhere."""
    if response is None or not response.ok or not isinstance(response.payload, dict):
        return None
    info = response.payload.get("model_info")
    if not isinstance(info, dict):
        return None
    candidates = [
        value
        for key, value in info.items()
        if isinstance(key, str)
        and key.endswith(".context_length")
        and isinstance(value, int)
        and value > 0
    ]
    return max(candidates) if candidates else None


def pick_context_window(
    override: int | None,
    ollama_context: int | None,
    max_model_len: int | None,
) -> tuple[int | None, str | None]:
    """First hit wins: operator override, ollama `*.context_length`, vLLM
    `max_model_len`, then None. None means the harness assumes
    DEFAULT_ASSUMED_CONTEXT and SAYS SO - assuming 128k silently is how a step
    dies at turn 12 with an opaque 400."""
    if override is not None:
        return override, "override"
    if ollama_context is not None:
        return ollama_context, "ollama"
    if max_model_len is not None:
        return max_model_len, "max_model_len"
    return None, None


def compute_probe_status(
    reachable: bool,
    supports_tools: bool | None,
    supports_streaming: bool | None,
    reports_usage: bool | None,
    model_listed: bool | None,
) -> str:
    """`ok` | `degraded` | `unreachable`.

    Reconciliation, stated because wave8 s2.2's two bullets overlap: the `ok`
    bullet says "at least one of tools/streaming plus usage", and the
    `degraded` bullet says "tool calling failed -> degraded". A tools-less,
    streaming, usage-reporting endpoint satisfies both. **Degraded wins**,
    because degraded is the INFORMATIVE answer - it is still USABLE
    (`supports_tools=False` routes the fallback protocol; `reports_usage=False`
    routes an honest token-blind usage row), and the status exists precisely
    so the UI can say WHY the endpoint will behave the way it will.
    """
    if not reachable:
        return "unreachable"
    if supports_tools and reports_usage and model_listed is not False:
        return "ok"
    return "degraded"


# -----------------------------------------------------------------------------
# The probe itself
# -----------------------------------------------------------------------------

async def run_probe(spec: ProbeSpec, transport=None) -> ProbeResult:
    """Four requests, no side effects, NEVER raises.

    Request 1 failing at the TRANSPORT level stops the probe: requests 2-4 are
    pointless against a box that is not there, and running them anyway would
    triple the operator's wait for the same answer.
    """
    from app.services.model_endpoints.secrets import scrub_secrets

    transport = transport or HttpxProbeTransport(spec.headers)
    base = spec.base_url.rstrip("/")
    started = time.monotonic()
    deadline = started + max(1, spec.total_timeout_seconds)

    def remaining() -> float:
        return max(1.0, min(float(spec.timeout_seconds), deadline - time.monotonic()))

    def clean(text: Any, limit: int = BODY_SNIPPET_CHARS) -> str:
        return scrub_secrets(text, spec.secret_values)[:limit]

    detail: dict = {"base_url": base, "model": spec.model, "server_kind": spec.server_kind}

    # -- request 1: liveness and model presence -------------------------------
    try:
        models_response = await transport.request(
            "GET", f"{base}/models", timeout=remaining()
        )
    except ProbeTransportError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        message = clean(str(exc), LAST_ERROR_MAX_CHARS)
        detail["reachable"] = False
        detail["unreachable_reason"] = message
        return ProbeResult(
            reachable=False,
            probe_status="unreachable",
            detail=detail,
            error=message,
            elapsed_ms=elapsed,
        )
    except Exception as exc:  # a stub transport misbehaving must not 500
        elapsed = int((time.monotonic() - started) * 1000)
        message = clean(f"{type(exc).__name__}: {exc}", LAST_ERROR_MAX_CHARS)
        detail["reachable"] = False
        detail["unreachable_reason"] = message
        return ProbeResult(
            reachable=False,
            probe_status="unreachable",
            detail=detail,
            error=message,
            elapsed_ms=elapsed,
        )

    model_listed, max_model_len, models_reason = judge_models(models_response, spec.model)
    detail["reachable"] = True
    detail["models_status"] = models_response.status
    if models_reason:
        detail["models_reason"] = models_reason
    if not models_response.ok and models_response.status not in (404, 501):
        detail["models_body"] = clean(models_response.text)

    # -- request 2: tool calling ----------------------------------------------
    tools_response: ProbeHTTP | None = None
    tools_error: str | None = None
    try:
        tools_response = await transport.request(
            "POST",
            f"{base}/chat/completions",
            json_body=tool_probe_body(spec.model),
            timeout=remaining(),
        )
    except ProbeTransportError as exc:
        tools_error = "timeout"
        detail["tools_transport_error"] = clean(str(exc))
    except Exception as exc:
        tools_error = "timeout"
        detail["tools_transport_error"] = clean(f"{type(exc).__name__}: {exc}")

    supports_tools, tools_reason = judge_tools(tools_response, tools_error)
    if tools_reason:
        detail["tools_reason"] = tools_reason
        if tools_response is not None:
            detail["tools_status"] = tools_response.status
            detail["tools_body"] = clean(tools_response.text)

    # -- request 3: streaming --------------------------------------------------
    stream_response: ProbeHTTP | None = None
    stream_error: str | None = None
    try:
        stream_response = await transport.request(
            "POST",
            f"{base}/chat/completions",
            json_body=stream_probe_body(spec.model),
            timeout=remaining(),
            stream=True,
        )
    except ProbeTransportError as exc:
        stream_error = "timeout"
        detail["stream_transport_error"] = clean(str(exc))
    except Exception as exc:
        stream_error = "timeout"
        detail["stream_transport_error"] = clean(f"{type(exc).__name__}: {exc}")

    supports_streaming, stream_reason = judge_streaming(stream_response, stream_error)
    if stream_reason:
        detail["stream_reason"] = stream_reason
        if stream_response is not None and not stream_response.ok:
            detail["stream_status"] = stream_response.status
            detail["stream_body"] = clean(stream_response.text)

    # -- usage (harvested from 2 and 3) ---------------------------------------
    stream_usage_payloads = []
    if stream_response is not None:
        for line in stream_response.lines:
            body = line.strip()
            if not body.startswith("data:"):
                continue
            body = body[len("data:"):].strip()
            if body == "[DONE]":
                continue
            try:
                stream_usage_payloads.append(json.loads(body))
            except ValueError:
                continue
    reports_usage, usage_reason = judge_usage(
        [tools_response.payload if tools_response else None, *stream_usage_payloads]
    )
    if usage_reason:
        detail["usage_reason"] = usage_reason

    # -- request 4: ollama context window --------------------------------------
    ollama_context = None
    if spec.server_kind == "ollama":
        try:
            show_response = await transport.request(
                "POST",
                f"{spec.root_url}/api/show",
                json_body={"model": spec.model},
                timeout=remaining(),
            )
            ollama_context = judge_ollama_context(show_response)
            if ollama_context is None:
                detail["context_reason"] = f"api_show_http_{show_response.status}"
        except ProbeTransportError as exc:
            detail["context_reason"] = clean(f"api_show_unreachable: {exc}")
        except Exception as exc:
            detail["context_reason"] = clean(f"api_show_error: {type(exc).__name__}")

    # The override is applied by the CALLER (the column is the override), so
    # the probe reports only what it DISCOVERED.
    context_window, context_source = pick_context_window(
        None, ollama_context, max_model_len
    )
    if context_window is not None:
        detail["context_window"] = context_window
        detail["context_window_source"] = context_source

    probe_status = compute_probe_status(
        True, supports_tools, supports_streaming, reports_usage, model_listed
    )
    detail["probe_status"] = probe_status

    return ProbeResult(
        reachable=True,
        probe_status=probe_status,
        model_listed=model_listed,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        reports_usage=reports_usage,
        context_window=context_window,
        context_window_source=context_source,
        detail=detail,
        error=None,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# -----------------------------------------------------------------------------
# Applying an observation to a row
# -----------------------------------------------------------------------------

def lazyaf_harness_version() -> str | None:
    """M13 provenance: what build of LazyAF took this observation.

    Read from the environment (`LAZYAF_VERSION`, then `LAZYAF_GIT_SHA`) rather
    than shelled out to `git describe`: R5 forbids blocking I/O on the loop,
    and a probe must not depend on a git checkout being present in the image.
    """
    for var in ("LAZYAF_VERSION", "LAZYAF_GIT_SHA"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()[:64]
    return None


def spec_for_endpoint(endpoint, secret_value: str | None = None) -> ProbeSpec:
    """Build a `ProbeSpec` from a row plus an already-resolved secret."""
    from app.services.model_endpoints.secrets import auth_headers

    return ProbeSpec(
        base_url=endpoint.base_url,
        model=endpoint.model,
        server_kind=endpoint.server_kind,
        headers=auth_headers(endpoint, secret_value),
        timeout_seconds=min(
            int(endpoint.request_timeout_seconds or PROBE_TIMEOUT_SECONDS),
            PROBE_TIMEOUT_SECONDS,
        ),
        secret_values=(secret_value,) if secret_value else (),
    )


def apply_probe_result(
    endpoint,
    result: ProbeResult,
    *,
    probed_from: str = "backend",
    harness_version: str | None = None,
) -> None:
    """Fold one observation into the row. The ONE writer of the capability
    record, so the backend probe and the runner-local probe-result handler
    cannot drift apart.

    Unreachable: status, timestamp, failure counter and `last_error` move;
    **the three capability booleans and the discovered context window do
    NOT**. The record is timestamped and the UI shows its age, which is a
    strictly better outcome than a good record nulled by a reboot.
    """
    from app.services.model_endpoints.secrets import scrub_secrets

    now = datetime.utcnow()
    endpoint.probed_at = now
    endpoint.probed_from = probed_from[:64] if probed_from else None
    endpoint.probe_harness_version = harness_version or lazyaf_harness_version()

    if not result.reachable:
        endpoint.probe_status = "unreachable"
        endpoint.consecutive_failures = int(endpoint.consecutive_failures or 0) + 1
        endpoint.last_error = scrub_secrets(result.error or "unreachable")[
            :LAST_ERROR_MAX_CHARS
        ]
        # MERGE, do not replace: the surviving detail still explains the
        # surviving booleans.
        merged = endpoint.get_probe_detail()
        merged.update(
            {
                "reachable": False,
                "probe_status": "unreachable",
                "unreachable_reason": endpoint.last_error,
                "unreachable_at": now.isoformat(),
            }
        )
        endpoint.set_probe_detail(merged)
        return

    endpoint.probe_status = result.probe_status
    endpoint.supports_tools = result.supports_tools
    endpoint.supports_streaming = result.supports_streaming
    endpoint.reports_usage = result.reports_usage
    endpoint.consecutive_failures = 0
    endpoint.last_success_at = now
    endpoint.last_error = None

    detail = dict(result.detail or {})
    # The discovered window lives in ONE place - `probe_detail` - whichever
    # side observed it. `run_probe` already writes it there; a runner-reported
    # result carries it at the top level, and normalising here is what keeps
    # `effective_context_window` a single lookup instead of two.
    if result.context_window is not None:
        detail["context_window"] = result.context_window
        detail["context_window_source"] = result.context_window_source
    else:
        detail.pop("context_window", None)
        detail.pop("context_window_source", None)
    endpoint.set_probe_detail(detail)


# -----------------------------------------------------------------------------
# Orchestration: one probe in flight per endpoint, and a floor between probes
# -----------------------------------------------------------------------------

#: Per-endpoint-id locks. Process-local by design: the rate limit protects the
#: MODEL SERVER from a spinner-clicking operator, and a single-process backend
#: is what LazyAF runs (main.py warns when WEB_CONCURRENCY > 1).
_probe_locks: dict[str, asyncio.Lock] = {}


def _lock_for(endpoint_id: str) -> asyncio.Lock:
    lock = _probe_locks.get(endpoint_id)
    if lock is None:
        lock = asyncio.Lock()
        _probe_locks[endpoint_id] = lock
    return lock


def probe_is_recent(endpoint) -> bool:
    """Was this endpoint probed inside `PROBE_MIN_INTERVAL_SECONDS`?"""
    age = endpoint.probe_age_seconds
    return age is not None and age < PROBE_MIN_INTERVAL_SECONDS


def background_reprobe(session_factory, endpoint_id: str) -> None:
    """Fire-and-forget re-probe of a STALE endpoint (wave8 s2.4).

    Dispatch calls this after logging `endpoint_dispatch_warning`: the step
    runs on the stale record it already has, and the refresh happens beside
    it. Deliberately fire-and-forget with everything swallowed - a background
    capability refresh must never be able to fail the step that triggered it.
    """
    async def _run() -> None:
        try:
            async with session_factory() as session:
                from sqlalchemy import select

                from app.models.model_endpoint import ModelEndpoint

                result = await session.execute(
                    select(ModelEndpoint).where(ModelEndpoint.id == endpoint_id)
                )
                endpoint = result.scalar_one_or_none()
                if endpoint is None or endpoint.reach == "runner-local":
                    # runner-local is probed BY A RUN, not from here; probing
                    # it from the backend would record the wrong machine.
                    return
                await probe_endpoint(session, endpoint, force=True)
        except Exception:
            logger.exception("background re-probe of endpoint %s failed", endpoint_id)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.debug("no running loop; background re-probe of %s skipped", endpoint_id)


async def probe_endpoint(
    db,
    endpoint,
    *,
    force: bool = False,
    transport=None,
) -> tuple[bool, str | None]:
    """Probe `endpoint` from the BACKEND and persist the observation.

    Returns `(probed, detail)` - `probed=False` with a reason means the
    request was answered from the cached record (rate limit) and NO upstream
    call was made. Never raises: an operator's probe button must not be able
    to 500, and "the endpoint is down" is an OBSERVATION that belongs on the
    record, not an exception that belongs in a stack trace.
    """
    from app.services.model_endpoints.secrets import (
        EndpointSecretMissing,
        endpoint_secret_value,
    )

    if not force and probe_is_recent(endpoint):
        return False, (
            f"probed {int(endpoint.probe_age_seconds)}s ago; a probe inside "
            f"{PROBE_MIN_INTERVAL_SECONDS}s returns the cached record"
        )

    async with _lock_for(endpoint.id):
        # Re-check under the lock: two clicks race to here, one probes.
        if not force and probe_is_recent(endpoint):
            return False, (
                f"a probe of '{endpoint.name}' is already in flight or "
                f"completed within {PROBE_MIN_INTERVAL_SECONDS}s"
            )

        secret_value = None
        try:
            secret_value = endpoint_secret_value(endpoint, required=False)
        except EndpointSecretMissing as exc:
            # Structurally impossible through the API (422 at create), but a
            # hand-written row must not take the probe button down with it.
            logger.warning("endpoint %s: %s", endpoint.name, exc)

        spec = spec_for_endpoint(endpoint, secret_value)
        try:
            result = await asyncio.wait_for(
                run_probe(spec, transport=transport),
                timeout=spec.total_timeout_seconds + 5,
            )
        except asyncio.TimeoutError:
            result = ProbeResult(
                reachable=False,
                probe_status="unreachable",
                detail={"base_url": spec.base_url, "model": spec.model},
                error=(
                    f"probe exceeded PROBE_TOTAL_TIMEOUT_SECONDS "
                    f"({spec.total_timeout_seconds}s)"
                ),
            )
        except Exception as exc:  # never-fail-the-button
            logger.exception("probe of endpoint %s raised", endpoint.name)
            result = ProbeResult(
                reachable=False,
                probe_status="unreachable",
                detail={"base_url": spec.base_url, "model": spec.model},
                error=f"{type(exc).__name__}: {exc}",
            )

        apply_probe_result(endpoint, result, probed_from="backend")
        await db.commit()
        await db.refresh(endpoint)
        return True, None
