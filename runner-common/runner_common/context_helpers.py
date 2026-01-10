"""
Context directory helper functions for pipeline runs.

This module manages the .lazyaf-context directory which stores:
- Pipeline run metadata
- Step logs
- Step completion status

The context directory persists between pipeline steps when continue_in_context is True.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any


CONTEXT_DIR = ".lazyaf-context"


def get_context_path(workspace: Path) -> Path:
    """
    Get the path to the context directory.

    Args:
        workspace: Path to the workspace directory

    Returns:
        Path to .lazyaf-context directory
    """
    return workspace / CONTEXT_DIR


def context_exists(workspace: Path) -> bool:
    """
    Check if the context directory exists.

    Args:
        workspace: Path to the workspace directory

    Returns:
        True if context directory exists
    """
    return get_context_path(workspace).is_dir()


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
    context_path = get_context_path(workspace)

    # Create context directory
    context_path.mkdir(parents=True, exist_ok=True)

    # Create logs subdirectory
    logs_dir = context_path / "logs"
    logs_dir.mkdir(exist_ok=True)

    # Create metadata.json if it doesn't exist
    metadata_file = context_path / "metadata.json"
    if not metadata_file.exists():
        metadata = {
            "pipeline_run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_file.write_text(json.dumps(metadata, indent=2))

    return context_path


def _get_log_filename(step_index: int, step_name: Optional[str] = None) -> str:
    """Get the log filename for a step."""
    if step_name:
        return f"step_{step_index}_{step_name}.log"
    return f"step_{step_index}.log"


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
    context_path = get_context_path(workspace)
    logs_dir = context_path / "logs"
    log_file = logs_dir / _get_log_filename(step_index, step_name)

    mode = "a" if append else "w"
    log_file.write_text(log_content) if not append else _append_to_file(log_file, log_content)


def _append_to_file(path: Path, content: str) -> None:
    """Append content to a file."""
    with open(path, "a") as f:
        f.write(content)


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
    context_path = get_context_path(workspace)
    logs_dir = context_path / "logs"
    log_file = logs_dir / _get_log_filename(step_index, step_name)

    if not log_file.exists():
        return None

    return log_file.read_text()


def read_metadata(workspace: Path) -> Optional[dict]:
    """
    Read the context metadata.

    Args:
        workspace: Path to the workspace directory

    Returns:
        Metadata dict, or None if context doesn't exist
    """
    context_path = get_context_path(workspace)
    metadata_file = context_path / "metadata.json"

    if not metadata_file.exists():
        return None

    return json.loads(metadata_file.read_text())


def update_metadata(workspace: Path, key: str, value: Any) -> None:
    """
    Update a field in the context metadata.

    Args:
        workspace: Path to the workspace directory
        key: Metadata key to update
        value: Value to set (must be JSON-serializable)
    """
    context_path = get_context_path(workspace)
    metadata_file = context_path / "metadata.json"

    # Read existing metadata
    metadata = {}
    if metadata_file.exists():
        metadata = json.loads(metadata_file.read_text())

    # Update and write back
    metadata[key] = value
    metadata_file.write_text(json.dumps(metadata, indent=2))


def cleanup_context(workspace: Path) -> None:
    """
    Remove the context directory.

    Args:
        workspace: Path to the workspace directory
    """
    context_path = get_context_path(workspace)
    if context_path.exists():
        shutil.rmtree(context_path)
