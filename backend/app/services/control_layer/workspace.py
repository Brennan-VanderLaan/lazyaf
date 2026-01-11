"""
Workspace Layout - Phase 12.3

Defines workspace directory structure and initialization.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class WorkspaceLayout:
    """
    Defines the workspace directory structure.

    /workspace/
    ├── repo/           # Git repository checkout
    ├── home/           # Persistent HOME directory
    │   ├── .cache/     # Cache (pip, etc.)
    │   ├── .config/    # Config files
    │   └── .local/
    │       └── bin/    # User-installed tools
    └── .control/       # Control layer files
        └── step_config.json
    """
    workspace_root: str = "/workspace"

    @property
    def root(self) -> str:
        """Root workspace directory."""
        return self.workspace_root

    @property
    def repo(self) -> str:
        """Repository checkout directory."""
        return f"{self.workspace_root}/repo"

    @property
    def home(self) -> str:
        """Persistent HOME directory."""
        return f"{self.workspace_root}/home"

    @property
    def control(self) -> str:
        """Control layer directory."""
        return f"{self.workspace_root}/.control"

    @property
    def required_directories(self) -> List[str]:
        """List of directories that must exist."""
        return [
            "/workspace",
            "/workspace/repo",
            "/workspace/home",
            "/workspace/.control",
            "/control",  # Control layer script location
        ]

    @property
    def control_files(self) -> List[str]:
        """Files in the control directory."""
        return [
            "step_config.json",
        ]

    @property
    def home_subdirectories(self) -> List[str]:
        """Subdirectories under home."""
        return [
            ".cache",
            ".config",
            ".local",
            ".local/bin",
            ".local/share",
        ]


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
) -> Dict[str, Any]:
    """
    Generate step configuration for the control directory.

    Args:
        step_id: Step execution ID
        step_run_id: Step run ID
        execution_key: Unique execution key
        command: Command to execute
        backend_url: Backend API URL
        auth_token: Authentication token
        environment: Additional environment variables
        timeout_seconds: Execution timeout
        working_directory: Working directory for command

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
    }

    return config


def write_step_config(
    control_dir: str,
    config: Dict[str, Any],
) -> Path:
    """
    Write step configuration to control directory.

    Args:
        control_dir: Path to control directory
        config: Step configuration dictionary

    Returns:
        Path to written config file
    """
    control_path = Path(control_dir)
    control_path.mkdir(parents=True, exist_ok=True)

    config_path = control_path / "step_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    return config_path


def initialize_workspace(workspace: Path) -> None:
    """
    Initialize workspace directory structure.

    Creates:
    - repo/ directory
    - home/ directory with subdirectories
    - .control/ directory

    Preserves existing content.

    Args:
        workspace: Path to workspace root
    """
    # Create repo directory
    repo_dir = workspace / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    # Create home directory with subdirectories
    home_dir = workspace / "home"
    home_subdirs = [
        ".cache",
        ".cache/pip",
        ".config",
        ".local",
        ".local/bin",
        ".local/share",
        ".npm-global",
        ".npm-global/bin",
    ]

    home_dir.mkdir(parents=True, exist_ok=True)
    for subdir in home_subdirs:
        (home_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Create control directory
    control_dir = workspace / ".control"
    control_dir.mkdir(parents=True, exist_ok=True)


def get_workspace_paths(workspace_root: str = "/workspace") -> Dict[str, str]:
    """
    Get common workspace paths.

    Args:
        workspace_root: Root of workspace

    Returns:
        Dict mapping names to paths
    """
    return {
        "root": workspace_root,
        "repo": f"{workspace_root}/repo",
        "home": f"{workspace_root}/home",
        "control": f"{workspace_root}/.control",
        "config": f"{workspace_root}/.control/step_config.json",
        "cache": f"{workspace_root}/home/.cache",
        "local_bin": f"{workspace_root}/home/.local/bin",
    }
