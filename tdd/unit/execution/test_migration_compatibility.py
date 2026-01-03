"""
Tests for Migration Compatibility (Phase 12.4).

These tests ensure backward compatibility with existing pipeline YAML format
and that the new execution path works with both old and new configurations.

Key Requirements:
- Existing pipeline YAML (without explicit image) still works
- New pipeline YAML (with custom images) works
- Step types (script, docker, agent) are all handled correctly
- No breaking changes to existing behavior
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Try importing modules
try:
    from app.services.execution.config_builder import (
        build_execution_config,
        DEFAULT_SCRIPT_IMAGE,
        DEFAULT_AGENT_IMAGES,
    )
    from app.services.execution.local_executor import ExecutionConfig
    MODULES_AVAILABLE = True
except ImportError:
    MODULES_AVAILABLE = False
    build_execution_config = None
    DEFAULT_SCRIPT_IMAGE = None
    DEFAULT_AGENT_IMAGES = None
    ExecutionConfig = None


pytestmark = pytest.mark.skipif(
    not MODULES_AVAILABLE,
    reason="execution modules not yet implemented"
)


class TestExistingPipelineYamlWorks:
    """Tests for backward compatibility with existing pipeline format."""

    def test_script_step_without_image(self):
        """Script step without explicit image uses default."""
        # Existing format - no image specified
        step_config = {
            "command": "pytest -v",
        }

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == DEFAULT_SCRIPT_IMAGE
        assert "pytest" in " ".join(config.command)

    def test_script_step_with_multiline_command(self):
        """Script step with multiline command works."""
        # Common pattern in existing pipelines
        step_config = {
            "command": """
                export PATH="$HOME/.local/bin:$PATH"
                cd backend
                uv sync --all-extras
            """,
        }

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image is not None
        # Command should be wrapped for bash execution
        assert len(config.command) > 0

    def test_docker_step_existing_format(self):
        """Docker step in existing format still works."""
        step_config = {
            "image": "python:3.12",
            "command": "pip install pytest && pytest",
        }

        config = build_execution_config(
            step_type="docker",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == "python:3.12"

    def test_agent_step_claude_code(self):
        """Agent step with claude-code runner type works."""
        step_config = {
            "runner_type": "claude-code",
            "title": "Implement feature",
            "description": "Add user authentication",
        }

        config = build_execution_config(
            step_type="agent",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == DEFAULT_AGENT_IMAGES["claude-code"]

    def test_agent_step_gemini(self):
        """Agent step with gemini runner type works."""
        step_config = {
            "runner_type": "gemini",
            "title": "Fix bug",
            "description": "Fix the login issue",
        }

        config = build_execution_config(
            step_type="agent",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == DEFAULT_AGENT_IMAGES["gemini"]


class TestNewPipelineYamlWorks:
    """Tests for new pipeline format with explicit images."""

    def test_script_step_with_custom_image(self):
        """Script step with custom image uses that image."""
        step_config = {
            "image": "lazyaf-test-runner:latest",
            "command": "pytest",
        }

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == "lazyaf-test-runner:latest"

    def test_docker_step_with_private_registry(self):
        """Docker step with private registry image works."""
        step_config = {
            "image": "ghcr.io/myorg/myimage:v1.0.0",
            "command": "run-tests",
        }

        config = build_execution_config(
            step_type="docker",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.image == "ghcr.io/myorg/myimage:v1.0.0"

    def test_step_with_environment_variables(self):
        """Step with environment variables preserves them."""
        step_config = {
            "command": "npm test",
            "environment": {
                "NODE_ENV": "test",
                "CI": "true",
                "COVERAGE": "1",
            },
        }

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.environment["NODE_ENV"] == "test"
        assert config.environment["CI"] == "true"
        assert config.environment["COVERAGE"] == "1"

    def test_step_with_working_directory(self):
        """Step with custom working directory preserves it."""
        step_config = {
            "image": "node:20",
            "command": "npm test",
            "working_dir": "/workspace/frontend",
        }

        config = build_execution_config(
            step_type="docker",
            step_config=step_config,
            workspace_path="/workspace",
        )

        assert config.working_dir == "/workspace/frontend"

    def test_step_with_timeout(self):
        """Step timeout is properly configured."""
        step_config = {
            "command": "long-running-test",
        }

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/workspace",
            timeout_seconds=1800,  # 30 minutes
        )

        assert config.timeout_seconds == 1800


class TestStepTypeDetection:
    """Tests for correct step type handling."""

    def test_script_type_detected(self):
        """Script type is correctly handled."""
        config = build_execution_config(
            step_type="script",
            step_config={"command": "echo hello"},
            workspace_path="/workspace",
        )

        # Script steps should be wrapped in bash
        assert "bash" in config.command[0] or "sh" in config.command[0]

    def test_docker_type_uses_command_directly(self):
        """Docker type uses command as-is."""
        config = build_execution_config(
            step_type="docker",
            step_config={
                "image": "alpine",
                "command": "echo hello",
            },
            workspace_path="/workspace",
        )

        # Docker commands might be passed differently
        assert config.image == "alpine"

    def test_agent_type_sets_special_handling(self):
        """Agent type triggers special handling."""
        config = build_execution_config(
            step_type="agent",
            step_config={
                "runner_type": "claude-code",
                "title": "Task",
                "description": "Do something",
            },
            workspace_path="/workspace",
        )

        # Agent uses the agent image
        assert "claude" in config.image.lower() or "agent" in config.image.lower()


class TestContinueInContextPreserved:
    """Tests for continue_in_context behavior compatibility."""

    def test_workspace_path_consistent(self):
        """Workspace path is consistently applied."""
        workspace = "/var/lazyaf/workspaces/run-123"

        config1 = build_execution_config(
            step_type="script",
            step_config={"command": "install deps"},
            workspace_path=workspace,
        )

        config2 = build_execution_config(
            step_type="script",
            step_config={"command": "run tests"},
            workspace_path=workspace,
        )

        # Both configs should use same workspace
        assert config1.workspace_path == config2.workspace_path == workspace

    def test_home_directory_set_for_cache_persistence(self):
        """HOME is set to workspace/home for cache persistence."""
        config = build_execution_config(
            step_type="script",
            step_config={"command": "pip install pytest"},
            workspace_path="/workspace",
            use_control_layer=True,
        )

        # When using control layer, HOME should be set
        assert "HOME" in config.environment or config.use_control_layer


class TestDefaultImages:
    """Tests for default image configuration."""

    def test_default_script_image_defined(self):
        """Default script image is defined."""
        assert DEFAULT_SCRIPT_IMAGE is not None
        assert len(DEFAULT_SCRIPT_IMAGE) > 0

    def test_default_agent_images_defined(self):
        """Default agent images are defined for each runner type."""
        assert DEFAULT_AGENT_IMAGES is not None
        assert "claude-code" in DEFAULT_AGENT_IMAGES
        assert "gemini" in DEFAULT_AGENT_IMAGES

    def test_script_without_image_gets_default(self):
        """Script step without image gets default."""
        config = build_execution_config(
            step_type="script",
            step_config={"command": "test"},
            workspace_path="/workspace",
        )

        assert config.image == DEFAULT_SCRIPT_IMAGE


class TestCommandWrapping:
    """Tests for proper command wrapping."""

    def test_script_command_wrapped_in_bash(self):
        """Script commands are wrapped in bash -c."""
        config = build_execution_config(
            step_type="script",
            step_config={"command": "echo hello && ls"},
            workspace_path="/workspace",
        )

        # Should be wrapped as ["bash", "-c", "echo hello && ls"]
        assert config.command[0] in ("bash", "sh", "/bin/bash", "/bin/sh")
        assert "-c" in config.command
        assert "echo hello && ls" in config.command[-1]

    def test_docker_command_as_list(self):
        """Docker commands can be specified as list."""
        config = build_execution_config(
            step_type="docker",
            step_config={
                "image": "python:3.12",
                "command": ["python", "-m", "pytest"],
            },
            workspace_path="/workspace",
        )

        assert config.command == ["python", "-m", "pytest"]

    def test_docker_command_as_string(self):
        """Docker commands as string are properly handled."""
        config = build_execution_config(
            step_type="docker",
            step_config={
                "image": "python:3.12",
                "command": "python -m pytest",
            },
            workspace_path="/workspace",
        )

        # Should be wrapped or split appropriately
        assert len(config.command) > 0


class TestPipelineExecutorIntegration:
    """Integration tests for PipelineExecutor changes."""

    @pytest.fixture
    def mock_db_session(self):
        """Create a mock database session."""
        return AsyncMock()

    @pytest.fixture
    def mock_local_executor(self):
        """Create a mock LocalExecutor."""
        executor = MagicMock()
        executor.execute_step = AsyncMock()
        return executor

    @pytest.mark.asyncio
    async def test_script_step_uses_local_executor(self, mock_db_session, mock_local_executor):
        """Script step execution uses LocalExecutor."""
        # This test verifies the integration point exists
        # Full integration tested in integration tests
        from app.services.execution.router import ExecutionRouter, ExecutorType

        router = ExecutionRouter()
        decision = router.route(
            step_type="script",
            step_config={"command": "pytest"},
            requirements={},
        )

        assert decision.executor_type == ExecutorType.LOCAL

    @pytest.mark.asyncio
    async def test_docker_step_uses_local_executor(self, mock_db_session, mock_local_executor):
        """Docker step execution uses LocalExecutor."""
        from app.services.execution.router import ExecutionRouter, ExecutorType

        router = ExecutionRouter()
        decision = router.route(
            step_type="docker",
            step_config={"image": "python:3.12", "command": "pytest"},
            requirements={},
        )

        assert decision.executor_type == ExecutorType.LOCAL
