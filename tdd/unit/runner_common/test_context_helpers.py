"""
Tests for context_helpers module (Phase 12.0).

These tests DEFINE the contract for .lazyaf-context/ directory management.
Write tests first, then implement to make them pass.

Contract defined:
- init_context_directory(workspace, pipeline_run_id) -> Path
- write_step_log(workspace, step_index, step_id, step_name, logs) -> str
- read_step_log(workspace, filename) -> str
- update_context_metadata(workspace, step_index, step_name) -> None
- get_context_metadata(workspace) -> dict
- cleanup_context_directory(workspace) -> bool
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from runner_common.context_helpers import (
        CONTEXT_DIR,
        init_context_directory,
        write_step_log,
        read_step_log,
        update_context_metadata,
        get_context_metadata,
        cleanup_context_directory,
        get_previous_step_logs,
    )
    RUNNER_COMMON_AVAILABLE = True
except ImportError:
    RUNNER_COMMON_AVAILABLE = False
    # Define placeholders
    CONTEXT_DIR = ".lazyaf-context"
    init_context_directory = write_step_log = read_step_log = None
    update_context_metadata = get_context_metadata = cleanup_context_directory = None
    get_previous_step_logs = None


pytestmark = pytest.mark.skipif(
    not RUNNER_COMMON_AVAILABLE,
    reason="runner-common not yet implemented"
)


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace directory."""
    workspace_path = tmp_path / "workspace" / "repo"
    workspace_path.mkdir(parents=True)
    return workspace_path


class TestInitContextDirectory:
    """Tests for init_context_directory() function."""

    def test_creates_context_dir(self, workspace):
        """init_context_directory() creates .lazyaf-context/ directory."""
        context_path = init_context_directory(workspace, "pipeline-123")

        assert context_path.exists()
        assert context_path.is_dir()
        assert context_path.name == CONTEXT_DIR

    def test_creates_metadata_json(self, workspace):
        """init_context_directory() creates metadata.json file."""
        context_path = init_context_directory(workspace, "pipeline-123")

        metadata_file = context_path / "metadata.json"
        assert metadata_file.exists()

    def test_metadata_contains_required_fields(self, workspace):
        """metadata.json contains pipeline_run_id and created_at."""
        context_path = init_context_directory(workspace, "pipeline-123")

        metadata = json.loads((context_path / "metadata.json").read_text())

        assert metadata["pipeline_run_id"] == "pipeline-123"
        assert "created_at" in metadata
        assert "steps_completed" in metadata
        assert isinstance(metadata["steps_completed"], list)

    def test_returns_path_object(self, workspace):
        """init_context_directory() returns a Path object."""
        result = init_context_directory(workspace, "pipeline-123")

        assert isinstance(result, Path)

    def test_idempotent_creates_once(self, workspace):
        """Calling init_context_directory() twice doesn't overwrite."""
        context_path = init_context_directory(workspace, "pipeline-123")
        original_metadata = json.loads((context_path / "metadata.json").read_text())

        # Call again with different pipeline_run_id
        context_path2 = init_context_directory(workspace, "pipeline-456")
        new_metadata = json.loads((context_path2 / "metadata.json").read_text())

        # Should preserve original (idempotent)
        assert new_metadata["pipeline_run_id"] == original_metadata["pipeline_run_id"]


class TestWriteStepLog:
    """Tests for write_step_log() function."""

    def test_creates_log_file(self, workspace):
        """write_step_log() creates a log file."""
        init_context_directory(workspace, "pipeline-123")

        filename = write_step_log(
            workspace,
            step_index=0,
            step_id="step-abc",
            step_name="Build",
            logs="Build completed successfully\n",
        )

        log_path = workspace / CONTEXT_DIR / filename
        assert log_path.exists()

    def test_log_contains_content(self, workspace):
        """write_step_log() writes the provided log content."""
        init_context_directory(workspace, "pipeline-123")
        logs = "Line 1\nLine 2\nLine 3\n"

        filename = write_step_log(
            workspace,
            step_index=0,
            step_id="step-abc",
            step_name="Test",
            logs=logs,
        )

        log_path = workspace / CONTEXT_DIR / filename
        assert log_path.read_text() == logs

    def test_filename_includes_step_id(self, workspace):
        """write_step_log() includes step_id in filename when provided."""
        init_context_directory(workspace, "pipeline-123")

        filename = write_step_log(
            workspace,
            step_index=0,
            step_id="step-xyz",
            step_name="Build",
            logs="content",
        )

        assert "step-xyz" in filename or "xyz" in filename

    def test_filename_includes_index(self, workspace):
        """write_step_log() includes step index in filename."""
        init_context_directory(workspace, "pipeline-123")

        filename = write_step_log(
            workspace,
            step_index=5,
            step_id=None,
            step_name="Deploy",
            logs="content",
        )

        assert "005" in filename or "5" in filename

    def test_returns_filename(self, workspace):
        """write_step_log() returns the filename (not full path)."""
        init_context_directory(workspace, "pipeline-123")

        filename = write_step_log(
            workspace,
            step_index=0,
            step_id="step-abc",
            step_name="Build",
            logs="content",
        )

        assert "/" not in filename and "\\" not in filename


class TestReadStepLog:
    """Tests for read_step_log() function."""

    def test_reads_existing_log(self, workspace):
        """read_step_log() reads content of existing log file."""
        init_context_directory(workspace, "pipeline-123")
        expected = "Log content here\n"

        filename = write_step_log(
            workspace,
            step_index=0,
            step_id="step-abc",
            step_name="Build",
            logs=expected,
        )

        content = read_step_log(workspace, filename)

        assert content == expected

    def test_raises_on_missing_file(self, workspace):
        """read_step_log() raises FileNotFoundError for missing file."""
        init_context_directory(workspace, "pipeline-123")

        with pytest.raises(FileNotFoundError):
            read_step_log(workspace, "nonexistent.log")


class TestUpdateContextMetadata:
    """Tests for update_context_metadata() function."""

    def test_adds_step_to_completed_list(self, workspace):
        """update_context_metadata() adds step to steps_completed list."""
        init_context_directory(workspace, "pipeline-123")

        update_context_metadata(workspace, step_index=0, step_name="Build")

        metadata = get_context_metadata(workspace)
        assert len(metadata["steps_completed"]) == 1
        assert metadata["steps_completed"][0]["index"] == 0
        assert metadata["steps_completed"][0]["name"] == "Build"

    def test_adds_completed_at_timestamp(self, workspace):
        """update_context_metadata() records completion timestamp."""
        init_context_directory(workspace, "pipeline-123")

        update_context_metadata(workspace, step_index=0, step_name="Build")

        metadata = get_context_metadata(workspace)
        assert "completed_at" in metadata["steps_completed"][0]

    def test_multiple_steps_accumulate(self, workspace):
        """Multiple update_context_metadata() calls accumulate steps."""
        init_context_directory(workspace, "pipeline-123")

        update_context_metadata(workspace, step_index=0, step_name="Build")
        update_context_metadata(workspace, step_index=1, step_name="Test")
        update_context_metadata(workspace, step_index=2, step_name="Deploy")

        metadata = get_context_metadata(workspace)
        assert len(metadata["steps_completed"]) == 3


class TestGetContextMetadata:
    """Tests for get_context_metadata() function."""

    def test_returns_dict(self, workspace):
        """get_context_metadata() returns a dictionary."""
        init_context_directory(workspace, "pipeline-123")

        metadata = get_context_metadata(workspace)

        assert isinstance(metadata, dict)

    def test_contains_pipeline_run_id(self, workspace):
        """get_context_metadata() returns dict with pipeline_run_id."""
        init_context_directory(workspace, "pipeline-123")

        metadata = get_context_metadata(workspace)

        assert metadata["pipeline_run_id"] == "pipeline-123"

    def test_raises_on_missing_context(self, workspace):
        """get_context_metadata() raises when context doesn't exist."""
        with pytest.raises(FileNotFoundError):
            get_context_metadata(workspace)


class TestCleanupContextDirectory:
    """Tests for cleanup_context_directory() function."""

    def test_removes_context_directory(self, workspace):
        """cleanup_context_directory() removes the entire context directory."""
        context_path = init_context_directory(workspace, "pipeline-123")
        write_step_log(workspace, 0, "step-1", "Build", "logs")

        result = cleanup_context_directory(workspace)

        assert result is True
        assert not context_path.exists()

    def test_returns_true_on_success(self, workspace):
        """cleanup_context_directory() returns True on successful cleanup."""
        init_context_directory(workspace, "pipeline-123")

        result = cleanup_context_directory(workspace)

        assert result is True

    def test_returns_false_when_nothing_to_clean(self, workspace):
        """cleanup_context_directory() returns False when no context exists."""
        result = cleanup_context_directory(workspace)

        assert result is False


class TestGetPreviousStepLogs:
    """Tests for get_previous_step_logs() function."""

    def test_returns_list_of_logs(self, workspace):
        """get_previous_step_logs() returns list of log contents."""
        init_context_directory(workspace, "pipeline-123")
        write_step_log(workspace, 0, "step-1", "Build", "Build log\n")
        write_step_log(workspace, 1, "step-2", "Test", "Test log\n")

        logs = get_previous_step_logs(workspace)

        assert isinstance(logs, list)
        assert len(logs) == 2

    def test_logs_ordered_by_index(self, workspace):
        """get_previous_step_logs() returns logs in step order."""
        init_context_directory(workspace, "pipeline-123")
        write_step_log(workspace, 2, "step-3", "Deploy", "Deploy log\n")
        write_step_log(workspace, 0, "step-1", "Build", "Build log\n")
        write_step_log(workspace, 1, "step-2", "Test", "Test log\n")

        logs = get_previous_step_logs(workspace)

        assert "Build log" in logs[0]
        assert "Test log" in logs[1]
        assert "Deploy log" in logs[2]

    def test_returns_empty_list_when_no_logs(self, workspace):
        """get_previous_step_logs() returns empty list when no logs exist."""
        init_context_directory(workspace, "pipeline-123")

        logs = get_previous_step_logs(workspace)

        assert logs == []

    def test_returns_empty_when_no_context(self, workspace):
        """get_previous_step_logs() returns empty list when no context dir."""
        logs = get_previous_step_logs(workspace)

        assert logs == []
