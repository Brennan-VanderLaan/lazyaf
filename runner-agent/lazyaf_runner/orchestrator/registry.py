"""Orchestrator lookup - Phase 12.6, section 4.1.

One dict. Adding an out-of-tree orchestrator is one entry plus a class that
satisfies ``StepOrchestrator``; nothing in ``client.py``, ``session.py`` or the
wire protocol learns that it exists.
"""
from __future__ import annotations

from ..config import RunnerConfig
from .base import OrchestratorUnavailable, StepOrchestrator
from .docker_orch import DockerOrchestrator

#: name -> class. ``NativeOrchestrator`` is deliberately absent in 12.6
#: (section 10): the ABC, this registry, and the capability-driven matching in
#: the backend's requirement grammar are the guarantee that it can be added
#: without touching the protocol.
ORCHESTRATORS: dict[str, type[StepOrchestrator]] = {
    "docker": DockerOrchestrator,
}


def build_orchestrator(config: RunnerConfig, **kwargs) -> StepOrchestrator:
    """Instantiate the orchestrator named by ``config.orchestrator``."""
    try:
        cls = ORCHESTRATORS[config.orchestrator]
    except KeyError:
        raise OrchestratorUnavailable(
            f"unknown orchestrator {config.orchestrator!r}; "
            f"available: {', '.join(sorted(ORCHESTRATORS))}"
        ) from None
    return cls(config, **kwargs)


__all__ = ["ORCHESTRATORS", "build_orchestrator"]
