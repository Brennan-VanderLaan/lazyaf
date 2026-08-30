#!/usr/bin/env python3
"""
The agent step runtime (Phase 12.5).

`/control/run.py` DOES NOT KNOW it is running an agent step. For an agent
step the backend simply puts a fixed command in the step config::

    "command": "python3 -m runner_common.agent_wrapper"

That is the whole invocation contract, and every consequence of it is good:
run.py's dispatch, timeout watchdog, log pump and shell wrapping are
unchanged; this wrapper is an ordinary child process, so the in-container
watchdog kills its process group on timeout, its stdout IS the step's log
stream, and its exit code IS the step's exit code.

It is a MODULE in the tested package rather than a file copied into
/control: one packaging mechanism, tests live in runner-common/tests/, and
12.6's remote runner-agent invokes this identical module.

WHAT IT DOES
1. Reads the agent config from the sibling file announced by
   LAZYAF_AGENT_CONFIG_PATH (contract #1) and CONSUMES it.
2. Refuses to run as root (see the getuid check below).
3. Materialises the curated spec context (12.6.6) next to that config as
   ``spec_context.md`` so the agent can re-read its brief, logs one line
   about it either way, and deletes it in the same ``finally``.
4. Dispatches to one of runner_common.executors by ``agent`` — there is no
   default agent; an unknown one is a loud exit 1.
5. Renders the CLI's events to human-readable log lines as they arrive.
6. Commits/pushes the work when the config says to.
7. Writes the usage manifest (contract #2) in a ``finally`` and from a
   SIGTERM handler, so telemetry survives every outcome including a
   watchdog kill.

TIMEOUT OWNERSHIP: ``ExecutorConfig.timeout`` is None BY CONTRACT. The
control runtime (images/base/control/executor.py) already SIGTERMs then
SIGKILLs the process group at ``timeout_seconds``. Two timeout owners is how
a step ends up half-killed with no manifest.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .agent_config import (
    SPEC_CONTEXT_FILENAME,
    AgentConfig,
    config_path_from_env,
    load_and_consume,
    unlink_quietly,
)
from .executors import (
    ClaudeExecutor,
    ExecutorConfig,
    ExecutorResult,
    GeminiExecutor,
    MockExecutor,
)
from .usage import (
    SCRAPE_FAILED_LOG_MARKER,
    USAGE_PATH_ENV,
    scrape_failure_reason,
    write_usage_manifest,
)

#: The agent vocabulary (cross-agent contract #5). No default entry: a config
#: naming an agent that is not here is a refusal, not a guess.
#:
#: ONE MAPPING, not two (12.5 review finding F3.3): the value is the BUILDER,
#: not just the class, so the vocabulary and the construction of each
#: executor have a single source. Two of the three need config the wrapper
#: holds — the claude CLI's output format comes from ``cfg.stream`` and the
#: mock's behavior from ``cfg.mock_config`` (which the backend puts on the
#: wire so the dogfood ratchet is deterministic and free) — and a separate
#: if-chain for that is exactly how a fourth agent gets added to one place
#: and not the other. The builders resolve the executor classes from module
#: globals at CALL time, which is also what keeps them substitutable in
#: tests.
EXECUTORS = {
    "claude-code": lambda cfg: ClaudeExecutor(
        output_format="stream-json" if cfg.stream else "json"
    ),
    "gemini": lambda cfg: GeminiExecutor(),
    "mock": lambda cfg: MockExecutor(mock_config=cfg.mock_config),
}

#: Longest rendered log line for one streamed event (a 40 KB tool result is
#: not a log line).
MAX_EVENT_LINE = 2000

#: Module state the SIGTERM handler needs. A handler cannot take arguments,
#: and a partial manifest written from a signal is the whole point of having
#: one, so this is deliberate rather than lazy.
_STATE: Dict[str, Any] = {
    "usage_path": None,
    "agent": "unknown",
    "model": None,
    "role": None,
    "started": None,
    "written": False,
}


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def _log(message: str) -> None:
    """One log line onto the step's stdout (which run.py pumps to /logs)."""
    print(message, flush=True)


def _truncate(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_EVENT_LINE else text[:MAX_EVENT_LINE] + " ..."


def _render_event(event: Dict[str, Any]) -> Optional[str]:
    """Render one claude stream-json event as ONE human-readable line.

    Returns None for events with nothing worth showing. This is what buys
    the deviation from api-surface 2.3: machine-readable output that is
    still legible in the step log, instead of a 20-minute dark step.
    """
    kind = event.get("type")

    if kind == "system":
        subtype = event.get("subtype") or "event"
        model = event.get("model")
        suffix = f" model={model}" if model else ""
        return f"[agent] system:{subtype}{suffix}"

    if kind in ("assistant", "user"):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return f"[agent] {_truncate(content)}" if content.strip() else None
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text", "").strip():
                parts.append(block["text"])
            elif block_type == "tool_use":
                parts.append(f"<tool {block.get('name', 'unknown')}>")
            elif block_type == "tool_result":
                parts.append("<tool result>")
        if not parts:
            return None
        return f"[agent] {_truncate(' '.join(parts))}"

    if kind == "result":
        cost = event.get("total_cost_usd")
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        bits = [f"subtype={event.get('subtype', 'ok')}"]
        if usage.get("input_tokens") is not None:
            bits.append(f"in={usage['input_tokens']}")
        if usage.get("output_tokens") is not None:
            bits.append(f"out={usage['output_tokens']}")
        if cost is not None:
            bits.append(f"cost_usd={cost}")
        return "[agent] result: " + " ".join(bits)

    if kind:
        return f"[agent] {kind}"
    return None


def _emit(line: str) -> None:
    """Executor log callback: render JSON events, pass prose through.

    The base executor prefixes captured lines with two spaces; strip before
    probing for JSON so a stream-json event is still recognized.
    """
    text = str(line).rstrip("\n")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except ValueError:
            event = None
        if isinstance(event, dict):
            rendered = _render_event(event)
            if rendered is not None:
                _log(rendered)
            return
    _log(text)


# --------------------------------------------------------------------------
# usage manifest
# --------------------------------------------------------------------------

def _elapsed_ms() -> int:
    started = _STATE.get("started")
    if started is None:
        return 0
    return int((time.monotonic() - started) * 1000)


def _write_usage(result: Optional[ExecutorResult]) -> None:
    """Write the usage manifest. NEVER raises (write_usage_manifest cannot).

    Called from the ``finally`` and from the SIGTERM handler; the first write
    wins so a graceful kill's partial record is not overwritten by an
    identical one on the way out.

    A SCRAPE FAILURE is announced on the step's own log stream before the
    write (F3.1). ``scripts/verify_executor.py`` greps for this marker, so a
    vendor output change fails the push instead of quietly recording a free
    step. It still does not touch the exit code.
    """
    if _STATE.get("written"):
        return
    _STATE["written"] = True
    usage = result.usage if result is not None else None
    reason = scrape_failure_reason(usage)
    if reason is not None:
        _log(f"{SCRAPE_FAILED_LOG_MARKER}: {reason}")
        _log(
            "[agent] the usage record for this step carries no numbers from "
            "the CLI; it is NOT evidence that the step was free"
        )
    write_usage_manifest(
        _STATE.get("usage_path"),
        _STATE.get("agent") or "unknown",
        usage,
        wall_clock_ms=_elapsed_ms(),
        role=_STATE.get("role"),
        model=_STATE.get("model"),
    )


# --------------------------------------------------------------------------
# curated spec context (12.6.6)
# --------------------------------------------------------------------------

def _write_spec_context(cfg: AgentConfig, control_dir: Path) -> Optional[Path]:
    """Materialise the curated spec bundle next to the agent config.

    WHY AT ALL, when the same markdown is already inside ``cfg.prompt``: an
    agent 40 turns into a session should be able to ``cat`` its brief rather
    than trust its own context window. The prompt is the channel; the file is
    the reference copy.

    WHY THE PATH IS DERIVED, NOT TAKEN FROM THE PAYLOAD: ``control_dir`` is
    ``config_path.parent`` — the directory the backend already announced
    through LAZYAF_AGENT_CONFIG_PATH. The wrapper never writes to a path a
    payload told it to.

    NEVER FATAL. The bundle is already in the prompt, so failing the step over
    a convenience file would be a worse outcome than the file's absence. The
    failure is a WARNING naming the path and the errno — loud, per R1; the
    silent version is the violation.

    ALWAYS SAYS SOMETHING. No bundle logs one line too: a silent absence is
    indistinguishable from a bug that dropped the brief.
    """
    if not cfg.has_spec_context:
        _log("[agent] spec context: none (no spec links for this card)")
        return None

    meta = cfg.spec_context or {}
    markdown = cfg.spec_markdown or ""
    path = control_dir / SPEC_CONTEXT_FILENAME
    try:
        # 0600, matching the tar's file mode. The .control DIRECTORY is
        # already chowned to the agent uid by images/base/control/entrypoint.sh
        # before gosu, so this needs no image change.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as exc:
        print(
            f"[agent] WARNING: could not write the spec context to {path}: "
            f"{exc} — the same text is already in the prompt, so the step "
            "continues",
            file=sys.stderr,
            flush=True,
        )
        return None

    _log(
        "[agent] spec context: "
        f"{meta.get('criteria_count', 0)} criteria, "
        f"{meta.get('test_ref_count', 0)} related tests, "
        f"~{meta.get('estimated_tokens', 0)} tokens, "
        f"truncated={bool(meta.get('truncated'))} -> {path}"
    )
    if meta.get("truncated"):
        dropped = meta.get("dropped") or []
        _log(
            "[agent] note: spec context was truncated (dropped: "
            f"{', '.join(str(d) for d in dropped) or 'unspecified'})"
        )
    return path


def _running_as_root() -> bool:
    """True when this process is uid 0 on a POSIX system.

    `claude --dangerously-skip-permissions` refuses to run as root, and a
    root-owned $HOME/.claude poisons every later step on the shared workspace
    volume. The image entrypoint gosu's down to uid 1000 before exec'ing
    run.py, so this being true means the image contract broke.
    """
    return os.name == "posix" and hasattr(os, "getuid") and os.getuid() == 0


def _install_sigterm_handler() -> None:
    """Write a partial usage manifest when the watchdog kills us gracefully.

    images/base/control/executor.py SIGTERMs the process group, waits 5s,
    then SIGKILLs. A SIGKILL leaves no manifest at all — that case is covered
    by run.py's fallback record (``cost_source="unknown"``).
    """

    def _handle(signum, _frame):
        _log(f"[agent] received signal {signum}; writing partial usage manifest")
        _write_usage(None)
        sys.exit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handle)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or a platform without SIGTERM. Telemetry is
        # best-effort; the step is not.
        pass


# --------------------------------------------------------------------------
# executor construction
# --------------------------------------------------------------------------

def make_executor(cfg: AgentConfig):
    """Build the executor named by ``cfg.agent``, from ``EXECUTORS``.

    The mapping is the ONLY source for both the vocabulary and the
    construction (F3.3). An agent that is not in it raises ``KeyError`` —
    caught by ``main()``'s try, whose ``finally`` still writes the usage
    manifest.
    """
    build = EXECUTORS.get(cfg.agent)
    if build is None:
        raise KeyError(cfg.agent)
    return build(cfg)


# --------------------------------------------------------------------------
# commit / push
# --------------------------------------------------------------------------

def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _finish(cfg: AgentConfig, result: ExecutorResult) -> int:
    """Land the agent's work. Returns the wrapper's exit code.

    Never raises: a git failure is a FAILED STEP with a readable reason, and
    the usage manifest written by the caller's ``finally`` survives it.
    """
    if not result.success:
        _log(f"[agent] agent failed: {result.error or 'no error reported'}")
        return result.exit_code if result.exit_code not in (0, None) else 1

    if not cfg.commit_enabled:
        _log("[agent] commit disabled for this step; leaving the workspace as-is")
        return 0

    from .git_helpers import GitError, configure_git, push

    workdir = cfg.workdir
    commit = cfg.commit
    branch = cfg.repo.get("branch")

    try:
        configure_git(
            commit.get("author_email") or "agent@lazyaf.local",
            commit.get("author_name") or "LazyAF Agent",
        )
    except GitError as exc:
        _log(f"[agent] ERROR: could not configure git: {exc}")
        return 1

    if branch:
        # The workspace was cloned at base_branch; -B is a no-op when we are
        # already on the work branch and creates it at HEAD when we are not.
        checkout = _git(["checkout", "-B", branch], workdir)
        if checkout.returncode != 0:
            _log(f"[agent] ERROR: could not checkout {branch}: {checkout.stderr.strip()}")
            return 1
        _log(f"[agent] on branch {branch}")

    status = _git(["status", "--porcelain"], workdir)
    has_changes = bool(status.stdout.strip())
    allow_empty = bool(commit.get("allow_empty"))

    if has_changes or allow_empty:
        _git(["add", "-A"], workdir)
        message = commit.get("message") or "chore: LazyAF agent step"
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        committed = _git(args, workdir)
        if committed.returncode != 0:
            _log(
                "[agent] ERROR: commit failed: "
                f"{(committed.stderr or committed.stdout).strip()}"
            )
            return 1
        _log("[agent] committed the agent's changes")
    else:
        _log("[agent] no changes to commit")

    if cfg.push_enabled:
        if not branch:
            _log("[agent] ERROR: push requested but the config names no branch")
            return 1
        _log(f"[agent] pushing {branch}...")
        try:
            push(workdir, branch, set_upstream=True)
        except GitError as exc:
            _log(f"[agent] ERROR: push failed: {exc}")
            return 1
        _log(f"[agent] pushed {branch}")

    return 0


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

def main() -> int:
    """Run one agent step. Returns the process exit code."""
    # Full reset, not just an assignment: main() is called repeatedly in one
    # process by the tests, and a stale ``written`` flag would silently
    # suppress the next step's manifest.
    _STATE.update(
        {
            "usage_path": os.environ.get(USAGE_PATH_ENV),
            "agent": "unknown",
            "model": None,
            "role": None,
            "started": time.monotonic(),
            "written": False,
        }
    )

    config_path = config_path_from_env()
    if config_path is None:
        return 1

    cfg = load_and_consume(config_path)
    if cfg is None:
        return 1

    _STATE["agent"] = cfg.agent
    _STATE["model"] = cfg.model
    _STATE["role"] = cfg.role

    if _running_as_root():
        print(
            "[agent] ERROR: wrapper is running as root; the image entrypoint "
            "must gosu down to uid 1000",
            file=sys.stderr,
            flush=True,
        )
        return 1

    _install_sigterm_handler()

    # Log the resolved target BEFORE invoking: a DNS/egress failure to
    # api.anthropic.com or the internal git server is then one grep away.
    _log(
        f"[agent] agent={cfg.agent} model={cfg.model or '<default>'} "
        f"stream={cfg.stream} workdir={cfg.workdir} "
        f"backend={os.environ.get('LAZYAF_BACKEND_URL', '<unset>')}"
    )
    if cfg.context.get("previous_step_logs_truncated"):
        _log("[agent] note: previous step logs were truncated to fit the config")

    spec_context_path = _write_spec_context(cfg, config_path.parent)

    result: Optional[ExecutorResult] = None
    try:
        # INSIDE the try whose finally writes the manifest (F3.3): the agent
        # refusal and the executor's own constructor are both failure modes,
        # and a step that dies here used to leave NO usage record at all —
        # a hole in the very completeness the dogfood gate asserts.
        if cfg.agent not in EXECUTORS:
            print(
                f"[agent] ERROR: unknown agent {cfg.agent!r}; expected one of "
                f"{sorted(EXECUTORS)} — there is no default agent",
                file=sys.stderr,
                flush=True,
            )
            return 1

        executor = make_executor(cfg)
        executor_config = ExecutorConfig(
            workspace=cfg.workdir,
            prompt=cfg.prompt,
            model=cfg.model,
            agents_json=cfg.agents_json,
            # ONE timeout owner: the control runtime's watchdog (see the
            # module docstring). Never set this.
            timeout=None,
            env={},
        )

        result = executor.execute(
            executor_config, log_callback=_emit, streaming=cfg.stream
        )
        return _finish(cfg, result)
    except Exception as exc:  # noqa: BLE001 - the wrapper is the last line
        print(f"[agent] ERROR: agent execution crashed: {exc!r}", file=sys.stderr, flush=True)
        return 1
    finally:
        _write_usage(result)
        # Consume-once, same rule as the agent config: the workspace volume
        # is shared by every step of the run, and step N+1's agent must never
        # read step N's brief.
        if spec_context_path is not None:
            unlink_quietly(spec_context_path)


if __name__ == "__main__":
    sys.exit(main())
