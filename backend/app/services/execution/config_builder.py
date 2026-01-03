"""
Config Builder for Phase 12.4.

Builds ExecutionConfig from step configuration (step_type + step_config).
Handles:
- Default images for script/agent steps
- Command wrapping for shell execution
- Environment variable merging
- Working directory configuration
"""

from typing import Any, Dict, List, Optional

from .local_executor import ExecutionConfig


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

    Returns:
        ExecutionConfig ready for LocalExecutor
    """
    # Get image
    image = _get_image(step_type, step_config)

    # Get command
    command = _get_command(step_type, step_config)

    # Get environment
    environment = _get_environment(step_config, use_control_layer)

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

    Agent steps run the AI agent CLI (claude, gemini, etc.)
    with the appropriate configuration.
    """
    runner_type = step_config.get("runner_type", "claude-code")
    title = step_config.get("title", "")
    description = step_config.get("description", "")

    # Combine title and description for prompt
    prompt = f"{title}\n\n{description}".strip()

    if runner_type in ("claude-code", "claude", "any"):
        # Claude Code CLI invocation
        return [
            "claude",
            "-p",  # Print mode (non-interactive)
            prompt,
        ]
    elif runner_type == "gemini":
        # Gemini SDK invocation (placeholder)
        return [
            "python",
            "-m",
            "gemini_agent",
            prompt,
        ]
    else:
        # Unknown runner, default to claude
        return ["claude", "-p", prompt]


def _get_environment(
    step_config: Dict[str, Any],
    use_control_layer: bool = False,
) -> Dict[str, str]:
    """
    Build environment variables for execution.

    Includes:
    - User-specified environment from step_config
    - HOME set to /workspace/home for cache persistence (when using control layer)
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

    return env
