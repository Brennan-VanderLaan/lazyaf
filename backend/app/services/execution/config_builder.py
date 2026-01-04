"""
Config Builder for Phase 12.4 + 12.5.

Builds ExecutionConfig from step configuration (step_type + step_config).
Handles:
- Default images for script/agent steps
- Command wrapping for shell execution
- Environment variable merging (including API keys for agents)
- Working directory configuration
- Agent wrapper invocation for agent steps
"""

from typing import Any, Dict, List, Optional

from .local_executor import ExecutionConfig
from app.config import get_settings


# Default images for each step type
DEFAULT_SCRIPT_IMAGE = "lazyaf-base:latest"

DEFAULT_AGENT_IMAGES = {
    "claude-code": "lazyaf-claude:latest",
    "gemini": "lazyaf-gemini:latest",
    "any": "lazyaf-claude:latest",  # Default to Claude
}


def build_execution_config(
    step_type: str,
    step_config: Dict[str, Any],
    workspace_path: str,
    timeout_seconds: int = 3600,
    use_control_layer: bool = False,
    backend_url: Optional[str] = None,
    agent_config: Optional[Dict[str, Any]] = None,
) -> ExecutionConfig:
    """
    Build an ExecutionConfig from step configuration.

    Args:
        step_type: Type of step (script, docker, agent)
        step_config: Step configuration dict from pipeline YAML
        workspace_path: Path to workspace directory
        timeout_seconds: Execution timeout
        use_control_layer: Whether to use control layer for execution
        backend_url: Backend URL for control layer communication
        agent_config: Agent-specific config for agent steps (repo URL, branch, etc.)

    Returns:
        ExecutionConfig ready for LocalExecutor
    """
    # Get image
    image = _get_image(step_type, step_config)

    # Get command
    command = _get_command(step_type, step_config)

    # Get environment (includes API keys for agent steps)
    environment = _get_environment(step_config, use_control_layer, step_type)

    # Get working directory
    working_dir = step_config.get("working_dir", "/workspace")

    return ExecutionConfig(
        image=image,
        command=command,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        environment=environment,
        working_dir=working_dir,
        use_control_layer=use_control_layer,
        backend_url=backend_url,
        agent_config=agent_config,
    )


def _get_image(step_type: str, step_config: Dict[str, Any]) -> str:
    """
    Determine the Docker image for execution.

    Priority:
    1. Explicit image in step_config
    2. Default image for step type
    """
    # Check for explicit image
    if "image" in step_config:
        return step_config["image"]

    # Use default based on step type
    if step_type == "script":
        return DEFAULT_SCRIPT_IMAGE
    elif step_type == "docker":
        # Docker steps should always have an image
        raise ValueError("Docker step must specify an image")
    elif step_type == "agent":
        runner_type = step_config.get("runner_type", "any")
        return DEFAULT_AGENT_IMAGES.get(runner_type, DEFAULT_AGENT_IMAGES["any"])
    else:
        return DEFAULT_SCRIPT_IMAGE


def _get_command(step_type: str, step_config: Dict[str, Any]) -> List[str]:
    """
    Build the command list for execution.

    Script commands are wrapped in bash -c for proper shell execution.
    Docker commands can be string or list.
    Agent commands are constructed from the agent configuration.
    """
    if step_type == "script":
        return _wrap_script_command(step_config.get("command", ""))
    elif step_type == "docker":
        return _parse_docker_command(step_config.get("command", ""))
    elif step_type == "agent":
        return _build_agent_command(step_config)
    else:
        # Unknown type, treat as script
        return _wrap_script_command(step_config.get("command", ""))


def _wrap_script_command(command: str) -> List[str]:
    """
    Wrap a script command in bash -c for execution.

    This handles:
    - Multiline commands
    - Shell features (pipes, redirects, etc.)
    - Environment variable expansion
    """
    if not command:
        return ["bash", "-c", "true"]

    # Strip leading/trailing whitespace but preserve internal structure
    command = command.strip()

    return ["bash", "-c", command]


def _parse_docker_command(command) -> List[str]:
    """
    Parse a docker command (can be string or list).
    """
    if not command:
        return []

    if isinstance(command, list):
        return command
    elif isinstance(command, str):
        # Wrap in bash for proper execution
        return ["bash", "-c", command]
    else:
        return [str(command)]


def _build_agent_command(step_config: Dict[str, Any]) -> List[str]:
    """
    Build command for agent step execution.

    Agent steps invoke the agent wrapper script, which handles:
    - Git clone/checkout/branch
    - Prompt building
    - Agent CLI invocation (Claude/Gemini)
    - Commit and push

    The wrapper reads agent config from step_config.json written by LocalExecutor.
    """
    # Agent steps always invoke the wrapper script
    # The wrapper handles git, prompt building, and CLI invocation
    return ["python", "/control/agent_wrapper.py"]


def _get_environment(
    step_config: Dict[str, Any],
    use_control_layer: bool = False,
    step_type: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build environment variables for execution.

    Includes:
    - User-specified environment from step_config
    - HOME set to /workspace/home for cache persistence (when using control layer)
    - API keys for agent steps (ANTHROPIC_API_KEY, GEMINI_API_KEY)
    """
    env = {}

    # Add user-specified environment
    if "environment" in step_config:
        user_env = step_config["environment"]
        if isinstance(user_env, dict):
            env.update({str(k): str(v) for k, v in user_env.items()})

    # Set HOME for cache persistence when using control layer
    if use_control_layer:
        env.setdefault("HOME", "/workspace/home")

    # Add API keys for agent steps
    if step_type == "agent":
        settings = get_settings()
        runner_type = step_config.get("runner_type", "claude-code")

        if runner_type in ("claude-code", "claude", "any"):
            if settings.anthropic_api_key:
                env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        elif runner_type == "gemini":
            if settings.gemini_api_key:
                env["GEMINI_API_KEY"] = settings.gemini_api_key

    return env
