"""
A REAL OpenAI-compatible server, in-process (Milestone 14.2 test support).

The harness's only external dependency is an HTTP session with ``.post`` and
``.get``. This module supplies one that answers with byte-exact OpenAI
response shapes, records every request it was handed, and can be scripted per
turn — which is what lets the loop tests drive the ACTUAL loop, the ACTUAL
transcript and the ACTUAL tools against deterministic model behaviour instead
of stubbing the thing under test.

The scenario names mirror ``tdd/support/mock_openai_server.py``'s (design
section 8.1) on purpose: a scenario that behaves differently here and there is
a bug in one of them, and matching names make that visible.
"""
import json
from typing import Any, Dict, List, Optional

#: Per-turn token numbers are all DISTINCT and none is the sum of the others,
#: so a last-response-wins accumulator bug cannot accidentally pass.
DEFAULT_USAGE_SERIES = (
    {"prompt_tokens": 101, "completion_tokens": 11},
    {"prompt_tokens": 203, "completion_tokens": 23},
    {"prompt_tokens": 307, "completion_tokens": 37},
    {"prompt_tokens": 409, "completion_tokens": 41},
    {"prompt_tokens": 503, "completion_tokens": 53},
    {"prompt_tokens": 601, "completion_tokens": 67},
    {"prompt_tokens": 701, "completion_tokens": 71},
    {"prompt_tokens": 809, "completion_tokens": 83},
)


class FakeResponse:
    """The subset of ``requests.Response`` the client actually touches."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: Optional[str] = None,
        lines: Optional[List[str]] = None,
    ):
        self.status_code = status_code
        self._payload = payload
        self._lines = lines
        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def iter_lines(self):
        for line in self._lines or []:
            yield line


class FakeSession:
    """Scripted stand-in for ``requests.Session``.

    ``script`` is a list of ``FakeResponse`` or callables taking the request
    body and returning one. The LAST entry repeats, so a test that only cares
    about the first two turns does not have to pad.
    """

    def __init__(self, script=None, get_script=None):
        self.script = list(script or [])
        self.get_script = list(get_script or [])
        self.requests: List[Dict[str, Any]] = []
        self.gets: List[Dict[str, Any]] = []

    def _next(self, script, index):
        if not script:
            return FakeResponse(200, {"choices": [{"message": {"content": ""}}]})
        return script[index] if index < len(script) else script[-1]

    def post(self, url, headers=None, json=None, timeout=None, stream=False, **kwargs):
        entry = self._next(self.script, len(self.requests))
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "body": json,
                "stream": stream,
                "timeout": timeout,
            }
        )
        return entry(json) if callable(entry) else entry

    def get(self, url, headers=None, timeout=None, **kwargs):
        entry = self._next(self.get_script, len(self.gets))
        self.gets.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        return entry() if callable(entry) else entry

    # -- convenience -------------------------------------------------------

    @property
    def bodies(self):
        return [request["body"] for request in self.requests]

    @property
    def sent_headers(self):
        return [request["headers"] for request in self.requests]


# --------------------------------------------------------------------------
# response builders
# --------------------------------------------------------------------------

def tool_call(name: str, args: Dict[str, Any], call_id: Optional[str] = None) -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def chat_payload(
    content: str = "",
    tool_calls: Optional[List[dict]] = None,
    usage: Optional[dict] = None,
    model: str = "test-model",
    finish_reason: str = "stop",
) -> dict:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    payload: Dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def chat_response(
    content: str = "",
    tool_calls: Optional[List[dict]] = None,
    usage: Optional[dict] = None,
    model: str = "test-model",
    status: int = 200,
) -> FakeResponse:
    return FakeResponse(
        status,
        chat_payload(content=content, tool_calls=tool_calls, usage=usage, model=model),
    )


def sse_response(
    chunks: List[str],
    usage: Optional[dict] = None,
    tool_calls: Optional[List[dict]] = None,
    model: str = "test-model",
) -> FakeResponse:
    """A streamed turn as real SSE lines, ``[DONE]`` included."""
    lines: List[str] = []
    for chunk in chunks:
        frame = {
            "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk}}],
        }
        lines.append("data: " + json.dumps(frame))
    for index, call in enumerate(tool_calls or []):
        # Fragmented on purpose: name on the first fragment, arguments split
        # across two, exactly as vLLM and ollama emit them.
        function = call["function"]
        lines.append(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": call["id"],
                                        "type": "function",
                                        "function": {"name": function["name"], "arguments": ""},
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        )
        half = len(function["arguments"]) // 2
        for part in (function["arguments"][:half], function["arguments"][half:]):
            lines.append(
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": index, "function": {"arguments": part}}
                                    ]
                                },
                            }
                        ]
                    }
                )
            )
    lines.append(
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    )
    if usage is not None:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return FakeResponse(200, lines=lines)


def lazyaf_block(tool: str, args: Dict[str, Any], prose: str = "") -> str:
    """One fenced ``lazyaf`` block, the way the fallback protocol asks for it."""
    body = json.dumps({"tool": tool, "args": args})
    prefix = f"{prose}\n\n" if prose else ""
    return f"{prefix}```lazyaf\n{body}\n```"


# --------------------------------------------------------------------------
# endpoint / harness wire blocks
# --------------------------------------------------------------------------

API_KEY_ENV = "LAZYAF_ENDPOINT_API_KEY"
SENTINEL_KEY = "sk-lazyafSENTINEL0123456789"


def endpoint_block(**overrides) -> dict:
    """A wire-shaped ``endpoint`` block (design section 4.1)."""
    block = {
        "id": "e7c1a4b2-0000-4000-8000-000000000001",
        "name": "local-4090",
        "base_url": "http://172.17.0.1:11434/v1",
        "model": "qwen2.5-coder:32b",
        "server_kind": "ollama",
        "reach": "runner-local",
        "auth_style": "none",
        "auth_env": None,
        "auth_header": None,
        "request_timeout_seconds": 300,
        "capabilities": {
            "supports_tools": True,
            "supports_streaming": True,
            "reports_usage": True,
            "context_window": 32768,
            "max_output_tokens": 4096,
            "probe_status": "ok",
            "probed_at": "2026-08-30T09:14:22Z",
            "probed_from": "runner:workshop-1",
            "probe_age_seconds": 3821,
            "stale": False,
        },
        "pricing": {
            "gpu_node_id": "endpoint:local-4090",
            "gpu_fraction": 1.0,
            "priced": True,
        },
    }
    capabilities = overrides.pop("capabilities", None)
    pricing = overrides.pop("pricing", None)
    if capabilities:
        block["capabilities"].update(capabilities)
    if pricing:
        block["pricing"].update(pricing)
    block.update(overrides)
    return block


def harness_block(**overrides) -> dict:
    """A wire-shaped ``harness`` block (design section 4.1)."""
    block = {
        "mode": "auto",
        "max_iterations": 40,
        "max_total_tokens": 400000,
        "time_budget_seconds": 1740,
        "max_tool_calls_per_turn": 4,
        "shell_timeout_seconds": 120,
        "tool_output_max_bytes": 8192,
        "temperature": 0,
        "top_p": None,
        "seed": 7,
        "require_changes": True,
        "debug_transcript": False,
    }
    block.update(overrides)
    return block


# --------------------------------------------------------------------------
# driving the REAL executor against the fake server
# --------------------------------------------------------------------------

def make_repo(tmp_path):
    """A REAL git repository with one commit, so ``git status --porcelain``
    answers truthfully (design section 3.5's change check is not stubbed)."""
    import subprocess
    from pathlib import Path

    repo = Path(tmp_path) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# fixture repo" + chr(10), encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "main.py").write_text(
        chr(10).join(["def main():", "    return 1", ""]), encoding="utf-8"
    )
    env = {"GIT_CONFIG_GLOBAL": str(Path(tmp_path) / "gitconfig"),
           "GIT_CONFIG_SYSTEM": str(Path(tmp_path) / "gitsystem")}
    import os

    child = {**os.environ, **env}
    for args in (
        ["init", "-q"],
        ["config", "user.email", "fixture@lazyaf.local"],
        ["config", "user.name", "fixture"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git"] + args, cwd=str(repo), capture_output=True, env=child)
    return repo


def run_harness(
    repo,
    script,
    *,
    endpoint=None,
    harness=None,
    streaming=False,
    env=None,
    prompt="Create a file called done.txt containing the word done.",
    session=None,
):
    """Drive the REAL ``HarnessExecutor`` against a scripted fake server.

    Returns ``(result, logs, session, executor)``.
    """
    from runner_common.executors import ExecutorConfig
    from runner_common.harness import HarnessExecutor
    from runner_common.harness.client import OpenAICompatClient

    session = session if session is not None else FakeSession(script)
    logs = []

    def factory(**kwargs):
        kwargs["session"] = session
        kwargs["sleep"] = lambda seconds: None
        kwargs["rand"] = lambda: 0.0
        return OpenAICompatClient(**kwargs)

    executor = HarnessExecutor(
        endpoint=endpoint if endpoint is not None else endpoint_block(),
        harness=harness if harness is not None else harness_block(),
        client_factory=factory,
        env=env,
    )
    result = executor.execute(
        ExecutorConfig(workspace=repo, prompt=prompt, timeout=None, env={}),
        log_callback=logs.append,
        streaming=streaming,
    )
    return result, logs, session, executor


def make_context(
    repo,
    script,
    *,
    mode="tools",
    endpoint=None,
    harness=None,
    clock=None,
    logs=None,
    session=None,
    **ctx_kwargs,
):
    """Build a REAL ``HarnessContext`` over a scripted fake server.

    Used where a test needs to inject a clock or flip ``cancelled`` mid-run —
    seams the executor does not expose because production has no use for them.
    Returns ``(ctx, logs, session)``.
    """
    from runner_common.harness.client import OpenAICompatClient
    from runner_common.harness.fallback import system_prompt
    from runner_common.harness.loop import HarnessContext
    from runner_common.harness.tools import Sandbox
    from runner_common.harness.transcript import Transcript
    from runner_common.usage import TokenAccumulator

    endpoint = endpoint if endpoint is not None else endpoint_block()
    harness = harness if harness is not None else harness_block()
    logs = logs if logs is not None else []
    session = session if session is not None else FakeSession(script)

    client = OpenAICompatClient(
        base_url=endpoint["base_url"],
        model=endpoint["model"],
        session=session,
        sleep=lambda seconds: None,
        rand=lambda: 0.0,
    )
    sandbox = Sandbox(workdir=repo)
    max_iterations = int(harness.get("max_iterations") or 40)
    transcript = Transcript(
        system=system_prompt(mode, str(sandbox.workdir), max_iterations),
        task="Do the thing.",
        context_window=(endpoint.get("capabilities") or {}).get("context_window"),
        max_output_tokens=(endpoint.get("capabilities") or {}).get("max_output_tokens"),
        endpoint_name=endpoint.get("name") or "endpoint",
        log=lambda message: logs.append("[agent] " + message),
    )
    kwargs = dict(
        client=client,
        sandbox=sandbox,
        transcript=transcript,
        accumulator=TokenAccumulator(),
        endpoint=endpoint,
        mode=mode,
        max_iterations=max_iterations,
        max_total_tokens=int(harness.get("max_total_tokens") or 400000),
        time_budget_seconds=harness.get("time_budget_seconds"),
        log=logs.append,
    )
    if clock is not None:
        kwargs["clock"] = clock
    kwargs.update(ctx_kwargs)
    return HarnessContext(**kwargs), logs, session
