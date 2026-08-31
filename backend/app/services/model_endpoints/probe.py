"""The capability probe (M14 wave8 section 2; modalities M14.6).

Up to eight requests, no side effects, never raises. The probe answers the
questions the platform needs before it dares spend GPU time on a model it has
never driven:

    1. is it there, and does it have the model?   GET  {base}/models
    2. can it TOOL CALL?                          POST {base}/chat/completions
    3. can it STREAM?                             POST {base}/chat/completions
    4. how big is its context window,             POST {root}/api/show (ollama)
       and can it SEE?                            ...same request, M14.6
    5. can it take an IMAGE content part?         POST {base}/chat/completions
    6. ...and did the image actually arrive?      POST {base}/chat/completions
    7. can it take an AUDIO content part?         POST {base}/chat/completions
    8. ...and did the audio actually arrive?      POST {base}/chat/completions

...plus `reports_usage`, harvested from 2 and 3, because the whole cost story
depends on whether the server returns a `usage` block at all.

WHAT THE MODALITY HALF COSTS (M14.6), because a probe spends real money:

    ollama `capabilities` read   0 requests, 0 tokens (rides request 4)
    image/audio REFUSED          1 request,  0 tokens - the server rejects the
                                 shape before inference, so the common case is
                                 free
    image ACCEPTED               2 requests, ~14 text + 6..576 image tokens
    audio ACCEPTED               2 requests, ~14 text + up to ~1500 - a
                                 Whisper-class encoder pads to a fixed 30s mel
                                 window, so 1ms of silence is billed as one
    Worst case added: 4 requests, ~1700 prompt tokens, 16 completion tokens.
    A modern ollama: 1 request (the audio 400), 0 tokens.

VIDEO IS ABSENT FROM THAT LIST AND ALWAYS WILL BE. The OpenAI
chat-completions user-content-part vocabulary is `text` / `image_url` /
`input_audio` / `file`. There is no `video_url` and no `input_video`, so there
is no cross-server way to ASK the question and no cross-server way to SEND
video even if the answer were yes. vLLM's `video_url` is a documented
OpenAI-incompatible extension; frame-sampling a video into N `image_url` parts
is images. See `UNREPRESENTABLE_MODALITIES` in `models/model_endpoint.py`: it
is declared as a property of the wire format, not probed, and surfaced as a
permanently-explained state.

THE DECISION THAT MAKES THIS PROBE WORTH RUNNING: `supports_tools` is judged on
the **RESPONSE SHAPE**, not on the server accepting the `tools` parameter, and
the probe sends `tool_choice: "auto"` rather than `"required"`. Several servers
accept `required` and then emit prose anyway; a probe that trusts the parameter
is testing the server's ADVERTISING. This one checks
`choices[0].message.tool_calls[0].function.name == "probe"` and that its
arguments parse as a JSON object - the only thing the harness can rely on.

THE SAME DECISION, MADE TWICE MORE, FOR MODALITIES: a modality is judged on
the HTTP status and the TOKEN LEDGER, never on what the model says. Asking a
7B model to describe a 32x32 square tests its competence, not its wiring. And
because a server can accept an image content part, return a clean 200, and
silently DISCARD the image (llama.cpp-class shims have flattened content parts
by concatenating their text for years), acceptance alone is not evidence. The
probe therefore sends a MATCHED CONTROL - the same request minus the image -
and requires `prompt_tokens` to have gone UP. A success with no token delta is
recorded as `undetectable`, which is a worse state than "unsupported" and is
the one R1 most needs surfaced: the request succeeds while half its input
vanishes.

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
#: Every request together. The operator is watching a spinner.
#:
#: Raised 60 -> 90 by M14.6. Four requests at 20s each already exceeded 60,
#: and the modality probes add up to four more; leaving it at 60 would starve
#: them BY CONSTRUCTION, and a capability that is structurally guaranteed to
#: record `deadline_exhausted` is not a capability, it is a decoration. 90s is
#: the stated trade against the spinner, and the modality probes run LAST
#: precisely so that when this budget does run out, the thing that gets
#: starved is the one dispatch does not depend on.
PROBE_TOTAL_TIMEOUT_SECONDS = 90
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
#: Modality refusal bodies are quoted HALF as far, and the reason is
#: arithmetic, not taste: `set_probe_detail` does not TRIM an oversized
#: detail dict, it REPLACES THE WHOLE THING with a `{"truncated": true}` stub.
#: Two more 512-char snippets on a bad day push the dict toward the 4 KiB cap
#: and would take `tools_reason` down with them as collateral. The real
#: refusals are short ("this model does not support image input").
MODALITY_BODY_SNIPPET_CHARS = 256

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

#: `probe_detail["<modality>_reason"]`. Grouped by the VERDICT each one
#: produces, because that grouping is the contract - not the strings.
MODALITY_REASONS = (
    # -> False. A positive refusal, and the only modality answer an operator
    #    can act on directly. Costs zero tokens: the server rejects the
    #    request shape before inference.
    "http_400",
    "http_415",
    "http_422",
    "not_in_capabilities",
    # -> None, "we could not tell". Every one of these is UNKNOWN, never
    #    False (constraint 4: a failed probe is not a negative observation).
    "http_4xx",
    "http_5xx",
    "bad_response_shape",
    "timeout",
    "transport_error",
    "deadline_exhausted",
    "api_show_unavailable",
    "api_show_has_no_capabilities_field",
    # -> None, and the WORST case: 200 OK, and the attachment measurably
    #    never entered the prompt. The request succeeds and the input
    #    vanishes.
    "no_prompt_token_delta",
)

#: The subset that means "we asked, it answered 200, and the answer does not
#: decide it". Read by the wire projection to pick `undetectable` over
#: `probe_failed`; both are NULL columns and both refuse at dispatch, but they
#: tell an operator to do completely different things.
UNDETECTABLE_MODALITY_REASONS: tuple[str, ...] = ("no_prompt_token_delta",)

#: The subset that means "the ASKING broke". Re-probing may help; reading the
#: reason first is usually the better move.
MODALITY_FAILURE_REASONS: tuple[str, ...] = (
    "http_4xx",
    "http_5xx",
    "bad_response_shape",
    "timeout",
    "transport_error",
    "deadline_exhausted",
    "api_show_unavailable",
)

#: Statuses that are a POSITIVE REFUSAL of the content-part shape, i.e. the
#: only ones that make a modality `False`. vLLM raises on multimodal parts
#: when the model config carries no multimodal processor; ollama answers
#: "model does not support images"; a shim that has never heard of
#: `image_url` answers "invalid content type". Everything else 4xx (401 auth,
#: 404 routing, 413 our payload, 429 rate limit) says nothing about the
#: modality and must stay UNKNOWN.
MODALITY_REFUSAL_STATUSES: tuple[int, ...] = (400, 415, 422)

#: Recorded beside a `True` that could not be corroborated by a control.
MODALITY_CAVEATS: tuple[str, ...] = ("no_usage_no_control", "control_unavailable")


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
    #: THREE-STATE, like `supports_tools`. `None` here is "we could not tell"
    #: and is produced by four different situations the detail keys keep
    #: apart: never asked, the asking broke, the deadline ran out, and - the
    #: dangerous one - a 200 whose token ledger proves the attachment never
    #: entered the prompt.
    supports_images: bool | None = None
    supports_audio: bool | None = None
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
            "supports_images": self.supports_images,
            "supports_audio": self.supports_audio,
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


# -- the modality payloads (M14.6) --------------------------------------------
#
# BUILT, NOT REMEMBERED. Both blobs below were generated by a script and their
# byte counts verified; neither is a string recalled from somewhere.

#: A 32x32 all-black RGB PNG: 82 bytes, 112 base64 chars, 134-char data URL.
#:
#: 32x32 AND NOT 1x1, and this is the single most likely way a hasty
#: implementation ships a false negative. Qwen2-VL's image processor raises
#: `height:1 must be larger than factor:28` on any dimension below its patch
#: factor, so a 1x1 probe would collect a 400 and record
#: `supports_images = False` AGAINST A MODEL THAT GENUINELY SEES. A false
#: negative manufactured by our own payload is worse than no probe at all.
#: 32 clears every patch factor in common use (14, 16, 28) with room over.
#:
#: It is also deliberately not a recognisable test image: a shim that flattens
#: content parts by concatenating their text will charge us for the base64 as
#: prose, and 112 characters is a cheap way to be wrong.
PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAGUlEQVR42u3BMQEAAADCoPVP"
    "7WENoAAAAG4MIAABITLN7AAAAABJRU5ErkJggg=="
)

#: 8 kHz mono 8-bit PCM WAV, 8 silent samples = 1 millisecond. 52 bytes, 72
#: base64 chars - the smallest thing that is unambiguously a WAV file.
#:
#: ⚠ ITS SIZE DOES NOT BOUND ITS COST. Whisper-family audio encoders pad every
#: input to a fixed 30-second mel window, so this 1ms of silence can be billed
#: as ~1500 prompt tokens. That is the largest single cost in the whole probe
#: and it is charged only when a server ACCEPTS audio, which is rare.
PROBE_AUDIO_B64 = (
    "UklGRiwAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQgAAACAgICAgICAgA=="
)
PROBE_AUDIO_FORMAT = "wav"

#: The text part. Identical in the attachment request and in its control, so
#: the only difference between the two is the attachment itself.
MODALITY_PROBE_TEXT = "Reply with the single word: ok"
#: Four. The reply is never read - the judgements are the HTTP status and the
#: token ledger - so anything larger is money spent on prose nobody parses.
MODALITY_MAX_TOKENS = 4

#: The content-part `type` each modality is spelled as on this wire format.
#: Note what is NOT here: there is no video spelling to put in it.
MODALITY_PART_TYPES: dict[str, str] = {
    "images": "image_url",
    "audio": "input_audio",
}


def modality_probe_body(model: str, modality: str, *, attach: bool = True) -> dict:
    """One modality request, or its MATCHED CONTROL when `attach=False`.

    The pair is the whole design. `attach=True` and `attach=False` differ by
    exactly one content part, so subtracting the control's `prompt_tokens`
    from the attachment request's isolates the attachment - and a positive
    delta is the only model-independent, competence-independent evidence that
    the server actually ENCODED it rather than silently dropping it.

    Note what this deliberately does NOT do: it does not ask the model to
    describe the image. Asking a 7B model to name the colour of a 32x32 square
    tests its COMPETENCE, and a wrong answer would record `False` against a
    model that sees perfectly well.
    """
    if modality not in MODALITY_PART_TYPES:
        raise ValueError(
            f"unknown modality {modality!r}; this wire format can carry "
            f"{', '.join(sorted(MODALITY_PART_TYPES))} and nothing else"
        )

    parts: list[dict] = [{"type": "text", "text": MODALITY_PROBE_TEXT}]
    if attach:
        if modality == "images":
            parts.append(
                {
                    "type": "image_url",
                    # `detail: low` caps the vision encoder's tiling on servers
                    # that honour it (85 tokens on OpenAI's class of model
                    # instead of up to 1445). Ignored elsewhere, harmless.
                    "image_url": {"url": PROBE_IMAGE_DATA_URL, "detail": "low"},
                }
            )
        else:
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": PROBE_AUDIO_B64,
                        "format": PROBE_AUDIO_FORMAT,
                    },
                }
            )

    return {
        "model": model,
        "messages": [{"role": "user", "content": parts}],
        "max_tokens": MODALITY_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
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


def ollama_capabilities(response: ProbeHTTP | None) -> list | None:
    """The raw `capabilities` array `/api/show` returned, or None if absent.

    Returned verbatim so `probe_detail` can record the WHOLE evidence for a
    free `True`. An operator asking "why does this say it sees images" wants
    the array ollama actually sent, not our reading of it. ~60 bytes.
    """
    if response is None or not response.ok or not isinstance(response.payload, dict):
        return None
    capabilities = response.payload.get("capabilities")
    return capabilities if isinstance(capabilities, list) else None


def judge_ollama_vision(response: ProbeHTTP | None) -> tuple[bool | None, str | None]:
    """The FREE image answer, from `/api/show`. Zero extra requests, zero tokens.

    ollama >= 0.6 computes `capabilities` from the loaded model's projector and
    architecture - not from its name - so this is real evidence, not a
    heuristic. It piggybacks the `/api/show` request the probe already makes
    for the context window, which is why a modern ollama learns about images
    for nothing.

    **THIS JUDGE ANSWERS IMAGES ONLY.** ollama's vocabulary
    (`types/model/capability.go`) is completion / tools / insert / vision /
    embedding / thinking: there is no `audio` member and no `video` member. A
    `capabilities` array that omits `audio` says NOTHING about audio, so audio
    always falls through to the paid wire probe.

    **AN ABSENT `capabilities` KEY IS None, NEVER False.** It means "this
    ollama predates the field", not "this model cannot see". Recording False
    there would make every pre-0.6 ollama claim it is blind - the exact shape
    of lie the three states exist to forbid - and it would do it silently,
    because the wire probe that could have corrected it would never run.
    """
    capabilities = ollama_capabilities(response)
    if capabilities is None:
        if response is None or not response.ok:
            return None, "api_show_unavailable"
        return None, "api_show_has_no_capabilities_field"
    if "vision" in capabilities:
        return True, None
    return False, "not_in_capabilities"


def judge_modality(
    response: ProbeHTTP | None, error: str | None = None
) -> tuple[bool | None, str | None]:
    """Did the endpoint ACCEPT this content-part shape? THREE-STATE.

    Judged on the transport and nothing else - never on what the model said.
    A `True` here means "the request shape was accepted"; whether the
    attachment reached the prompt is a separate question the matched control
    answers.

    **Two deliberate divergences from `judge_tools`, both of them the same
    principle**: that function returns `False` on a 5xx and on a malformed
    envelope, which is defensible for tools (a corroborating response-shape
    signal exists, and "asking for tools broke it" is operationally the same
    as no tools). For a modality neither is defensible. A 500 is genuinely
    ambiguous between "no vision, and it crashed" and "vision, and the server
    is broken right now", and a mangled envelope is not evidence about vision
    at all. Constraint 4 governs: a failed probe is UNKNOWN, not FALSE.
    """
    if error is not None:
        return None, error
    if response is None:
        return None, "timeout"
    if response.status in MODALITY_REFUSAL_STATUSES:
        return False, f"http_{response.status}"
    if response.status >= 500:
        return None, "http_5xx"
    if not response.ok:
        # 401 auth, 404 routing, 413 our own payload, 429 rate limit. None of
        # them is an answer about the modality.
        return None, "http_4xx"

    payload = response.payload
    if not isinstance(payload, dict):
        return None, "bad_response_shape"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "bad_response_shape"
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None, "bad_response_shape"
    return True, None


def modality_state(
    value: bool | None, reason: str | None, caveat: str | None = None
) -> str:
    """One probed modality's six-state answer. THE only place this is decided.

    Lives here, beside the reason vocabularies it reads, so that the wire
    projection (`schemas.model_endpoint.modalities_of`) and the dispatch
    refusal (`services.model_endpoints.resolve`) are reading ONE function
    rather than two implementations of the same table (R3). A UI that showed
    `undetectable` while dispatch treated the row as `unprobed` would be the
    drift this prevents.

    The null column splits three ways and the split lives entirely in
    `reason`: a delta that never appeared is `undetectable`, a probe that
    broke is `probe_failed`, and no reason at all means nobody ever asked.
    Returns a `models.model_endpoint.MODALITY_STATES` member.
    """
    if value is True:
        # A caveat means the acceptance was never corroborated - see
        # MODALITY_CAVEATS. `supported` is reserved for a capability the probe
        # actually demonstrated; this is "the server took it and nothing
        # contradicted that", which is a weaker and different claim.
        return "supported_unverified" if caveat else "supported"
    if value is False:
        return "unsupported"
    if reason in UNDETECTABLE_MODALITY_REASONS:
        return "undetectable"
    if reason in MODALITY_FAILURE_REASONS:
        return "probe_failed"
    return "unprobed"


def prompt_tokens_of(payload: Any) -> int | None:
    """`usage.prompt_tokens`, or None. The matched control's only reading."""
    usage = _usage_from(payload)
    if usage is None:
        return None
    value = usage.get("prompt_tokens")
    return value if isinstance(value, int) else None


def judge_modality_delta(
    attached_tokens: int | None, control_tokens: int | None
) -> tuple[bool | None, str | None]:
    """(B) "it saw it" vs (C) "the server silently dropped it".

    This is the case that actually bites. llama.cpp-class shims have
    historically flattened content parts by concatenating their `text` and
    discarding the rest, then returned a perfectly good 200. Acceptance alone
    cannot tell that apart from real support, and the difference matters
    enormously: in one case an attached image is read, in the other the step
    SUCCEEDS while half its input silently vanished.

    The discriminator is arithmetic and needs nothing from the model:

        delta = prompt_tokens(with attachment) - prompt_tokens(control)

    A positive delta means the attachment was encoded into the prompt. A
    delta of zero or less means the request succeeded and the attachment went
    nowhere - `None`, reason `no_prompt_token_delta`, which the UI renders as
    `undetectable` and dispatch refuses. Recording `True` on a zero delta
    would be claiming a capability that was demonstrably not exercised.

    Returns `(None, None)` when the comparison is not available at all; the
    caller keeps its acceptance verdict and records a CAVEAT saying the claim
    is "it accepted the shape" and nothing more.
    """
    if attached_tokens is None or control_tokens is None:
        return None, None
    if attached_tokens > control_tokens:
        return True, None
    return None, "no_prompt_token_delta"


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

    def spent() -> bool:
        """Is the shared budget gone?

        `remaining()` floors at 1.0s and therefore never says "stop" - which
        is right for the four dispatch-critical requests (a 1s attempt beats
        no attempt) and wrong for the modality probes, which would otherwise
        fire doomed 1s requests at a server that is already too slow. Asked
        BEFORE each modality request so starvation is recorded honestly as
        `deadline_exhausted` rather than mislabelled as a timeout.
        """
        return time.monotonic() >= deadline

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

    # -- request 4: ollama context window AND the free vision answer -----------
    #
    # One request, two answers. `/api/show` was already being made for the
    # context window; M14.6 reads `capabilities` off the SAME payload, so a
    # modern ollama learns whether it can see for zero extra requests and zero
    # tokens. That is why this stays request 4 and the modality probes come
    # after it: the free answer, when it exists, makes the paid one
    # unnecessary.
    ollama_context = None
    ollama_vision: bool | None = None
    ollama_vision_reason: str | None = None
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
            ollama_vision, ollama_vision_reason = judge_ollama_vision(show_response)
            capabilities = ollama_capabilities(show_response)
            if capabilities is not None:
                # Verbatim: this array IS the evidence for a free True, and
                # an operator asking "why" wants what ollama actually said.
                detail["ollama_capabilities"] = capabilities
        except ProbeTransportError as exc:
            detail["context_reason"] = clean(f"api_show_unreachable: {exc}")
            ollama_vision_reason = "api_show_unavailable"
        except Exception as exc:
            detail["context_reason"] = clean(f"api_show_error: {type(exc).__name__}")
            ollama_vision_reason = "api_show_unavailable"

    # -- requests 5-8: modalities (LAST, and often zero of them) ---------------
    #
    # Last on purpose. Tools is dispatch-critical - every harness step has to
    # pick a protocol - and images is not; if the shared deadline runs out,
    # these are the right things to starve, and starvation records
    # `deadline_exhausted`, which is honest.
    #
    # Sequential, not gathered. Firing concurrent inference at a
    # `max_concurrency: 1` box is rude, gains nothing on the single-slot
    # hardware M14 targets, and would make the shared-deadline arithmetic
    # untestable.

    async def probe_wire_modality(modality: str) -> bool | None:
        """One modality over the wire: attach, judge, and corroborate.

        Up to two requests, and frequently zero tokens - a server that refuses
        the shape rejects it BEFORE inference.
        """
        if spent():
            # NO source recorded: nothing was sent, and claiming `wire_probe`
            # would imply a request that never left the building.
            detail[f"{modality}_reason"] = "deadline_exhausted"
            return None

        detail[f"{modality}_source"] = "wire_probe"
        response: ProbeHTTP | None = None
        error: str | None = None
        try:
            response = await transport.request(
                "POST",
                f"{base}/chat/completions",
                json_body=modality_probe_body(spec.model, modality, attach=True),
                timeout=remaining(),
            )
        except ProbeTransportError as exc:
            error = "transport_error"
            detail[f"{modality}_transport_error"] = clean(
                str(exc), MODALITY_BODY_SNIPPET_CHARS
            )
        except Exception as exc:
            error = "transport_error"
            detail[f"{modality}_transport_error"] = clean(
                f"{type(exc).__name__}: {exc}", MODALITY_BODY_SNIPPET_CHARS
            )

        supported, reason = judge_modality(response, error)
        if response is not None:
            detail[f"{modality}_status"] = response.status
            if not response.ok:
                detail[f"{modality}_body"] = clean(
                    response.text, MODALITY_BODY_SNIPPET_CHARS
                )
        if reason:
            detail[f"{modality}_reason"] = reason
        if supported is not True:
            return supported

        # Accepted. Now: did the attachment actually GET there?
        attached_tokens = prompt_tokens_of(response.payload if response else None)
        if reports_usage is not True or attached_tokens is None:
            # No token ledger, so no control worth spending. The claim
            # narrows to "it accepted the shape", and the caveat says so.
            detail[f"{modality}_caveat"] = "no_usage_no_control"
            return True

        detail[f"{modality}_prompt_tokens"] = attached_tokens
        if spent():
            detail[f"{modality}_caveat"] = "control_unavailable"
            return True

        control: ProbeHTTP | None = None
        try:
            control = await transport.request(
                "POST",
                f"{base}/chat/completions",
                json_body=modality_probe_body(spec.model, modality, attach=False),
                timeout=remaining(),
            )
        except Exception:
            control = None

        control_tokens = (
            prompt_tokens_of(control.payload)
            if control is not None and control.ok
            else None
        )
        if control_tokens is None:
            detail[f"{modality}_caveat"] = "control_unavailable"
            return True

        detail[f"{modality}_control_tokens"] = control_tokens
        verdict, delta_reason = judge_modality_delta(attached_tokens, control_tokens)
        if delta_reason:
            detail[f"{modality}_reason"] = delta_reason
        return verdict

    if ollama_vision is not None:
        # The free path answered. The wire probe is NOT sent - that is the
        # whole value of `/api/show`, and it is worth an assertion.
        supports_images: bool | None = ollama_vision
        detail["images_source"] = "ollama_capabilities"
        if ollama_vision_reason:
            detail["images_reason"] = ollama_vision_reason
    else:
        if ollama_vision_reason:
            # Why the free path could not answer. Kept even though the wire
            # probe is about to overwrite `images_reason`, because "ollama is
            # too old to say" is the actionable half of a paid probe.
            detail["images_free_path_reason"] = ollama_vision_reason
        supports_images = await probe_wire_modality("images")

    # Audio is ALWAYS the wire probe: ollama's capability vocabulary has no
    # `audio` member, so there is no free answer to inherit - not for ollama
    # and not for anything else. Against ollama specifically this reliably
    # 400s (its OpenAI layer knows `text` and `image_url` only), and that is a
    # TRUE `False` for this endpoint, recorded at zero tokens.
    supports_audio = await probe_wire_modality("audio")

    # The override is applied by the CALLER (the column is the override), so
    # the probe reports only what it DISCOVERED.
    context_window, context_source = pick_context_window(
        None, ollama_context, max_model_len
    )
    if context_window is not None:
        detail["context_window"] = context_window
        detail["context_window_source"] = context_source

    # NOTE the modalities are NOT arguments here, and must not become them. A
    # model with no vision is not a degraded endpoint - it is a text model,
    # which is what almost every endpoint on this platform is. Folding a
    # missing modality into `probe_status` would paint most of the registry
    # amber for a capability nothing in the pipeline uses.
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
        supports_images=supports_images,
        supports_audio=supports_audio,
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
    # M14.6. Written straight through INCLUDING None: a probe that reached the
    # server but could not settle the image question must clear a previous
    # answer, not leave a stale one standing behind a fresh `probed_at`
    # timestamp. (The unreachable branch above returns before this and keeps
    # everything, which is the opposite case and the opposite rule.)
    endpoint.supports_images = result.supports_images
    endpoint.supports_audio = result.supports_audio
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
