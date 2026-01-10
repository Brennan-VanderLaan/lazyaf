"""
Tests for agent executors - defines the contract for each executor type.

These tests verify:
- Command building for each executor type
- Mock executor file operations
- Executor result handling
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from runner_common.executors import (
    ClaudeExecutor,
    GeminiExecutor,
    MockExecutor,
    ExecutorConfig,
    ExecutorResult,
)


class TestClaudeExecutor:
    """Tests for ClaudeExecutor."""

    def test_name_and_type(self):
        """ClaudeExecutor has correct name and runner_type."""
        executor = ClaudeExecutor()
        assert executor.name == "Claude Code"
        assert executor.runner_type == "claude-code"

    def test_build_command_basic(self, tmp_path):
        """build_command() creates correct base command."""
        executor = ClaudeExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Fix the bug in main.py",
        )

        cmd = executor.build_command(config)

        assert cmd == [
            "claude",
            "-p", "Fix the bug in main.py",
            "--dangerously-skip-permissions",
        ]

    def test_build_command_with_model(self, tmp_path):
        """build_command() includes --model when specified."""
        executor = ClaudeExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Fix the bug",
            model="claude-3-opus-20240229",
        )

        cmd = executor.build_command(config)

        assert "--model" in cmd
        assert "claude-3-opus-20240229" in cmd

    def test_build_command_with_agents(self, tmp_path):
        """build_command() includes --agents when specified."""
        executor = ClaudeExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Fix the bug",
            agents_json='[{"name": "test"}]',
        )

        cmd = executor.build_command(config)

        assert "--agents" in cmd
        assert '[{"name": "test"}]' in cmd

    def test_build_command_with_all_options(self, tmp_path):
        """build_command() includes all options when specified."""
        executor = ClaudeExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Fix the bug",
            model="claude-3-sonnet",
            agents_json='[{"name": "agent1"}]',
        )

        cmd = executor.build_command(config)

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "Fix the bug" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--model" in cmd
        assert "claude-3-sonnet" in cmd
        assert "--agents" in cmd


class TestGeminiExecutor:
    """Tests for GeminiExecutor."""

    def test_name_and_type(self):
        """GeminiExecutor has correct name and runner_type."""
        executor = GeminiExecutor()
        assert executor.name == "Gemini"
        assert executor.runner_type == "gemini"

    def test_build_command(self, tmp_path):
        """build_command() creates correct command."""
        executor = GeminiExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Implement feature X",
        )

        cmd = executor.build_command(config)

        assert cmd == [
            "gemini",
            "-p", "Implement feature X",
            "--yolo",
        ]

    def test_setup_config_creates_settings(self, tmp_path, monkeypatch):
        """setup_config() creates Gemini settings files."""
        import json

        # Mock home directory
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        executor = GeminiExecutor()
        executor.setup_config()

        settings_path = tmp_path / ".gemini" / "settings.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert settings["tools"]["autoAccept"] is True

    def test_setup_config_creates_trusted_folders(self, tmp_path, monkeypatch):
        """setup_config() creates trusted folders file."""
        import json

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        executor = GeminiExecutor()
        executor.setup_config()

        trusted_path = tmp_path / ".gemini" / "trustedFolders.json"
        assert trusted_path.exists()

        trusted = json.loads(trusted_path.read_text())
        assert "/workspace" in trusted["folders"]


class TestMockExecutor:
    """Tests for MockExecutor."""

    def test_name_and_type(self):
        """MockExecutor has correct name and runner_type."""
        executor = MockExecutor()
        assert executor.name == "Mock"
        assert executor.runner_type == "mock"

    def test_execute_with_default_config(self, tmp_path):
        """execute() with default config creates marker file."""
        executor = MockExecutor()
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Ignored for mock",
        )

        result = executor.execute(config, streaming=False)

        assert result.success is True
        assert result.exit_code == 0

        marker = tmp_path / ".lazyaf-mock-marker"
        assert marker.exists()

    def test_execute_with_custom_config(self, tmp_path):
        """execute() with custom config applies operations."""
        mock_config = {
            "file_operations": [
                {"action": "create", "path": "test.txt", "content": "Hello"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Ignored",
        )

        result = executor.execute(config, streaming=False)

        assert result.success is True
        assert (tmp_path / "test.txt").read_text() == "Hello"

    def test_execute_file_create(self, tmp_path):
        """execute() creates files correctly."""
        mock_config = {
            "file_operations": [
                {"action": "create", "path": "src/main.py", "content": "print('hello')"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, streaming=False)

        file_path = tmp_path / "src" / "main.py"
        assert file_path.exists()
        assert file_path.read_text() == "print('hello')"

    def test_execute_file_modify(self, tmp_path):
        """execute() modifies files with search/replace."""
        # Create initial file
        test_file = tmp_path / "test.txt"
        test_file.write_text("old value here")

        mock_config = {
            "file_operations": [
                {"action": "modify", "path": "test.txt", "search": "old", "replace": "new"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, streaming=False)

        assert test_file.read_text() == "new value here"

    def test_execute_file_delete(self, tmp_path):
        """execute() deletes files correctly."""
        # Create file to delete
        test_file = tmp_path / "to_delete.txt"
        test_file.write_text("delete me")

        mock_config = {
            "file_operations": [
                {"action": "delete", "path": "to_delete.txt"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, streaming=False)

        assert not test_file.exists()

    def test_execute_file_append(self, tmp_path):
        """execute() appends to files correctly."""
        # Create initial file
        test_file = tmp_path / "append.txt"
        test_file.write_text("line1\n")

        mock_config = {
            "file_operations": [
                {"action": "append", "path": "append.txt", "content": "line2\n"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, streaming=False)

        assert test_file.read_text() == "line1\nline2\n"

    def test_execute_with_nonzero_exit_code(self, tmp_path):
        """execute() returns failure for nonzero exit code."""
        mock_config = {
            "file_operations": [],
            "exit_code": 1,
            "error_message": "Simulated failure",
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        result = executor.execute(config, streaming=False)

        assert result.success is False
        assert result.exit_code == 1
        assert result.error == "Simulated failure"

    def test_execute_logs_to_callback(self, tmp_path):
        """execute() calls log_callback with messages."""
        logs = []

        mock_config = {
            "output_events": [
                {"type": "content", "text": "Test message"},
            ],
            "exit_code": 0,
        }

        executor = MockExecutor(mock_config=mock_config)
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, log_callback=logs.append, streaming=False)

        assert any("Test message" in log for log in logs)

    def test_load_config_from_workspace(self, tmp_path):
        """execute() loads config from .control/mock_config.json."""
        import json

        # Create config file in workspace
        control_dir = tmp_path / ".control"
        control_dir.mkdir()
        config_file = control_dir / "mock_config.json"
        config_file.write_text(json.dumps({
            "file_operations": [
                {"action": "create", "path": "from_workspace.txt", "content": "loaded"},
            ],
            "exit_code": 0,
        }))

        executor = MockExecutor()
        config = ExecutorConfig(workspace=tmp_path, prompt="")

        executor.execute(config, streaming=False)

        assert (tmp_path / "from_workspace.txt").read_text() == "loaded"


class TestExecutorResult:
    """Tests for ExecutorResult dataclass."""

    def test_success_result(self):
        """ExecutorResult correctly represents success."""
        result = ExecutorResult(success=True, exit_code=0)

        assert result.success is True
        assert result.exit_code == 0
        assert result.error is None

    def test_failure_result(self):
        """ExecutorResult correctly represents failure."""
        result = ExecutorResult(
            success=False,
            exit_code=1,
            stdout="output",
            stderr="error output",
            error="Command failed",
        )

        assert result.success is False
        assert result.exit_code == 1
        assert result.error == "Command failed"


class TestExecutorConfig:
    """Tests for ExecutorConfig dataclass."""

    def test_basic_config(self, tmp_path):
        """ExecutorConfig with minimal required fields."""
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Test prompt",
        )

        assert config.workspace == tmp_path
        assert config.prompt == "Test prompt"
        assert config.model is None
        assert config.agents_json is None

    def test_full_config(self, tmp_path):
        """ExecutorConfig with all fields populated."""
        config = ExecutorConfig(
            workspace=tmp_path,
            prompt="Test prompt",
            model="test-model",
            agents_json='[{"name": "agent"}]',
            timeout=300,
            env={"CUSTOM_VAR": "value"},
        )

        assert config.model == "test-model"
        assert config.agents_json == '[{"name": "agent"}]'
        assert config.timeout == 300
        assert config.env == {"CUSTOM_VAR": "value"}
