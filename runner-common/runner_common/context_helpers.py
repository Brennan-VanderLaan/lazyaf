"""
Context directory helper functions for pipeline runs.

This module manages the .lazyaf-context directory which stores:
- Pipeline run metadata
- Step logs
- Step completion status

The context directory persists between pipeline steps when continue_in_context is True.
"""

from pathlib import Path
from typing import Optional, Any


CONTEXT_DIR = ".lazyaf-context"


def init_context(workspace: Path, run_id: str) -> Path:
    """
    Initialize the context directory for a pipeline run.

    Creates:
    - .lazyaf-context/
    - .lazyaf-context/metadata.json
    - .lazyaf-context/logs/

    Args:
        workspace: Path to the workspace directory
        run_id: Pipeline run ID

    Returns:
        Path to the context directory
    """
    raise NotImplementedError("TODO: Implement init_context()")


def write_step_log(
    workspace: Path,
    step_index: int,
    log_content: str,
    step_name: Optional[str] = None,
    append: bool = False,
) -> None:
    """
    Write log content for a step.

    Args:
        workspace: Path to the workspace directory
        step_index: Step index (0-based)
        log_content: Log content to write
        step_name: Optional step name for filename
        append: If True, append to existing log
    """
    raise NotImplementedError("TODO: Implement write_step_log()")


def read_step_log(workspace: Path, step_index: int, step_name: Optional[str] = None) -> Optional[str]:
    """
    Read log content for a step.

    Args:
        workspace: Path to the workspace directory
        step_index: Step index (0-based)
        step_name: Optional step name for filename

    Returns:
        Log content as string, or None if log doesn't exist
    """
    raise NotImplementedError("TODO: Implement read_step_log()")


def update_metadata(workspace: Path, key: str, value: Any) -> None:
    """
    Update a field in the context metadata.

    Args:
        workspace: Path to the workspace directory
        key: Metadata key to update
        value: Value to set (must be JSON-serializable)
    """
    raise NotImplementedError("TODO: Implement update_metadata()")


def read_metadata(workspace: Path) -> Optional[dict]:
    """
    Read the context metadata.

    Args:
        workspace: Path to the workspace directory

    Returns:
        Metadata dict, or None if context doesn't exist
    """
    raise NotImplementedError("TODO: Implement read_metadata()")


def cleanup_context(workspace: Path) -> None:
    """
    Remove the context directory.

    Args:
        workspace: Path to the workspace directory
    """
    raise NotImplementedError("TODO: Implement cleanup_context()")


def get_context_path(workspace: Path) -> Path:
    """
    Get the path to the context directory.

    Args:
        workspace: Path to the workspace directory

    Returns:
        Path to .lazyaf-context directory
    """
    raise NotImplementedError("TODO: Implement get_context_path()")


def context_exists(workspace: Path) -> bool:
    """
    Check if the context directory exists.

    Args:
        workspace: Path to the workspace directory

    Returns:
        True if context directory exists
    """
    raise NotImplementedError("TODO: Implement context_exists()")
