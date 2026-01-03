"""
Context directory helpers for pipeline step communication.

The .lazyaf-context/ directory is used to pass information between
pipeline steps, including logs, metadata, and step results.
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

# Name of the context directory
CONTEXT_DIR = ".lazyaf-context"


def init_context_directory(workspace: Path | str, pipeline_run_id: str) -> Path:
    """
    Initialize the context directory for a pipeline run.

    Creates .lazyaf-context/ with metadata.json if it doesn't exist.
    If it already exists, preserves the existing content (idempotent).

    Args:
        workspace: Workspace directory (usually /workspace/repo)
        pipeline_run_id: ID of the pipeline run

    Returns:
        Path to the context directory
    """
    workspace = Path(workspace)
    context_path = workspace / CONTEXT_DIR

    # Create directory if needed
    context_path.mkdir(exist_ok=True)

    # Create metadata.json if it doesn't exist
    metadata_file = context_path / "metadata.json"
    if not metadata_file.exists():
        metadata = {
            "pipeline_run_id": pipeline_run_id,
            "created_at": datetime.utcnow().isoformat(),
            "steps_completed": [],
        }
        metadata_file.write_text(json.dumps(metadata, indent=2))

    return context_path


def write_step_log(
    workspace: Path | str,
    step_index: int,
    step_id: str | None,
    step_name: str,
    logs: str,
) -> str:
    """
    Write step logs to the context directory.

    Args:
        workspace: Workspace directory
        step_index: Step index in the pipeline (0-based)
        step_id: Step ID (optional, used in filename if provided)
        step_name: Human-readable step name
        logs: Log content to write

    Returns:
        Filename of the created log file (not full path)
    """
    workspace = Path(workspace)
    context_path = workspace / CONTEXT_DIR

    # Generate filename
    if step_id:
        # Sanitize step_id for filename
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", step_id)
        filename = f"id_{safe_id}_{step_index:03d}.log"
    else:
        # Sanitize step name for filename
        safe_name = step_name.lower().replace(" ", "_")
        safe_name = re.sub(r"[^a-z0-9_]", "", safe_name)[:20]
        filename = f"step_{step_index:03d}_{safe_name}.log"

    log_path = context_path / filename
    log_path.write_text(logs)

    return filename


def read_step_log(workspace: Path | str, filename: str) -> str:
    """
    Read a step log from the context directory.

    Args:
        workspace: Workspace directory
        filename: Log filename (not full path)

    Returns:
        Log content

    Raises:
        FileNotFoundError: If the log file doesn't exist
    """
    workspace = Path(workspace)
    log_path = workspace / CONTEXT_DIR / filename

    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {filename}")

    return log_path.read_text()


def update_context_metadata(
    workspace: Path | str,
    step_index: int,
    step_name: str,
) -> None:
    """
    Update metadata.json with completed step information.

    Args:
        workspace: Workspace directory
        step_index: Step index that completed
        step_name: Step name
    """
    workspace = Path(workspace)
    metadata_path = workspace / CONTEXT_DIR / "metadata.json"

    if not metadata_path.exists():
        raise FileNotFoundError("Context metadata not found - initialize first")

    metadata = json.loads(metadata_path.read_text())

    metadata["steps_completed"].append({
        "index": step_index,
        "name": step_name,
        "completed_at": datetime.utcnow().isoformat(),
    })

    metadata_path.write_text(json.dumps(metadata, indent=2))


def get_context_metadata(workspace: Path | str) -> dict:
    """
    Get the context metadata.

    Args:
        workspace: Workspace directory

    Returns:
        Metadata dictionary

    Raises:
        FileNotFoundError: If context doesn't exist
    """
    workspace = Path(workspace)
    metadata_path = workspace / CONTEXT_DIR / "metadata.json"

    if not metadata_path.exists():
        raise FileNotFoundError("Context metadata not found")

    return json.loads(metadata_path.read_text())


def cleanup_context_directory(workspace: Path | str) -> bool:
    """
    Remove the context directory entirely.

    Args:
        workspace: Workspace directory

    Returns:
        True if directory was removed, False if it didn't exist
    """
    workspace = Path(workspace)
    context_path = workspace / CONTEXT_DIR

    if not context_path.exists():
        return False

    shutil.rmtree(context_path)
    return True


def get_previous_step_logs(workspace: Path | str) -> list[str]:
    """
    Get all previous step logs in order.

    Args:
        workspace: Workspace directory

    Returns:
        List of log contents, ordered by step index
    """
    workspace = Path(workspace)
    context_path = workspace / CONTEXT_DIR

    if not context_path.exists():
        return []

    # Find all log files
    log_files = list(context_path.glob("*.log"))
    if not log_files:
        return []

    # Extract step index from filename and sort
    def get_index(path: Path) -> int:
        """Extract step index from filename."""
        name = path.stem
        # Format: step_NNN_name or id_xxx_NNN
        match = re.search(r"_(\d{3})(?:_|\.|$)", name)
        if match:
            return int(match.group(1))
        # Try just finding any number
        numbers = re.findall(r"\d+", name)
        if numbers:
            return int(numbers[-1])
        return 0

    sorted_files = sorted(log_files, key=get_index)

    # Read each file
    logs = []
    for log_file in sorted_files:
        try:
            logs.append(log_file.read_text())
        except Exception:
            # Skip files that can't be read
            pass

    return logs


def commit_context_changes(workspace: Path | str, step_name: str) -> bool:
    """
    Commit context directory changes to git.

    This is used when continue_in_context is True to persist
    context across pipeline steps.

    Args:
        workspace: Workspace directory
        step_name: Name of the completed step (for commit message)

    Returns:
        True if commit was made, False if no changes
    """
    from . import git_helpers

    workspace = Path(workspace)

    try:
        # Add context directory
        git_helpers.add(str(workspace), [CONTEXT_DIR])

        # Check if there are staged changes
        returncode, stdout, stderr = git_helpers._run_git(
            ["diff", "--cached", "--quiet"],
            cwd=workspace,
        )
        if returncode == 0:
            # No changes to commit
            return False

        # Commit
        message = f"[lazyaf] Context: {step_name} completed"
        git_helpers.commit(str(workspace), message)

        return True

    except git_helpers.GitError:
        return False
