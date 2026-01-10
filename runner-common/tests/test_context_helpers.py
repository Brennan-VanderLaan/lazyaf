"""
Tests for context_helpers module - defines the contract for .lazyaf-context management.

These tests are written BEFORE the implementation to define expected behavior.
The .lazyaf-context directory stores step metadata and logs for pipeline runs.
"""

import json
from pathlib import Path

import pytest


CONTEXT_DIR = ".lazyaf-context"


class TestInitContext:
    """Tests for init_context() function."""

    def test_init_creates_context_dir(self, tmp_path):
        """init_context(workspace, run_id) creates .lazyaf-context directory."""
        from runner_common.context_helpers import init_context

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        context_path = init_context(workspace, "run-123")

        assert context_path.exists()
        assert context_path.is_dir()
        assert context_path.name == CONTEXT_DIR

    def test_init_creates_metadata_file(self, tmp_path):
        """init_context() creates metadata.json with run info."""
        from runner_common.context_helpers import init_context

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        context_path = init_context(workspace, "run-456")

        metadata_file = context_path / "metadata.json"
        assert metadata_file.exists()

        metadata = json.loads(metadata_file.read_text())
        assert metadata["pipeline_run_id"] == "run-456"
        assert "created_at" in metadata

    def test_init_creates_logs_subdir(self, tmp_path):
        """init_context() creates logs/ subdirectory."""
        from runner_common.context_helpers import init_context

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        context_path = init_context(workspace, "run-789")

        logs_dir = context_path / "logs"
        assert logs_dir.exists()
        assert logs_dir.is_dir()

    def test_init_idempotent(self, tmp_path):
        """init_context() is idempotent - calling twice doesn't error."""
        from runner_common.context_helpers import init_context

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        path1 = init_context(workspace, "run-aaa")
        path2 = init_context(workspace, "run-aaa")

        assert path1 == path2


class TestWriteStepLog:
    """Tests for write_step_log() function."""

    def test_write_step_log_creates_file(self, context_workspace):
        """write_step_log(workspace, step_index, log_content) creates log file."""
        from runner_common.context_helpers import write_step_log

        workspace, context_path = context_workspace

        write_step_log(workspace, 0, "Step output here")

        log_file = context_path / "logs" / "step_0.log"
        assert log_file.exists()
        assert "Step output here" in log_file.read_text()

    def test_write_step_log_with_name(self, context_workspace):
        """write_step_log() can include step name in filename."""
        from runner_common.context_helpers import write_step_log

        workspace, context_path = context_workspace

        write_step_log(workspace, 1, "Build output", step_name="build")

        log_file = context_path / "logs" / "step_1_build.log"
        assert log_file.exists()

    def test_write_step_log_appends(self, context_workspace):
        """write_step_log() with append=True adds to existing log."""
        from runner_common.context_helpers import write_step_log

        workspace, _ = context_workspace

        write_step_log(workspace, 0, "Line 1\n")
        write_step_log(workspace, 0, "Line 2\n", append=True)

        from runner_common.context_helpers import read_step_log
        content = read_step_log(workspace, 0)
        assert "Line 1" in content
        assert "Line 2" in content


class TestReadStepLog:
    """Tests for read_step_log() function."""

    def test_read_step_log_returns_content(self, context_workspace):
        """read_step_log(workspace, step_index) returns log content."""
        from runner_common.context_helpers import write_step_log, read_step_log

        workspace, _ = context_workspace

        write_step_log(workspace, 2, "Test log content")
        content = read_step_log(workspace, 2)

        assert content == "Test log content"

    def test_read_step_log_returns_none_if_missing(self, context_workspace):
        """read_step_log() returns None if log doesn't exist."""
        from runner_common.context_helpers import read_step_log

        workspace, _ = context_workspace

        content = read_step_log(workspace, 99)
        assert content is None


class TestUpdateMetadata:
    """Tests for update_metadata() function."""

    def test_update_metadata_adds_field(self, context_workspace):
        """update_metadata(workspace, key, value) adds field to metadata.json."""
        from runner_common.context_helpers import update_metadata, read_metadata

        workspace, context_path = context_workspace

        update_metadata(workspace, "current_step", 3)

        metadata = read_metadata(workspace)
        assert metadata["current_step"] == 3

    def test_update_metadata_preserves_existing(self, context_workspace):
        """update_metadata() preserves existing fields."""
        from runner_common.context_helpers import update_metadata, read_metadata

        workspace, _ = context_workspace

        update_metadata(workspace, "field1", "value1")
        update_metadata(workspace, "field2", "value2")

        metadata = read_metadata(workspace)
        assert metadata["field1"] == "value1"
        assert metadata["field2"] == "value2"

    def test_update_metadata_with_step_info(self, context_workspace):
        """update_metadata() can store step completion info."""
        from runner_common.context_helpers import update_metadata, read_metadata

        workspace, _ = context_workspace

        step_info = {
            "index": 0,
            "name": "build",
            "status": "completed",
            "exit_code": 0,
        }
        update_metadata(workspace, "steps", [step_info])

        metadata = read_metadata(workspace)
        assert len(metadata["steps"]) == 1
        assert metadata["steps"][0]["name"] == "build"


class TestReadMetadata:
    """Tests for read_metadata() function."""

    def test_read_metadata_returns_dict(self, context_workspace):
        """read_metadata(workspace) returns parsed JSON as dict."""
        from runner_common.context_helpers import read_metadata

        workspace, _ = context_workspace

        metadata = read_metadata(workspace)
        assert isinstance(metadata, dict)
        assert "pipeline_run_id" in metadata

    def test_read_metadata_returns_none_if_missing(self, tmp_path):
        """read_metadata() returns None if context doesn't exist."""
        from runner_common.context_helpers import read_metadata

        workspace = tmp_path / "no_context"
        workspace.mkdir()

        metadata = read_metadata(workspace)
        assert metadata is None


class TestCleanupContext:
    """Tests for cleanup_context() function."""

    def test_cleanup_removes_context_dir(self, context_workspace):
        """cleanup_context(workspace) removes .lazyaf-context directory."""
        from runner_common.context_helpers import cleanup_context

        workspace, context_path = context_workspace

        cleanup_context(workspace)

        assert not context_path.exists()

    def test_cleanup_idempotent(self, tmp_path):
        """cleanup_context() doesn't error if context doesn't exist."""
        from runner_common.context_helpers import cleanup_context

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Should not raise
        cleanup_context(workspace)


class TestGetContextPath:
    """Tests for get_context_path() function."""

    def test_get_context_path_returns_path(self, context_workspace):
        """get_context_path(workspace) returns Path to .lazyaf-context."""
        from runner_common.context_helpers import get_context_path

        workspace, expected_path = context_workspace

        path = get_context_path(workspace)
        assert path == expected_path

    def test_get_context_path_works_without_init(self, tmp_path):
        """get_context_path() returns path even if context not initialized."""
        from runner_common.context_helpers import get_context_path

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        path = get_context_path(workspace)
        assert path == workspace / CONTEXT_DIR


class TestContextExists:
    """Tests for context_exists() function."""

    def test_context_exists_true_after_init(self, context_workspace):
        """context_exists(workspace) returns True after init_context()."""
        from runner_common.context_helpers import context_exists

        workspace, _ = context_workspace
        assert context_exists(workspace) is True

    def test_context_exists_false_before_init(self, tmp_path):
        """context_exists(workspace) returns False before init_context()."""
        from runner_common.context_helpers import context_exists

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        assert context_exists(workspace) is False


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def context_workspace(tmp_path):
    """Create a workspace with initialized context directory."""
    from runner_common.context_helpers import init_context

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    context_path = init_context(workspace, "test-run-id")
    return workspace, context_path
