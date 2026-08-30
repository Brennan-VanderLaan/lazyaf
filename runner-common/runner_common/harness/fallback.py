"""
The system prompts, and THE NO-TOOLS FALLBACK PROTOCOL
(Milestone 14.2, design sections 3.3 and 3.8).

WHY A TEXT PROTOCOL AT ALL. Plenty of genuinely useful self-hosted models
either do not implement the OpenAI ``tools`` parameter or implement it badly
enough that the server accepts it and the model emits prose anyway. Refusing
those models would remove most of the hardware this milestone exists to
support. So the harness asks for ONE fenced ``lazyaf`` block per reply, parses
it with six ordered rules, and corrects the model at most three consecutive
times before giving up LOUDLY with the raw response in the step log.

IT NEVER SILENTLY PASSES. On the fourth consecutive unparseable reply the step
is FAILED (exit 5), the usage row still lands with every token spent, and the
ENDPOINT's ``consecutive_failures`` is NOT bumped — this is a model-capability
failure, not an endpoint failure, and conflating them would make a working
endpoint look down.

Stdlib only.
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from .tools import TOOL_ORDER, TOOLS, validate_args

# --------------------------------------------------------------------------
# system prompts
# --------------------------------------------------------------------------

#: Short on purpose — every token here is paid on EVERY turn.
SYSTEM_PROMPT_TOOLS = """You are a software engineer working inside a git repository at {workdir}.
Complete the task using the tools provided. Work in small steps: read before
you write, and run the project's tests to check yourself.

Rules:
- Only the tools change anything. Describing an edit does not make it.
- When the task is done, call finish(status="success", summary=...).
- If the task cannot be done, call finish(status="blocked", summary=<why>).
- Do not commit or push. The platform commits your work for you.
- You have at most {max_iterations} turns."""


_PROMPT_TYPE_NAMES = {"string": "str", "integer": "int"}

#: ``finish`` is spelled with its enum rather than ``str`` because the status
#: vocabulary is the one argument a model gets wrong in a way the parser
#: cannot repair.
_PROMPT_SIGNATURE_OVERRIDES = {
    "finish": '{"status": "success"|"failed"|"blocked", "summary": str}',
}


def tool_menu() -> str:
    """The tool list for the text prompt, generated from the ONE tool table.

    Generated, not typed out: a seventh tool added to ``tools.TOOLS`` and not
    to this prompt would be a tool the fallback protocol accepts and never
    advertises.
    """
    width = max(len(name) for name in TOOL_ORDER)
    lines = []
    for name in TOOL_ORDER:
        override = _PROMPT_SIGNATURE_OVERRIDES.get(name)
        if override:
            signature = override
        else:
            spec = TOOLS[name]
            parts = ", ".join(
                f'"{arg.name}": {_PROMPT_TYPE_NAMES[arg.json_type]}'
                for arg in spec.args
            )
            signature = "{" + parts + "}"
        lines.append(f"  {name.ljust(width)}  {signature}")
    return "\n".join(lines)


SYSTEM_PROMPT_TEXT_TEMPLATE = """You are a software engineer working inside a git repository at {workdir}.
You cannot act directly. To act, emit EXACTLY ONE fenced block per reply, in
this format and nothing else after it:

```lazyaf
{{"tool": "read_file", "args": {{"path": "src/main.py", "start_line": 1, "max_lines": 200}}}}
```

Available tools:
{menu}

Rules:
- One block per reply. Prose before the block is allowed; nothing after it.
- The block must be valid JSON with exactly the keys "tool" and "args".
- Do not commit or push. The platform commits your work for you.
- When the task is done, emit a finish block.
- You have at most {max_iterations} turns."""


def system_prompt(mode: str, workdir: str, max_iterations: int) -> str:
    if mode == "text":
        return SYSTEM_PROMPT_TEXT_TEMPLATE.format(
            workdir=workdir, menu=tool_menu(), max_iterations=max_iterations
        )
    return SYSTEM_PROMPT_TOOLS.format(
        workdir=workdir, max_iterations=max_iterations
    )


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------

@dataclass
class Action:
    """A parsed, VALIDATED action. ``args`` are defaults-filled."""

    tool: str
    args: Dict[str, Any]
    warn: Optional[str] = None
    #: Present so a fallback action walks the same code path as a real
    #: ``tool_calls`` entry — the loop should not care which mode produced it.
    id: Optional[str] = None

    @property
    def name(self) -> str:
        return self.tool

    @property
    def arguments_raw(self) -> str:
        try:
            return json.dumps(self.args)
        except (TypeError, ValueError):
            return "{}"

    def arguments(self):
        return dict(self.args), None


@dataclass
class Malformed:
    """Why a reply could not be turned into an action."""

    reason: str
    detail: Optional[str] = None
    warn: Optional[str] = None
    raw: str = ""


#: Rule 1, verbatim from the design plus IGNORECASE for the info string.
BLOCK_RE = re.compile(
    r"^```[ \t]*lazyaf[ \t]*\r?\n(.*?)^```",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def _balanced_object(text: str) -> Optional[str]:
    """The span from the first ``{`` to its balanced ``}``, or None.

    Repairs the overwhelmingly common failure: trailing prose INSIDE the
    fence. One repair attempt, not a general JSON healer — a parser that
    guesses is a parser that eventually runs the wrong tool.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start: index + 1]
    return None


def parse_action(text: Any) -> Union[Action, Malformed]:
    """The six ordered rules of design section 3.8. NEVER raises."""
    body = text if isinstance(text, str) else ""
    raw = body[-2000:]

    # 1 + 2 + 3
    blocks = BLOCK_RE.findall(body)
    if not blocks:
        return Malformed(reason="no_block", raw=raw)
    warn = "multiple_blocks" if len(blocks) > 1 else None
    block = blocks[0]

    # 4
    payload = None
    try:
        payload = json.loads(block)
    except ValueError as first_error:
        repaired = _balanced_object(block)
        if repaired is not None:
            try:
                payload = json.loads(repaired)
            except ValueError as second_error:
                return Malformed(
                    reason="bad_json", detail=str(second_error), warn=warn, raw=raw
                )
        else:
            return Malformed(
                reason="bad_json", detail=str(first_error), warn=warn, raw=raw
            )

    if not isinstance(payload, dict):
        return Malformed(
            reason="bad_json",
            detail=(
                f"top level is a {type(payload).__name__}, expected an object "
                'with keys "tool" and "args"'
            ),
            warn=warn,
            raw=raw,
        )

    # 5
    name = payload.get("tool")
    if not isinstance(name, str) or name not in TOOLS:
        shown = name if isinstance(name, str) else repr(name)
        return Malformed(reason=f"unknown_tool: {shown}", warn=warn, raw=raw)

    # 6
    args = payload.get("args")
    if args is None:
        args = {}
    clean, reason = validate_args(TOOLS[name], args)
    if reason:
        return Malformed(reason=reason, warn=warn, raw=raw)

    return Action(tool=name, args=clean, warn=warn, id=None)


# --------------------------------------------------------------------------
# corrections
# --------------------------------------------------------------------------

_EXAMPLE = (
    'Reply with exactly one ```lazyaf block containing JSON with keys "tool" '
    'and "args", like this:\n'
    '```lazyaf\n'
    '{"tool": "read_file", "args": {"path": "README.md", "max_lines": 100}}\n'
    '```'
)


def correction_for(malformed: Malformed):
    """``(reason_phrase, example)`` for ``Transcript.append_correction``.

    ONE correction message per malformed reply, quoting the exact reason and
    re-stating the format with a one-line example. Quoting the reason is what
    makes the retry informative rather than a coin flip.
    """
    reason = malformed.reason or "unknown"
    if reason == "no_block":
        phrase = "no ```lazyaf block found"
    elif reason == "bad_json":
        detail = malformed.detail or "it was not valid JSON"
        phrase = f"the block was not valid JSON ({detail})"
    elif reason.startswith("unknown_tool: "):
        phrase = (
            f'unknown tool {reason.split(": ", 1)[1]!r}; the tools are '
            + ", ".join(TOOL_ORDER)
        )
    elif reason.startswith("missing_arg: "):
        phrase = f'missing required argument "{reason.split(": ", 1)[1]}"'
    elif reason.startswith("bad_arg_type: "):
        phrase = reason.split(": ", 1)[1]
    else:
        phrase = reason
    if malformed.warn == "multiple_blocks":
        phrase += (
            " (your reply contained more than one ```lazyaf block; only the "
            "first was read)"
        )
    return phrase, _EXAMPLE
