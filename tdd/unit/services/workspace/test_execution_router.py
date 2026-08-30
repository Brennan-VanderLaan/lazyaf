"""
Unit tests for the Execution Router (Phase 12.2-INT).

Contract (pre-agreed interface #4):
    decide(step_type: str, step_config: dict) -> RoutingDecision
    RoutingDecision has mode: "local" | "legacy" and reason: str.

Rules:
- script / docker steps -> local (LocalExecutor is their ONLY path since
  Phase 12.4 deleted script/docker execution from the runners)
- agent steps -> legacy (until Phase 12.5)
- step_config["executor"] == "legacy" -> legacy with reason
  "explicit-override", logged at WARNING (never silent) ... EXCEPT on a
  script/docker step, where it raises ValueError naming the unsupported
  combination (the requested path no longer exists)
- a runner pin (runner_type / requires) on a script/docker step -> local
  anyway, at WARNING (the pin is dropped, not honored, until 12.6)
- unknown step types -> legacy, logged at WARNING with the type in the reason
- any other executor override value -> ValueError (loud config error)

This file replaces the pre-12.2-INT stub-contract tests (local/remote
routing, force modes, runner requirements): that API was never wired into
the live path and is superseded by the mode/reason contract above. Remote
routing returns at Phase 12.6 through RemoteExecutor.
"""
import logging
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.workspace.execution_router import (  # noqa: E402
    ExecutionRouter,
    RoutingDecision,
    execution_router,
)

ROUTER_LOGGER = "app.services.workspace.execution_router"


@pytest.fixture
def router():
    return ExecutionRouter()


# -----------------------------------------------------------------------------
# Contract: default routing per step type
# -----------------------------------------------------------------------------

class TestDefaultRouting:
    def test_script_routes_local(self, router):
        decision = router.decide("script", {"command": "pytest tests/"})
        assert decision.mode == "local"
        assert decision.reason

    def test_docker_routes_local(self, router):
        decision = router.decide(
            "docker", {"command": "npm test", "image": "node:20"}
        )
        assert decision.mode == "local"
        assert decision.reason

    def test_agent_routes_legacy(self, router):
        decision = router.decide("agent", {"prompt": "Fix the failing tests"})
        assert decision.mode == "legacy"
        assert "12.5" in decision.reason  # states WHY agent steps stay legacy

    def test_script_with_image_still_local(self, router):
        decision = router.decide(
            "script", {"command": "pytest", "image": "python:3.12"}
        )
        assert decision.mode == "local"

    def test_decision_is_routing_decision_dataclass(self, router):
        decision = router.decide("script", {"command": "echo hi"})
        assert isinstance(decision, RoutingDecision)
        assert set(vars(decision)) == {"mode", "reason"}


# -----------------------------------------------------------------------------
# Contract: explicit legacy override - loud, never silent
# -----------------------------------------------------------------------------

class TestExplicitOverride:
    def test_explicit_legacy_override_on_agent_routes_legacy(self, router):
        decision = router.decide(
            "agent", {"prompt": "do it", "executor": "legacy"}
        )
        assert decision.mode == "legacy"
        assert decision.reason == "explicit-override"

    def test_explicit_override_logged_at_warning(self, router, caplog):
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            router.decide("agent", {"prompt": "do it", "executor": "legacy"})

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == ROUTER_LOGGER
        ]
        assert len(warnings) == 1
        assert "legacy" in warnings[0].getMessage().lower()
        assert "override" in warnings[0].getMessage().lower()

    def test_no_warning_for_default_routing(self, router, caplog):
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            router.decide("script", {"command": "pytest"})
            router.decide("docker", {"command": "ls", "image": "node:20"})
            router.decide("agent", {"prompt": "fix"})
        assert not [r for r in caplog.records if r.name == ROUTER_LOGGER]

    def test_invalid_override_value_raises(self, router):
        with pytest.raises(ValueError, match="executor override"):
            router.decide("script", {"command": "pytest", "executor": "quantum"})

    def test_invalid_override_local_raises(self, router):
        # "local" is the default, not an override value - misspelled intent
        # fails loudly instead of being half-honored.
        with pytest.raises(ValueError):
            router.decide("script", {"command": "pytest", "executor": "local"})


# -----------------------------------------------------------------------------
# Contract (12.4 fallout): `executor: legacy` on a script/docker step is a
# DEAD path - the runners reject those step types now. The router refuses the
# combination outright so the step fails at dispatch with a real message,
# instead of being enqueued into a queue whose consumers will reject it
# (the silent in_progress -> failed loop).
# -----------------------------------------------------------------------------

class TestLegacyOverrideOnDeletedPath:
    @pytest.mark.parametrize("step_type", ["script", "docker"])
    def test_legacy_override_on_script_or_docker_raises(self, router, step_type):
        with pytest.raises(ValueError) as exc:
            router.decide(
                step_type,
                {"command": "pytest", "image": "python:3.12", "executor": "legacy"},
            )
        message = str(exc.value)
        # Names the unsupported COMBINATION, not just "bad config".
        assert step_type in message
        assert "legacy" in message
        assert "12.4" in message

    def test_legacy_override_raise_beats_pin(self, router):
        """An explicit override is still evaluated first - it raises rather
        than falling through to the pin's local routing."""
        with pytest.raises(ValueError):
            router.decide(
                "script",
                {"command": "x", "runner_type": "any", "executor": "legacy"},
            )

    def test_raise_happens_before_any_decision_is_returned(self, router):
        """No RoutingDecision escapes for the unsupported combination."""
        for step_type in ("script", "docker"):
            with pytest.raises(ValueError):
                router.decide(step_type, {"executor": "legacy"})


# -----------------------------------------------------------------------------
# Contract (12.4 fallout): runner pins on script/docker route LOCAL anyway.
#
# Before 12.4 a pin routed LEGACY so the runner queue could honor it. The
# runners no longer execute script/docker steps at all, so legacy would mean
# "never runs". The work matters more than the pin: route local, and WARN
# loudly that the pin is being dropped (not honored) until RemoteExecutor
# arrives at 12.6.
# -----------------------------------------------------------------------------

class TestRunnerPins:
    def test_script_with_runner_type_routes_local(self, router):
        decision = router.decide(
            "script", {"command": "flash firmware.bin", "runner_type": "claude-code"}
        )
        assert decision.mode == "local"
        assert decision.reason == "pin-not-honorable-local-until-12.6"

    def test_docker_with_requires_routes_local(self, router):
        decision = router.decide(
            "docker",
            {
                "command": "run-hw-tests",
                "image": "alpine:latest",
                "requires": {"hardware": ["gpio", "uart"]},
            },
        )
        assert decision.mode == "local"
        assert decision.reason == "pin-not-honorable-local-until-12.6"

    def test_pin_logged_at_warning_naming_pin_and_phase(self, router, caplog):
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            router.decide("script", {"command": "x", "runner_type": "gemini"})
        warnings = [r for r in caplog.records if r.name == ROUTER_LOGGER]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "runner_type" in message
        assert "12.6" in message
        # The pin is DROPPED, and the log says so - never a silent strip.
        assert "LOCAL" in message

    def test_agent_step_keeps_agent_reason_over_pin(self, router):
        """Agent steps are legacy for their own (12.5) reason; a runner_type
        on an agent step is normal config, not a warning-worthy pin."""
        decision = router.decide(
            "agent", {"prompt": "fix it", "runner_type": "claude-code"}
        )
        assert decision.mode == "legacy"
        assert decision.reason == "agent-steps-legacy-until-12.5"

    def test_unpinned_script_still_local(self, router):
        decision = router.decide("script", {"command": "pytest"})
        assert decision.mode == "local"
        assert decision.reason == "script-default-local"

    def test_no_script_or_docker_config_can_route_legacy(self, router):
        """Property: the router NEVER hands a script/docker step to the
        legacy queue, whatever the config carries (it either routes local or
        raises)."""
        configs = [
            {},
            {"command": "pytest"},
            {"runner_type": "claude-code"},
            {"requires": {"hardware": ["gpio"]}},
            {"runner_type": "any", "requires": {"gpu": True}},
            {"image": "python:3.12", "env": {"A": "b"}},
        ]
        for step_type in ("script", "docker"):
            for config in configs:
                decision = router.decide(step_type, config)
                assert decision.mode == "local", (step_type, config)


# -----------------------------------------------------------------------------
# Contract: unknown step types - observable fallback
# -----------------------------------------------------------------------------

class TestUnknownStepTypes:
    def test_unknown_type_routes_legacy_with_named_reason(self, router):
        decision = router.decide("teleport", {"command": "beam me up"})
        assert decision.mode == "legacy"
        assert decision.reason == "unknown-step-type:teleport"

    def test_unknown_type_logged_at_warning(self, router, caplog):
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            router.decide("teleport", {})
        warnings = [r for r in caplog.records if r.name == ROUTER_LOGGER]
        assert len(warnings) == 1
        assert "teleport" in warnings[0].getMessage()


# -----------------------------------------------------------------------------
# Module singleton seam for the pipeline executor
# -----------------------------------------------------------------------------

class TestModuleSingleton:
    def test_singleton_exists_and_routes(self):
        decision = execution_router.decide("script", {"command": "echo hi"})
        assert decision.mode == "local"

    def test_singleton_is_execution_router(self):
        assert isinstance(execution_router, ExecutionRouter)


# -----------------------------------------------------------------------------
# Package re-export stays importable (app.services.workspace consumers)
# -----------------------------------------------------------------------------

class TestPackageExports:
    def test_workspace_package_reexports_router(self):
        from app.services.workspace import ExecutionRouter as Reexported
        from app.services.workspace import RoutingDecision as ReexportedDecision

        assert Reexported is ExecutionRouter
        assert ReexportedDecision is RoutingDecision
