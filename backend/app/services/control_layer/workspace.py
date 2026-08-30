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
AGENT_TYPES = ("claude-code", "gemini", "mock")

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

    Returns:
        The agent configuration dictionary.

    Raises:
        ValueError: on an unknown agent (there is NO default agent).
    """
    if agent not in AGENT_TYPES:
        raise ValueError(
            f"unknown agent {agent!r}: valid agents are {', '.join(AGENT_TYPES)}"
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
    ]
