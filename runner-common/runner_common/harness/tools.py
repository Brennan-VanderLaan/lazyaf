"""
The six tools, their schemas, their argument validation and their sandbox
(Milestone 14.2, design section 3.1).

SIX, and no more. Every additional tool costs schema tokens in EVERY request,
which on an 8k-context model is a real budget line — so ``search`` is
``run_shell("grep -rn ...")``, ``delete_file`` is ``run_shell("rm ...")``
(which also puts the deletion verbatim in the step log where an operator can
see it), and ``git_commit`` does not exist because the platform commits.

EVERYTHING HERE RETURNS A ``ToolResult``; nothing raises. A tool error is a
MESSAGE THE MODEL CAN LEARN FROM — "path escapes the workspace", "`find`
matched 0 occurrences, nearest line 88: ..." — and a traceback is not.

Stdlib only.
"""
import difflib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .constants import TOOL_OUTPUT_MAX_BYTES, TOOL_SHELL_TIMEOUT

#: Directories a listing never descends into. Not a security control (the
#: sandbox below is): a listing that spends a small model's whole context on
#: ``.git/objects`` is useless to it.
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"})

#: Denied even though it is outside the workdir anyway — belt and braces,
#: because that directory holds a sibling step's config and the usage manifest.
CONTROL_DIR_NAME = ".control"

#: The env vars a shell command may keep. The model has no business holding
#: the endpoint key or the step's identity; these two are kept because a test
#: command that wants to label its own output legitimately reads them.
SHELL_ENV_LAZYAF_ALLOWLIST = frozenset(
    {"LAZYAF_PIPELINE_RUN_ID", "LAZYAF_STEP_RUN_ID"}
)

#: Stated, explicit, and small (section 3.1 rule 4).
SHELL_DENY_MESSAGE = (
    "the platform commits and pushes this step's work; do not push"
)

#: Timeout exit code, the conventional shell value for "killed by SIGTERM at
#: 124 seconds" that ``timeout(1)`` uses. A timeout is a RESULT, like a failing
#: test is a result.
SHELL_TIMEOUT_EXIT = 124


# --------------------------------------------------------------------------
# result + argument specs
# --------------------------------------------------------------------------

@dataclass
class ToolResult:
    """What one tool call produced.

    ``is_error`` is about the TOOL, not about the world: a failing test suite
    is ``is_error=False`` with ``exit_code=1`` in its text, because "the tests
    failed" is the single most useful observation the loop can make.
    """

    text: str
    is_error: bool = False
    summary: str = ""

    def __post_init__(self):
        if self.summary:
            return
        lines = (self.text or "").strip().splitlines()
        self.summary = lines[0][:200] if lines else ""


@dataclass(frozen=True)
class ArgSpec:
    name: str
    json_type: str  # "string" | "integer"
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: Tuple[ArgSpec, ...]
    run: Optional[Callable[[Any, Dict[str, Any]], ToolResult]] = None

    @property
    def required_names(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.args if spec.required)

    def schema(self) -> Dict[str, Any]:
        """The OpenAI ``tools[]`` entry for this tool."""
        properties = {}
        for spec in self.args:
            entry: Dict[str, Any] = {
                "type": spec.json_type,
                "description": spec.description,
            }
            properties[spec.name] = entry
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.required_names),
                },
            },
        }

    def signature(self) -> str:
        """The one-line spelling used in the fallback system prompt."""
        parts = ", ".join(f'"{a.name}": {a.json_type}' for a in self.args)
        return "{" + parts + "}"


_JSON_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    # bools are ints in Python; they are NOT integers on this wire.
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
}


def validate_args(spec: ToolSpec, args: Any):
    """``(clean_args, reason)`` — reason is None when the call is usable.

    The reason strings are the ones section 3.8 rule 6 names verbatim, because
    they are quoted back to the model in the correction message and that is
    the feedback loop that makes the retry work.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, f"bad_args: expected an object, got {type(args).__name__}"
    clean: Dict[str, Any] = {}
    for arg in spec.args:
        if arg.name not in args or args[arg.name] is None:
            if arg.required:
                return None, f"missing_arg: {arg.name}"
            clean[arg.name] = arg.default
            continue
        value = args[arg.name]
        if not _JSON_TYPE_CHECKS[arg.json_type](value):
            return None, f"bad_arg_type: {arg.name} expected {arg.json_type}"
        clean[arg.name] = value
    return clean, None


# --------------------------------------------------------------------------
# the sandbox
# --------------------------------------------------------------------------

@dataclass
class Sandbox:
    """Everything a tool is allowed to touch, and nothing else."""

    workdir: Path
    shell_timeout: int = TOOL_SHELL_TIMEOUT
    output_max_bytes: int = TOOL_OUTPUT_MAX_BYTES
    #: The NAME of the env var holding the endpoint key. Stripped from every
    #: shell child's environment.
    api_key_env: Optional[str] = None
    #: The VALUE, stripped from the child's environment wherever it appears
    #: under any other name too (belt and braces).
    api_key_value: Optional[str] = None
    #: The step's git remote. A command carrying its credential form is denied.
    remote_url: Optional[str] = None
    #: Overridable so the unit suite can drive a shell that exists on the host.
    shell: Tuple[str, ...] = ("bash", "-lc")
    base_env: Dict[str, str] = field(default_factory=lambda: dict(os.environ))

    def __post_init__(self):
        self.workdir = Path(os.path.realpath(str(self.workdir)))

    # -- paths -------------------------------------------------------------

    def resolve(self, raw_path: Any):
        """``(Path, reason)``. Every ``path`` argument goes through here.

        ``os.path.realpath`` FIRST, so a symlink pointing out of the workspace
        is caught by the same check as ``../../etc/passwd``.
        """
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None, "path must be a non-empty string"
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        resolved = Path(os.path.realpath(str(candidate)))

        if CONTROL_DIR_NAME in resolved.parts:
            return None, (
                f"{CONTROL_DIR_NAME} is the platform's control directory and is "
                "denied to the agent"
            )

        try:
            resolved.relative_to(self.workdir)
        except ValueError:
            return None, (
                f"path escapes the workspace ({self.workdir}); "
                "the agent may only read and write inside the repository"
            )
        return resolved, None

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.workdir).as_posix() or "."
        except ValueError:
            return str(path)

    # -- output ------------------------------------------------------------

    def cap(self, text: str, limit: Optional[int] = None) -> str:
        """Head+tail elision with a middle marker. Never raises."""
        limit = self.output_max_bytes if limit is None else limit
        return cap_output(text, limit)

    # -- shell -------------------------------------------------------------

    def shell_env(self) -> Dict[str, str]:
        """The step's own environment MINUS the endpoint key and MINUS every
        ``LAZYAF_*`` that is not the run/step id (section 3.1 rule 3)."""
        env: Dict[str, str] = {}
        for key, value in (self.base_env or {}).items():
            if self.api_key_env and key == self.api_key_env:
                continue
            if (
                self.api_key_value
                and isinstance(value, str)
                and value
                and value == self.api_key_value
            ):
                continue
            if key.startswith("LAZYAF_") and key not in SHELL_ENV_LAZYAF_ALLOWLIST:
                continue
            env[key] = value
        return env

    def denied_reason(self, command: str) -> Optional[str]:
        return shell_denial_reason(command, self.remote_url)


def cap_output(text: Any, limit: int) -> str:
    """Head+tail elide ``text`` to ``limit`` BYTES with a middle marker."""
    try:
        raw = text if isinstance(text, str) else str(text)
    except Exception:  # noqa: BLE001
        return ""
    data = raw.encode("utf-8", "replace")
    if limit <= 0 or len(data) <= limit:
        return raw
    head = limit // 2
    tail = limit - head
    elided = len(data) - limit
    return (
        data[:head].decode("utf-8", "replace")
        + f"\n...[{elided} bytes elided]...\n"
        + data[len(data) - tail:].decode("utf-8", "replace")
    )


# --------------------------------------------------------------------------
# the shell denylist
# --------------------------------------------------------------------------

_SEGMENT_SPLIT = ("&&", "||", ";", "|", "\n", "&")


def _segments(command: str) -> List[str]:
    parts = [command]
    for separator in _SEGMENT_SPLIT:
        expanded: List[str] = []
        for part in parts:
            expanded.extend(part.split(separator))
        parts = expanded
    return [part.strip() for part in parts if part.strip()]


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def shell_denial_reason(command: Any, remote_url: Optional[str] = None) -> Optional[str]:
    """Why this command is refused, or None.

    RATIONALE, stated because a denylist without one rots: a model pushing to
    the run's own trigger branch RE-FIRES the push trigger that started the
    run — the exact loop ``resolve_agent_work_branch`` exists to prevent.
    ``git add`` / ``commit`` / ``status`` / ``diff`` stay allowed and are
    harmless: the wrapper's later commit simply finds nothing to add.
    """
    if not isinstance(command, str) or not command.strip():
        return "command must be a non-empty string"

    if remote_url and _credentialed(remote_url) and remote_url in command:
        return (
            f"the command carries the step's git credentials; {SHELL_DENY_MESSAGE}"
        )
    if _credentialed(command):
        return (
            f"the command carries an embedded credential in a URL; {SHELL_DENY_MESSAGE}"
        )

    for segment in _segments(command):
        tokens = [token for token in _tokens(segment)]
        lowered = [token.lower() for token in tokens]
        if "git" not in lowered:
            continue
        index = lowered.index("git")
        rest = lowered[index + 1:]
        if "push" in rest:
            return f"`git push` is denied: {SHELL_DENY_MESSAGE}"
        if "remote" in rest:
            return (
                "`git remote` is denied: the platform owns this workspace's "
                f"remotes; {SHELL_DENY_MESSAGE}"
            )
        if "config" in rest and "--global" in rest:
            return (
                "`git config --global` is denied: it would outlive this step on "
                f"the shared workspace volume; {SHELL_DENY_MESSAGE}"
            )
    return None


def _credentialed(text: str) -> bool:
    """True for ``scheme://user:password@host`` shapes."""
    if "://" not in text:
        return False
    for chunk in text.split("://")[1:]:
        authority = chunk.split("/")[0]
        if "@" in authority and ":" in authority.split("@")[0]:
            return True
    return False


# --------------------------------------------------------------------------
# the six implementations
# --------------------------------------------------------------------------

def _list_files(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    path = args.get("path") or "."
    depth = args.get("depth")
    depth = 2 if depth is None else max(int(depth), 0)
    max_entries = args.get("max_entries")
    max_entries = 200 if max_entries is None else max(int(max_entries), 1)

    resolved, reason = sandbox.resolve(path)
    if reason:
        return ToolResult(f"list_files: {reason}", is_error=True, summary=reason)
    if not resolved.exists():
        reason = f"no such path: {sandbox.relative(resolved)}"
        return ToolResult(f"list_files: {reason}", is_error=True, summary=reason)
    if resolved.is_file():
        return ToolResult(
            sandbox.relative(resolved), summary="1 entry (a file)"
        )

    entries: List[str] = []
    elided = 0
    root_depth = len(resolved.parts)
    for current, dirnames, filenames in os.walk(str(resolved)):
        current_path = Path(current)
        level = len(current_path.parts) - root_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if level >= depth:
            dirnames[:] = []
        for name in dirnames:
            candidate = sandbox.relative(current_path / name) + "/"
            if len(entries) < max_entries:
                entries.append(candidate)
            else:
                elided += 1
        for name in sorted(filenames):
            candidate = sandbox.relative(current_path / name)
            if len(entries) < max_entries:
                entries.append(candidate)
            else:
                elided += 1

    text = "\n".join(entries)
    if elided:
        text += f"\n[{elided} more elided]"
    summary = f"{len(entries)} entries" + (f" (+{elided} elided)" if elided else "")
    return ToolResult(sandbox.cap(text), summary=summary)


def _read_file(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    resolved, reason = sandbox.resolve(args.get("path"))
    if reason:
        return ToolResult(f"read_file: {reason}", is_error=True, summary=reason)
    if not resolved.exists() or not resolved.is_file():
        reason = f"no such file: {sandbox.relative(resolved)}"
        return ToolResult(f"read_file: {reason}", is_error=True, summary=reason)

    start = args.get("start_line")
    start = 1 if start is None else max(int(start), 1)
    max_lines = args.get("max_lines")
    max_lines = 400 if max_lines is None else max(int(max_lines), 1)

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        reason = f"could not read {sandbox.relative(resolved)}: {exc}"
        return ToolResult(f"read_file: {reason}", is_error=True, summary=reason)

    lines = text.splitlines()
    total = len(lines)
    window = lines[start - 1: start - 1 + max_lines]
    rendered = "\n".join(
        f"{start + offset:>4} | {line}" for offset, line in enumerate(window)
    )
    body = f"{sandbox.relative(resolved)} (total_lines={total})\n{rendered}"
    end = start + len(window) - 1 if window else start
    return ToolResult(
        sandbox.cap(body),
        summary=f"lines {start}-{end} of {total}",
    )


def _write_file(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    resolved, reason = sandbox.resolve(args.get("path"))
    if reason:
        return ToolResult(f"write_file: {reason}", is_error=True, summary=reason)
    content = args.get("content")
    if not isinstance(content, str):
        reason = "content must be a string"
        return ToolResult(f"write_file: {reason}", is_error=True, summary=reason)
    created = not resolved.exists()
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        reason = f"could not write {sandbox.relative(resolved)}: {exc}"
        return ToolResult(f"write_file: {reason}", is_error=True, summary=reason)
    payload = {"bytes": len(content.encode("utf-8")), "created": created}
    return ToolResult(
        json.dumps(payload),
        summary=f"{payload['bytes']} bytes {'created' if created else 'written'}",
    )


def _nearest_line(lines: List[str], needle: str) -> Optional[str]:
    """The existing line most like the first line of ``find``.

    This is the feedback that makes the retry work: a model told only "0
    occurrences" edits blind, while a model shown the line that ALMOST matched
    usually fixes its whitespace on the next turn.
    """
    probe = (needle or "").strip().splitlines()
    if not probe:
        return None
    head = probe[0].strip()
    if not head:
        return None
    best_ratio = 0.0
    best_index = -1
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(head)
    for index, line in enumerate(lines):
        candidate = line.strip()
        if not candidate:
            continue
        matcher.set_seq1(candidate)
        ratio = matcher.quick_ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_index = index
    if best_index < 0 or best_ratio < 0.3:
        return None
    return f"nearest line {best_index + 1}: `{lines[best_index].strip()[:200]}`"


def _apply_patch(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    resolved, reason = sandbox.resolve(args.get("path"))
    if reason:
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)
    if not resolved.exists() or not resolved.is_file():
        reason = f"no such file: {sandbox.relative(resolved)}"
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)

    find = args.get("find")
    replace = args.get("replace")
    if not isinstance(find, str) or not find:
        reason = "`find` must be a non-empty string"
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)
    if not isinstance(replace, str):
        reason = "`replace` must be a string"
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)
    count = args.get("count")
    count = 1 if count is None else int(count)

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        reason = f"could not read {sandbox.relative(resolved)}: {exc}"
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)

    occurrences = text.count(find)
    if occurrences == 0:
        nearest = _nearest_line(text.splitlines(), find)
        reason = (
            f"`find` matched 0 occurrences in {sandbox.relative(resolved)}"
        )
        body = f"apply_patch: {reason}"
        if nearest:
            body += f"\n{nearest}"
        body += (
            "\nThe `find` string must match the file EXACTLY, including "
            "indentation. Read the file and copy the text you want to replace."
        )
        return ToolResult(body, is_error=True, summary=reason)

    applied = occurrences if count <= 0 else min(occurrences, count)
    patched = text.replace(find, replace, applied)
    try:
        resolved.write_text(patched, encoding="utf-8")
    except OSError as exc:
        reason = f"could not write {sandbox.relative(resolved)}: {exc}"
        return ToolResult(f"apply_patch: {reason}", is_error=True, summary=reason)

    payload = {"occurrences": occurrences, "applied": applied}
    return ToolResult(
        json.dumps(payload),
        summary=f"{applied} of {occurrences} occurrence(s) replaced",
    )


def _run_shell(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    command = args.get("command")
    denial = sandbox.denied_reason(command if isinstance(command, str) else "")
    if denial:
        return ToolResult(f"run_shell: {denial}", is_error=True, summary=denial)

    timeout = args.get("timeout")
    timeout = sandbox.shell_timeout if timeout is None else max(int(timeout), 1)
    timeout = min(timeout, sandbox.shell_timeout)

    argv = list(sandbox.shell) + [command]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(sandbox.workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=sandbox.shell_env(),
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = SHELL_TIMEOUT_EXIT
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr) + f"\n[command timed out after {timeout}s]"
        timed_out = True
    except OSError as exc:
        reason = f"could not start a shell ({sandbox.shell[0]}): {exc}"
        return ToolResult(f"run_shell: {reason}", is_error=True, summary=reason)

    # HALF the budget each, so a chatty stdout cannot hide the stderr that
    # explains it.
    half = max(sandbox.output_max_bytes // 2, 256)
    payload = {
        "exit_code": exit_code,
        "stdout": cap_output(stdout, half),
        "stderr": cap_output(stderr, half),
    }
    summary = f"exit {exit_code}" + (" (timed out)" if timed_out else "")
    # A NON-ZERO EXIT IS A RESULT, NOT AN ERROR. "the tests failed" is the
    # single most useful observation the loop can make, and marking it an
    # error would trip MAX_CONSECUTIVE_TOOL_ERRORS on a model doing its job.
    return ToolResult(json.dumps(payload), is_error=False, summary=summary)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _finish(sandbox: Sandbox, args: Dict[str, Any]) -> ToolResult:
    """Never called: the loop intercepts ``finish`` before dispatch.

    Present so the tool table is complete for schema generation, validation
    and the fallback prompt — one table, no second list to drift.
    """
    return ToolResult(
        "finish is handled by the harness and never executed as a tool",
        is_error=True,
    )


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

FINISH_STATUSES = ("success", "failed", "blocked")

TOOLS: Dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="list_files",
            description=(
                "List files and directories under a path in the repository."
            ),
            args=(
                ArgSpec("path", "string", default=".", description="Relative path. Defaults to the repository root."),
                ArgSpec("depth", "integer", default=2, description="How many directory levels to descend."),
                ArgSpec("max_entries", "integer", default=200, description="Maximum entries to return."),
            ),
            run=_list_files,
        ),
        ToolSpec(
            name="read_file",
            description=(
                "Read a range of lines from a file. Output is numbered and "
                "reports the file's total line count."
            ),
            args=(
                ArgSpec("path", "string", required=True, description="Relative path to the file."),
                ArgSpec("start_line", "integer", default=1, description="First line to read, 1-based."),
                ArgSpec("max_lines", "integer", default=400, description="How many lines to read."),
            ),
            run=_read_file,
        ),
        ToolSpec(
            name="write_file",
            description="Create or overwrite a file with the given content.",
            args=(
                ArgSpec("path", "string", required=True, description="Relative path to the file."),
                ArgSpec("content", "string", required=True, description="The complete new file content."),
            ),
            run=_write_file,
        ),
        ToolSpec(
            name="apply_patch",
            description=(
                "Replace an EXACT string in a file. `find` must match the file "
                "byte for byte, including indentation."
            ),
            args=(
                ArgSpec("path", "string", required=True, description="Relative path to the file."),
                ArgSpec("find", "string", required=True, description="The exact text to find."),
                ArgSpec("replace", "string", required=True, description="The text to put in its place."),
                ArgSpec("count", "integer", default=1, description="How many occurrences to replace; 0 means all."),
            ),
            run=_apply_patch,
        ),
        ToolSpec(
            name="run_shell",
            description=(
                "Run a shell command in the repository. A non-zero exit code is "
                "a normal result. Do not commit or push."
            ),
            args=(
                ArgSpec("command", "string", required=True, description="The command line to run."),
                ArgSpec("timeout", "integer", default=TOOL_SHELL_TIMEOUT, description="Seconds before the command is killed."),
            ),
            run=_run_shell,
        ),
        ToolSpec(
            name="finish",
            description=(
                "End the task. Call this when the work is done or cannot be done."
            ),
            args=(
                ArgSpec("status", "string", required=True, description='One of "success", "failed", "blocked".'),
                ArgSpec("summary", "string", required=True, description="One paragraph describing what you did or why you stopped."),
            ),
            run=_finish,
        ),
    )
}

#: Order matters for the prompt and for the schema list: it is the order a
#: model reads the tools in, and reading order is the cheapest prior we get.
TOOL_ORDER = ("list_files", "read_file", "write_file", "apply_patch", "run_shell", "finish")

FINISH_TOOL = "finish"


def tool_schemas() -> List[Dict[str, Any]]:
    return [TOOLS[name].schema() for name in TOOL_ORDER]


def run_tool(sandbox: Sandbox, name: str, args: Dict[str, Any]) -> ToolResult:
    """Dispatch one validated call. NEVER raises."""
    spec = TOOLS.get(name)
    if spec is None:
        reason = f"unknown_tool: {name}"
        return ToolResult(reason, is_error=True, summary=reason)
    clean, reason = validate_args(spec, args)
    if reason:
        return ToolResult(f"{name}: {reason}", is_error=True, summary=reason)
    try:
        return spec.run(sandbox, clean)
    except Exception as exc:  # noqa: BLE001 - a tool crash is a tool error
        reason = f"{name} raised {type(exc).__name__}: {exc}"
        return ToolResult(reason, is_error=True, summary=reason)


# --------------------------------------------------------------------------
# the ONE read-only git observation the harness makes
# --------------------------------------------------------------------------

def changed_path_count(workdir: Path, runner=None) -> int:
    """How many paths ``git status --porcelain`` reports. ``0`` when it cannot
    tell — see ``working_tree_changed`` for why that direction is the safe one.

    ``runner`` is resolved at CALL time, not bound as a default, so a test
    spying on ``subprocess.run`` actually sees this invocation. A seam that
    silently bypasses the spy is a seam that proves nothing.
    """
    runner = runner or subprocess.run
    try:
        completed = runner(
            ["git", "status", "--porcelain"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return 0
    if getattr(completed, "returncode", 1) != 0:
        return 0
    lines = [
        line for line in (getattr(completed, "stdout", "") or "").splitlines()
        if line.strip()
    ]
    return len(lines)


def working_tree_changed(workdir: Path, runner=None) -> bool:
    """``git status --porcelain`` produced output (design section 3.5).

    THE HARNESS OWNS NOTHING ABOUT LANDING WORK. This is a read-only
    OBSERVATION — the same command ``agent_wrapper._finish`` already runs —
    and it is the only git invocation anywhere in the harness. There is no
    ``add``, no ``commit``, no ``checkout``, no ``push``: those belong to the
    12.5 wrapper and there is no second implementation (section 3.6).

    Never raises: a workspace that is not a git repo answers False, because
    "we could not tell" must not become "the agent changed nothing" by way of
    a traceback. It answers False, which FAILS a ``finish(success)`` step —
    the loud direction, not the quiet one.
    """
    return changed_path_count(workdir, runner=runner) > 0
