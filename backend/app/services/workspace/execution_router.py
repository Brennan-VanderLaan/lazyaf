"""
Execution Router - Phase 12.2-INT

Routes pipeline steps to an execution mode:
- "local":  LocalExecutor - the backend spawns the step container directly
            (default for script and docker steps, R1: default-ON).
- "legacy": the pre-12.2 job_queue/polling-runner path. Agent steps stay
            legacy until Phase 12.5; an explicit `executor: legacy` override
            in the step config also routes here - logged at WARNING, never
            silent (R1).

Every decision carries a human-readable `reason` so routing is observable
end-to-end (StepRun.executor records what actually ran it; the dogfood
pipeline asserts on it via the API).

"remote" (RemoteExecutor / runner agents) arrives at Phase 12.6.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Step types the LocalExecutor handles today.
LOCAL_STEP_TYPES = ("script", "docker")

# Valid values for a step-level `executor:` override.
_VALID_EXECUTOR_OVERRIDES = ("legacy",)

# Step-config keys that pin a step to a specific runner (runner affinity /
# hardware requirements). The local path cannot honor them - only the legacy
# runner queue can today, and RemoteExecutor takes over at Phase 12.6 -
# so their presence routes the step LEGACY, loudly (fix 9: never silently
# strip a pin by running the step locally).
_RUNNER_PIN_KEYS = ("runner_type", "requires")


@dataclass
class RoutingDecision:
    """Result of a routing decision for a step.

    mode:   "local" | "legacy"
    reason: why this mode was chosen (persisted/logged - never silent).
    """
    mode: str
    reason: str


@dataclass
class ExecutorHandle:
    """Handle to an executor for step execution.

    Retained for import compatibility (app.services.workspace re-exports it);
    the pipeline executor drives LocalExecutor's event stream directly.
    """
    is_local: bool
    executor: Any = None
    job_id: Optional[str] = None
    execution_context: Dict[str, Any] = field(default_factory=dict)


class ExecutionRouter:
    """
    Routes pipeline steps to an execution mode.

    Decision logic (in order):
    1. step_config["executor"] == "legacy" -> legacy ("explicit-override",
       logged at WARNING - an override is loud, never silent).
    2. Agent steps -> legacy (need the AI runner path until 12.5).
    3. step_config carrying a runner pin (runner_type / requires) -> legacy
       ("pinned-runner-legacy-until-12.6", logged at WARNING): only the
       legacy runner queue can honor pins until RemoteExecutor (12.6).
    4. script / docker steps -> local (LocalExecutor, default-ON per R1).
    5. Unknown step types -> legacy, logged at WARNING (observable fallback:
       the reason names the unknown type).

    An `executor:` override with any value other than "legacy" is a
    configuration error and raises ValueError (fail the run loudly rather
    than guess).
    """

    def decide(self, step_type: str, step_config: dict) -> RoutingDecision:
        """Decide which execution mode should handle a step.

        Args:
            step_type: The step's type ("script" | "docker" | "agent" | ...)
            step_config: Full step configuration from the pipeline definition.

        Returns:
            RoutingDecision with mode ("local" | "legacy") and reason.
        """
        override = step_config.get("executor")
        if override is not None:
            if override == "legacy":
                logger.warning(
                    "Step routed to LEGACY executor by explicit override "
                    "(step_type=%s). Remove the 'executor: legacy' key to use "
                    "the local execution path.",
                    step_type,
                )
                return RoutingDecision(mode="legacy", reason="explicit-override")
            raise ValueError(
                f"Invalid executor override {override!r} (step_type={step_type!r}): "
                f"valid values: {', '.join(_VALID_EXECUTOR_OVERRIDES)}"
            )

        if step_type == "agent":
            return RoutingDecision(mode="legacy", reason="agent-steps-legacy-until-12.5")

        pins = [key for key in _RUNNER_PIN_KEYS if key in step_config]
        if pins:
            logger.warning(
                "Step routed to LEGACY executor: step_config carries runner "
                "pin(s) %s which only the legacy runner queue can honor until "
                "RemoteExecutor lands at Phase 12.6 (step_type=%s).",
                pins,
                step_type,
            )
            return RoutingDecision(mode="legacy", reason="pinned-runner-legacy-until-12.6")

        if step_type in LOCAL_STEP_TYPES:
            return RoutingDecision(mode="local", reason=f"{step_type}-default-local")

        logger.warning(
            "Unknown step type %r routed to LEGACY executor - add it to the "
            "router when it gains a local execution path.",
            step_type,
        )
        return RoutingDecision(mode="legacy", reason=f"unknown-step-type:{step_type}")


# Module singleton - the pipeline executor imports this (seam for 12.2-INT
# rewiring; stateless, safe to share).
execution_router = ExecutionRouter()
