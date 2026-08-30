"""
Tests for the unified entrypoint.

These tests verify:
- Executor selection based on runner type
- Job routing to correct step handlers
- Workspace management
- Prompt building
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from runner_common.entrypoint import (
    get_executor,
    get_workspace,
    build_prompt,
    EXECUTORS,
)
from runner_common.executors import ClaudeExecutor, GeminiExecutor, MockExecutor


class TestGetExecutor:
    """Tests for executor selection."""

    def test_get_executor_claude(self, monkeypatch):
        """get_executor() returns ClaudeExecutor for claude-code type."""
        monkeypatch.setattr("runner_common.entrypoint.RUNNER_TYPE", "claude-code")
        executor = get_executor()
        assert isinstance(executor, ClaudeExecutor)

    def test_get_executor_gemini(self, monkeypatch):
        """get_executor() returns GeminiExecutor for gemini type."""
        monkeypatch.setattr("runner_common.entrypoint.RUNNER_TYPE", "gemini")
        executor = get_executor()
        assert isinstance(executor, GeminiExecutor)

    def test_get_executor_mock(self, monkeypatch):
        """get_executor() returns MockExecutor for mock type."""
        monkeypatch.setattr("runner_common.entrypoint.RUNNER_TYPE", "mock")
        executor = get_executor()
        assert isinstance(executor, MockExecutor)

    def test_get_executor_unknown_raises(self, monkeypatch):
        """get_executor() raises ValueError for unknown type."""
        monkeypatch.setattr("runner_common.entrypoint.RUNNER_TYPE", "unknown-type")
        with pytest.raises(ValueError, match="Unknown runner type"):
            get_executor()


class TestGetWorkspace:
    """Tests for workspace path resolution."""

    def test_get_workspace_default(self):
        """get_workspace() returns default path without pipeline ID."""
        path = get_workspace()
        assert path == Path("/workspace/repo")

    def test_get_workspace_with_pipeline_id(self):
        """get_workspace() scopes path to pipeline ID."""
        path = get_workspace("12345678-abcd-efgh")
        assert path == Path("/workspace/12345678/repo")

    def test_get_workspace_truncates_pipeline_id(self):
        """get_workspace() uses first 8 chars of pipeline ID."""
        path = get_workspace("abcdefghijklmnop")
        assert path == Path("/workspace/abcdefgh/repo")


class TestBuildPrompt:
    """Tests for prompt building."""

    def test_build_prompt_with_template(self, tmp_path):
        """build_prompt() uses template when provided."""
        job = {
            "card_title": "Add feature",
            "card_description": "Implement the new feature",
            "prompt_template": "Title: {{title}}\nTask: {{description}}",
        }

        prompt = build_prompt(job, tmp_path)

        assert "Title: Add feature" in prompt
        assert "Task: Implement the new feature" in prompt

    def test_build_prompt_default(self, tmp_path):
        """build_prompt() uses default template without prompt_template."""
        job = {
            "card_title": "Fix bug",
            "card_description": "Fix the login bug",
        }

        prompt = build_prompt(job, tmp_path)

        assert "Fix bug" in prompt
        assert "Fix the login bug" in prompt
        assert "Feature Request" in prompt

    def test_build_prompt_includes_readme(self, tmp_path):
        """build_prompt() includes README content."""
        (tmp_path / "README.md").write_text("# My Project\nA cool project.")

        job = {
            "card_title": "Feature",
            "card_description": "Description",
        }

        prompt = build_prompt(job, tmp_path)

        assert "My Project" in prompt
        assert "Repository Context" in prompt

    def test_build_prompt_with_previous_logs(self, tmp_path):
        """build_prompt() includes previous step logs."""
        job = {
            "card_title": "Feature",
            "card_description": "Description",
        }

        prompt = build_prompt(job, tmp_path, previous_logs="Previous step output here")

        assert "Previous Step Output" in prompt
        assert "Previous step output here" in prompt


class TestExecutorRegistry:
    """Tests for the executor registry."""

    def test_registry_has_claude(self):
        """EXECUTORS contains claude-code."""
        assert "claude-code" in EXECUTORS
        assert EXECUTORS["claude-code"] == ClaudeExecutor

    def test_registry_has_gemini(self):
        """EXECUTORS contains gemini."""
        assert "gemini" in EXECUTORS
        assert EXECUTORS["gemini"] == GeminiExecutor

    def test_registry_has_mock(self):
        """EXECUTORS contains mock."""
        assert "mock" in EXECUTORS
        assert EXECUTORS["mock"] == MockExecutor


class TestJobRouting:
    """Tests for job type routing."""

    def test_execute_job_rejects_script(self, monkeypatch):
        """execute_job() REJECTS script jobs - Phase 12.4 deleted the path.

        Nothing here may pin the removed routing: there is no
        execute_script_step to dispatch to any more, and the runner must fail
        the job loudly instead of quietly running a shell command the
        ExecutionRouter never sent it.
        """
        from runner_common import entrypoint

        mock_reject = MagicMock()
        monkeypatch.setattr(entrypoint, "reject_non_agent_step", mock_reject)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id", "step_type": "script"}
        entrypoint.execute_job(job)

        mock_reject.assert_called_once_with(job)

    def test_execute_job_rejects_docker(self, monkeypatch):
        """execute_job() REJECTS docker jobs - Phase 12.4 deleted the path."""
        from runner_common import entrypoint

        mock_reject = MagicMock()
        monkeypatch.setattr(entrypoint, "reject_non_agent_step", mock_reject)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id", "step_type": "docker"}
        entrypoint.execute_job(job)

        mock_reject.assert_called_once_with(job)

    def test_script_and_docker_executors_are_gone(self):
        """The second source of truth for script/docker semantics is deleted.

        A re-added execute_script_step/execute_docker_step would silently
        become a competing implementation of what LocalExecutor now owns.
        """
        from runner_common import entrypoint

        assert not hasattr(entrypoint, "execute_script_step")
        assert not hasattr(entrypoint, "execute_docker_step")

    def test_reject_fails_the_job_with_a_clear_error(self, monkeypatch):
        """reject_non_agent_step reports running, then completes FAILED with
        a message naming the step type and the reason."""
        from runner_common import entrypoint

        mock_complete = MagicMock()
        mock_report = MagicMock()
        monkeypatch.setattr(entrypoint, "complete_job", mock_complete)
        monkeypatch.setattr(entrypoint, "report_status", mock_report)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "abcdef1234", "step_type": "script"}
        entrypoint.reject_non_agent_step(job)

        mock_report.assert_called_once()
        assert mock_report.call_args[0][1] == "running"

        mock_complete.assert_called_once()
        assert mock_complete.call_args[0][1] is False
        error = mock_complete.call_args[1]["error"]
        assert "script" in error
        assert "12.4" in error
        assert "local executor" in error

    def test_reject_covers_docker_too(self, monkeypatch):
        from runner_common import entrypoint

        mock_complete = MagicMock()
        monkeypatch.setattr(entrypoint, "complete_job", mock_complete)
        monkeypatch.setattr(entrypoint, "report_status", MagicMock())
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        entrypoint.reject_non_agent_step({"id": "abcdef1234", "step_type": "docker"})

        assert mock_complete.call_args[0][1] is False
        assert "docker" in mock_complete.call_args[1]["error"]

    def test_execute_job_routes_to_agent(self, monkeypatch):
        """execute_job() routes agent type to execute_agent_step."""
        from runner_common import entrypoint

        mock_agent = MagicMock()
        monkeypatch.setattr(entrypoint, "execute_agent_step", mock_agent)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id", "step_type": "agent"}
        entrypoint.execute_job(job)

        mock_agent.assert_called_once_with(job)

    def test_execute_job_defaults_to_agent(self, monkeypatch):
        """execute_job() defaults to agent for unknown/missing step_type."""
        from runner_common import entrypoint

        mock_agent = MagicMock()
        monkeypatch.setattr(entrypoint, "execute_agent_step", mock_agent)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id"}  # No step_type
        entrypoint.execute_job(job)

        mock_agent.assert_called_once_with(job)
