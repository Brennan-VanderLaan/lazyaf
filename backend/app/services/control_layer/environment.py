"""
Step Environment - Phase 12.3

Defines environment variables for step execution.
Ensures HOME and caches persist in workspace volume.
"""
import os
from typing import Dict


def get_step_environment(
    base_env: Dict[str, str] = None,
    workspace_root: str = "/workspace",
) -> Dict[str, str]:
    """
    Get environment variables for step execution.

    Sets up:
    - HOME at /workspace/home (persists across steps)
    - XDG directories for cache/config persistence
    - PATH includes ~/.local/bin for user tools
    - pip/npm configuration for user installs

    Args:
        base_env: Base environment to extend
        workspace_root: Root of workspace volume

    Returns:
        Dict of environment variables
    """
    if base_env is None:
        base_env = {}

    home_dir = f"{workspace_root}/home"
    cache_dir = f"{home_dir}/.cache"
    config_dir = f"{home_dir}/.config"
    local_dir = f"{home_dir}/.local"
    local_bin = f"{local_dir}/bin"
    npm_global_dir = f"{home_dir}/.npm-global"
    npm_global_bin = f"{npm_global_dir}/bin"

    # Build PATH with user directories first
    existing_path = base_env.get("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    path_parts = [local_bin, npm_global_bin] + existing_path.split(":")
    new_path = ":".join(path_parts)

    env = {
        # HOME directory in workspace (persists)
        "HOME": home_dir,

        # XDG directories for proper cache/config locations
        "XDG_CACHE_HOME": cache_dir,
        "XDG_CONFIG_HOME": config_dir,
        "XDG_DATA_HOME": f"{local_dir}/share",

        # PATH includes user tool directories
        "PATH": new_path,

        # pip configuration for user installs
        "PIP_CACHE_DIR": f"{cache_dir}/pip",
        "PIP_USER": "1",
        "PYTHONUSERBASE": local_dir,

        # npm configuration for global packages
        "NPM_CONFIG_PREFIX": npm_global_dir,

        # LazyAF identifiers
        "LAZYAF_WORKSPACE": workspace_root,
        "LAZYAF_REPO": f"{workspace_root}/repo",
    }

    # Merge with base env (base takes precedence)
    result = {**env, **base_env}

    return result


def get_lazyaf_env_vars(
    pipeline_run_id: str,
    step_run_id: str,
    step_index: int,
    repo_id: str = "",
    card_id: str = "",
) -> Dict[str, str]:
    """
    Get LazyAF-specific environment variables.

    Args:
        pipeline_run_id: Pipeline run ID
        step_run_id: Step run ID
        step_index: Index of step in pipeline
        repo_id: Repository ID
        card_id: Card ID if applicable

    Returns:
        Dict of LazyAF environment variables
    """
    return {
        "LAZYAF_PIPELINE_RUN_ID": pipeline_run_id,
        "LAZYAF_STEP_RUN_ID": step_run_id,
        "LAZYAF_STEP_INDEX": str(step_index),
        "LAZYAF_REPO_ID": repo_id,
        "LAZYAF_CARD_ID": card_id,
    }
