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

#: The 14.x agent vocabulary entry (cross-agent contract #5). Named once here
#: so the two places this module checks it cannot drift apart.
HARNESS_AGENT = "openai-harness"

#: Filename the wrapper materialises the curated spec bundle under, inside the
#: directory the backend announced through AGENT_CONFIG_PATH_ENV (12.6.6).
#: Pinned equal to ``control_layer.workspace.SPEC_CONTEXT_FILENAME`` by the
#: contract test - the two sides name the same file or neither does.
SPEC_CONTEXT_FILENAME = "spec_context.md"


@dataclass
class AgentConfig:
    """One agent step's instructions (the payload of contract #1)."""

    # --- what to run -------------------------------------------------------
    agent: str
    """Executor selector: 'claude-code' | 'gemini' | 'mock' |
    'openai-harness'. No default."""

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

    spec_context: Optional[Dict[str, Any]] = None
    """The curated spec bundle (12.6.6), or None when the card has no spec
    links / curation was switched off for this step.

    {markdown, source, criteria_count, test_ref_count, estimated_tokens,
     truncated, dropped}

    ``markdown`` is ALREADY inside ``prompt`` — the backend is the single
    renderer (R3). It travels here as well so the wrapper can materialise it
    at ``<control dir>/spec_context.md`` for an agent that wants to re-read
    its brief 40 turns in, and so the size/truncation facts can be LOGGED: a
    silently-shrunk brief is exactly the dark behaviour R1 forbids."""

    endpoint: Optional[Dict[str, Any]] = None
    """The self-hosted endpoint this step runs against (14.1), or None.

    {id, name, base_url, model, server_kind, reach, auth_style, auth_env,
     auth_header, request_timeout_seconds, capabilities, pricing}

    ``capabilities`` is a SNAPSHOT taken at dispatch, not a live reference: a
    step must behave identically if someone re-probes the endpoint mid-run,
    and a snapshot is also what M13 needs to attribute a result to the
    capabilities that were actually in force.

    ``auth_env`` names the FIXED container-side variable the harness reads.
    THE AGENT CONFIG NEVER CARRIES THE KEY — the value arrives through 12.5's
    ``secret_environment`` in the STEP config file, which run.py merges into
    the child's env and then deletes."""

    harness: Dict[str, Any] = field(default_factory=dict)
    """The harness's budgets and loop shape (14.2).

    {mode, max_iterations, max_total_tokens, time_budget_seconds,
     max_tool_calls_per_turn, shell_timeout_seconds, tool_output_max_bytes,
     temperature, top_p, seed, require_changes, debug_transcript}

    ``mode`` is how M13 makes LOOP SHAPE an independent variable: forcing
    ``text`` on a tool-capable model measures the cost of the fallback
    protocol directly."""

    @property
    def harness_mode(self) -> str:
        """``'tools'`` | ``'text'``, resolved from ``harness.mode`` and the
        probed capability.

        ONE FUNCTION DECIDES (cross-agent contract 4.3.7):
        ``runner_common.harness.loop.resolve_harness_mode``. ``auto`` with
        ``supports_tools is None`` is a REFUSAL, not a guess — the backend
        already refuses to dispatch an unprobed endpoint, so reaching that
        branch means the wire lied."""
        from .harness.loop import resolve_harness_mode

        return resolve_harness_mode(self.endpoint, self.harness)

    @property
    def spec_markdown(self) -> Optional[str]:
        """The bundle text, or None. Empty string is None: a bundle with no
        markdown is not a bundle."""
        if not isinstance(self.spec_context, dict):
            return None
        markdown = self.spec_context.get("markdown")
        return markdown if isinstance(markdown, str) and markdown else None

    @property
    def has_spec_context(self) -> bool:
        return self.spec_markdown is not None

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
            "agent must be a string ('claude-code' | 'gemini' | 'mock' | "
            "'openai-harness'); "
            f"got {type(data['agent']).__name__}"
        )
        return None
    if not isinstance(data["prompt"], str):
        _fail(f"prompt must be a string; got {type(data['prompt']).__name__}")
        return None
    if not isinstance(data["repo"], dict):
        _fail(f"repo must be an object; got {type(data['repo']).__name__}")
        return None

    # 12.6.6. ABSENT is fine and means "pre-12.6.6 backend" — an additive,
    # optional key that an old consumer ignores and a new one defaults is
    # exactly what does NOT justify a version bump (bumping would strand every
    # runner agent in the field mid-phase). PRESENT-BUT-WRONG is a refusal:
    # a wrapper that half-understands its instructions is worse than one that
    # refuses, and the curated brief is not a field to guess at.
    spec_context = data.get("spec_context")
    if spec_context is not None:
        if not isinstance(spec_context, dict):
            _fail(
                "spec_context must be an object or null; got "
                f"{type(spec_context).__name__}"
            )
            return None
        markdown = spec_context.get("markdown")
        if markdown is not None and not isinstance(markdown, str):
            _fail(
                "spec_context.markdown must be a string or null; got "
                f"{type(markdown).__name__}"
            )
            return None

    # 14.1/14.2. SAME three-way strictness as spec_context above, and for the
    # same reason: an additive optional key that an old consumer ignores and a
    # new one defaults does NOT justify a version bump, but a key that is
    # PRESENT AND WRONG is a refusal — a wrapper that half-understands its
    # instructions is worse than one that refuses, and "which GPU am I
    # billing" is not a field to guess at.
    endpoint = data.get("endpoint")
    if endpoint is not None and not isinstance(endpoint, dict):
        _fail(
            f"endpoint must be an object or null; got {type(endpoint).__name__}"
        )
        return None

    harness = data.get("harness")
    if harness is not None and not isinstance(harness, dict):
        _fail(f"harness must be an object or null; got {type(harness).__name__}")
        return None
    harness = harness or {}

    if data["agent"] == HARNESS_AGENT:
        if not isinstance(endpoint, dict):
            _fail(
                f"agent '{HARNESS_AGENT}' requires an endpoint block naming the "
                "OpenAI-compatible server to drive; there is no default endpoint"
            )
            return None
        for key in ("base_url", "model"):
            if not endpoint.get(key):
                _fail(
                    f"agent '{HARNESS_AGENT}' requires endpoint.{key}; the "
                    "backend must not dispatch a harness step without one"
                )
                return None
        # The mode is resolved HERE, at load, so an endpoint whose capability
        # record cannot answer "does it do tool calling" fails before the
        # container spends a token — not 40 turns in.
        try:
            from .harness.loop import resolve_harness_mode

            resolve_harness_mode(endpoint, harness)
        except ValueError as exc:
            _fail(str(exc))
            return None
        except ImportError as exc:  # pragma: no cover - packaging regression
            _fail(f"the harness package is not importable: {exc}")
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
        spec_context=spec_context,
        endpoint=endpoint,
        harness=harness,
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
