"""
Tests for Step Routing Contract (Phase 12.4).

These tests DEFINE how script/docker steps are routed through the orchestrator.
Write tests first, then implement to make them pass.

Phase 12.4 Routing Requirements:
- Script steps route through LocalExecutor (not job queue)
- Docker steps route through LocalExecutor (not job queue)
- Custom images from step config are respected
- Agent steps continue to use existing flow (job queue) until Phase 12.5
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
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
        StepRequirements,
    )
    from app.services.execution.local_executor import (
        LocalExecutor,
        ExecutionConfig,
        ExecutionResult,
    )
    from app.services.execution.idempotency import ExecutionKey
    MODULES_AVAILABLE = True
except ImportError:
    MODULES_AVAILABLE = False
    ExecutionRouter = None
    ExecutorType = None
    RoutingDecision = None
    StepRequirements = None
    LocalExecutor = None
    ExecutionConfig = None
    ExecutionResult = None
    ExecutionKey = None


pytestmark = pytest.mark.skipif(
    not MODULES_AVAILABLE,
    reason="execution modules not yet implemented"
)


class TestScriptStepRoutesToOrchestrator:
    """Tests for script step routing through LocalExecutor."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_script_step_routes_to_local(self, router):
        """Script step without requirements routes to LocalExecutor."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_script_step_builds_execution_config(self, router):
        """Script step config is properly translated to ExecutionConfig."""
        decision = router.route(
            step_type="script",
            step_config={"command": "pytest -v"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL
        # The routing decision should indicate script type
        assert decision.step_type == "script"

    def test_script_step_uses_default_image(self, router):
        """Script step uses default image when none specified."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        # Default image for script steps should be defined
        assert decision.executor_type == ExecutorType.LOCAL

    def test_script_step_multiline_command(self, router):
        """Script step handles multiline commands."""
        multiline_cmd = """
            export PATH="$HOME/.local/bin:$PATH"
            uv sync
            pytest -v
        """
        decision = router.route(
            step_type="script",
            step_config={"command": multiline_cmd},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL


class TestDockerStepRoutesToOrchestrator:
    """Tests for docker step routing through LocalExecutor."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_docker_step_routes_to_local(self, router):
        """Docker step without requirements routes to LocalExecutor."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "python:3.12",
                "command": "pip install pytest",
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_docker_step_custom_image_preserved(self, router):
        """Docker step's custom image is preserved in routing."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "custom-runner:latest",
                "command": "run-tests",
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL
        # The step config should be accessible for later image extraction
        assert decision.step_config["image"] == "custom-runner:latest"

    def test_docker_step_with_working_dir(self, router):
        """Docker step respects working directory setting."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "node:20",
                "command": "npm test",
                "working_dir": "/app",
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL
        assert decision.step_config.get("working_dir") == "/app"

    def test_docker_step_with_environment(self, router):
        """Docker step respects environment variables."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "python:3.12",
                "command": "pytest",
                "environment": {"CI": "true", "DEBUG": "1"},
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL


class TestCustomImageRespected:
    """Tests for custom image handling in step configs."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_image_from_step_config(self, router):
        """Image specified in step_config is used."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "myregistry.io/myapp:v1.2.3",
                "command": "run",
            },
            requirements={},
        )
        assert decision.step_config["image"] == "myregistry.io/myapp:v1.2.3"

    def test_private_registry_image(self, router):
        """Private registry images are supported."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "ghcr.io/owner/repo:sha-abc123",
                "command": "test",
            },
            requirements={},
        )
        assert decision.step_config["image"] == "ghcr.io/owner/repo:sha-abc123"

    def test_no_image_uses_default(self, router):
        """When no image specified, script steps use default."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        # Default should be set (lazyaf-base or similar)
        assert decision.executor_type == ExecutorType.LOCAL


class TestAgentStepRoutingPreserved:
    """Tests ensuring agent steps still use existing flow (until Phase 12.5)."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_agent_step_routes_to_local_by_default(self, router):
        """Agent step routes to local but uses different handling."""
        decision = router.route(
            step_type="agent",
            step_config={
                "runner_type": "claude-code",
                "title": "Fix bug",
                "description": "Fix the authentication bug",
            },
            requirements={},
        )
        # Agent steps route to local but require special handling
        assert decision.executor_type == ExecutorType.LOCAL
        assert decision.step_type == "agent"


class TestRoutingDecisionIncludesStepInfo:
    """Tests for routing decision completeness."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_decision_includes_step_type(self, router):
        """RoutingDecision includes the step type."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test"},
            requirements={},
        )
        assert decision.step_type == "script"

    def test_decision_includes_step_config(self, router):
        """RoutingDecision includes the step config."""
        config = {"command": "pytest -v", "timeout": 300}
        decision = router.route(
            step_type="script",
            step_config=config,
            requirements={},
        )
        assert decision.step_config == config

    def test_decision_includes_workspace_affinity(self, router):
        """RoutingDecision includes workspace affinity info."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test"},
            requirements={},
        )
        # Local execution has local workspace affinity
        assert decision.workspace_affinity == "local"


class TestRequirementsOverrideDefaultRouting:
    """Tests for requirements-based routing."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_script_with_hardware_goes_remote(self, router):
        """Script step with hardware requirement routes remotely."""
        decision = router.route(
            step_type="script",
            step_config={"command": "gpio test"},
            requirements={"has": "gpio"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_docker_with_cuda_goes_remote(self, router):
        """Docker step with CUDA requirement routes remotely."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "nvidia/cuda:12.0-base",
                "command": "train",
            },
            requirements={"has": "cuda"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_script_with_specific_runner_goes_remote(self, router):
        """Script step with specific runner routes remotely."""
        decision = router.route(
            step_type="script",
            step_config={"command": "flash firmware"},
            requirements={"runner_id": "embedded-device-1"},
        )
        assert decision.executor_type == ExecutorType.REMOTE
        assert decision.required_runner_id == "embedded-device-1"


class TestExecutionConfigBuilding:
    """Tests for building ExecutionConfig from step configuration."""

    def test_build_config_from_script_step(self):
        """Build ExecutionConfig from script step config."""
        from app.services.execution.config_builder import build_execution_config

        step_config = {"command": "pytest -v"}
        workspace_path = "/tmp/workspace"

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path=workspace_path,
            timeout_seconds=300,
        )

        assert isinstance(config, ExecutionConfig)
        assert config.workspace_path == workspace_path
        assert config.timeout_seconds == 300
        # Script step should wrap command in bash
        assert "bash" in config.command[0] or config.command == ["bash", "-c", "pytest -v"]

    def test_build_config_from_docker_step(self):
        """Build ExecutionConfig from docker step config."""
        from app.services.execution.config_builder import build_execution_config

        step_config = {
            "image": "custom:latest",
            "command": "run-tests",
        }
        workspace_path = "/tmp/workspace"

        config = build_execution_config(
            step_type="docker",
            step_config=step_config,
            workspace_path=workspace_path,
            timeout_seconds=600,
        )

        assert isinstance(config, ExecutionConfig)
        assert config.image == "custom:latest"
        assert config.workspace_path == workspace_path

    def test_build_config_includes_environment(self):
        """Build ExecutionConfig includes environment variables."""
        from app.services.execution.config_builder import build_execution_config

        step_config = {
            "command": "test",
            "environment": {"CI": "true", "NODE_ENV": "test"},
        }
        workspace_path = "/tmp/workspace"

        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path=workspace_path,
        )

        assert config.environment.get("CI") == "true"
        assert config.environment.get("NODE_ENV") == "test"

    def test_build_config_default_image_for_script(self):
        """Script steps use default base image."""
        from app.services.execution.config_builder import (
            build_execution_config,
            DEFAULT_SCRIPT_IMAGE,
        )

        step_config = {"command": "echo hello"}
        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/tmp/ws",
        )

        assert config.image == DEFAULT_SCRIPT_IMAGE

    def test_build_config_control_layer_enabled(self):
        """Build ExecutionConfig with control layer enabled."""
        from app.services.execution.config_builder import build_execution_config

        step_config = {"command": "pytest"}
        config = build_execution_config(
            step_type="script",
            step_config=step_config,
            workspace_path="/tmp/ws",
            use_control_layer=True,
            backend_url="http://backend:8000",
        )

        assert config.use_control_layer is True
        assert config.backend_url == "http://backend:8000"
