"""
Tests for unified entrypoint (Phase 12.0 integration validation).

These tests verify that:
1. The unified entrypoint dispatches correctly based on RUNNER_TYPE
2. Both Claude and Gemini runners work with the shared code
3. Existing pipeline behavior is preserved
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from runner_common import (
        BaseRunner,
        ClaudeRunner,
        GeminiRunner,
        get_runner,
    )
    from runner_common.entrypoint import get_runner
    RUNNER_COMMON_AVAILABLE = True
except ImportError:
    RUNNER_COMMON_AVAILABLE = False
    BaseRunner = ClaudeRunner = GeminiRunner = get_runner = None


pytestmark = pytest.mark.skipif(
    not RUNNER_COMMON_AVAILABLE,
    reason="runner-common not yet implemented"
)


class TestGetRunner:
    """Tests for get_runner() function."""

    def test_returns_claude_runner_for_claude_code(self, monkeypatch):
        """get_runner() returns ClaudeRunner when RUNNER_TYPE=claude-code."""
        monkeypatch.setenv("RUNNER_TYPE", "claude-code")

        runner = get_runner()

        assert isinstance(runner, ClaudeRunner)
        assert runner.runner_type == "claude-code"

    def test_returns_gemini_runner_for_gemini(self, monkeypatch):
        """get_runner() returns GeminiRunner when RUNNER_TYPE=gemini."""
        monkeypatch.setenv("RUNNER_TYPE", "gemini")

        runner = get_runner()

        assert isinstance(runner, GeminiRunner)
        assert runner.runner_type == "gemini"

    def test_raises_on_missing_runner_type(self, monkeypatch):
        """get_runner() raises ValueError when RUNNER_TYPE not set."""
        monkeypatch.delenv("RUNNER_TYPE", raising=False)

        with pytest.raises(ValueError) as exc_info:
            get_runner()

        assert "RUNNER_TYPE" in str(exc_info.value)

    def test_raises_on_invalid_runner_type(self, monkeypatch):
        """get_runner() raises ValueError for unknown RUNNER_TYPE."""
        monkeypatch.setenv("RUNNER_TYPE", "unknown-runner")

        with pytest.raises(ValueError) as exc_info:
            get_runner()

        assert "unknown-runner" in str(exc_info.value)

    def test_case_insensitive(self, monkeypatch):
        """get_runner() handles case-insensitive RUNNER_TYPE."""
        monkeypatch.setenv("RUNNER_TYPE", "CLAUDE-CODE")

        runner = get_runner()

        assert isinstance(runner, ClaudeRunner)


class TestClaudeRunner:
    """Tests for ClaudeRunner class."""

    def test_inherits_from_base_runner(self):
        """ClaudeRunner inherits from BaseRunner."""
        assert issubclass(ClaudeRunner, BaseRunner)

    def test_runner_type_is_claude_code(self):
        """ClaudeRunner.runner_type is 'claude-code'."""
        runner = ClaudeRunner()
        assert runner.runner_type == "claude-code"

    def test_uses_default_model_from_env(self, monkeypatch):
        """ClaudeRunner uses CLAUDE_MODEL from environment."""
        monkeypatch.setenv("CLAUDE_MODEL", "sonnet")

        runner = ClaudeRunner()

        assert runner.claude_model == "sonnet"

    def test_uses_opus_as_default_model(self, monkeypatch):
        """ClaudeRunner defaults to opus model."""
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)

        runner = ClaudeRunner()

        assert runner.claude_model == "opus"


class TestGeminiRunner:
    """Tests for GeminiRunner class."""

    def test_inherits_from_base_runner(self):
        """GeminiRunner inherits from BaseRunner."""
        assert issubclass(GeminiRunner, BaseRunner)

    def test_runner_type_is_gemini(self):
        """GeminiRunner.runner_type is 'gemini'."""
        runner = GeminiRunner()
        assert runner.runner_type == "gemini"

    def test_uses_default_model_from_env(self, monkeypatch):
        """GeminiRunner uses GEMINI_MODEL from environment."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5-pro")

        runner = GeminiRunner()

        assert runner.gemini_model == "gemini-1.5-pro"


class TestBaseRunner:
    """Tests for BaseRunner class."""

    def test_uses_backend_url_from_env(self, monkeypatch):
        """BaseRunner uses BACKEND_URL from environment."""
        monkeypatch.setenv("BACKEND_URL", "http://custom:9000")
        monkeypatch.setenv("RUNNER_TYPE", "claude-code")

        runner = ClaudeRunner()

        assert runner.backend_url == "http://custom:9000"

    def test_uses_runner_name_from_env(self, monkeypatch):
        """BaseRunner uses RUNNER_NAME from environment."""
        monkeypatch.setenv("RUNNER_NAME", "my-runner")
        monkeypatch.setenv("RUNNER_TYPE", "claude-code")

        runner = ClaudeRunner()

        assert runner.runner_name == "my-runner"

    def test_generates_persistent_uuid(self, monkeypatch):
        """BaseRunner generates a persistent UUID."""
        monkeypatch.setenv("RUNNER_TYPE", "claude-code")
        monkeypatch.delenv("RUNNER_UUID", raising=False)

        runner1 = ClaudeRunner()
        uuid1 = runner1.runner_uuid

        assert uuid1 is not None
        assert len(uuid1) == 36  # UUID format

    def test_uses_uuid_from_env(self, monkeypatch):
        """BaseRunner uses RUNNER_UUID from environment if set."""
        monkeypatch.setenv("RUNNER_UUID", "custom-uuid-123")
        monkeypatch.setenv("RUNNER_TYPE", "claude-code")

        runner = ClaudeRunner()

        assert runner.runner_uuid == "custom-uuid-123"


class TestBaseRunnerMethods:
    """Tests for BaseRunner methods."""

    @pytest.fixture
    def runner(self, monkeypatch):
        """Create a test runner."""
        monkeypatch.setenv("BACKEND_URL", "http://test:8000")
        return ClaudeRunner()

    def test_log_adds_to_buffer(self, runner):
        """log() adds messages to log buffer."""
        runner.log("Test message")

        assert "Test message" in runner._log_buffer

    def test_stop_sets_event(self, runner):
        """stop() sets the stop event."""
        assert not runner._stop_event.is_set()

        runner.stop()

        assert runner._stop_event.is_set()

    def test_setup_workspace_handles_continuation(self, runner, tmp_path):
        """setup_workspace() handles continuation jobs."""
        runner.workspace_path = tmp_path / "workspace"
        runner.workspace_path.mkdir(parents=True)

        job = {"is_continuation": True}

        result = runner.setup_workspace(job)

        assert result is True

    def test_setup_workspace_fails_for_continuation_without_workspace(self, runner, tmp_path):
        """setup_workspace() fails for continuation without existing workspace."""
        runner.workspace_path = tmp_path / "nonexistent"

        job = {"is_continuation": True}

        result = runner.setup_workspace(job)

        assert result is False


class TestJobExecution:
    """Tests for job execution paths."""

    @pytest.fixture
    def runner(self, monkeypatch, tmp_path):
        """Create a test runner with workspace."""
        monkeypatch.setenv("BACKEND_URL", "http://test:8000")
        runner = ClaudeRunner()
        runner.workspace_path = tmp_path / "workspace"
        runner.workspace_path.mkdir(parents=True)
        return runner

    def test_execute_script_step_runs_command(self, runner):
        """_execute_script_step() runs the specified command."""
        job = {}
        config = {"command": "echo hello"}

        with patch.object(runner, "log"):
            result = runner._execute_script_step(job, config)

        assert result.success
        assert "hello" in result.logs

    def test_execute_script_step_fails_on_error(self, runner):
        """_execute_script_step() reports failure on non-zero exit."""
        job = {}
        config = {"command": "exit 1"}

        with patch.object(runner, "log"):
            result = runner._execute_script_step(job, config)

        assert not result.success
        assert "exit code" in result.error

    def test_execute_script_step_detects_tests(self, runner):
        """_execute_script_step() detects test output."""
        job = {}
        config = {"command": "echo '5 passed, 1 failed'"}

        with patch.object(runner, "log"):
            result = runner._execute_script_step(job, config)

        assert result.test_results is not None
        assert result.test_results.pass_count == 5
        assert result.test_results.fail_count == 1
