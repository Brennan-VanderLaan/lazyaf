"""
Agent step configuration parser (Phase 12.5, cross-agent contract #1).

The CONSUMER side of ``/workspace/.control/agent.<step_execution_id>.json``,
written verbatim by the backend's
``app.services.control_layer.workspace.generate_agent_config`` — that producer
is the single source of truth for the shape (R3), and the producer<->consumer
round trip is pinned by ``tdd/unit/control_runtime/test_agent_step_container``
-era contract tests that load a real producer payload through this module.

WHY A SECOND FILE AT ALL. ``/control/run.py`` deletes the STEP config
(``<step_execution_id>.json``) in a ``finally`` that runs BEFORE the step
command starts — consume-once, so the step JWT can never be read by the step
process. An agent payload carried inside that file would therefore be
unreadable by the wrapper. Splitting also keeps the step JWT and the API key
out of every file the wrapper opens.

The path is announced through ``LAZYAF_AGENT_CONFIG_PATH``, which the backend
places in the STEP CONFIG FILE's ``environment`` (never in the container's
inspectable env).

This module mirrors ``images/base/control/config.py`` deliberately:
- ``version`` is pinned to 1; an unknown version is a LOUD stderr failure and
  a ``None`` return (a wrapper that half-understands its instructions is worse
  than one that refuses),
- a missing required key prints the reason — never a silent ``None``,
- nothing here raises: every failure is a printed reason plus ``None``.

Stdlib only.
"""
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

#: The ONE version this wrapper understands (contract #1).
AGENT_CONFIG_VERSION = 1

#: Env var carrying the agent config path into the step process. Set by the
#: backend inside the step config FILE's ``environment`` block.
AGENT_CONFIG_PATH_ENV = "LAZYAF_AGENT_CONFIG_PATH"

#: Keys that must be present and truthy.
REQUIRED_KEYS = ("agent", "prompt", "repo")


@dataclass
class AgentConfig:
    """One agent step's instructions (the payload of contract #1)."""

    # --- what to run -------------------------------------------------------
    agent: str
    """Executor selector: 'claude-code' | 'gemini' | 'mock'. No default."""

    prompt: str
    """The work itself. RENDERED BACKEND-SIDE (app/services/agent_prompt.py);
    the container never re-templates — a second renderer would be a second
    source of truth for the most important string in the system."""

    repo: Dict[str, Any]
    """{repo_id, workdir, base_branch, branch, remote_url}."""

    version: int = AGENT_CONFIG_VERSION
    model: Optional[str] = None
    """ExecutorConfig.model -> the CLI's --model. The M13 comparison axis."""

    stream: bool = True
    """claude: --output-format stream-json --verbose vs plain json."""

    agents_json: Optional[str] = None
    """Resolved agent_file_ids as a JSON string -> claude --agents. The
    backend owns AgentFile/agent_resolver; the container has no DB."""

    task: Dict[str, Any] = field(default_factory=dict)
    """{card_id, card_title, card_description, step_index, step_name} —
    commit message, forensics, and the card join."""

    context: Dict[str, Any] = field(default_factory=dict)
    """{previous_step_name, previous_step_logs, previous_step_logs_truncated}
    — the legacy .lazyaf-context/ channel, replaced by a DB-sourced field."""

    commit: Dict[str, Any] = field(default_factory=dict)
    """{enabled, message, author_name, author_email, push, allow_empty}."""

    mock_config: Optional[Dict[str, Any]] = None
    """Deterministic mock behavior, so the dogfood ratchet costs nothing."""

    role: Optional[str] = None
    """M13 fan-out attribution. Always null in 12.5 — on the wire NOW so the
    usage channel is not a retrofit (api-surface 2.6)."""

    @property
    def workdir(self) -> Path:
        """Where the agent runs. Falls back to the image's repo location."""
        return Path(self.repo.get("workdir") or "/workspace/repo")

    @property
    def commit_enabled(self) -> bool:
        return bool(self.commit.get("enabled"))

    @property
    def push_enabled(self) -> bool:
        return bool(self.commit.get("push"))


def _fail(reason: str) -> None:
    print(f"[agent] ERROR: invalid agent config: {reason}", file=sys.stderr, flush=True)


def load_agent_config(config_path: Path) -> Optional[AgentConfig]:
    """Load and validate the agent config. Returns ``None`` on any problem.

    NEVER raises and NEVER returns silently: every ``None`` is preceded by a
    printed reason naming the exact key or version that was wrong.
    """
    try:
        if not config_path.exists():
            _fail(f"config file not found: {config_path}")
            return None
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        _fail(f"config is not valid JSON: {exc}")
        return None
    except OSError as exc:
        _fail(f"could not read config file: {exc}")
        return None

    if not isinstance(data, dict):
        _fail(
            f"top level is a {type(data).__name__}, expected a JSON object"
        )
        return None

    # STRICT: an int that is exactly 1. `True == 1` and `1.0 == 1` in Python,
    # so a bare equality check would let `{"version": true}` through as
    # version 1 — the sort of half-understanding this refusal exists to stop.
    version = data.get("version", AGENT_CONFIG_VERSION)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != AGENT_CONFIG_VERSION
    ):
        _fail(
            f"unsupported version {version!r} (this runtime speaks "
            f"version {AGENT_CONFIG_VERSION} only) — refusing to run a step "
            "whose instructions it half-understands"
        )
        return None

    for key in REQUIRED_KEYS:
        if not data.get(key):
            _fail(f"missing required key: {key}")
            return None

    if not isinstance(data["agent"], str):
        _fail(
            "agent must be a string ('claude-code' | 'gemini' | 'mock'); "
            f"got {type(data['agent']).__name__}"
        )
        return None
    if not isinstance(data["prompt"], str):
        _fail(f"prompt must be a string; got {type(data['prompt']).__name__}")
        return None
    if not isinstance(data["repo"], dict):
        _fail(f"repo must be an object; got {type(data['repo']).__name__}")
        return None

    return AgentConfig(
        version=AGENT_CONFIG_VERSION,
        agent=data["agent"],
        prompt=data["prompt"],
        repo=data["repo"],
        model=data.get("model"),
        stream=bool(data.get("stream", True)),
        agents_json=data.get("agents_json"),
        task=data.get("task") or {},
        context=data.get("context") or {},
        commit=data.get("commit") or {},
        mock_config=data.get("mock_config"),
        role=data.get("role"),
    )


def unlink_quietly(config_path: Path) -> None:
    """Delete a consume-once file, reporting only real problems.

    A missing file is not a problem (another shutdown path may already have
    removed it); an unlink failure is a WARNING, never an exception — the
    step's outcome must not depend on housekeeping.
    """
    try:
        config_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"[agent] WARNING: could not delete {config_path}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def load_and_consume(config_path: Path) -> Optional[AgentConfig]:
    """``load_agent_config`` + consume-once delete on EVERY path.

    The workspace volume outlives this step, so the agent config — which
    carries the rendered prompt and, at 12.6.6, curated spec context — must
    not survive it. Deleted on parse failure too: a config the wrapper could
    not understand is exactly the one that must not be re-read by the next
    step on the same volume.
    """
    try:
        return load_agent_config(config_path)
    finally:
        unlink_quietly(config_path)


def config_path_from_env() -> Optional[Path]:
    """Resolve the agent config path from ``LAZYAF_AGENT_CONFIG_PATH``.

    Returns ``None`` (with a printed reason) when the variable is absent —
    which means the step was dispatched as an agent step without control
    mode, a combination the backend refuses at dispatch.
    """
    raw = os.environ.get(AGENT_CONFIG_PATH_ENV)
    if not raw:
        _fail(
            f"{AGENT_CONFIG_PATH_ENV} is not set — an agent step must be "
            "dispatched in control mode, which announces the agent config "
            "path inside the step config file"
        )
        return None
    return Path(raw)
