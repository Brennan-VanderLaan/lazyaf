"""
Execution Router - Phase 12.2

Routes steps to appropriate executors:
- LocalExecutor: backend spawns containers directly (default)
- Remote: delegates to remote runners (hardware, AI agents)
"""
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional


# Default container image when not specified
DEFAULT_IMAGE = "alpine:latest"


@dataclass
class RoutingDecision:
    """Result of routing decision for a step."""
    executor_type: str  # "local" or "remote"
    step_type: str
    image: Optional[str] = None
    runner_requirements: Optional[Dict[str, Any]] = None
    runner_type: Optional[str] = None
    required_runner_id: Optional[str] = None
    fallback_reason: Optional[str] = None
    runner_available: bool = True


@dataclass
class ExecutorHandle:
    """Handle to an executor for step execution."""
    is_local: bool
    executor: Any = None
    job_id: Optional[str] = None
    execution_context: Dict[str, Any] = field(default_factory=dict)


class ExecutionRouter:
    """
    Routes pipeline steps to appropriate executors.

    Decision logic:
    1. Agent steps -> remote (need AI runner)
    2. Steps with hardware requirements -> remote
    3. Steps with specific runner_type -> remote
    4. Steps with required_runner_id -> remote
    5. Default -> local (backend spawns containers)

    Override modes:
    - force_local: All steps run locally (for development)
    - force_remote: All steps run remotely (for testing)
    """

    def __init__(
        self,
        force_local: bool = False,
        force_remote: bool = False,
    ):
        self._force_local = force_local
        self._force_remote = force_remote
        self._local_executor: Any = None
        self._local_executor_available = True
        self._check_runner_availability = lambda: True

    def decide(self, step_config: Dict[str, Any]) -> RoutingDecision:
        """
        Decide which executor should handle a step.

        Args:
            step_config: Step configuration from pipeline

        Returns:
            RoutingDecision with executor_type and requirements
        """
        step_type = step_config.get("type", "script")
        image = step_config.get("image", DEFAULT_IMAGE)
        requires = step_config.get("requires", {})
        runner_type = step_config.get("runner_type")
        required_runner_id = step_config.get("required_runner_id")

        # Extract runner requirements
        runner_requirements = None
        if requires:
            runner_requirements = requires

        # Force local mode overrides everything
        if self._force_local:
            return RoutingDecision(
                executor_type="local",
                step_type=step_type,
                image=image,
            )

        # Force remote mode overrides everything
        if self._force_remote:
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_type=runner_type,
                required_runner_id=required_runner_id,
            )

        # Check for local executor unavailability
        if not self._local_executor_available:
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_type=runner_type,
                required_runner_id=required_runner_id,
                fallback_reason="local_executor_unavailable",
            )

        # Agent steps always go remote (need AI runner)
        if step_type == "agent":
            runner_available = self._check_runner_availability()
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_type=runner_type,
                required_runner_id=required_runner_id,
                runner_available=runner_available,
            )

        # Steps with specific runner ID go remote
        if required_runner_id:
            runner_available = self._check_runner_availability()
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_type=runner_type,
                required_runner_id=required_runner_id,
                runner_available=runner_available,
            )

        # Steps with specific runner type go remote
        if runner_type:
            runner_available = self._check_runner_availability()
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_type=runner_type,
                runner_available=runner_available,
            )

        # Steps with hardware requirements go remote
        if requires and requires.get("hardware"):
            runner_available = self._check_runner_availability()
            return RoutingDecision(
                executor_type="remote",
                step_type=step_type,
                image=image,
                runner_requirements=runner_requirements,
                runner_available=runner_available,
            )

        # Default: local execution
        return RoutingDecision(
            executor_type="local",
            step_type=step_type,
            image=image,
        )

    async def get_executor(
        self,
        step_config: Dict[str, Any],
        execution_context: Dict[str, Any],
    ) -> ExecutorHandle:
        """
        Get an executor handle for a step.

        Args:
            step_config: Step configuration from pipeline
            execution_context: Context for execution (IDs, workspace volume, etc.)

        Returns:
            ExecutorHandle for executing the step
        """
        decision = self.decide(step_config)

        if decision.executor_type == "local":
            return ExecutorHandle(
                is_local=True,
                executor=self._local_executor,
                execution_context=execution_context,
            )
        else:
            # Remote execution - return handle for job enqueue
            return ExecutorHandle(
                is_local=False,
                execution_context=execution_context,
            )
