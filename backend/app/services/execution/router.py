"""
Execution Router for Phase 12.2.

Routes step execution to the appropriate executor:
- LocalExecutor: Backend spawns containers directly (fast path)
- RemoteExecutor: Jobs pushed to remote runners via WebSocket

Routing Rules:
- No requirements -> LocalExecutor (default)
- Hardware requirements (gpio, camera, cuda) -> RemoteExecutor
- Specific runner_id -> RemoteExecutor
- Architecture mismatch -> RemoteExecutor
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .local_executor import LocalExecutor


class ExecutorType(Enum):
    """Type of executor to use."""
    LOCAL = "local"
    REMOTE = "remote"


@dataclass
class StepRequirements:
    """
    Parsed step requirements from pipeline YAML.

    Example YAML:
        requires:
          arch: arm64
          has: gpio,camera
          runner_id: pi-workshop-1
    """
    arch: Optional[str] = None
    has: List[str] = field(default_factory=list)
    runner_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepRequirements:
        """Parse requirements from dict."""
        has_value = data.get("has", [])

        # Handle string format "gpio,camera" or list format
        if isinstance(has_value, str):
            has_list = [h.strip() for h in has_value.split(",") if h.strip()]
        else:
            has_list = list(has_value)

        return cls(
            arch=data.get("arch"),
            has=has_list,
            runner_id=data.get("runner_id"),
        )

    @property
    def is_empty(self) -> bool:
        """True if no requirements specified."""
        return self.arch is None and not self.has and self.runner_id is None


@dataclass
class RoutingDecision:
    """
    Result of routing decision.

    Contains all info needed to dispatch to the chosen executor.
    """
    executor_type: ExecutorType
    reason: str
    required_labels: Dict[str, Any] = field(default_factory=dict)
    required_runner_id: Optional[str] = None
    workspace_affinity: str = "local"  # "local" or runner_id for remote
    # Phase 12.4: Include step info for execution config building
    step_type: str = "script"
    step_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Set workspace affinity based on executor type
        if self.executor_type == ExecutorType.LOCAL:
            self.workspace_affinity = "local"
        elif self.required_runner_id:
            self.workspace_affinity = self.required_runner_id


class ExecutionRouter:
    """
    Routes step execution to appropriate executor.

    Decides between LocalExecutor and RemoteExecutor based on
    step requirements and available resources.
    """

    def __init__(
        self,
        local_arch: Optional[str] = None,
        allow_remote: bool = True,
    ):
        """
        Initialize router.

        Args:
            local_arch: Architecture of local Docker host (auto-detected if None)
            allow_remote: If False, raise error instead of routing to remote
        """
        self.local_arch = local_arch or self._detect_local_arch()
        self.allow_remote = allow_remote
        self._local_executor: Optional[LocalExecutor] = None

    @staticmethod
    def _detect_local_arch() -> str:
        """Detect local machine architecture."""
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            return "amd64"
        elif machine in ("aarch64", "arm64"):
            return "arm64"
        elif machine.startswith("arm"):
            return "arm"
        return machine

    def route(
        self,
        step_type: str,
        step_config: Dict[str, Any],
        requirements: Dict[str, Any],
        previous_runner_id: Optional[str] = None,
    ) -> RoutingDecision:
        """
        Determine which executor should handle this step.

        Args:
            step_type: Type of step (script, docker, agent)
            step_config: Step configuration
            requirements: Step requirements dict
            previous_runner_id: Runner ID from previous step (for affinity)

        Returns:
            RoutingDecision with executor info

        Raises:
            ValueError: If remote execution needed but disabled
        """
        reqs = StepRequirements.from_dict(requirements)

        # Affinity: if previous step ran on a specific runner, continue there
        if previous_runner_id:
            return RoutingDecision(
                executor_type=ExecutorType.REMOTE,
                reason=f"Affinity: continuing on runner {previous_runner_id}",
                required_runner_id=previous_runner_id,
                step_type=step_type,
                step_config=step_config,
            )

        # Specific runner requested
        if reqs.runner_id:
            if not self.allow_remote:
                raise ValueError(
                    f"Remote execution disabled but runner_id={reqs.runner_id} requested"
                )
            return RoutingDecision(
                executor_type=ExecutorType.REMOTE,
                reason=f"Specific runner requested: {reqs.runner_id}",
                required_runner_id=reqs.runner_id,
                step_type=step_type,
                step_config=step_config,
            )

        # Hardware requirements (gpio, camera, cuda, etc.)
        if reqs.has:
            if not self.allow_remote:
                raise ValueError(
                    f"Remote execution disabled but hardware required: {reqs.has}"
                )
            labels = {"has": reqs.has}
            if reqs.arch:
                labels["arch"] = reqs.arch
            return RoutingDecision(
                executor_type=ExecutorType.REMOTE,
                reason=f"Hardware requirements: {', '.join(reqs.has)}",
                required_labels=labels,
                step_type=step_type,
                step_config=step_config,
            )

        # Architecture mismatch
        if reqs.arch and reqs.arch != self.local_arch:
            if not self.allow_remote:
                raise ValueError(
                    f"Remote execution disabled but arch={reqs.arch} required (local={self.local_arch})"
                )
            return RoutingDecision(
                executor_type=ExecutorType.REMOTE,
                reason=f"Architecture mismatch: need {reqs.arch}, local is {self.local_arch}",
                required_labels={"arch": reqs.arch},
                step_type=step_type,
                step_config=step_config,
            )

        # Default: local execution
        return RoutingDecision(
            executor_type=ExecutorType.LOCAL,
            reason="No special requirements, using local executor",
            step_type=step_type,
            step_config=step_config,
        )

    async def get_executor(self, decision: RoutingDecision):
        """
        Get the executor instance for a routing decision.

        Args:
            decision: Routing decision from route()

        Returns:
            Executor instance (LocalExecutor or RemoteExecutor)
        """
        if decision.executor_type == ExecutorType.LOCAL:
            return await self._get_local_executor()
        else:
            return await self._get_remote_executor(decision)

    async def _get_local_executor(self):
        """Get or create LocalExecutor singleton."""
        if self._local_executor is None:
            from .local_executor import LocalExecutor
            self._local_executor = LocalExecutor()
        return self._local_executor

    async def _get_remote_executor(self, decision: RoutingDecision):
        """Get RemoteExecutor for the decision."""
        from .remote_executor import get_remote_executor
        return get_remote_executor()
