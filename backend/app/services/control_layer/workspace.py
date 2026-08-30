"""
Step config producers - Phase 12.3 (step config) / 12.5 (agent config).

`generate_step_config` is the SINGLE PRODUCER of the step config file
contract (R3) consumed by the in-container control runtime at
`images/base/control/` (see `config.py` there). LocalExecutor calls it and
ships the result verbatim into the step container as
`/workspace/.control/<step_execution_id>.json` (path announced to the
runtime via the CONFIG_PATH env var); the runtime verifies and unlinks
that exact file. The consumer-side contract test
(`tdd/unit/control_runtime/test_config_contract.py`) pins that the
consumer understands every key produced here.

The former workspace-layout half of this module (WorkspaceLayout,
initialize_workspace, get_workspace_paths, write_step_config) was dead
code and is deleted: `images/base/entrypoint.sh` is the single owner of
the /workspace HOME skeleton, covered behaviorally by
`tdd/integration/services/test_home_persistence.py`.

`generate_agent_config` (12.5) is the SINGLE PRODUCER of the SECOND file an
agent step carries: `/workspace/.control/agent.<step_execution_id>.json`,
consumed by `runner_common.agent_wrapper` via `runner_common.agent_config`.
It is a separate file, not extra keys on the step config, for two reasons
that are both load-bearing:

1. `run.py` deletes the step config in a `finally` BEFORE the command runs
   (consume-once, 12.3) - an agent payload carried there would be
   unreadable by the wrapper.
2. The step JWT and the provider API key live in the step config; the
   wrapper never needs either, and a file it opens must not carry them.

The producer<->consumer round trip is pinned by
`tdd/unit/control_runtime/test_agent_config_contract.py` (cross-agent
contract #1).
"""
import math
from typing import Any, Dict, List, Optional


def generate_step_config(
    step_id: str,
    step_run_id: str,
    execution_key: str,
    command: str,
    backend_url: str,
    auth_token: str,
    environment: Optional[Dict[str, str]] = None,
    timeout_seconds: int = 3600,
    working_directory: str = "/workspace/repo",
    shell: str = "bash",
) -> Dict[str, Any]:
    """
    Generate the step config payload for the in-container control runtime.

    SINGLE PRODUCER of the config file contract (R3): every key here must
    be understood by the consumer (`images/base/control/config.py`); the
    contract test asserts consumer-keys are a superset of these.

    Args:
        step_id: Step execution ID
        step_run_id: Step run ID
        execution_key: Unique execution key
        command: RAW command string (the runtime shell-wraps it)
        backend_url: Backend API URL
        auth_token: Authentication token (frozen key name - never "token")
        environment: Additional environment variables
        timeout_seconds: Execution timeout
        working_directory: Working directory for command (frozen key name -
            never "working_dir")
        shell: Shell the runtime wraps the command with (sourced from step
            config; default "bash", images without bash declare e.g. "sh")

    Returns:
        Step configuration dictionary
    """
    config = {
        "step_id": step_id,
        "step_run_id": step_run_id,
        "execution_key": execution_key,
        "command": command,
        "backend_url": backend_url,
        "auth_token": auth_token,
        "environment": environment or {},
        "timeout_seconds": timeout_seconds,
        "working_directory": working_directory,
        "shell": shell,
    }

    return config


# --- Agent config (12.5) ----------------------------------------------------

#: Wire version of the agent config file. The consumer
#: (`runner_common.agent_config`) pins this exact value and fails LOUDLY on
#: anything else - a wrapper that half-understands its instructions is worse
#: than one that refuses.
AGENT_CONFIG_VERSION = 1

#: The agent vocabulary (cross-agent contract #5). There is no default
#: agent: an unknown value fails at dispatch, never silently picks one.
#: `openai-harness` joined at M14: the LazyAF-supplied agent loop driving a
#: self-hosted OpenAI-compatible endpoint. It is an EXECUTOR, not a new step
#: type, which is why this is a one-word edit here.
AGENT_TYPES = ("claude-code", "gemini", "mock", "openai-harness")

#: The one agent that drives a `ModelEndpoint`. Spelled once; every check
#: below and in `pipeline_executor` reads it from here.
HARNESS_AGENT = "openai-harness"

#: `context.previous_step_logs` cap. The previous step's whole log stream can
#: be megabytes; a prompt is not a log sink. Head-truncated (the TAIL is what
#: a following step needs) with an explicit marker plus
#: `previous_step_logs_truncated: true`, never a silent slice.
PREVIOUS_STEP_LOGS_MAX_BYTES = 32 * 1024

PREVIOUS_STEP_LOGS_TRUNCATION_MARKER = (
    "[...truncated: only the last {kept} bytes of {total} are shown...]\n"
)


def truncate_previous_step_logs(
    logs: Optional[str], max_bytes: int = PREVIOUS_STEP_LOGS_MAX_BYTES
) -> tuple[Optional[str], bool]:
    """Head-truncate previous-step logs to `max_bytes`, loudly.

    Returns ``(text_or_None, truncated)``. Measured in UTF-8 BYTES (the wire
    unit), not characters, so a log full of multi-byte output cannot blow the
    cap. The marker is prepended so the agent can SEE that it is reading a
    tail rather than the whole thing.
    """
    if not logs:
        return None, False
    encoded = logs.encode("utf-8")
    if len(encoded) <= max_bytes:
        return logs, False
    kept = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    marker = PREVIOUS_STEP_LOGS_TRUNCATION_MARKER.format(
        kept=len(kept.encode("utf-8")), total=len(encoded)
    )
    return marker + kept, True


# --- Curated spec context (12.6.6) -----------------------------------------
#
# The budget is stated in TOKENS (the unit the operator and PLAN think in) and
# enforced in BYTES (the unit the wire and the kernel think in) - the same
# split `truncate_previous_step_logs` already uses one section above.
#
# WHY 4000 TOKENS AND NOT MORE. `runner_common.executors.claude` builds
# `["claude", "-p", config.prompt, ...]`: the whole prompt is ONE argv element,
# and Linux caps a single argv element at MAX_ARG_STRLEN = 131072 bytes (32
# pages, not tunable). The prompt already carries up to
# PREVIOUS_STEP_LOGS_MAX_BYTES (32 KiB) of previous-step logs plus an unbounded
# card description. 16 KiB of spec leaves ~80 KiB of headroom for the
# description before an agent step starts dying with E2BIG. This cap is not a
# nicety - it is what keeps the step dispatchable.

SPEC_CONTEXT_MAX_TOKENS = 4000

#: Bytes-per-token used by the estimator. There is no offline tokenizer for
#: the target models here and the model varies per step, so this is a
#: deliberately CONSERVATIVE constant for English prose plus source paths.
#: Every number derived from it is documented as an estimate.
SPEC_CONTEXT_BYTES_PER_TOKEN = 4

SPEC_CONTEXT_MAX_BYTES = SPEC_CONTEXT_MAX_TOKENS * SPEC_CONTEXT_BYTES_PER_TOKEN

#: Most related tests listed in one bundle. Paths are cheap (~80 bytes each);
#: this exists so a story with 400 refs cannot spend the whole budget on them.
SPEC_CONTEXT_MAX_TEST_REFS = 25

#: Most sibling story titles listed on the feature-only path.
SPEC_CONTEXT_MAX_STORY_TITLES = 20

#: Every truncation rule that fires leaves one of these IN the markdown, so an
#: agent reading a shrunk brief can SEE that it is shrunk (R1).
SPEC_CONTEXT_TRUNCATION_MARKER = (
    "> [spec context truncated to fit the {tokens}-token budget: {what}]\n"
)

#: Where the wrapper materialises the bundle inside the container. Mirrors
#: `execution.local_executor.CONTROL_CONFIG_DIR` deliberately rather than
#: importing it: this module must stay importable with no docker/config
#: dependency (the contract suite imports it standalone). The contract test
#: pins the two against each other, exactly as AGENT_CONFIG_VERSION is pinned.
SPEC_CONTEXT_DIR = "/workspace/.control"
SPEC_CONTEXT_FILENAME = "spec_context.md"
SPEC_CONTEXT_PATH = f"{SPEC_CONTEXT_DIR}/{SPEC_CONTEXT_FILENAME}"


def estimate_spec_context_tokens(markdown: str) -> int:
    """Estimated token count of a bundle. An ESTIMATE, never a measurement."""
    if not markdown:
        return 0
    return math.ceil(
        len(markdown.encode("utf-8")) / SPEC_CONTEXT_BYTES_PER_TOKEN
    )


def validate_spec_context(spec_context: Optional[Dict[str, Any]]) -> None:
    """Last gate before the wire for the 12.6.6 curated spec bundle.

    `None` is the clean no-op and passes. Anything else must be a dict
    carrying non-empty `markdown` inside the byte budget.

    This is a LOUD refusal at dispatch on purpose: a bundle that slipped the
    assembler's truncation would otherwise arrive oversized and kill the CLI
    with E2BIG twenty minutes into a paid run, with nothing in the log naming
    the reason.
    """
    if spec_context is None:
        return
    if not isinstance(spec_context, dict):
        raise ValueError(
            "spec_context must be an object or None; got "
            f"{type(spec_context).__name__}"
        )
    markdown = spec_context.get("markdown")
    if not isinstance(markdown, str) or not markdown:
        raise ValueError(
            "spec_context must carry a non-empty 'markdown' string, or be "
            "None - an empty bundle is spelled None, never {}"
        )
    size = len(markdown.encode("utf-8"))
    if size > SPEC_CONTEXT_MAX_BYTES:
        raise ValueError(
            f"spec_context is {size} bytes, over the {SPEC_CONTEXT_MAX_BYTES}-"
            f"byte ({SPEC_CONTEXT_MAX_TOKENS}-token) budget; the assembler "
            "must truncate before dispatch"
        )


# --- The endpoint / harness blocks (M14, wave8 s4.1) ------------------------
#
# Two ADDITIVE optional top-level keys, and `version` stays 1. This follows
# the precedent this module already documents for `spec_context`: an additive
# optional key that an old consumer ignores and a new one defaults does not
# justify a version bump, because bumping strands every runner agent in the
# field mid-phase.
#
# The key sets are declared as data so the contract test can assert on them
# by identity rather than by re-typing them (R3).

ENDPOINT_BLOCK_KEYS = (
    "id",
    "name",
    "base_url",
    "model",
    "server_kind",
    "reach",
    "auth_style",
    "auth_env",
    "auth_header",
    "request_timeout_seconds",
    "capabilities",
    "pricing",
)
ENDPOINT_CAPABILITY_KEYS = (
    "supports_tools",
    "supports_streaming",
    "reports_usage",
    "context_window",
    "max_output_tokens",
    "probe_status",
    "probed_at",
    "probed_from",
    "probe_age_seconds",
    "stale",
)
ENDPOINT_PRICING_KEYS = ("gpu_node_id", "gpu_fraction", "priced")
HARNESS_BLOCK_KEYS = (
    "mode",
    "max_iterations",
    "max_total_tokens",
    "time_budget_seconds",
    "max_tool_calls_per_turn",
    "shell_timeout_seconds",
    "tool_output_max_bytes",
    "temperature",
    "top_p",
    "seed",
    "require_changes",
    "debug_transcript",
)


def validate_endpoint_block(agent: str, endpoint: Optional[Dict[str, Any]]) -> None:
    """Raise ValueError when the agent and the endpoint block disagree.

    - `openai-harness` REQUIRES a block with non-empty `base_url` and `model`,
      and `capabilities.probe_status != "unprobed"`. Dispatching a harness
      step against an unprobed endpoint would put a 60s capability discovery
      inside a 30-minute step's timeout budget and would only then find out
      the model cannot tool-call.
    - ANY OTHER AGENT MUST NOT CARRY ONE. An endpoint block on a `claude-code`
      step is an authoring mistake whose silent acceptance would be a step
      that looks self-hosted in the UI and bills Anthropic.
    """
    if agent != HARNESS_AGENT:
        if endpoint is not None:
            raise ValueError(
                f"agent {agent!r} must not carry an `endpoint` block: an "
                f"endpoint on a non-harness step is a step that would look "
                f"self-hosted in the UI and bill a commercial provider. Use "
                f"agent: {HARNESS_AGENT!r} to run against a model endpoint."
            )
        return

    if not isinstance(endpoint, dict):
        raise ValueError(
            f"agent {HARNESS_AGENT!r} requires an `endpoint` block naming the "
            f"OpenAI-compatible server to drive; there is no default endpoint "
            f"(guessing which GPU to bill is not a recoverable mistake)"
        )
    for key in ("base_url", "model"):
        if not endpoint.get(key):
            raise ValueError(
                f"agent {HARNESS_AGENT!r} requires endpoint.{key}; refusing to "
                f"dispatch a harness step without one"
            )
    capabilities = endpoint.get("capabilities") or {}
    if capabilities.get("probe_status") == "unprobed":
        name = endpoint.get("name") or endpoint.get("id") or "?"
        raise ValueError(
            f"endpoint {name!r} has never been probed; POST "
            f"/api/model-endpoints/{endpoint.get('id')}/probe first. A "
            f"30-minute agent step is not the place to discover that the "
            f"model cannot tool-call."
        )


def validate_harness_block(agent: str, harness: Optional[Dict[str, Any]]) -> None:
    """The `harness` block is meaningful only for the harness agent."""
    if harness in (None, {}):
        return
    if not isinstance(harness, dict):
        raise ValueError(
            f"harness must be an object or None; got {type(harness).__name__}"
        )
    if agent != HARNESS_AGENT:
        raise ValueError(
            f"agent {agent!r} must not carry a `harness` block: the loop "
            f"budgets it configures exist only inside the LazyAF agent harness"
        )


def generate_agent_config(
    *,
    agent: str,
    prompt: str,
    model: Optional[str] = None,
    agents_json: Optional[str] = None,
    stream: bool = True,
    card_id: Optional[str] = None,
    card_title: str = "",
    card_description: str = "",
    step_index: int = 0,
    step_name: str = "",
    previous_step_name: Optional[str] = None,
    previous_step_logs: Optional[str] = None,
    repo_id: str = "",
    workdir: str = "/workspace/repo",
    base_branch: str = "main",
    branch: Optional[str] = None,
    remote_url: str = "",
    commit_enabled: bool = True,
    commit_message: Optional[str] = None,
    commit_author_name: str = "LazyAF Agent",
    commit_author_email: str = "agent@lazyaf.local",
    push: bool = True,
    allow_empty: bool = False,
    mock_config: Optional[Dict[str, Any]] = None,
    role: Optional[str] = None,
    spec_context: Optional[Dict[str, Any]] = None,
    endpoint: Optional[Dict[str, Any]] = None,
    harness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the agent payload file for an agent step (12.5).

    SINGLE PRODUCER of the agent config contract (R3): every key here must
    be understood by `runner_common.agent_config.load_agent_config`, pinned
    by the round-trip contract test.

    Args:
        agent: One of AGENT_TYPES. Selects the executor in the wrapper AND
            the default image backend-side.
        prompt: The work itself, rendered BACKEND-side (`agent_prompt.py`) -
            the container never re-templates.
        model: Optional model override (`ExecutorConfig.model` -> `--model`).
        agents_json: Resolved AgentFile definitions as a JSON string
            (`claude --agents`); the container has no DB.
        stream: claude `--output-format stream-json --verbose` when true,
            `json` when false (R1: a 20-minute dark step is unacceptable).
        card_id/card_title/card_description/step_index/step_name: the `task`
            block - commit message, forensics, and the card join.
        previous_step_name/previous_step_logs: the `context` block, replacing
            the legacy `.lazyaf-context/` channel with a DB-sourced field.
            Capped at PREVIOUS_STEP_LOGS_MAX_BYTES.
        repo_id/workdir/base_branch/branch/remote_url: the `repo` block - an
            agent commits and pushes; a script step does not.
        commit_*/push/allow_empty: the `commit` block (US-2 branch).
        mock_config: deterministic MockExecutor behavior (the dogfood ratchet).
        role: M13 fan-out attribution. `None` in 12.5, on the wire NOW so
            M13 is not a retrofit.
        spec_context: The curated spec bundle (12.6.6) from
            `app.services.spec_context.build_spec_context`, or `None` when the
            card has no spec links / curation is switched off for the step.
            Its `markdown` is ALREADY inside `prompt`; it travels here as well
            so the wrapper can materialise it at SPEC_CONTEXT_PATH and log
            what it received. Refused at dispatch when it is over budget.
        endpoint: The `ModelEndpoint` SNAPSHOT (M14) an `openai-harness` step
            drives, or None for every other agent. A snapshot, not a live
            reference: a step must behave identically if someone re-probes the
            endpoint mid-run, and M13 needs to attribute a result to the
            capabilities that were actually in force. It carries `auth_env`
            (the NAME of the container-side variable) and NEVER a key value.
        harness: The harness's budgets and loop shape (M14). Meaningful only
            for `openai-harness`.

    Returns:
        The agent configuration dictionary.

    Raises:
        ValueError: on an unknown agent (there is NO default agent), on a
            spec_context that is malformed or over the token budget, on an
            endpoint/agent mismatch, on an unprobed endpoint, or when `model`
            and `endpoint.model` disagree.
    """
    if agent not in AGENT_TYPES:
        raise ValueError(
            f"unknown agent {agent!r}: valid agents are {', '.join(AGENT_TYPES)}"
        )

    validate_spec_context(spec_context)
    validate_endpoint_block(agent, endpoint)
    validate_harness_block(agent, harness)

    if agent == HARNESS_AGENT:
        # The top-level `model` is the 12.5 contract every executor reads; the
        # endpoint block is self-contained so `HarnessExecutor` needs exactly
        # one argument. Both come from ONE source and the contract test pins
        # that they are equal - a mismatch would mean the usage row's model and
        # the model actually driven are two different strings.
        endpoint_model = (endpoint or {}).get("model")
        if model is None:
            model = endpoint_model
        elif model != endpoint_model:
            raise ValueError(
                f"agent config model {model!r} disagrees with endpoint.model "
                f"{endpoint_model!r}; they are two spellings of one fact and "
                f"the producer sets both from the same source"
            )
        if agents_json is not None:
            # Seam left open on purpose (wave8 s12): the harness does not do
            # subagents. Multi-agent shapes belong in the graph, where they are
            # visible and costed per role, not inside one step's loop.
            raise ValueError(
                f"agent {HARNESS_AGENT!r} does not support `agents_json`: the "
                f"harness runs one loop, and multi-agent shapes belong in the "
                f"graph where they are visible and costed per role"
            )

    logs, truncated = truncate_previous_step_logs(previous_step_logs)

    return {
        "version": AGENT_CONFIG_VERSION,
        "agent": agent,
        "model": model,
        "stream": bool(stream),
        "prompt": prompt,
        "agents_json": agents_json,
        "task": {
            "card_id": card_id,
            "card_title": card_title,
            "card_description": card_description,
            "step_index": step_index,
            "step_name": step_name,
        },
        "context": {
            "previous_step_name": previous_step_name,
            "previous_step_logs": logs,
            "previous_step_logs_truncated": truncated,
        },
        "repo": {
            "repo_id": repo_id,
            "workdir": workdir,
            "base_branch": base_branch,
            "branch": branch,
            "remote_url": remote_url,
        },
        "commit": {
            "enabled": bool(commit_enabled),
            "message": commit_message
            or default_commit_message(card_title or step_name),
            "author_name": commit_author_name,
            "author_email": commit_author_email,
            "push": bool(push),
            "allow_empty": bool(allow_empty),
        },
        "mock_config": mock_config,
        "role": role,
        "spec_context": spec_context,
        # M14, additive and LAST: a pre-14 consumer ignores both; a 14 consumer
        # defaults them to None/{} (runner_common.agent_config), which is why
        # `version` stays 1 and no runner agent in the field is stranded.
        "endpoint": endpoint,
        "harness": dict(harness or {}) if agent == HARNESS_AGENT else {},
    }


def default_commit_message(subject: str) -> str:
    """The commit subject an agent step lands its work under."""
    subject = (subject or "agent work").strip().splitlines()[0]
    return f"feat: {subject}\n\nImplemented by LazyAF agent"


def agent_config_keys() -> List[str]:
    """Top-level keys of the agent config, for the contract test."""
    return [
        "version",
        "agent",
        "model",
        "stream",
        "prompt",
        "agents_json",
        "task",
        "context",
        "repo",
        "commit",
        "mock_config",
        "role",
        "spec_context",
        "endpoint",
        "harness",
    ]
