"""The nine named scenarios of wave8 section 8.1, and the turn planner.

A scenario is a pure function of the TURN NUMBER (and, for the tool-probe
request, of the tool schemas the caller sent). It returns a `Turn` saying what
this reply should be: a tool call, a fenced ```lazyaf block, plain prose, or an
HTTP error. The server renders that into OpenAI wire shapes.

| Scenario           | Behavior                                              | Covers                                    |
|--------------------|-------------------------------------------------------|-------------------------------------------|
| `happy_tools`      | the six-action script as real `tool_calls`             | the tools path end to end                 |
| `happy_text`       | the same six actions as ```lazyaf blocks               | the fallback protocol                     |
| `never_finishes`   | echoes `list_files` forever                            | stop condition 2 (iteration budget)       |
| `malformed`        | prose, prose, prose, then valid blocks                 | the malformed retry counter AND its reset |
| `malformed_forever`| prose only, forever                                    | stop condition 7 and exit 5               |
| `no_usage`         | the happy script with NO `usage` block                 | the null-tokens path + the scrape marker  |
| `lying_tools`      | tool-calls at PROBE time, ```lazyaf block in `content` | the probe-drift bridge and the demotion   |
| `slow`             | the happy script, 3s per turn                          | the soft deadline                         |
| `flaky_5xx`        | two 503s, then the happy script                        | the endpoint retry policy                 |

TOKEN ACCOUNTING IS THE LOAD-BEARING PART. Every reporting turn declares

    prompt_tokens     = MOCK_PROMPT_TOKENS_PER_TURN     * turn
    completion_tokens = MOCK_COMPLETION_TOKENS_PER_TURN * turn

which GROWS with the turn, exactly as a real transcript does. That makes the
difference between "the harness summed every turn" and "the harness kept the
last turn" a numeric fact:

    summed  over N turns = STEP * N * (N + 1) / 2
    largest single turn  = STEP * N

and `summed > largest` for every N >= 2. `scripts/verify_executor.py`
assertion 13 checks exactly that inequality against the endpoint's real usage
row, and `tdd/unit/scripts/test_verify_executor.py` pins the constants on both
sides so the two copies cannot drift (R3).
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# -----------------------------------------------------------------------------
# Token accounting (see the module docstring - this is contract, not decoration)
# -----------------------------------------------------------------------------

#: `prompt_tokens` reported on turn N is this times N.
MOCK_PROMPT_TOKENS_PER_TURN = 100
#: `completion_tokens` reported on turn N is this times N.
MOCK_COMPLETION_TOKENS_PER_TURN = 20


def largest_single_turn_tokens(turns: int) -> tuple[int, int]:
    """(prompt, completion) reported by the LARGEST single turn of a run.

    A harness that recorded only the last response would report exactly this
    pair. Anything that summed reports strictly more (for turns >= 2).
    """
    turns = max(int(turns or 0), 0)
    return (
        MOCK_PROMPT_TOKENS_PER_TURN * turns,
        MOCK_COMPLETION_TOKENS_PER_TURN * turns,
    )


def expected_summed_tokens(turns: int) -> tuple[int, int]:
    """(prompt, completion) a correct accumulator reports over `turns` turns."""
    turns = max(int(turns or 0), 0)
    triangular = turns * (turns + 1) // 2
    return (
        MOCK_PROMPT_TOKENS_PER_TURN * triangular,
        MOCK_COMPLETION_TOKENS_PER_TURN * triangular,
    )


# -----------------------------------------------------------------------------
# Model listing
# -----------------------------------------------------------------------------

#: Ids `GET /v1/models` advertises. The probe's `model_listed` check is a
#: containment test against these, and an endpoint registered with a model that
#: is NOT here probes `degraded` - which is itself a useful test case.
MOCK_MODELS: tuple[str, ...] = ("mock-model", "mock-model-notools")

#: Reported as `max_model_len` on every listed model (the vLLM spelling), and
#: as `*.context_length` by the ollama `/api/show` extension. Both discovery
#: paths therefore work against this one server.
MOCK_MODEL_CONTEXT_WINDOW = 32768


# -----------------------------------------------------------------------------
# The action script the "happy" scenarios drive
# -----------------------------------------------------------------------------

#: Where the scripted agent writes when the task text names no path.
DEFAULT_TARGET_PATH = ".lazyaf-dogfood/harness-ran"

#: The literal the script writes and then patches out. Having `apply_patch`
#: actually change a file already on disk is the only way this script covers
#: the find/replace tool rather than just naming it.
PLACEHOLDER = "PLACEHOLDER-ENDPOINT"

#: A task-text path looks like `a/b`, `.lazyaf-dogfood/x`, `src/main.py`.
#: First match wins; deterministic given the prompt.
_PATH_RE = re.compile(r"(?<![\w./-])((?:\.?[\w-]+/)+[\w][\w.-]*)")


def target_path(messages: list[dict]) -> str:
    """The file the scripted agent creates, read out of the task text.

    Deterministic and stateless: the same prompt always yields the same path,
    so a retried request replays identically. Falls back to
    `DEFAULT_TARGET_PATH` when the task names nothing that looks like one.
    """
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = _PATH_RE.search(content)
        if match:
            return match.group(1)
    return DEFAULT_TARGET_PATH


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def action_script(path: str, model: str) -> list[dict]:
    """The six actions every "happy" scenario performs, in order.

    Chosen to touch FIVE of the harness's six tools plus `finish`, with no
    dependency on what the repository contains - the only file it reads is the
    one it just wrote. A script that read `README.md` would pass in this repo
    and fail in the next one.
    """
    directory = _dirname(path)
    return [
        {"tool": "list_files", "args": {"path": ".", "depth": 1, "max_entries": 50}},
        {
            "tool": "run_shell",
            "args": {
                "command": f"mkdir -p {directory}" if directory else "true",
                "timeout": 30,
            },
        },
        {
            "tool": "write_file",
            "args": {
                "path": path,
                "content": (
                    f"lazyaf harness reached endpoint {PLACEHOLDER}\n"
                    "written by tdd/shared/mock_openai (no GPU involved)\n"
                ),
            },
        },
        {
            "tool": "apply_patch",
            "args": {
                "path": path,
                "find": PLACEHOLDER,
                "replace": model,
                "count": 1,
            },
        },
        {
            "tool": "read_file",
            "args": {"path": path, "start_line": 1, "max_lines": 20},
        },
        {
            "tool": "finish",
            "args": {
                "status": "success",
                "summary": f"wrote {path} using {model}",
            },
        },
    ]


#: How many turns a happy scenario takes. Any dogfood/T2 step driving one of
#: them needs `max_iterations` strictly greater than this.
ACTION_SCRIPT_LENGTH = 6

_PROSE = [
    "Let me think about this task before I do anything.",
    "I believe the right approach is to create the file the task describes.",
    "Actually, on reflection, I should just go ahead and write it.",
]


# -----------------------------------------------------------------------------
# Turn plan
# -----------------------------------------------------------------------------

@dataclass
class Turn:
    """What the server should reply with for one request.

    `kind` is one of:
      tool_calls  - an assistant message carrying `tool_calls` (tools mode)
      text_block  - an assistant message whose `content` is a ```lazyaf block
      prose       - an assistant message of plain prose and nothing else
      http_error  - do not answer; return `status` with `body`
    """

    kind: str
    actions: list[dict] = field(default_factory=list)
    text: str = ""
    include_usage: bool = True
    delay_seconds: float = 0.0
    status: int = 200
    body: dict | None = None
    finish_reason: str = "stop"


def turn_number(messages: list[dict]) -> int:
    """1-based turn index, derived from the transcript itself.

    Counting `assistant` messages is what makes this server stateless: turn 1
    is the request with no assistant messages yet, turn 2 is the one carrying
    the first reply, and so on. It is identical in tools mode (where results
    come back as `tool` messages) and in fallback mode (where they come back as
    `user` messages), so both modes replay the same script.
    """
    count = 0
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "assistant":
            count += 1
    return count + 1


def is_capability_probe(body: dict) -> bool:
    """True when this request is the CAPABILITY PROBE, not a harness turn.

    The probe is recognised by its own tool schema (a single function named
    `probe`, wave8 s2.1 request 2) or by its `stream_options.include_usage`
    liveness request. Scenarios need to tell the two apart because
    `lying_tools` exists precisely to answer them differently.
    """
    tools = body.get("tools")
    if isinstance(tools, list) and len(tools) == 1:
        function = tools[0].get("function") if isinstance(tools[0], dict) else None
        if isinstance(function, dict) and function.get("name") == "probe":
            return True
    # The streaming probe carries no tools at all and a tiny max_tokens.
    if not tools and body.get("stream") and body.get("max_tokens") == 8:
        return True
    return False


def probe_tool_turn() -> Turn:
    """A well-formed answer to the capability probe's tool request."""
    return Turn(
        kind="tool_calls",
        actions=[{"tool": "probe", "args": {"value": 7}}],
        finish_reason="tool_calls",
    )


def _script_turn(index: int, path: str, model: str, as_text: bool) -> Turn:
    script = action_script(path, model)
    action = script[min(index, len(script) - 1)]
    return Turn(
        kind="text_block" if as_text else "tool_calls",
        actions=[action],
        finish_reason="stop" if as_text else "tool_calls",
    )


# -----------------------------------------------------------------------------
# The nine scenarios
# -----------------------------------------------------------------------------

def _happy_tools(turn: int, body: dict, path: str, model: str) -> Turn:
    if is_capability_probe(body):
        return probe_tool_turn()
    return _script_turn(turn - 1, path, model, as_text=False)


def _happy_text(turn: int, body: dict, path: str, model: str) -> Turn:
    if is_capability_probe(body):
        # This endpoint genuinely cannot tool-call: the probe sees prose, so
        # `supports_tools` records False and `probe_status` records `degraded`
        # - which is USABLE, via the fallback protocol. That is the whole point
        # of the three-state capability.
        return Turn(kind="prose", text="Sure! I would call the probe tool if I could.")
    return _script_turn(turn - 1, path, model, as_text=True)


def _never_finishes(turn: int, body: dict, path: str, model: str) -> Turn:
    if is_capability_probe(body):
        return probe_tool_turn()
    return Turn(
        kind="tool_calls",
        actions=[{"tool": "list_files", "args": {"path": ".", "depth": 1}}],
        finish_reason="tool_calls",
    )


def _malformed(turn: int, body: dict, path: str, model: str) -> Turn:
    """Three consecutive unparseable replies, then valid ones.

    Three is deliberately one BELOW `MAX_MALFORMED_RETRIES = 3`'s failure
    point (the FOURTH consecutive malformed reply is what stops the step), so
    this scenario proves the counter RESETS on a successful parse rather than
    merely that it counts.
    """
    if is_capability_probe(body):
        return probe_tool_turn()
    if turn <= len(_PROSE):
        return Turn(kind="prose", text=_PROSE[turn - 1])
    return _script_turn(turn - len(_PROSE) - 1, path, model, as_text=True)


def _malformed_forever(turn: int, body: dict, path: str, model: str) -> Turn:
    if is_capability_probe(body):
        return probe_tool_turn()
    return Turn(kind="prose", text=_PROSE[(turn - 1) % len(_PROSE)])


def _no_usage(turn: int, body: dict, path: str, model: str) -> Turn:
    plan = _happy_tools(turn, body, path, model)
    plan.include_usage = False
    return plan


def _lying_tools(turn: int, body: dict, path: str, model: str) -> Turn:
    """Advertises tool calling at probe time and never does it in real work.

    The probe answers correctly, so the endpoint is stored with
    `supports_tools = True`; every harness turn then emits a ```lazyaf block
    in `content` with NO `tool_calls`. The harness's runtime bridge is
    supposed to re-parse that with the fallback parser, keep working, record
    `probe_drift`, and let `record_step_outcome` demote the endpoint.
    """
    if is_capability_probe(body):
        return probe_tool_turn()
    return _script_turn(turn - 1, path, model, as_text=True)


def _slow(turn: int, body: dict, path: str, model: str) -> Turn:
    plan = _happy_tools(turn, body, path, model)
    plan.delay_seconds = float(os.environ.get("LAZYAF_MOCK_SLOW_SECONDS", "3"))
    return plan


#: `flaky_5xx` is the ONE stateful scenario, and it has to be: "fail the first
#: two attempts" is not a function of the transcript, because a retried request
#: is byte-identical to the one that failed. The counter lives in this dict,
#: keyed by scenario name, and `POST /_control/reset` clears it.
_ATTEMPTS: dict[str, int] = {}
FLAKY_FAILURES = 2


def _flaky_5xx(turn: int, body: dict, path: str, model: str) -> Turn:
    if is_capability_probe(body):
        return probe_tool_turn()
    seen = _ATTEMPTS.get("flaky_5xx", 0)
    _ATTEMPTS["flaky_5xx"] = seen + 1
    if seen < FLAKY_FAILURES:
        return Turn(
            kind="http_error",
            status=503,
            body={
                "error": {
                    "message": (
                        f"mock: transient overload ({seen + 1} of "
                        f"{FLAKY_FAILURES} scripted failures)"
                    ),
                    "type": "server_error",
                }
            },
        )
    return _script_turn(turn - 1, path, model, as_text=False)


def reset_state() -> None:
    """Clear the one piece of cross-request state (`flaky_5xx`'s counter)."""
    _ATTEMPTS.clear()


SCENARIOS: dict[str, Callable[[int, dict, str, str], Turn]] = {
    "happy_tools": _happy_tools,
    "happy_text": _happy_text,
    "never_finishes": _never_finishes,
    "malformed": _malformed,
    "malformed_forever": _malformed_forever,
    "no_usage": _no_usage,
    "lying_tools": _lying_tools,
    "slow": _slow,
    "flaky_5xx": _flaky_5xx,
}


def plan_turn(scenario: str, body: dict) -> Turn:
    """Resolve one request to a `Turn`. Raises KeyError on an unknown name."""
    handler = SCENARIOS[scenario]
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    model = str(body.get("model") or MOCK_MODELS[0])
    return handler(turn_number(messages), body, target_path(messages), model)


def render_block(action: dict) -> str:
    """The ```lazyaf fenced block spelling of one action (fallback protocol)."""
    payload = json.dumps({"tool": action["tool"], "args": action.get("args") or {}})
    return f"```lazyaf\n{payload}\n```"


def usage_block(turn: int, prompt_override: int | None = None) -> dict[str, Any]:
    """The `usage` object a reporting turn carries."""
    prompt = (
        prompt_override
        if prompt_override is not None
        else MOCK_PROMPT_TOKENS_PER_TURN * turn
    )
    completion = MOCK_COMPLETION_TOKENS_PER_TURN * turn
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
