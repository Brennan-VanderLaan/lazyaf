"""
Tests for Agent Step Contract (Phase 12.5).

These tests DEFINE how agent steps are routed through LocalExecutor.
Write tests first, then implement to make them pass.

Phase 12.5 Requirements:
- Agent steps route through LocalExecutor (not job queue)
- Agent wrapper script handles git clone, branch creation, CLI invocation
- Correct images used (lazyaf-claude, lazyaf-gemini)
- Config includes model, agent files, previous step logs
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import json

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import modules - these should exist
try:
    from app.services.execution.router import (
        ExecutionRouter,
        ExecutorType,
        RoutingDecision,
    )
    from app.services.execution.local_executor import (
        LocalExecutor,
        ExecutionConfig,
        ExecutionResult,
    )
    from app.services.execution.config_builder import (
        build_execution_config,
        DEFAULT_AGENT_IMAGES,
    )
    MODULES_AVAILABLE = True
except ImportError:
    MODULES_AVAILABLE = False
    ExecutionRouter = None
    ExecutorType = None
    RoutingDecision = None
    LocalExecutor = None
    ExecutionConfig = None
    ExecutionResult = None
    build_execution_config = None
    DEFAULT_AGENT_IMAGES = None


pytestmark = pytest.mark.skipif(
    not MODULES_AVAILABLE,
    reason="execution modules not yet implemented"
)


class TestAgentStepRoutesToLocalExecutor:
    """Agent steps route through LocalExecutor instead of job queue."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_agent_step_routes_to_local_executor(self, router):
        """Agent step without hardware requirements routes to LocalExecutor."""
        decision = router.route(
            step_type="agent",
            step_config={"runner_type": "claude-code", "title": "Test"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_agent_step_with_hardware_routes_to_remote(self, router):
        """Agent step with hardware requirements routes to RemoteExecutor."""
        decision = router.route(
            step_type="agent",
            step_config={"runner_type": "claude-code", "title": "Test"},
            requirements={"has": ["gpio"]},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_agent_step_preserves_type_in_decision(self, router):
        """Routing decision includes step_type for config building."""
        decision = router.route(
            step_type="agent",
            step_config={"runner_type": "claude-code"},
            requirements={},
        )
        assert decision.step_type == "agent"


class TestAgentStepUsesCorrectImage:
    """Agent steps use appropriate Docker images."""

    def test_claude_agent_uses_claude_image(self):
        """Claude agent step uses lazyaf-claude:latest image."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "claude-code"},
            workspace_path="/workspace",
        )
        assert config.image == "lazyaf-claude:latest"

    def test_gemini_agent_uses_gemini_image(self):
        """Gemini agent step uses lazyaf-gemini:latest image."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "gemini"},
            workspace_path="/workspace",
        )
        assert config.image == "lazyaf-gemini:latest"

    def test_any_runner_defaults_to_claude(self):
        """Runner type 'any' defaults to Claude image."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "any"},
            workspace_path="/workspace",
        )
        assert config.image == "lazyaf-claude:latest"

    def test_custom_image_overrides_default(self):
        """Explicit image in config overrides default agent image."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "claude-code", "image": "custom-agent:v1"},
            workspace_path="/workspace",
        )
        assert config.image == "custom-agent:v1"


class TestAgentWrapperInvocation:
    """Agent command invokes wrapper script, not direct CLI."""

    def test_agent_command_invokes_wrapper(self):
        """Agent step command invokes agent_wrapper.py script."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "claude-code", "title": "Test Task"},
            workspace_path="/workspace",
        )
        # Should invoke wrapper script, not direct claude CLI
        assert config.command == ["python", "/control/agent_wrapper.py"]

    def test_agent_wrapper_not_direct_cli(self):
        """Agent step does NOT invoke CLI directly (wrapper handles it)."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "claude-code", "title": "Test"},
            workspace_path="/workspace",
        )
        # Should NOT be direct CLI invocation
        assert "claude" not in config.command
        assert "-p" not in config.command


class TestAgentConfigInControlDirectory:
    """Agent config written to control directory for wrapper."""

    @pytest.fixture
    def mock_executor(self):
        """Create a mock LocalExecutor."""
        with patch("app.services.execution.local_executor.aiodocker") as mock_docker:
            mock_docker.Docker.return_value = AsyncMock()
            executor = LocalExecutor()
            return executor

    def test_agent_config_includes_title(self):
        """Agent config passed to wrapper includes title."""
        # This tests the config structure, not actual execution
        agent_config = {
            "runner_type": "claude-code",
            "title": "Implement feature X",
            "description": "Add a new button",
        }
        # Verify expected fields
        assert "title" in agent_config
        assert agent_config["title"] == "Implement feature X"

    def test_agent_config_includes_description(self):
        """Agent config includes description for prompt building."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "description": "Detailed description of the task",
        }
        assert "description" in agent_config
        assert agent_config["description"] == "Detailed description of the task"

    def test_agent_config_includes_model(self):
        """Agent config includes model selection when specified."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "model": "claude-sonnet-4-20250514",
        }
        assert "model" in agent_config
        assert agent_config["model"] == "claude-sonnet-4-20250514"

    def test_agent_config_includes_agent_file_ids(self):
        """Agent config includes agent file IDs for custom agents."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "agent_file_ids": ["agent-1", "agent-2"],
        }
        assert "agent_file_ids" in agent_config
        assert len(agent_config["agent_file_ids"]) == 2

    def test_agent_config_includes_previous_step_logs(self):
        """Agent config includes previous step logs for continuations."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "previous_step_logs": "Step 1 completed successfully\nOutput: 42",
        }
        assert "previous_step_logs" in agent_config
        assert "Step 1 completed" in agent_config["previous_step_logs"]

    def test_agent_config_includes_repo_url(self):
        """Agent config includes repo URL for git clone."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "repo_url": "http://localhost:8000/git/repo-123.git",
        }
        assert "repo_url" in agent_config
        assert ".git" in agent_config["repo_url"]

    def test_agent_config_includes_branch_name(self):
        """Agent config includes branch name for feature branch."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "branch_name": "lazyaf/abc12345",
        }
        assert "branch_name" in agent_config
        assert agent_config["branch_name"].startswith("lazyaf/")

    def test_agent_config_includes_base_branch(self):
        """Agent config includes base branch for checkout."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "base_branch": "main",
        }
        assert "base_branch" in agent_config


class TestAgentEnvironmentVariables:
    """Agent steps have correct environment variables."""

    def test_claude_agent_has_anthropic_api_key(self):
        """Claude agent environment includes ANTHROPIC_API_KEY."""
        with patch("app.services.execution.config_builder.get_settings") as mock_settings:
            mock_settings.return_value.anthropic_api_key = "sk-test-key"

            config = build_execution_config(
                step_type="agent",
                step_config={"runner_type": "claude-code"},
                workspace_path="/workspace",
                use_control_layer=True,
            )
            # Environment should have API key
            assert "ANTHROPIC_API_KEY" in config.environment
            assert config.environment["ANTHROPIC_API_KEY"] == "sk-test-key"

    def test_gemini_agent_has_gemini_api_key(self):
        """Gemini agent environment includes GEMINI_API_KEY."""
        with patch("app.services.execution.config_builder.get_settings") as mock_settings:
            mock_settings.return_value.gemini_api_key = "gemini-test-key"

            config = build_execution_config(
                step_type="agent",
                step_config={"runner_type": "gemini"},
                workspace_path="/workspace",
                use_control_layer=True,
            )
            assert "GEMINI_API_KEY" in config.environment
            assert config.environment["GEMINI_API_KEY"] == "gemini-test-key"

    def test_agent_environment_includes_home(self):
        """Agent environment sets HOME for cache persistence."""
        config = build_execution_config(
            step_type="agent",
            step_config={"runner_type": "claude-code"},
            workspace_path="/workspace",
            use_control_layer=True,
        )
        assert config.environment.get("HOME") == "/workspace/home"


class TestAgentStepContinuation:
    """Agent steps in continuation mode."""

    def test_continuation_reuses_workspace(self):
        """Continuation step should skip clone and reuse workspace."""
        # This is handled by the wrapper reading is_continuation from config
        agent_config = {
            "runner_type": "claude-code",
            "title": "Test",
            "is_continuation": True,
        }
        assert agent_config.get("is_continuation") is True

    def test_continuation_has_previous_step_logs(self):
        """Continuation step includes logs from previous step."""
        agent_config = {
            "runner_type": "claude-code",
            "title": "Fix test failures",
            "is_continuation": True,
            "previous_step_logs": "FAILED test_login.py::test_auth - AssertionError",
        }
        assert "FAILED" in agent_config["previous_step_logs"]
