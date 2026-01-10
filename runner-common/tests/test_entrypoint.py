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

    def test_execute_job_routes_to_script(self, monkeypatch):
        """execute_job() routes script type to execute_script_step."""
        from runner_common import entrypoint

        mock_script = MagicMock()
        monkeypatch.setattr(entrypoint, "execute_script_step", mock_script)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id", "step_type": "script"}
        entrypoint.execute_job(job)

        mock_script.assert_called_once_with(job)

    def test_execute_job_routes_to_docker(self, monkeypatch):
        """execute_job() routes docker type to execute_docker_step."""
        from runner_common import entrypoint

        mock_docker = MagicMock()
        monkeypatch.setattr(entrypoint, "execute_docker_step", mock_docker)
        monkeypatch.setattr(entrypoint, "runner_id", "test-runner")

        job = {"id": "test-job-id", "step_type": "docker"}
        entrypoint.execute_job(job)

        mock_docker.assert_called_once_with(job)

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
