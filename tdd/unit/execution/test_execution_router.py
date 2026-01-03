"""
Tests for Execution Router (Phase 12.2).

These tests DEFINE the execution routing contract.
Write tests first, then implement to make them pass.

Router Responsibilities:
- Decide between LocalExecutor and RemoteExecutor
- Route based on step requirements (hardware, labels)
- Return appropriate executor handle
- Default to LocalExecutor when no special requirements

Routing Rules:
- No requirements -> LocalExecutor (fast path)
- Hardware requirements (gpio, camera, cuda) -> RemoteExecutor
- Specific runner_id -> RemoteExecutor
- Architecture requirements (arm64) -> RemoteExecutor (if local is different)
"""
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.router import (
        ExecutionRouter,
        ExecutorType,
        RoutingDecision,
        StepRequirements,
    )
    from app.services.execution.local_executor import LocalExecutor
    ROUTER_MODULE_AVAILABLE = True
except ImportError:
    ROUTER_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    from enum import Enum
    ExecutorType = Enum("ExecutorType", ["LOCAL", "REMOTE"])
    ExecutionRouter = None
    RoutingDecision = None
    StepRequirements = None
    LocalExecutor = None


pytestmark = pytest.mark.skipif(
    not ROUTER_MODULE_AVAILABLE,
    reason="execution router module not yet implemented"
)


class TestExecutorTypes:
    """Tests for executor type definitions."""

    def test_has_local_executor_type(self):
        """ExecutorType has LOCAL value."""
        assert ExecutorType.LOCAL is not None
        assert ExecutorType.LOCAL.value == "local"

    def test_has_remote_executor_type(self):
        """ExecutorType has REMOTE value."""
        assert ExecutorType.REMOTE is not None
        assert ExecutorType.REMOTE.value == "remote"


class TestStepRequirements:
    """Tests for step requirements parsing."""

    def test_empty_requirements(self):
        """Empty requirements dict is valid."""
        reqs = StepRequirements.from_dict({})
        assert reqs.arch is None
        assert reqs.has == []
        assert reqs.runner_id is None

    def test_arch_requirement(self):
        """Architecture requirement is parsed."""
        reqs = StepRequirements.from_dict({"arch": "arm64"})
        assert reqs.arch == "arm64"

    def test_has_requirement_single(self):
        """Single 'has' requirement is parsed."""
        reqs = StepRequirements.from_dict({"has": "gpio"})
        assert reqs.has == ["gpio"]

    def test_has_requirement_multiple(self):
        """Multiple 'has' requirements are parsed."""
        reqs = StepRequirements.from_dict({"has": "gpio,camera,spi"})
        assert reqs.has == ["gpio", "camera", "spi"]

    def test_has_requirement_list(self):
        """List format 'has' requirements are parsed."""
        reqs = StepRequirements.from_dict({"has": ["gpio", "camera"]})
        assert reqs.has == ["gpio", "camera"]

    def test_runner_id_requirement(self):
        """Specific runner_id requirement is parsed."""
        reqs = StepRequirements.from_dict({"runner_id": "pi-workshop-1"})
        assert reqs.runner_id == "pi-workshop-1"

    def test_combined_requirements(self):
        """Multiple requirements can be combined."""
        reqs = StepRequirements.from_dict({
            "arch": "arm64",
            "has": "gpio,camera",
            "runner_id": "pi-workshop-1",
        })
        assert reqs.arch == "arm64"
        assert reqs.has == ["gpio", "camera"]
        assert reqs.runner_id == "pi-workshop-1"

    def test_is_empty_true_for_no_requirements(self):
        """is_empty returns True when no requirements set."""
        reqs = StepRequirements.from_dict({})
        assert reqs.is_empty is True

    def test_is_empty_false_for_any_requirement(self):
        """is_empty returns False when any requirement set."""
        reqs = StepRequirements.from_dict({"arch": "amd64"})
        assert reqs.is_empty is False


class TestRoutesToLocalWhenNoRequirements:
    """Tests for local routing with no special requirements."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_routes_to_local_when_no_requirements(self, router):
        """Default routing is LocalExecutor."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_routes_to_local_for_script_step(self, router):
        """Script steps route to LocalExecutor by default."""
        decision = router.route(
            step_type="script",
            step_config={"command": "pytest -v"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_routes_to_local_for_docker_step(self, router):
        """Docker steps route to LocalExecutor by default."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "python:3.12",
                "command": "pip install pytest",
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_routes_to_local_for_agent_step(self, router):
        """Agent steps route to LocalExecutor by default."""
        decision = router.route(
            step_type="agent",
            step_config={
                "agent_id": "claude-code",
                "task": "Implement feature X",
            },
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL


class TestRoutesToRemoteWhenHardwareRequired:
    """Tests for remote routing with hardware requirements."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_routes_to_remote_when_gpio_required(self, router):
        """GPIO requirement routes to RemoteExecutor."""
        decision = router.route(
            step_type="script",
            step_config={"command": "python gpio_test.py"},
            requirements={"has": "gpio"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_routes_to_remote_when_camera_required(self, router):
        """Camera requirement routes to RemoteExecutor."""
        decision = router.route(
            step_type="script",
            step_config={"command": "python capture.py"},
            requirements={"has": "camera"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_routes_to_remote_when_cuda_required(self, router):
        """CUDA requirement routes to RemoteExecutor."""
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "nvidia/cuda:12.0-base",
                "command": "python train.py",
            },
            requirements={"has": "cuda"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_routes_to_remote_when_specific_runner_required(self, router):
        """Specific runner_id routes to RemoteExecutor."""
        decision = router.route(
            step_type="script",
            step_config={"command": "flash-firmware"},
            requirements={"runner_id": "pi-workshop-1"},
        )
        assert decision.executor_type == ExecutorType.REMOTE
        assert decision.required_runner_id == "pi-workshop-1"

    def test_routes_to_remote_when_arch_different(self, router):
        """Different architecture routes to RemoteExecutor."""
        # Assuming local is amd64
        decision = router.route(
            step_type="docker",
            step_config={
                "image": "arm64v8/python:3.12",
                "command": "python test.py",
            },
            requirements={"arch": "arm64"},
        )
        assert decision.executor_type == ExecutorType.REMOTE
        assert decision.required_labels.get("arch") == "arm64"


class TestRoutingDecision:
    """Tests for routing decision structure."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_returns_executor_handle(self, router):
        """Router returns a RoutingDecision with executor info."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        assert isinstance(decision, RoutingDecision)

    def test_decision_has_executor_type(self, router):
        """RoutingDecision includes executor type."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        assert hasattr(decision, "executor_type")
        assert decision.executor_type in [ExecutorType.LOCAL, ExecutorType.REMOTE]

    def test_decision_has_required_labels(self, router):
        """RoutingDecision includes required labels for remote."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test.py"},
            requirements={"has": "gpio,camera", "arch": "arm64"},
        )
        assert hasattr(decision, "required_labels")
        assert decision.required_labels["arch"] == "arm64"
        assert "gpio" in decision.required_labels.get("has", [])
        assert "camera" in decision.required_labels.get("has", [])

    def test_decision_has_required_runner_id(self, router):
        """RoutingDecision includes required runner_id if specified."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test.py"},
            requirements={"runner_id": "specific-runner"},
        )
        assert decision.required_runner_id == "specific-runner"


class TestExecutorCreation:
    """Tests for executor creation from routing decision."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    @pytest.mark.asyncio
    async def test_get_executor_returns_local(self, router):
        """get_executor returns LocalExecutor for local decisions."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        executor = await router.get_executor(decision)
        assert isinstance(executor, LocalExecutor)

    @pytest.mark.asyncio
    async def test_local_executor_is_singleton(self, router):
        """LocalExecutor is reused (not recreated each time)."""
        decision1 = router.route(
            step_type="script",
            step_config={"command": "echo 1"},
            requirements={},
        )
        decision2 = router.route(
            step_type="script",
            step_config={"command": "echo 2"},
            requirements={},
        )

        executor1 = await router.get_executor(decision1)
        executor2 = await router.get_executor(decision2)

        assert executor1 is executor2


class TestAffinityRouting:
    """Tests for affinity-based routing (continue_in_context)."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_affinity_overrides_default(self, router):
        """When previous_runner_id is set, route to same runner."""
        decision = router.route(
            step_type="script",
            step_config={"command": "pytest -v"},
            requirements={},  # No requirements
            previous_runner_id="runner-abc-123",
        )
        # Even with no requirements, should route to previous runner
        assert decision.executor_type == ExecutorType.REMOTE
        assert decision.required_runner_id == "runner-abc-123"

    def test_affinity_compatible_with_requirements(self, router):
        """Affinity routing works with matching requirements."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test.py"},
            requirements={"arch": "arm64"},
            previous_runner_id="pi-runner-1",
        )
        assert decision.executor_type == ExecutorType.REMOTE
        assert decision.required_runner_id == "pi-runner-1"

    def test_local_executor_affinity(self, router):
        """Local execution has affinity marker for workspace sharing."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo 1"},
            requirements={},
        )
        # Local executor uses same host, so workspace is shared by default
        assert decision.executor_type == ExecutorType.LOCAL
        # Local decisions should indicate workspace affinity
        assert decision.workspace_affinity == "local"


class TestRoutingWithConfig:
    """Tests for router configuration."""

    def test_router_with_local_arch(self):
        """Router can be configured with local architecture."""
        router = ExecutionRouter(local_arch="amd64")
        assert router.local_arch == "amd64"

    def test_same_arch_routes_local(self):
        """Same architecture as local routes locally."""
        router = ExecutionRouter(local_arch="amd64")
        decision = router.route(
            step_type="docker",
            step_config={"image": "python:3.12", "command": "test"},
            requirements={"arch": "amd64"},
        )
        assert decision.executor_type == ExecutorType.LOCAL

    def test_different_arch_routes_remote(self):
        """Different architecture routes remotely."""
        router = ExecutionRouter(local_arch="amd64")
        decision = router.route(
            step_type="docker",
            step_config={"image": "arm64v8/python:3.12", "command": "test"},
            requirements={"arch": "arm64"},
        )
        assert decision.executor_type == ExecutorType.REMOTE

    def test_router_with_no_remote_fallback(self):
        """Router can be configured to fail instead of remote fallback."""
        router = ExecutionRouter(local_arch="amd64", allow_remote=False)
        with pytest.raises(ValueError) as exc_info:
            router.route(
                step_type="script",
                step_config={"command": "test"},
                requirements={"arch": "arm64"},
            )
        assert "remote execution disabled" in str(exc_info.value).lower()


class TestRoutingReason:
    """Tests for routing decision reasoning."""

    @pytest.fixture
    def router(self):
        """Create an execution router."""
        return ExecutionRouter()

    def test_decision_includes_reason(self, router):
        """RoutingDecision includes human-readable reason."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test.py"},
            requirements={"has": "gpio"},
        )
        assert hasattr(decision, "reason")
        assert isinstance(decision.reason, str)
        assert len(decision.reason) > 0

    def test_reason_explains_local(self, router):
        """Reason explains why local was chosen."""
        decision = router.route(
            step_type="script",
            step_config={"command": "echo hello"},
            requirements={},
        )
        assert decision.executor_type == ExecutorType.LOCAL
        assert "local" in decision.reason.lower() or "no requirement" in decision.reason.lower()

    def test_reason_explains_remote_hardware(self, router):
        """Reason explains hardware requirement for remote."""
        decision = router.route(
            step_type="script",
            step_config={"command": "test.py"},
            requirements={"has": "cuda"},
        )
        assert decision.executor_type == ExecutorType.REMOTE
        assert "cuda" in decision.reason.lower() or "hardware" in decision.reason.lower()
