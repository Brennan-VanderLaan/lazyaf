"""
Step config producer - Phase 12.3.

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
"""
from typing import Any, Dict, Optional


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
