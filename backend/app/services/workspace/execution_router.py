"""
Execution Router - Phase 12.2-INT / 12.4 / 12.6

Routes pipeline steps to an execution mode:
- "local":  LocalExecutor - the backend spawns the step container directly.
            The DEFAULT path for script, docker and agent steps.
- "remote": RemoteExecutor - the step is dispatched over the runner
            WebSocket to a runner agent that satisfies the step's
            `requires:` block. Arrived at Phase 12.6.
There is no third mode. The pre-12.2 polling-runner path was removed in the
12.6 deletion commit together with the queue it drained and the three runner
entrypoints that polled it, so `executor: legacy` and an unknown step type
both RAISE now: they name execution paths that do not exist, and routing them
somewhere plausible-looking would fail later, further from the cause.

Phase 12.6 also closes the last hole 12.4's fallout left open. Between 12.4 and
12.6 a runner pin (`runner_type:` / `requires:`) on a script/docker step was
routed LOCAL with a WARNING and reason "pin-not-honorable-local-until-12.6":
the work ran, but on the wrong machine. RemoteExecutor now exists, so a pin
is HONORED - it routes remote and the dispatcher matches it against the
registry. That reason string is deleted; a pin nobody can satisfy fails the
step loudly at `NO_RUNNER_TIMEOUT` rather than silently running on the
backend host.

Every decision carries a human-readable `reason` so routing is observable
end-to-end (StepRun.executor records what actually ran it; the dogfood
pipeline asserts on it via the API).
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.services.execution.runner_protocol import normalize_arch

logger = logging.getLogger(__name__)


# The step types the LocalExecutor handles directly.
#
# NOTE: "agent" is deliberately NOT in this tuple even though it also routes
# local by default. The distinction survives 12.6 because the two tuples mean
# different things: this one is "the executor runs a plain container for this
# step type", while LOCAL_DEFAULT_STEP_TYPES below is "this step type needs no
# pin to find a home". A bare `runner_type:` is routing sugar for the first
# group and ordinary AI-flavor config for an agent step - that asymmetry is
# the reason both names exist.
LOCAL_STEP_TYPES = ("script", "docker")

# Step types that route local by DEFAULT. Agent steps joined at 12.5, when
# the wrapper (runner_common.agent_wrapper) and the agent images landed.
LOCAL_DEFAULT_STEP_TYPES = LOCAL_STEP_TYPES + ("agent",)

# Valid values for a step-level `executor:` override. "remote" joined at 12.6
# and is now the only one: "legacy" left with the queue it named.
_VALID_EXECUTOR_OVERRIDES = ("remote",)

# Step-config keys that pin a step to a specific runner. `requires:` pins on
# EVERY step type; a top-level `runner_type:` is sugar for
# `requires.runner_type` on script/docker steps ONLY.
#
# The asymmetry is deliberate and load-bearing, not an accident: on an AGENT
# step `runner_type:` has meant "which AI flavor" since long before runners
# had hardware labels, and flipping those steps to remote would silently move
# every existing agent pipeline onto a runner that may not exist. An agent
# step goes remote only when its author writes an explicit `requires:` block.
_RUNNER_PIN_KEYS = ("runner_type", "requires")

# Requirement keys with dedicated matching semantics in
# `Runner.matches_requirements`. Everything else is matched for equality
# against the runner's labels - an unknown key is NEVER ignored (that is the
# failure_01 regression where `requires: {gpu: a100}` matched every runner).
KNOWN_REQUIREMENT_KEYS = ("runner_id", "runner_type", "arch", "has")


@dataclass
class RoutingDecision:
    """Result of a routing decision for a step.

    mode:         "local" | "remote"
    reason:       why this mode was chosen (persisted/logged - never silent).
    requirements: the parsed `requires:` block for a remote route. Empty for
                  a local route, and empty-but-remote is legal: it
                  means "any connected runner will do".
    """
    mode: str
    reason: str
    requirements: dict = field(default_factory=dict)


@dataclass
class ExecutorHandle:
    """Handle to an executor for step execution.

    Retained for import compatibility (app.services.workspace re-exports it);
    the pipeline executor drives the executor event stream directly.
    """
    is_local: bool
    executor: Any = None
    job_id: Optional[str] = None
    execution_context: Dict[str, Any] = field(default_factory=dict)


class ExecutionRouter:
    """
    Routes pipeline steps to an execution mode.

    Decision logic (in order):
    1. `executor:` override.
       - "remote" -> remote ("explicit-override") carrying whatever
         `requires:` the step declared (possibly nothing).
       - anything else -> ValueError. "legacy" included: 12.6 deleted the
         polling queue, so the value now names a path that does not exist,
         and the error says exactly that rather than routing somewhere
         plausible-looking.
    2. A runner pin -> remote ("runner-pin") with the parsed requirements.
       `requires:` pins any step type; a bare `runner_type:` pins script and
       docker steps only.
    3. Agent steps -> local ("agent-default-local").
    4. script / docker steps -> local ("<type>-default-local").
    5. Unknown step types -> ValueError. Until 12.6 this fell back to the
       legacy queue with a WARNING, which was an observable fallback while a
       fallback existed. There is none now, and inventing a route for a step
       type nobody has implemented would fail later, further from the cause.
    """

    def decide(self, step_type: str, step_config: dict) -> RoutingDecision:
        """Decide which execution mode should handle a step.

        Args:
            step_type: The step's type ("script" | "docker" | "agent" | ...)
            step_config: Full step configuration from the pipeline definition.

        Returns:
            RoutingDecision with mode, reason, and (for remote) requirements.

        Raises:
            ValueError: on an invalid `executor:` value (including the removed
                "legacy"), on an unknown step type, or on a malformed
                `requires:` block.
        """
        override = step_config.get("executor")
        if override is not None:
            if override == "legacy":
                raise ValueError(
                    "executor='legacy' names an execution path that no longer "
                    "exists: Phase 12.6 deleted the polling runner queue and "
                    "the runner entrypoints that drained it. Remove the "
                    "'executor: legacy' key to run this step on the local "
                    "executor, or pin it to a runner agent with a 'requires:' "
                    "block to run it remotely."
                )
            if override == "remote":
                requirements = self.parse_requirements(step_config, step_type)
                logger.info(
                    "Step routed to REMOTE executor by explicit override "
                    "(step_type=%s, requirements=%s)",
                    step_type,
                    requirements or "{} (any connected runner)",
                )
                return RoutingDecision(
                    mode="remote",
                    reason="explicit-override",
                    requirements=requirements,
                )
            raise ValueError(
                f"Invalid executor override {override!r} (step_type={step_type!r}): "
                f"valid values: {', '.join(_VALID_EXECUTOR_OVERRIDES)}"
            )

        pins = self.pin_keys(step_type, step_config)
        if pins:
            requirements = self.parse_requirements(step_config, step_type)
            # INFO, not WARNING: as of 12.6 the pin is HONORED. The only
            # loud event left on this path is "nothing matched", and that
            # belongs to the dispatcher (which can see the fleet) rather
            # than to a router that only sees one step's config.
            logger.info(
                "Step (step_type=%s) carries runner pin(s) %s -> REMOTE "
                "executor with requirements %s",
                step_type,
                pins,
                requirements,
            )
            return RoutingDecision(
                mode="remote", reason="runner-pin", requirements=requirements
            )

        if step_type == "agent":
            # 12.5: agent steps run on the control layer like everything
            # else. A bare runner_type on an agent step is ordinary config
            # (it names the AI flavor), so it never reaches the pin branch.
            return RoutingDecision(mode="local", reason="agent-default-local")

        if step_type in LOCAL_STEP_TYPES:
            return RoutingDecision(mode="local", reason=f"{step_type}-default-local")

        raise ValueError(
            f"Unknown step type {step_type!r}: there is no execution path for "
            "it. Until 12.6 this fell back to the polling runner queue with a "
            "WARNING; that queue is gone, so a step type the router does not "
            "know is now a definition error rather than a route to nowhere. "
            f"Add {step_type!r} to the router when it gains an execution path."
        )

    # -------------------------------------------------------------------------
    # The requirement grammar (cross-agent contract #5) - ONE parser
    # -------------------------------------------------------------------------

    def pin_keys(self, step_type: str, step_config: dict) -> list[str]:
        """Which keys of this step config pin it to a runner.

        Presence, not truthiness: `requires: {}` is still an author saying
        "run this remotely", and an empty requirement set legitimately means
        "any connected runner will do".
        """
        pins: list[str] = []
        if "requires" in step_config:
            pins.append("requires")
        if step_type in LOCAL_STEP_TYPES and "runner_type" in step_config:
            pins.append("runner_type")
        return pins

    def parse_requirements(
        self, step_config: dict, step_type: str | None = None
    ) -> dict:
        """Parse a step's `requires:` block into the matching grammar.

        This is the ONLY parser (cross-agent contract #5). Grammar:

            runner_id   -> exact match against the runner's id
            runner_type -> exact match; "any" matches everything
            arch        -> normalized on BOTH sides (normalize_arch)
            has         -> subset containment against labels["has"]
            any other k -> equality against labels[k]

        Normalization happens HERE, backend-side, so the agent can ship raw
        `platform.machine()` and there is exactly one implementation (R3).

        A top-level `runner_type:` on a script/docker step is sugar for
        `requires.runner_type` and is applied only when `requires` does not
        already name one - an explicit block always wins over the sugar.

        Raises:
            ValueError: `requires:` is present but is not a mapping. A list
                or a string there is an authoring mistake whose silent
                acceptance would produce a pin that matches everything.
        """
        raw = step_config.get("requires")
        if raw is None:
            requirements: dict = {}
        elif isinstance(raw, dict):
            requirements = dict(raw)
        else:
            raise ValueError(
                f"step 'requires:' must be a mapping of requirement keys, got "
                f"{type(raw).__name__}. Valid keys: "
                f"{', '.join(KNOWN_REQUIREMENT_KEYS)}, plus any label name."
            )

        sugar_applies = step_type is None or step_type in LOCAL_STEP_TYPES
        if sugar_applies and "runner_type" not in requirements:
            sugar = step_config.get("runner_type")
            if sugar is not None:
                requirements["runner_type"] = sugar

        parsed: dict = {}
        for key, value in requirements.items():
            if key == "arch":
                parsed[key] = normalize_arch(value)
            elif key == "has":
                parsed[key] = _as_list(value)
            elif key in ("runner_id", "runner_type"):
                parsed[key] = str(value)
            else:
                parsed[key] = value
        return parsed


def _as_list(value: Any) -> list:
    """Coerce a `has:` value to a list so a single string and a one-item
    list compare identically. Set/tuple order is normalized for stable
    persistence in `StepExecution.runner_requirements`."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return [value]


# Module singleton - the pipeline executor imports this (seam for 12.2-INT
# rewiring; stateless, safe to share).
execution_router = ExecutionRouter()
