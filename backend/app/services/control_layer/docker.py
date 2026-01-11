"""
Docker Configuration - Phase 12.3

Provides volume mount configuration for step containers.
"""
from typing import Any, Dict, List, Optional


def get_volume_mounts(
    workspace_volume: str,
    repo_path: str = "/workspace/repo",
    control_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get volume mount configuration for step container.

    Mounts:
    - Workspace volume at /workspace (includes home for persistence)
    - Control directory at /workspace/.control (step config)

    Args:
        workspace_volume: Name of workspace Docker volume
        repo_path: Path to repo within workspace
        control_dir: Optional host path for control directory

    Returns:
        List of mount configurations
    """
    mounts = []

    # Main workspace volume
    mounts.append({
        "type": "volume",
        "source": workspace_volume,
        "target": "/workspace",
        "read_only": False,
    })

    # Control directory (bind mount from host if provided)
    if control_dir:
        mounts.append({
            "type": "bind",
            "source": control_dir,
            "target": "/workspace/.control",
            "read_only": True,
        })

    return mounts


def get_container_config(
    image: str,
    command: Optional[str] = None,
    workspace_volume: str = "",
    control_dir: Optional[str] = None,
    environment: Optional[Dict[str, str]] = None,
    network: str = "lazyaf-network",
    labels: Optional[Dict[str, str]] = None,
    memory_limit: str = "2g",
    cpu_limit: float = 2.0,
) -> Dict[str, Any]:
    """
    Get full container configuration for docker-py.

    Args:
        image: Docker image name
        command: Optional command override
        workspace_volume: Name of workspace volume
        control_dir: Host path for control directory
        environment: Environment variables
        network: Docker network name
        labels: Container labels
        memory_limit: Memory limit (e.g., "2g")
        cpu_limit: CPU limit (e.g., 2.0)

    Returns:
        Container configuration dict for docker-py
    """
    config = {
        "image": image,
        "detach": True,
        "network": network,
        "environment": environment or {},
        "labels": labels or {},
        "mem_limit": memory_limit,
        "nano_cpus": int(cpu_limit * 1e9),
        "working_dir": "/workspace/repo",
    }

    # Add command if provided
    if command:
        config["command"] = command

    # Add volume mounts
    if workspace_volume:
        config["volumes"] = {
            workspace_volume: {
                "bind": "/workspace",
                "mode": "rw",
            },
        }

        if control_dir:
            config["volumes"][control_dir] = {
                "bind": "/workspace/.control",
                "mode": "ro",
            }

    return config


def get_step_labels(
    step_id: str,
    step_run_id: str,
    pipeline_run_id: str,
    execution_key: str,
) -> Dict[str, str]:
    """
    Get container labels for step identification.

    Args:
        step_id: Step execution ID
        step_run_id: Step run ID
        pipeline_run_id: Pipeline run ID
        execution_key: Unique execution key

    Returns:
        Dict of labels
    """
    return {
        "lazyaf.managed": "true",
        "lazyaf.step_id": step_id,
        "lazyaf.step_run_id": step_run_id,
        "lazyaf.pipeline_run_id": pipeline_run_id,
        "lazyaf.execution_key": execution_key,
    }
