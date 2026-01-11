"""
Unit tests for Execution Router.

These tests define the contract for routing steps to executors:
- Default routes to LocalExecutor (backend spawns containers)
- Routes to remote runner when hardware requirements specified
- Returns async generator handle for streaming results

Write these tests BEFORE implementing the execution router.
"""
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock, AsyncMock

import pytest

# Tests enabled - execution router implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Routing Decisions
# -----------------------------------------------------------------------------

class TestRoutingDecisions:
    """Tests that verify routing decision logic."""

    def test_routes_to_local_when_no_requirements(self):
        """Steps with no hardware requirements route to LocalExecutor."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "echo hello",
            "image": "alpine:latest",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "local"
        assert decision.runner_requirements is None

    def test_routes_to_local_for_script_steps(self):
        """Script steps default to local execution."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "pytest tests/",
            "image": "python:3.12",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "local"

    def test_routes_to_local_for_docker_steps(self):
        """Docker steps default to local execution."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "docker",
            "command": "npm test",
            "image": "node:20",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "local"

    def test_routes_to_remote_when_hardware_required(self):
        """Steps with hardware requirements route to remote runner."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "flash firmware.bin",
            "image": "alpine:latest",
            "requires": {
                "hardware": ["gpio", "uart"],
            },
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"
        assert decision.runner_requirements == {"hardware": ["gpio", "uart"]}

    def test_routes_to_remote_when_runner_type_specified(self):
        """Steps with specific runner_type route to remote."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "agent",
            "command": "implement feature X",
            "runner_type": "claude-code",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"
        assert decision.runner_type == "claude-code"

    def test_routes_agent_steps_to_remote(self):
        """Agent steps always route to remote (need AI runner)."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "agent",
            "prompt": "Fix the failing tests",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"

    def test_routes_to_specific_runner_id(self):
        """Steps can specify a specific runner ID for affinity."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "echo hello",
            "required_runner_id": "runner-abc123",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"
        assert decision.required_runner_id == "runner-abc123"


# -----------------------------------------------------------------------------
# Contract: Executor Handle
# -----------------------------------------------------------------------------

class TestExecutorHandle:
    """Tests that verify executor handle creation."""

    async def test_returns_async_generator_for_local(self):
        """Local execution returns async generator for streaming."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "echo hello",
            "image": "alpine:latest",
        }

        execution_context = {
            "pipeline_run_id": str(uuid4()),
            "step_run_id": str(uuid4()),
            "step_index": 0,
            "execution_key": f"{uuid4()}:0:1",
            "workspace_volume": f"lazyaf-ws-{uuid4()}",
        }

        # Mock the LocalExecutor
        mock_executor = Mock()
        mock_generator = AsyncMock()
        mock_executor.execute_step.return_value = mock_generator
        router._local_executor = mock_executor

        handle = await router.get_executor(step_config, execution_context)
        assert handle is not None
        assert handle.is_local

    async def test_returns_job_enqueue_for_remote(self):
        """Remote execution returns job enqueue handle."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "agent",
            "prompt": "Fix tests",
        }

        execution_context = {
            "pipeline_run_id": str(uuid4()),
            "step_run_id": str(uuid4()),
            "step_index": 0,
            "execution_key": f"{uuid4()}:0:1",
            "workspace_volume": f"lazyaf-ws-{uuid4()}",
        }

        handle = await router.get_executor(step_config, execution_context)
        assert handle is not None
        assert not handle.is_local


# -----------------------------------------------------------------------------
# Contract: Router Configuration
# -----------------------------------------------------------------------------

class TestRouterConfiguration:
    """Tests that verify router configuration options."""

    def test_force_local_mode(self):
        """Router can be configured to force local execution for all steps."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter(force_local=True)

        # Even agent steps go local in force_local mode
        step_config = {
            "type": "agent",
            "prompt": "Fix tests",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "local"

    def test_force_remote_mode(self):
        """Router can be configured to force remote execution for all steps."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter(force_remote=True)

        # Even simple scripts go remote in force_remote mode
        step_config = {
            "type": "script",
            "command": "echo hello",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"

    def test_local_executor_available_check(self):
        """Router checks if LocalExecutor is available (Docker running)."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        # When local executor not available, fall back to remote
        router._local_executor_available = False

        step_config = {
            "type": "script",
            "command": "echo hello",
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"
        assert decision.fallback_reason == "local_executor_unavailable"


# -----------------------------------------------------------------------------
# Contract: Routing Metadata
# -----------------------------------------------------------------------------

class TestRoutingMetadata:
    """Tests that verify routing decision metadata."""

    def test_decision_includes_step_type(self):
        """Routing decision includes the original step type."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "docker",
            "command": "npm test",
            "image": "node:20",
        }

        decision = router.decide(step_config)
        assert decision.step_type == "docker"

    def test_decision_includes_image(self):
        """Routing decision includes the container image."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "pytest",
            "image": "python:3.12-slim",
        }

        decision = router.decide(step_config)
        assert decision.image == "python:3.12-slim"

    def test_decision_uses_default_image_when_not_specified(self):
        """Routing decision uses default image when not specified."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "echo hello",
        }

        decision = router.decide(step_config)
        assert decision.image is not None  # Uses default


# -----------------------------------------------------------------------------
# Contract: Runner Requirements Matching
# -----------------------------------------------------------------------------

class TestRunnerRequirements:
    """Tests that verify runner requirement matching."""

    def test_hardware_requirements_extracted(self):
        """Hardware requirements are extracted for routing."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "script",
            "command": "test hardware",
            "requires": {
                "hardware": ["gpio", "spi"],
                "capabilities": ["arm64"],
            },
        }

        decision = router.decide(step_config)
        assert decision.runner_requirements["hardware"] == ["gpio", "spi"]
        assert decision.runner_requirements["capabilities"] == ["arm64"]

    def test_runner_type_extracted(self):
        """Runner type is extracted for routing."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        step_config = {
            "type": "agent",
            "prompt": "Fix tests",
            "runner_type": "gemini",
        }

        decision = router.decide(step_config)
        assert decision.runner_type == "gemini"

    def test_no_matching_runner_returns_none(self):
        """When no runner matches requirements, decision indicates unavailable."""
        from app.services.workspace.execution_router import ExecutionRouter

        router = ExecutionRouter()

        # Mock no matching runners available
        router._check_runner_availability = Mock(return_value=False)

        step_config = {
            "type": "script",
            "command": "special hardware",
            "requires": {
                "hardware": ["nonexistent-device"],
            },
        }

        decision = router.decide(step_config)
        assert decision.executor_type == "remote"
        assert not decision.runner_available
