"""
Unit tests for the Execution Router (Phase 12.2-INT).

Contract (pre-agreed interface #4):
    decide(step_type: str, step_config: dict) -> RoutingDecision
    RoutingDecision has mode: "local" | "legacy" and reason: str.

Rules:
- UNPINNED script / docker steps -> local (LocalExecutor is their default
  path since Phase 12.4 deleted script/docker execution from the runners)
- UNPINNED agent steps -> local (since Phase 12.5)
- step_config["executor"] == "legacy" -> legacy with reason
  "explicit-override", logged at WARNING (never silent) ... EXCEPT on a
  script/docker step, where it raises ValueError naming the unsupported
  combination (the requested path no longer exists)
- a runner pin (runner_type / requires) -> REMOTE with reason "runner-pin"
  (Phase 12.6: RemoteExecutor exists, so the pin is HONORED). The 12.4-12.6
  interim reason "pin-not-honorable-local-until-12.6" no longer exists.
- unknown step types -> legacy, logged at WARNING with the type in the reason
- any other executor override value -> ValueError (loud config error)

The requirement GRAMMAR (parse_requirements) has its own file:
tdd/unit/services/test_execution_router_requires.py. This file stays on the
mode/reason contract the pipeline executor consumes.
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

    def test_agent_routes_local(self, router):
        """12.5 flipped this: agent steps run on the control layer."""
        decision = router.decide("agent", {"prompt": "Fix the failing tests"})
        assert decision.mode == "local"
        assert decision.reason == "agent-default-local"

    def test_script_with_image_still_local(self, router):
        decision = router.decide(
            "script", {"command": "pytest", "image": "python:3.12"}
        )
        assert decision.mode == "local"

    def test_decision_is_routing_decision_dataclass(self, router):
        decision = router.decide("script", {"command": "echo hi"})
        assert isinstance(decision, RoutingDecision)
        assert set(vars(decision)) == {"mode", "reason", "requirements"}
        assert decision.requirements == {}


# -----------------------------------------------------------------------------
# Contract: explicit legacy override - loud, never silent
# -----------------------------------------------------------------------------

class TestExplicitOverride:
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
# Contract (12.6 deletion commit): `executor: legacy` names a path that no
# longer exists ANYWHERE. Between 12.4 and 12.6 it raised for script/docker
# steps (the runners had stopped executing those) and was still honored for
# agent steps as the R2 escape hatch. The queue and the runner entrypoints
# are gone now, so it raises for EVERY step type - and the message says the
# path was removed rather than describing a combination problem.
# -----------------------------------------------------------------------------

class TestLegacyOverrideIsGone:
    @pytest.mark.parametrize("step_type", ["script", "docker", "agent"])
    def test_legacy_override_raises_for_every_step_type(self, router, step_type):
        with pytest.raises(ValueError) as exc:
            router.decide(
                step_type,
                {"command": "pytest", "image": "python:3.12", "executor": "legacy"},
            )
        message = str(exc.value)
        assert "legacy" in message
        # Names what happened to the path, so the fix is obvious from the
        # error alone rather than requiring a git archaeology session.
        assert "no longer exists" in message
        assert "12.6" in message

    def test_legacy_override_raise_beats_pin(self, router):
        """An explicit override is still evaluated first - it raises rather
        than falling through to the pin's remote routing."""
        with pytest.raises(ValueError):
            router.decide(
                "script",
                {"command": "x", "runner_type": "any", "executor": "legacy"},
            )

    def test_no_decision_escapes_for_a_legacy_override(self, router):
        """No RoutingDecision escapes for the removed value."""
        for step_type in ("script", "docker", "agent"):
            with pytest.raises(ValueError):
                router.decide(step_type, {"executor": "legacy"})


# -----------------------------------------------------------------------------
# Contract (12.6): runner pins route REMOTE.
#
# 12.4 deleted script/docker execution from the runners, so between 12.4 and
# 12.6 a pin routed LOCAL with a WARNING - the work ran, on the wrong
# machine, and the reason string said so. RemoteExecutor exists now, so the
# pin is HONORED: remote, reason "runner-pin", requirements parsed. A pin
# nobody can satisfy is failed by the dispatcher at NO_RUNNER_TIMEOUT with a
# message naming the requirements, never silently run on the backend host.
# -----------------------------------------------------------------------------

class TestRunnerPins:
    def test_script_with_runner_type_routes_remote(self, router):
        decision = router.decide(
            "script", {"command": "flash firmware.bin", "runner_type": "generic"}
        )
        assert decision.mode == "remote"
        assert decision.reason == "runner-pin"
        # Top-level runner_type is sugar for requires.runner_type.
        assert decision.requirements == {"runner_type": "generic"}

    def test_docker_with_requires_routes_remote(self, router):
        decision = router.decide(
            "docker",
            {
                "command": "run-hw-tests",
                "image": "alpine:latest",
                "requires": {"has": ["gpio", "uart"]},
            },
        )
        assert decision.mode == "remote"
        assert decision.reason == "runner-pin"
        assert decision.requirements == {"has": ["gpio", "uart"]}

    def test_pin_is_not_a_warning_now_that_it_is_honorable(self, router, caplog):
        """The 12.4-12.6 WARNING said the pin was being DROPPED. It is not
        dropped any more, so warning about it would be a lie."""
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            router.decide("script", {"command": "x", "runner_type": "generic"})
        assert not [r for r in caplog.records if r.name == ROUTER_LOGGER]

    def test_interim_reason_string_is_gone(self, router):
        """`pin-not-honorable-local-until-12.6` must not exist anywhere."""
        for step_type in ("script", "docker", "agent"):
            decision = router.decide(
                step_type, {"command": "x", "requires": {"arch": "arm64"}}
            )
            assert decision.reason != "pin-not-honorable-local-until-12.6"
            assert decision.mode == "remote"

    def test_agent_step_with_requires_routes_remote(self, router):
        """`requires:` pins EVERY step type, agent included."""
        decision = router.decide(
            "agent", {"prompt": "fix it", "requires": {"has": ["gpu"]}}
        )
        assert decision.mode == "remote"
        assert decision.reason == "runner-pin"
        assert decision.requirements == {"has": ["gpu"]}

    def test_agent_step_runner_type_alone_still_local(self, router):
        """12.5 meaning preserved: on an agent step `runner_type` names the
        AI flavor, not a hardware pin. Flipping those steps remote would
        silently move every existing agent pipeline onto a runner that may
        not exist."""
        decision = router.decide(
            "agent", {"prompt": "fix it", "runner_type": "claude-code"}
        )
        assert decision.mode == "local"
        assert decision.reason == "agent-default-local"
        assert decision.requirements == {}

    def test_unpinned_script_still_local(self, router):
        decision = router.decide("script", {"command": "pytest"})
        assert decision.mode == "local"
        assert decision.reason == "script-default-local"

    def test_no_script_or_docker_config_can_route_legacy(self, router):
        """Property: the router NEVER hands a script/docker step to the
        legacy queue, whatever the config carries (local, remote, or raise)."""
        configs = [
            {},
            {"command": "pytest"},
            {"runner_type": "generic"},
            {"requires": {"has": ["gpio"]}},
            {"runner_type": "any", "requires": {"gpu": True}},
            {"image": "python:3.12", "env": {"A": "b"}},
        ]
        for step_type in ("script", "docker"):
            for config in configs:
                decision = router.decide(step_type, config)
                assert decision.mode in ("local", "remote"), (step_type, config)


# -----------------------------------------------------------------------------
# Contract: explicit `executor: remote` override (12.6)
# -----------------------------------------------------------------------------

class TestRemoteOverride:
    def test_remote_override_on_script(self, router):
        decision = router.decide(
            "script", {"command": "pytest", "executor": "remote"}
        )
        assert decision.mode == "remote"
        assert decision.reason == "explicit-override"
        # No requires: any connected runner will do.
        assert decision.requirements == {}

    def test_remote_override_carries_requirements(self, router):
        decision = router.decide(
            "agent",
            {"prompt": "x", "executor": "remote", "requires": {"arch": "aarch64"}},
        )
        assert decision.mode == "remote"
        assert decision.reason == "explicit-override"
        assert decision.requirements == {"arch": "arm64"}

    def test_remote_is_a_valid_override_value(self, router):
        from app.services.workspace.execution_router import (
            _VALID_EXECUTOR_OVERRIDES,
        )

        assert "remote" in _VALID_EXECUTOR_OVERRIDES

    def test_invalid_override_names_remote_in_the_message(self, router):
        with pytest.raises(ValueError) as exc:
            router.decide("script", {"executor": "quantum"})
        assert "remote" in str(exc.value)


# -----------------------------------------------------------------------------
# Contract: unknown step types - observable fallback
# -----------------------------------------------------------------------------

class TestUnknownStepTypes:
    """12.6: an unknown step type RAISES.

    Until the deletion commit it fell back to the polling queue with a
    WARNING, which was an honest observable fallback while a fallback
    existed. There is none now, so inventing a route would only move the
    failure further from its cause.
    """

    def test_unknown_type_raises_naming_the_type(self, router):
        with pytest.raises(ValueError) as exc:
            router.decide("teleport", {"command": "beam me up"})
        assert "teleport" in str(exc.value)

    def test_unknown_type_does_not_warn_and_continue(self, router, caplog):
        """It fails - it does not log and route somewhere anyway."""
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            with pytest.raises(ValueError):
                router.decide("teleport", {})
        assert not [r for r in caplog.records if r.name == ROUTER_LOGGER]


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
