"""The stdlib HTTP server that renders `scenarios.Turn` into OpenAI wire shapes.

Routes (every one of them under an optional `/<scenario>` prefix):

    GET  /<scenario>/v1/models             the probe's request 1
    POST /<scenario>/v1/chat/completions   requests 2 and 3, and every harness turn
    POST /<scenario>/api/show              ollama's context-window extension
    GET  /health                           readiness for compose / tests
    GET  /_control/scenarios               the scenario names this build serves
    POST /_control/reset                   clears `flaky_5xx`'s counter

`/v1/...` with no prefix uses the process default scenario
(`--scenario`, or `$LAZYAF_MOCK_SCENARIO`, default `happy_tools`).

WHY http.server AND NOT FASTAPI: this runs in a bare `python:3.12-slim`
container in compose and as an in-process fixture inside T1, where importing
the backend's dependency tree would be both slow and a lie about what is being
tested. Stdlib only, no third-party imports, no shared state with the app.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scenarios import (
    MOCK_MODEL_CONTEXT_WINDOW,
    MOCK_MODELS,
    SCENARIOS,
    Turn,
    plan_show,
    plan_turn,
    render_block,
    reset_state,
    usage_block,
)

DEFAULT_PORT = 8099
DEFAULT_SCENARIO = "happy_tools"

#: Optional. When set, every /v1 request must carry `Authorization: Bearer
#: <this>` (or the header named by LAZYAF_MOCK_AUTH_HEADER) or gets a 401. It
#: exists so the endpoint auth path can be exercised without a real provider -
#: and so a test can prove a MISSING key produces an honest 401 rather than a
#: silent success.
ENV_API_KEY = "LAZYAF_MOCK_API_KEY"
ENV_AUTH_HEADER = "LAZYAF_MOCK_AUTH_HEADER"
ENV_SCENARIO = "LAZYAF_MOCK_SCENARIO"

MAX_BODY_BYTES = 8 * 1024 * 1024


def _now() -> int:
    return int(time.time())


class _Handler(BaseHTTPRequestHandler):
    server_version = "lazyaf-mock-openai/1"
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: D102 - quiet by default
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": {"message": message, "type": "invalid_request_error"}})

    def _drain(self) -> bytes:
        """Read the whole request body off the socket. See do_POST."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(min(length, MAX_BODY_BYTES))

    def _parse_json(self, raw: bytes) -> dict | None:
        if int(self.headers.get("Content-Length") or 0) > MAX_BODY_BYTES:
            self._error(413, "request body too large")
            return None
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError as exc:
            self._error(400, f"body is not JSON: {exc}")
            return None
        return parsed if isinstance(parsed, dict) else {}

    def _split_path(self) -> tuple[str, str]:
        """(scenario, remainder). `/happy_text/v1/models` -> (happy_text, /v1/models)."""
        path = self.path.split("?", 1)[0]
        head = path.lstrip("/").split("/", 1)
        if head and head[0] in SCENARIOS:
            return head[0], "/" + (head[1] if len(head) > 1 else "")
        return self.server.default_scenario, path  # type: ignore[attr-defined]

    def _auth_ok(self) -> bool:
        expected = self.server.api_key  # type: ignore[attr-defined]
        if not expected:
            return True
        header = self.server.auth_header  # type: ignore[attr-defined]
        value = self.headers.get(header) or ""
        if header.lower() == "authorization":
            return value.strip() == f"Bearer {expected}"
        return value.strip() == expected

    # ---- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        scenario, rest = self._split_path()
        if rest == "/health":
            self._send(200, {"status": "ok", "scenarios": sorted(SCENARIOS)})
            return
        if rest == "/_control/scenarios":
            self._send(
                200,
                {
                    "scenarios": sorted(SCENARIOS),
                    "default": self.server.default_scenario,  # type: ignore[attr-defined]
                    "models": list(MOCK_MODELS),
                },
            )
            return
        if rest in ("/v1/models", "/models"):
            if not self._auth_ok():
                self._error(401, "missing or wrong api key")
                return
            self._send(200, self._models_payload())
            return
        self._error(404, self._not_found_message(rest))

    def do_POST(self):  # noqa: N802
        scenario, rest = self._split_path()
        # DRAIN THE BODY FIRST, ALWAYS. Under HTTP/1.1 keep-alive, replying
        # without consuming the request body leaves bytes in the socket
        # buffer; closing a socket with unread data sends an RST rather than a
        # FIN, and the client sees a connection reset instead of the 200 the
        # server logged. That is a real bug this server hit, not a hypothetical.
        raw_body = self._drain()
        if rest == "/_control/reset":
            reset_state()
            self._send(200, {"reset": True})
            return
        if not self._auth_ok():
            self._error(401, "missing or wrong api key")
            return
        if rest in ("/v1/chat/completions", "/chat/completions"):
            body = self._parse_json(raw_body)
            if body is None:
                return
            self._chat(scenario, body)
            return
        if rest == "/api/show":
            # ollama's NAMED vendor extension - the probe attempts it for
            # `server_kind == "ollama"` only. The payload is SCENARIO-SCOPED
            # (M14.6): most scenarios omit `capabilities` entirely, which is
            # what every ollama before v0.6 does and what the probe has to
            # read as "we do not know" rather than as "no vision".
            self._send(200, plan_show(scenario))
            return
        self._error(404, self._not_found_message(rest))

    def _not_found_message(self, rest: str) -> str:
        return (
            f"no route {rest!r}. This is the LazyAF mock OpenAI server; it "
            f"serves /<scenario>/v1/models, /<scenario>/v1/chat/completions "
            f"and /<scenario>/api/show. Known scenarios: "
            f"{', '.join(sorted(SCENARIOS))}"
        )

    def _models_payload(self) -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "lazyaf-mock",
                    # The vLLM spelling the probe harvests for context
                    # discovery when there is no operator override.
                    "max_model_len": MOCK_MODEL_CONTEXT_WINDOW,
                }
                for name in MOCK_MODELS
            ],
        }

    # ---- chat/completions --------------------------------------------------

    def _chat(self, scenario: str, body: dict) -> None:
        try:
            plan = plan_turn(scenario, body)
        except KeyError:
            self._error(404, f"unknown scenario {scenario!r}")
            return

        if plan.delay_seconds:
            time.sleep(plan.delay_seconds)

        if plan.kind == "http_error":
            self._send(plan.status, plan.body or {"error": {"message": "mock error"}})
            return

        model = str(body.get("model") or MOCK_MODELS[0])
        turn = _turn_of(body)
        include_usage = plan.include_usage
        if body.get("stream"):
            options = body.get("stream_options")
            want_usage = not isinstance(options, dict) or bool(
                options.get("include_usage", True)
            )
            self._stream(plan, model, turn, include_usage and want_usage)
        else:
            self._send(200, _completion_payload(plan, model, turn, include_usage))

    def _stream(self, plan: Turn, model: str, turn: int, include_usage: bool) -> None:
        frames = _stream_frames(plan, model, turn, include_usage)
        chunks = b"".join(
            f"data: {json.dumps(frame)}\n\n".encode() for frame in frames
        ) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(chunks)))
        self.end_headers()
        self.wfile.write(chunks)


def _turn_of(body: dict) -> int:
    from .scenarios import turn_number

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    return turn_number(messages)


def _message_for(plan: Turn, model: str) -> tuple[dict, str]:
    """(assistant message, finish_reason) for a non-streaming reply."""
    if plan.kind == "tool_calls":
        calls = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": action["tool"],
                    # OpenAI sends arguments as a JSON STRING, not an object.
                    # Sending an object is a real-world variation the probe
                    # tolerates, but the mock speaks the canonical shape.
                    "arguments": json.dumps(action.get("args") or {}),
                },
            }
            for index, action in enumerate(plan.actions)
        ]
        return {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
    if plan.kind == "text_block":
        blocks = "\n".join(render_block(action) for action in plan.actions)
        return {"role": "assistant", "content": f"{blocks}"}, "stop"
    return {"role": "assistant", "content": plan.text}, "stop"


def _completion_payload(plan: Turn, model: str, turn: int, include_usage: bool) -> dict:
    message, finish_reason = _message_for(plan, model)
    payload = {
        "id": f"chatcmpl-mock-{turn}",
        "object": "chat.completion",
        "created": _now(),
        # The RESOLVED model tag, which is what `model_version` is for: a real
        # ollama answers `qwen2.5-coder:32b` here even when asked for
        # `qwen2.5-coder`.
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if include_usage:
        payload["usage"] = _usage_for(plan, turn)
    return payload


def _usage_for(plan: Turn, turn: int) -> dict:
    """The `usage` block for one reply.

    A modality probe carries its own (M14.6): its contract is a controlled
    `prompt_tokens` DELTA against a matched control request, and a
    turn-derived count would move underneath that comparison.
    """
    return plan.usage_override if plan.usage_override is not None else usage_block(turn)


def _stream_frames(plan: Turn, model: str, turn: int, include_usage: bool) -> list[dict]:
    def frame(delta: dict, finish_reason=None) -> dict:
        return {
            "id": f"chatcmpl-mock-{turn}",
            "object": "chat.completion.chunk",
            "created": _now(),
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }

    frames = [frame({"role": "assistant"})]
    if plan.kind == "tool_calls":
        for index, action in enumerate(plan.actions):
            frames.append(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"call_{index}",
                                "type": "function",
                                "function": {
                                    "name": action["tool"],
                                    "arguments": json.dumps(action.get("args") or {}),
                                },
                            }
                        ]
                    }
                )
            )
        frames.append(frame({}, finish_reason="tool_calls"))
    else:
        message, _ = _message_for(plan, model)
        text = message.get("content") or ""
        # Two chunks, so a consumer that only ever reads the first delta is a
        # test failure rather than a coincidence.
        midpoint = max(len(text) // 2, 1)
        for piece in (text[:midpoint], text[midpoint:]):
            if piece:
                frames.append(frame({"content": piece}))
        frames.append(frame({}, finish_reason="stop"))

    if include_usage:
        final = frame({})
        final["choices"] = []
        final["usage"] = _usage_for(plan, turn)
        frames.append(final)
    return frames


class MockOpenAIServer:
    """An in-process mock endpoint. Use as a context manager in fixtures.

        with MockOpenAIServer() as srv:
            register_endpoint(base_url=srv.base_url("happy_tools"))

    `host` defaults to 0.0.0.0 so a SIBLING container can reach it under DooD;
    use `tdd.integration.conftest.advertise_addr()` to work out the address to
    hand a container (this repo already solved that - do not re-solve it).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 0,
        default_scenario: str = DEFAULT_SCENARIO,
        api_key: str | None = None,
        auth_header: str = "authorization",
        verbose: bool = False,
    ) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.default_scenario = default_scenario  # type: ignore[attr-defined]
        self._httpd.api_key = api_key  # type: ignore[attr-defined]
        self._httpd.auth_header = auth_header  # type: ignore[attr-defined]
        self._httpd.verbose = verbose  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def base_url(self, scenario: str = "", host: str = "127.0.0.1") -> str:
        """The OpenAI-compatible ROOT (including `/v1`) for one scenario."""
        prefix = f"/{scenario}" if scenario else ""
        return f"http://{host}:{self.port}{prefix}/v1"

    def start(self) -> "MockOpenAIServer":
        reset_state()
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockOpenAIServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def build_server(argv: list[str] | None = None) -> MockOpenAIServer:
    """Construct a server from argv/env - used by `__main__` and by compose."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m tdd.shared.mock_openai")
    parser.add_argument("--host", default=os.environ.get("LAZYAF_MOCK_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LAZYAF_MOCK_PORT", DEFAULT_PORT)),
    )
    parser.add_argument(
        "--scenario",
        default=os.environ.get(ENV_SCENARIO, DEFAULT_SCENARIO),
        choices=sorted(SCENARIOS),
        help="scenario served at the bare /v1 prefix",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return MockOpenAIServer(
        host=args.host,
        port=args.port,
        default_scenario=args.scenario,
        api_key=os.environ.get(ENV_API_KEY) or None,
        auth_header=os.environ.get(ENV_AUTH_HEADER, "authorization"),
        verbose=args.verbose,
    )
