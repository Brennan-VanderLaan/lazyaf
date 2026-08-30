"""The pluggable, Docker-agnostic executor seam - Phase 12.6, section 4.2.

HARD CONSTRAINT, pinned by ``tests/test_orchestrator_seam.py``: this module
imports nothing from ``docker``. Imports are abc, asyncio, typing and
``..types`` - nothing else.

Why it matters concretely: the owner target for remote execution is
runpod-style nodes that frequently run AS containers with no Docker socket at
all. Such a pod registers ``capabilities() -> {"orchestrator": "native",
"has": []}`` and simply never matches a step carrying
``requires: {has: [docker]}``, while a step that needs only a shell and a model
endpoint matches it fine. The routing grammar already expresses that; the
protocol never learns what an orchestrator is. Keeping this file import-clean
is what makes the claim checkable instead of aspirational.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from ..types import LogSink, StepAssignment, StepOutcome


class OrchestratorUnavailable(RuntimeError):
    """This host cannot execute steps, with an actionable reason.

    Raised by ``preflight()``. The agent exits non-zero on it rather than
    registering: a runner that advertises itself and then fails every
    assignment is worse than one that never appears.
    """


class StepOrchestrator(ABC):
    """Executes one step on this host, however this host executes things."""

    #: Key this orchestrator is selected by (``LAZYAF_ORCHESTRATOR``) and the
    #: value it reports as the ``orchestrator`` label.
    name: str = ""

    @abstractmethod
    async def preflight(self) -> None:
        """Raise :class:`OrchestratorUnavailable` with an actionable message if
        this host cannot execute steps (no daemon, no socket, no permissions).

        Called once before the first connection attempt.
        """

    @abstractmethod
    def capabilities(self) -> dict:
        """Labels this orchestrator contributes to ``register.labels``.

        e.g. ``{"orchestrator": "docker", "has": ["docker"]}``. Merged with the
        operator's configured labels by the CLI; ``has`` entries are unioned.
        """

    @abstractmethod
    async def run_step(
        self,
        assignment: StepAssignment,
        *,
        on_log: LogSink,
        cancel: asyncio.Event,
    ) -> StepOutcome:
        """Execute one assignment to a terminal outcome.

        ``on_log`` carries RUNNER-ORIGIN lines only, and there is a structural
        rule about WHEN it may be called (section 7.2, "log ordering across a
        network"): only BEFORE the step process starts and AFTER it exits.
        The step container reports its own logs over HTTP to
        ``/api/steps/{id}/logs``; if the two streams could overlap in time the
        merged log would read as though events happened out of order. Pinned by
        ``tests/test_log_ordering.py``.

        ``cancel`` is set by the agent on ``cancel_step`` or ``drain``. The
        implementation must kill the step promptly and return; raising is
        acceptable only for genuinely unexpected faults (the session turns any
        exception into a failed outcome).

        Must not raise on ordinary step failure - a non-zero exit is a
        :class:`~lazyaf_runner.types.StepOutcome`, not an exception.
        """

    @abstractmethod
    async def cleanup_workspace(self, retain_key: str) -> None:
        """Reap the workspace this runner provisioned for ``retain_key``.

        Idempotent, never raises: a cleanup that fails must not take down a
        runner that is otherwise healthy.
        """

    # --- optional hooks ----------------------------------------------------

    async def shutdown(self) -> None:
        """Release host resources. Default: nothing."""
        return None

    def describe(self) -> str:
        return self.name or type(self).__name__


def merge_labels(configured: dict, capabilities: dict) -> dict:
    """Merge operator labels with orchestrator capabilities.

    ``has`` is UNIONED (an operator adding ``has=gpio`` must not erase the
    orchestrator's ``has=docker``); every other key from ``configured`` wins,
    because the operator knows their host and the orchestrator only knows
    itself. Kept here rather than in the CLI so an out-of-tree orchestrator
    gets the same merge semantics for free.
    """
    merged: dict = dict(capabilities or {})
    merged.update(configured or {})

    def _as_list(value) -> list:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    has = _as_list((capabilities or {}).get("has")) + _as_list((configured or {}).get("has"))
    if has:
        seen: dict = {}
        for item in has:
            seen[str(item)] = None
        merged["has"] = list(seen)
    return merged


__all__ = [
    "LogSink",
    "OrchestratorUnavailable",
    "StepAssignment",
    "StepOrchestrator",
    "StepOutcome",
    "merge_labels",
]
