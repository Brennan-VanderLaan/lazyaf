"""
Execution Router - Phase 12.2-INT / 12.4

Routes pipeline steps to an execution mode:
- "local":  LocalExecutor - the backend spawns the step container directly.
            The ONLY path for script and docker steps since Phase 12.4, and
            the DEFAULT path for agent steps since Phase 12.5.
- "legacy": the pre-12.2 job_queue/polling-runner path. After 12.5 nothing
            routes there by default; it survives ONLY as the explicit
            `executor: legacy` escape hatch on an agent step (R2 requires it
            to stay callable until the 12.6 deletion commit) and as the loud
            fallback for unknown step types.

Phase 12.4 deleted script/docker execution from the runners: `execute_job`
in every runner entrypoint now REJECTS a script/docker job instead of
running it. That makes "legacy" a dead end for those step types, so the
router must never send them there - a legacy-routed script step would be
enqueued, picked up, and immediately failed by the runner (the silent
in_progress -> failed loop this rule exists to prevent).

Two former legacy escape hatches for script/docker are therefore closed:

1. Runner pins (`runner_type` / `requires`) route LOCAL anyway, at WARNING:
   the pin cannot be honored until RemoteExecutor lands at Phase 12.6, but
   the work still runs. The warning is the contract - never a silent strip.
2. An explicit `executor: legacy` override on a script/docker step RAISES:
   the user asked for an execution path that no longer exists, and guessing
   on their behalf (either honoring it into a dead queue, or quietly running
   local) is worse than failing the step at dispatch with a message naming
   the unsupported combination.

Every decision carries a human-readable `reason` so routing is observable
end-to-end (StepRun.executor records what actually ran it; the dogfood
pipeline asserts on it via the API).

"remote" (RemoteExecutor / runner agents) arrives at Phase 12.6.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Step types the LocalExecutor handles today. Since 12.4 these are also the
# step types the runners REFUSE, so local is their only execution path.
#
# NOTE: "agent" is deliberately NOT in this tuple even though it routes local
# since 12.5. This tuple means "the runners cannot execute this at all", and
# it gates the two hard errors below (executor: legacy raises; the enqueue
# site refuses). An agent step CAN still be executed by a runner, so its
# legacy escape hatch stays legal - that is exactly the difference.
LOCAL_STEP_TYPES = ("script", "docker")

# Step types that route local by DEFAULT. Agent steps joined at 12.5, when
# the wrapper (runner_common.agent_wrapper) and the agent images landed.
LOCAL_DEFAULT_STEP_TYPES = LOCAL_STEP_TYPES + ("agent",)

# Valid values for a step-level `executor:` override.
_VALID_EXECUTOR_OVERRIDES = ("legacy",)

# Step-config keys that pin a step to a specific runner (runner affinity /
# hardware requirements). Nothing can honor them today: the legacy runner
# queue no longer executes script/docker steps at all (12.4) and
# RemoteExecutor arrives at 12.6. Presence routes LOCAL with a loud WARNING
# so the pin is visibly dropped rather than silently stripped OR silently
# parked in a queue that will reject it.
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
    1. step_config["executor"] == "legacy":
       - on a script/docker step -> ValueError. Runners reject those step
         types since 12.4; the requested path does not exist (fail loudly at
         dispatch rather than enqueue into a guaranteed failure).
       - otherwise -> legacy ("explicit-override", logged at WARNING).
    2. Agent steps -> local ("agent-default-local", 12.5). The wrapper runs
       in an ephemeral control-mode container exactly like a script step.
    3. script / docker steps -> local. If the step_config carries a runner
       pin (runner_type / requires) it still routes local, with a WARNING
       naming the pin as unhonorable until 12.6 - reason
       "pin-not-honorable-local-until-12.6".
    4. Unknown step types -> legacy, logged at WARNING (observable fallback:
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

        Raises:
            ValueError: on an invalid `executor:` value, or on
                `executor: legacy` for a script/docker step (the legacy path
                for those step types was deleted in Phase 12.4).
        """
        override = step_config.get("executor")
        if override is not None:
            if override == "legacy":
                if step_type in LOCAL_STEP_TYPES:
                    raise ValueError(
                        f"Unsupported combination: step_type={step_type!r} with "
                        "executor='legacy'. Phase 12.4 removed script/docker "
                        "execution from the runners, so the legacy queue can no "
                        f"longer run a {step_type} step - a job enqueued there is "
                        "rejected on pickup. Remove the 'executor: legacy' key to "
                        "run this step on the local executor."
                    )
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
            # 12.5: agent steps run on the control layer like everything
            # else. A runner_type on an agent step is ordinary config (it
            # named the AI runner flavor), not an unhonorable hardware pin,
            # so it does NOT take the pin-warning branch below.
            return RoutingDecision(mode="local", reason="agent-default-local")

        if step_type in LOCAL_STEP_TYPES:
            pins = [key for key in _RUNNER_PIN_KEYS if key in step_config]
            if pins:
                logger.warning(
                    "Step (step_type=%s) carries runner pin(s) %s that CANNOT be "
                    "honored: the legacy runner queue no longer executes "
                    "script/docker steps (Phase 12.4) and RemoteExecutor lands at "
                    "Phase 12.6. Running it on the LOCAL executor anyway - the "
                    "pin is being dropped, not honored.",
                    step_type,
                    pins,
                )
                return RoutingDecision(
                    mode="local", reason="pin-not-honorable-local-until-12.6"
                )
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
