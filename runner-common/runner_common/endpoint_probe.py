"""
The runner-local capability probe (Milestone 14.1, design section 2.3).

``python3 -m runner_common.endpoint_probe`` is the whole invocation contract.
The backend schedules it as an ordinary one-step script run pinned by
``requires: {has: ["endpoint:<name>"]}``, so it executes ON THE BOX THAT HOSTS
THE MODEL and therefore probes from THE EXACT NETWORK POSITION THE REAL STEP
WILL OCCUPY — which the backend cannot do for a ``reach=runner-local``
endpoint, because such an endpoint is unreachable from the backend by
definition.

The failure to schedule it is itself information: if no runner carries the
label, the probe run fails at ``NO_RUNNER_TIMEOUT`` with "no runner carries
label endpoint:local-4090", which is the true reason the endpoint is unusable.

IT SHARES ``harness.client`` WITH THE HARNESS — one HTTP client, one bug
surface (design section 2.3, verbatim).

DELIBERATE TWIN, STATED NOT HIDDEN. The decision table below mirrors
``backend/app/services/model_endpoints/probe.py``'s pure judges reason-for
reason. It cannot import them: this module runs inside a step container that
has no ``backend/app`` and no ``httpx``. The two are pinned to the same
vocabulary by ``PROBE_REASONS`` here and by agent A's judge tests there; a new
reason added on one side and not the other is a visible mismatch in the
endpoint's ``probe_detail``.

Stdlib + ``requests``.
"""
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .harness.client import OpenAICompatClient, normalize_base_url, scrub_secrets

#: Env var naming the endpoint to probe. Placed by the backend in the probe
#: step's config ``environment`` (never in the container's inspectable env for
#: anything secret — this one is an id, not a secret).
PROBE_ENDPOINT_ID_ENV = "LAZYAF_PROBE_ENDPOINT_ID"

#: OPTIONAL: the endpoint spec as JSON, so the probe needs no round trip to
#: the backend before it can start. When absent the probe GETs
#: ``/api/model-endpoints/{id}``.
PROBE_ENDPOINT_JSON_ENV = "LAZYAF_PROBE_ENDPOINT"

#: Where to report. The 12.5 wrapper already logs this variable's value, so it
#: is an established convention rather than a new one.
BACKEND_URL_ENV = "LAZYAF_BACKEND_URL"

#: The step JWT, for the ``verify_step_auth``-gated ``/probe-result`` route.
#: REQUESTED EDIT (see the wave report): the backend must place this in the
#: probe step's config ``environment``; nothing else in the platform puts a
#: step token in a step's environment today.
STEP_TOKEN_ENV = "LAZYAF_STEP_TOKEN"

#: The container-side API key variable, named on the wire by
#: ``endpoint.auth_env`` exactly as it is for the harness.
DEFAULT_TIMEOUT = 20
TOTAL_TIMEOUT = 60
STREAM_MAX_LINES = 64

PROBE_TOOL_NAME = "probe"
PROBE_TOOL_VALUE = 7

#: The reason vocabulary, pinned so the twin cannot drift silently.
PROBE_REASONS = (
    "models_not_implemented",
    "model_not_listed",
    "bad_response_shape",
    "http_400",
    "http_4xx",
    "http_5xx",
    "timeout",
    "no_tool_calls",
    "wrong_tool",
    "bad_arguments_json",
    "no_delta_frames",
    "no_usage_block",
)

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
        "stream_options": {"include_usage": True},
    }


# --------------------------------------------------------------------------
# the decision table (pure)
# --------------------------------------------------------------------------

def judge_models(status: Optional[int], payload: Any, model: str):
    if status in (404, 501):
        return None, None, "models_not_implemented"
    if status is None or not (200 <= status < 300):
        return None, None, f"http_{status}"
    data = None
    if isinstance(payload, dict):
        data = payload.get("data")
    elif isinstance(payload, list):
        data = payload
    if not isinstance(data, list):
        return None, None, "bad_response_shape"
    listed = False
    max_model_len = None
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


def judge_tools(status: Optional[int], payload: Any, error: Optional[str] = None):
    """True ONLY when the response SHAPE proves a real tool call."""
    if error is not None:
        return False, error
    if status is None:
        return False, "timeout"
    if status == 400:
        return False, "http_400"
    if status >= 500:
        return False, "http_5xx"
    if not (200 <= status < 300):
        return False, "http_4xx"
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
    function = calls[0].get("function") if isinstance(calls[0], dict) else None
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


def judge_streaming(status: Optional[int], lines: List[str], error: Optional[str] = None):
    if error is not None:
        return False, error
    if status is None:
        return False, "timeout"
    if not (200 <= status < 300):
        return False, f"http_{status}"
    for line in lines or []:
        stripped = (line or "").strip()
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


def _usage_from(payload: Any):
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "completion_tokens"):
        if isinstance(usage.get(key), int):
            return usage
    return None


def judge_usage(payloads):
    for payload in payloads:
        if _usage_from(payload) is not None:
            return True, None
    return False, "no_usage_block"


def judge_ollama_context(payload: Any) -> Optional[int]:
    """ollama reports the window under a FAMILY-PREFIXED key, so scan for any
    key ENDING in ``.context_length`` and take the max."""
    if not isinstance(payload, dict):
        return None
    info = payload.get("model_info")
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


def pick_context_window(override, ollama_context, max_model_len):
    if override is not None:
        return override, "override"
    if ollama_context is not None:
        return ollama_context, "ollama"
    if max_model_len is not None:
        return max_model_len, "max_model_len"
    return None, None


def compute_probe_status(reachable, supports_tools, supports_streaming, reports_usage, model_listed):
    if not reachable:
        return "unreachable"
    if supports_tools and reports_usage and model_listed is not False:
        return "ok"
    return "degraded"


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------

def run_probe(endpoint: Dict[str, Any], *, client=None, api_key=None) -> Dict[str, Any]:
    """Four requests, no side effects, NEVER raises.

    Returns the ``ProbeResult`` wire dict — the same keys
    ``schemas.model_endpoint.ProbeResult`` declares, so the POST needs no
    translation layer.
    """
    started = time.monotonic()
    base_url = normalize_base_url(str(endpoint.get("base_url") or ""))
    model = str(endpoint.get("model") or "")
    server_kind = str(endpoint.get("server_kind") or "other")
    secrets = [api_key] if api_key else []

    client = client or OpenAICompatClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        auth_style=str(endpoint.get("auth_style") or "none"),
        auth_header=endpoint.get("auth_header"),
        timeout=float(endpoint.get("request_timeout_seconds") or DEFAULT_TIMEOUT),
    )

    detail: Dict[str, Any] = {}

    def finish(**kwargs) -> Dict[str, Any]:
        result = {
            "reachable": False,
            "probe_status": "unreachable",
            "model_listed": None,
            "supports_tools": None,
            "supports_streaming": None,
            "reports_usage": None,
            "context_window": None,
            "context_window_source": None,
            "detail": detail,
            "error": None,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        result.update(kwargs)
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    # -- request 1: liveness + model presence ------------------------------
    status, payload, transport_error = client.get_json("models", timeout=DEFAULT_TIMEOUT)
    if transport_error is not None:
        # A TRANSPORT failure is the ONLY thing that makes an endpoint
        # unreachable; requests 2-4 against a box that is not there would
        # triple the operator's wait for the same answer.
        detail["unreachable_reason"] = transport_error
        return finish(error=transport_error)

    model_listed, max_model_len, models_reason = judge_models(status, payload, model)
    if models_reason:
        detail["models_reason"] = models_reason

    # -- request 2: tool calling ------------------------------------------
    tools_status, tools_payload, tools_error = _post(
        client, tool_probe_body(model), secrets
    )
    supports_tools, tools_reason = judge_tools(tools_status, tools_payload, tools_error)
    if tools_reason:
        detail["tools_reason"] = tools_reason
        detail["tools_body"] = scrub_secrets(
            json.dumps(tools_payload)[:512] if tools_payload is not None else "",
            secrets,
        )

    # -- request 3: streaming ---------------------------------------------
    stream_status, stream_lines, stream_payloads, stream_error = _post_stream(
        client, stream_probe_body(model), secrets
    )
    supports_streaming, stream_reason = judge_streaming(
        stream_status, stream_lines, stream_error
    )
    if stream_reason:
        detail["stream_reason"] = stream_reason

    reports_usage, usage_reason = judge_usage([tools_payload] + list(stream_payloads))
    if usage_reason:
        detail["usage_reason"] = usage_reason

    # -- request 4: ollama context window ----------------------------------
    ollama_context = None
    if server_kind == "ollama":
        show_status, show_payload, show_error = _post_root(
            client, "api/show", {"model": model}
        )
        if show_error:
            detail["context_reason"] = scrub_secrets(show_error, secrets)
        elif show_status is None or not (200 <= show_status < 300):
            detail["context_reason"] = f"api_show_http_{show_status}"
        else:
            ollama_context = judge_ollama_context(show_payload)

    context_window, context_source = pick_context_window(
        endpoint.get("context_window"), ollama_context, max_model_len
    )
    probe_status = compute_probe_status(
        True, supports_tools, supports_streaming, reports_usage, model_listed
    )
    return finish(
        reachable=True,
        probe_status=probe_status,
        model_listed=model_listed,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        reports_usage=reports_usage,
        context_window=context_window,
        context_window_source=context_source,
    )


def _post(client, body, secrets):
    try:
        response = client.session.post(
            client.url("chat/completions"),
            headers=client._headers(),  # noqa: SLF001 - same package, one client
            json=body,
            timeout=client.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, scrub_secrets(f"{type(exc).__name__}: {exc}", secrets)
    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = None
    return status, payload, None


def _post_stream(client, body, secrets):
    try:
        response = client.session.post(
            client.url("chat/completions"),
            headers=client._headers(),  # noqa: SLF001
            json=body,
            timeout=client.timeout,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        return None, [], [], scrub_secrets(f"{type(exc).__name__}: {exc}", secrets)
    status = getattr(response, "status_code", None)
    lines: List[str] = []
    payloads: List[Any] = []
    try:
        for raw in response.iter_lines():
            if raw is None:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if not line.strip():
                continue
            lines.append(line)
            body_text = line.strip()
            if body_text.startswith("data:"):
                fragment = body_text[len("data:"):].strip()
                if fragment and fragment != "[DONE]":
                    try:
                        payloads.append(json.loads(fragment))
                    except ValueError:
                        pass
            if len(lines) >= STREAM_MAX_LINES or line.strip() == "data: [DONE]":
                break
    except Exception as exc:  # noqa: BLE001
        return status, lines, payloads, scrub_secrets(
            f"{type(exc).__name__}: {exc}", secrets
        )
    return status, lines, payloads, None


def _post_root(client, path, body):
    """POST to the vendor-extension ROOT (``base_url`` minus ``/v1``)."""
    base = client.base_url
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        response = client.session.post(
            f"{root}/{path.lstrip('/')}",
            headers=client._headers(),  # noqa: SLF001
            json=body,
            timeout=client.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, f"api_show_unreachable: {type(exc).__name__}: {exc}"
    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = None
    return status, payload, None


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

def _log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def _load_endpoint(endpoint_id: str, backend_url: str, token: Optional[str]):
    """The endpoint spec, from the env when the backend inlined it, else by
    GET. ``(endpoint, reason)`` — never raises."""
    inline = os.environ.get(PROBE_ENDPOINT_JSON_ENV)
    if inline:
        try:
            parsed = json.loads(inline)
        except ValueError as exc:
            return None, f"{PROBE_ENDPOINT_JSON_ENV} is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, f"{PROBE_ENDPOINT_JSON_ENV} is not a JSON object"
        return parsed, None

    if not backend_url:
        return None, (
            f"{BACKEND_URL_ENV} is unset and {PROBE_ENDPOINT_JSON_ENV} carries "
            "no inline endpoint; the probe has no way to learn what to probe"
        )
    import requests

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{backend_url.rstrip('/')}/api/model-endpoints/{endpoint_id}"
    try:
        response = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not reach the backend at {url}: {type(exc).__name__}"
    if response.status_code != 200:
        return None, f"the backend answered HTTP {response.status_code} for {url}"
    try:
        return response.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"the backend's endpoint payload is not JSON: {exc}"


def _report(endpoint_id: str, backend_url: str, token: Optional[str], result: dict) -> bool:
    if not backend_url:
        _log(
            f"WARNING: {BACKEND_URL_ENV} is unset; the probe result is printed "
            "below but not reported"
        )
        return False
    import requests

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{backend_url.rstrip('/')}/api/model-endpoints/{endpoint_id}/probe-result"
    try:
        response = requests.post(url, headers=headers, json=result, timeout=30)
    except Exception as exc:  # noqa: BLE001
        _log(f"ERROR: could not report the probe result: {type(exc).__name__}: {exc}")
        return False
    if 200 <= response.status_code < 300:
        _log(f"reported the probe result to {url}")
        return True
    _log(f"ERROR: the backend answered HTTP {response.status_code} for {url}")
    return False


def main() -> int:
    endpoint_id = os.environ.get(PROBE_ENDPOINT_ID_ENV)
    if not endpoint_id:
        print(
            f"[probe] ERROR: {PROBE_ENDPOINT_ID_ENV} is not set; this module is "
            "run by the backend's endpoint-probe pipeline, not by hand",
            file=sys.stderr,
            flush=True,
        )
        return 1

    backend_url = os.environ.get(BACKEND_URL_ENV) or ""
    token = os.environ.get(STEP_TOKEN_ENV) or None

    endpoint, reason = _load_endpoint(endpoint_id, backend_url, token)
    if endpoint is None:
        print(f"[probe] ERROR: {reason}", file=sys.stderr, flush=True)
        return 1

    auth_env = endpoint.get("auth_env") or endpoint.get("auth_secret_ref")
    api_key = os.environ.get(str(auth_env)) if auth_env else None

    _log(
        f"probing endpoint {endpoint.get('name')} "
        f"({endpoint.get('server_kind')}) at {endpoint.get('base_url')} "
        f"for model {endpoint.get('model')}"
    )
    result = run_probe(endpoint, api_key=api_key)
    _log(
        f"result: status={result['probe_status']} tools={result['supports_tools']} "
        f"stream={result['supports_streaming']} usage={result['reports_usage']} "
        f"ctx={result['context_window']} ({result['context_window_source']}) "
        f"in {result['elapsed_ms']}ms"
    )
    reported = _report(endpoint_id, backend_url, token, result)
    # THE PROBE IS AN OBSERVATION. "it is down" is a SUCCESSFUL observation, so
    # an unreachable endpoint still exits 0 as long as the result was reported;
    # failing to REPORT is the only failure this step has.
    return 0 if reported else 1


if __name__ == "__main__":
    sys.exit(main())
