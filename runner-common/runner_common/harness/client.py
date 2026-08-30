"""
The OpenAI-compatible HTTP client the harness (and the runner-local probe)
share (Milestone 14.2).

ONE client, ONE bug surface: ``runner_common.endpoint_probe`` imports this
module rather than growing a second requests wrapper, exactly as section 2.3
of the design requires.

WHAT IT IS NOT. It is not an SDK. It speaks the two endpoints the harness
needs (``/chat/completions`` and, for the probe, ``/models``), it retries the
three transient classes and nothing else, and every string it can be handed by
an upstream server passes through ``scrub_secrets`` before it becomes an
exception message or a log line — because a 401 body that echoes the key back
is a real failure mode, and it must not be the thing that puts the key in the
step log.

TIMEOUT OWNERSHIP. ``request_timeout_seconds`` is PER HTTP REQUEST. It is not
a step timeout, not the harness's soft deadline, and not the container
watchdog's hard one. A cold ollama loading a 32B model genuinely takes 60s on
its first request, which is why the endpoint default is 300.

Stdlib + ``requests`` (already a runner-common dependency; no new install, so
``images/agent-base``'s install line is unchanged).
"""
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .constants import (
    ENDPOINT_RETRY_BASE_SECONDS,
    ENDPOINT_RETRY_MAX_SECONDS,
    MAX_ENDPOINT_RETRIES,
)


# --------------------------------------------------------------------------
# secret hygiene
# --------------------------------------------------------------------------

#: ``Authorization: Bearer <anything>`` in any casing.
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")

#: The OpenAI-style key shape, which every compatible server's error bodies
#: cheerfully echo back.
_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")

REDACTED = "***"


def scrub_secrets(text: Any, known_values=()) -> str:
    """Replace the endpoint key, ``Bearer <x>`` and ``sk-...`` with ``***``.

    A CONTAINER-SIDE TWIN of the backend's
    ``app.services.model_endpoints.secrets.scrub_secrets``. It is duplicated
    on purpose and the duplication is stated rather than hidden: the harness
    runs in a step container that imports nothing from ``backend/app`` (wave 8
    agent B's ownership rule), so the two processes cannot share a function.
    Both sides are pinned by the same three cases — the known value, the
    bearer form, and the ``sk-`` shape.

    NEVER raises: this runs on the error path, and a scrubber that can throw
    would turn a readable failure into an opaque one.
    """
    try:
        result = str(text)
    except Exception:  # noqa: BLE001 - a __str__ that raises is still a leak
        return REDACTED
    try:
        for value in known_values or ():
            if isinstance(value, str) and len(value) >= 4:
                result = result.replace(value, REDACTED)
        result = _BEARER_RE.sub(f"Bearer {REDACTED}", result)
        result = _SK_RE.sub(REDACTED, result)
    except Exception:  # noqa: BLE001
        return REDACTED
    return result


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class EndpointFatal(Exception):
    """The endpoint cannot serve this step. Stop condition 8, exit 4.

    Raised after the retry budget is spent on 429/5xx/timeout, and on the
    FIRST response for any other 4xx — a 404 on the model id does not get
    better by asking again, and retrying it just burns the operator's wall
    clock before showing them the same message.
    """

    def __init__(self, reason: str, status: Optional[int] = None, attempts: int = 1):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.attempts = attempts


class ToolsRejected(Exception):
    """A 400 whose body mentions ``tool`` or ``function``.

    NOT an ``EndpointFatal``: it is a runtime DEMOTION signal (section 3.8).
    The endpoint is alive and answering; it simply does not accept the
    ``tools`` parameter, whatever the capability record says. The loop
    switches to the fallback protocol, logs the switch and records
    ``raw.probe_drift = true`` — which is how a lying probe gets caught at
    the request layer rather than after 40 wasted turns.
    """

    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# --------------------------------------------------------------------------
# response shape
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    """One normalized tool call, whatever shape the server used to send it."""

    name: str
    arguments_raw: str = ""
    id: Optional[str] = None
    index: int = 0

    def arguments(self):
        """``(args_dict, error)``. Never raises.

        ``error`` is a human reason when the server's ``arguments`` string is
        not a JSON object — which is a REAL and common small-model failure and
        must reach the model as a tool error it can learn from, not as a
        traceback.
        """
        raw = self.arguments_raw
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}, None
        if isinstance(raw, dict):
            return dict(raw), None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return None, f"arguments are not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, (
                f"arguments must be a JSON object; got {type(parsed).__name__}"
            )
        return parsed, None


@dataclass
class ChatResponse:
    """One assistant turn, normalized across streaming and non-streaming."""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    request_chars: int = 0
    elapsed_seconds: float = 0.0
    retries: int = 0
    streamed: bool = False

    @property
    def prompt_tokens(self) -> Optional[int]:
        return _int_or_none((self.usage or {}).get("prompt_tokens"))

    @property
    def completion_tokens(self) -> Optional[int]:
        return _int_or_none((self.usage or {}).get("completion_tokens"))

    @property
    def cached_tokens(self) -> Optional[int]:
        details = (self.usage or {}).get("prompt_tokens_details")
        if not isinstance(details, dict):
            return None
        return _int_or_none(details.get("cached_tokens"))

    @property
    def reports_usage(self) -> bool:
        return self.prompt_tokens is not None or self.completion_tokens is not None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    return None


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

def normalize_base_url(base_url: str) -> str:
    """Strip the trailing slash. NEVER rewrites the path.

    A URL that does not end in ``/v1`` is accepted as written (the backend
    records a WARNING on the row); silently appending ``/v1`` to a broker that
    does not use it would produce a 404 the operator cannot explain.
    """
    return (base_url or "").rstrip("/")


def auth_headers(auth_style: str, api_key: Optional[str], header_name: Optional[str]):
    """The ONE place a key becomes a header.

    ``none`` is a FIRST-CLASS case and produces no header at all: LAN ollama
    and vLLM behind a firewall genuinely have no key.
    """
    if not auth_style or auth_style == "none":
        return {}
    if not api_key:
        return {}
    if auth_style == "bearer":
        return {"Authorization": f"Bearer {api_key}"}
    if auth_style == "header":
        if not header_name:
            return {}
        return {header_name: api_key}
    return {}


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat client with a stated retry policy."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: Optional[str] = None,
        auth_style: str = "none",
        auth_header: Optional[str] = None,
        timeout: float = 300.0,
        temperature: Optional[float] = 0,
        top_p: Optional[float] = None,
        seed: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        session: Any = None,
        sleep=time.sleep,
        rand=random.random,
    ):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.api_key = api_key or None
        self.auth_style = auth_style or "none"
        self.auth_header = auth_header
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.max_output_tokens = max_output_tokens
        self._sleep = sleep
        self._rand = rand
        self._session = session
        #: Every request the client made, for ``raw.harness.endpoint_http_errors``.
        self.http_errors = 0

    # -- plumbing ---------------------------------------------------------

    @property
    def session(self):
        """Lazily built so importing the harness never imports ``requests``.

        The unit suite injects a fake session; the container gets the real
        one. Neither path pays for the other.
        """
        if self._session is None:
            import requests  # local: keeps import cost off the wrapper's path

            self._session = requests.Session()
        return self._session

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(auth_headers(self.auth_style, self.api_key, self.auth_header))
        return headers

    def scrub(self, text: Any) -> str:
        return scrub_secrets(text, [self.api_key] if self.api_key else [])

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # -- the one public call ----------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
    ) -> ChatResponse:
        """One assistant turn. Raises ``EndpointFatal`` / ``ToolsRejected``."""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": bool(stream),
        }
        if self.max_output_tokens:
            body["max_tokens"] = int(self.max_output_tokens)
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.seed is not None:
            body["seed"] = self.seed
        if tools:
            body["tools"] = tools
            # DELIBERATE: "auto", never "required". Several servers accept
            # `required` and emit prose anyway; trusting the parameter tests
            # the server's ADVERTISING, not its behaviour.
            body["tool_choice"] = "auto"
        if stream:
            # Without this the final SSE frame carries no usage and every
            # streamed turn is token-blind.
            body["stream_options"] = {"include_usage": True}

        request_chars = _message_chars(messages)
        started = time.monotonic()
        response, retries = self._request_with_retries(body, stream=bool(stream))
        parsed = (
            self._parse_stream(response)
            if stream
            else self._parse_blocking(response)
        )
        parsed.request_chars = request_chars
        parsed.elapsed_seconds = time.monotonic() - started
        parsed.retries = retries
        parsed.streamed = bool(stream)
        return parsed

    def get_json(self, path: str, timeout: Optional[float] = None):
        """``GET`` one JSON document. ``(status, payload_or_None, error)``.

        Used by the probe; never raises, because a probe is an OBSERVATION and
        "it is down" is a successful observation.
        """
        try:
            response = self.session.get(
                self.url(path),
                headers=self._headers(),
                timeout=timeout or self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport errors are data here
            return None, None, self.scrub(f"{type(exc).__name__}: {exc}")
        status = getattr(response, "status_code", None)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = None
        return status, payload, None

    # -- internals ---------------------------------------------------------

    def _request_with_retries(self, body: Dict[str, Any], stream: bool):
        """POST /chat/completions with the stated retry policy."""
        attempts = 0
        last_reason = "no attempt was made"
        last_status: Optional[int] = None
        while attempts <= MAX_ENDPOINT_RETRIES:
            attempts += 1
            try:
                response = self.session.post(
                    self.url("chat/completions"),
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                    stream=stream,
                )
            except Exception as exc:  # noqa: BLE001 - connect/read/TLS errors
                self.http_errors += 1
                last_reason = self.scrub(f"{type(exc).__name__}: {exc}")
                last_status = None
                if attempts > MAX_ENDPOINT_RETRIES:
                    break
                self._backoff(attempts)
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status < 300:
                return response, attempts - 1

            self.http_errors += 1
            detail = self.scrub(_body_text(response))
            last_status = status
            last_reason = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"

            if status == 400 and _mentions_tools(detail):
                raise ToolsRejected(last_reason, status=status)

            retryable = status == 429 or status >= 500
            if not retryable:
                # Any other 4xx is fatal on the FIRST response. Asking a 404
                # four times just delays the same message.
                raise EndpointFatal(last_reason, status=status, attempts=attempts)
            if attempts > MAX_ENDPOINT_RETRIES:
                break
            self._backoff(attempts)

        raise EndpointFatal(
            f"{last_reason} (after {attempts} attempts)",
            status=last_status,
            attempts=attempts,
        )

    def _backoff(self, attempt: int) -> None:
        """Full jitter, capped. ``attempt`` is 1-based."""
        ceiling = min(
            ENDPOINT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            ENDPOINT_RETRY_MAX_SECONDS,
        )
        self._sleep(self._rand() * ceiling)

    def _parse_blocking(self, response) -> ChatResponse:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise EndpointFatal(
                self.scrub(f"response body is not JSON: {exc}"),
                status=int(getattr(response, "status_code", 0) or 0),
            )
        if not isinstance(payload, dict):
            raise EndpointFatal(
                f"response body is a {type(payload).__name__}, expected an object"
            )
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        message = message if isinstance(message, dict) else {}
        return ChatResponse(
            content=_text_of(message.get("content")),
            tool_calls=_normalize_tool_calls(message.get("tool_calls")),
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            finish_reason=(
                choice.get("finish_reason") if isinstance(choice, dict) else None
            ),
        )

    def _parse_stream(self, response) -> ChatResponse:
        """Accumulate SSE deltas into one turn.

        Tool-call deltas arrive fragmented and indexed; they are merged by
        ``index`` because that is the only field every server sets on every
        fragment (``id`` and ``function.name`` typically appear once, on the
        first fragment of each call).
        """
        content_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        usage = None
        model = None
        finish_reason = None

        for raw_line in response.iter_lines():
            if raw_line is None:
                continue
            line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload_text = line[len("data:"):].strip()
            if payload_text == "[DONE]":
                break
            try:
                frame = json.loads(payload_text)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if isinstance(frame.get("usage"), dict):
                usage = frame["usage"]
            if isinstance(frame.get("model"), str):
                model = frame["model"]
            choices = frame.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = _text_of(delta.get("content"))
            if text:
                content_parts.append(text)
            for fragment in delta.get("tool_calls") or []:
                if not isinstance(fragment, dict):
                    continue
                index = fragment.get("index")
                index = index if isinstance(index, int) else len(calls)
                slot = calls.setdefault(
                    index, {"id": None, "name": "", "arguments": ""}
                )
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                function = fragment.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        slot["name"] = function["name"]
                    argument_fragment = function.get("arguments")
                    if isinstance(argument_fragment, str):
                        slot["arguments"] += argument_fragment

        tool_calls = [
            ToolCall(
                name=slot["name"],
                arguments_raw=slot["arguments"],
                id=slot["id"] or f"call_{index}",
                index=index,
            )
            for index, slot in sorted(calls.items())
            if slot["name"]
        ]
        return ChatResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_TOOL_WORDS = ("tool", "function")


def _mentions_tools(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(word in lowered for word in _TOOL_WORDS)


def _body_text(response) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text[:512]
    try:
        return json.dumps(response.json())[:512]
    except Exception:  # noqa: BLE001
        return ""


def _text_of(content: Any) -> str:
    """``content`` is a string on every server that matters, and a list of
    parts on the ones that copied the Anthropic block shape. Both are read."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _normalize_tool_calls(raw: Any) -> List[ToolCall]:
    if not isinstance(raw, list):
        return []
    calls: List[ToolCall] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name") or entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if arguments is None:
            arguments = entry.get("arguments")
        if isinstance(arguments, dict):
            arguments_raw: Any = json.dumps(arguments)
        elif isinstance(arguments, str):
            arguments_raw = arguments
        else:
            arguments_raw = ""
        calls.append(
            ToolCall(
                name=name,
                arguments_raw=arguments_raw,
                # SYNTHESIZED when the server omits one. In tools mode the
                # assistant message we echo back carries this id and the tool
                # result must carry the SAME one, or the next request is a 400
                # about an unanswered tool call.
                id=entry.get("id") or f"call_{index}",
                index=index,
            )
        )
    return calls


def _message_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        total += len(_text_of(message.get("content")))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                function = call.get("function") or {}
                total += len(str(function.get("name") or ""))
                total += len(str(function.get("arguments") or ""))
    return total
