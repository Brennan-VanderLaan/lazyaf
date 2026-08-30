"""DB-backed runner registry - Phase 12.6.

Replaces the 12.5 in-memory runner pool (deleted in 12.6), which kept
everything in process memory: nothing
survived a restart, nothing was queryable, and the `runners` table sat dead
beside it.

The shape here is deliberate and it is the fix for failure_01's central
defect ("the DB status never leaves idle"):

    The RunnerStateMachine is the in-memory AUTHORITY for a live connection.
    The DB row is its DURABLE PROJECTION.

One machine per live connection, owned by this registry, and **every**
transition goes through `transition()` - machine first, DB second, UI
broadcast third. An illegal move raises out of the machine before anything
is written, so the projection cannot record a state the machine never
entered. That is how `runner_state.py` becomes load-bearing rather than
decorative.

SINGLE-WORKER LIMIT (stated, not hidden): `_connections` is per-process. A
multi-worker uvicorn would route assignments to a worker that holds no
socket. LazyAF runs single-worker today; `main.py` warns on
WEB_CONCURRENCY > 1. The seam for a fan-out (Redis pubsub, or an in-cluster
router) is exactly one method - `send()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Iterable
from uuid import uuid4

from sqlalchemy import select, update

from app.models.runner import DEFAULT_RUNNER_TYPE, Runner
from app.schemas._datetime import utc_isoformat
from app.services.execution.runner_protocol import (
    BackendMessage,
    DrainMessage,
    RegisterMessage,
    normalize_labels,
)
from app.services.execution.runner_state import (
    InvalidRunnerTransitionError,
    RunnerState,
    RunnerStateMachine,
)

logger = logging.getLogger(__name__)


class DuplicateRunnerConnection(Exception):
    """A second live connection claimed a runner_id the registry already holds.

    The INCUMBENT wins and is left completely untouched; the endpoint closes
    the impostor with 4004. A shared enrollment secret does not bind an
    identity, so "first connection wins" is what stops a second process
    (honest restart or not) from silently stealing an in-flight assignment.
    """

    def __init__(self, runner_id: str):
        self.runner_id = runner_id
        super().__init__(f"runner {runner_id} already has a live connection")


class RunnerRegistry:
    """Live runner connections and their durable projections."""

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}
        self._machines: dict[str, RunnerStateMachine] = {}
        self._websocket_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: Set by the dispatcher at startup. A callback rather than an import
        #: so the registry never depends on the dispatcher (and the dormant
        #: contract tests can drive the registry with no dispatcher at all).
        self._dispatch_waker: Callable[[], None] | None = None

    # -- wiring ---------------------------------------------------------------

    def set_dispatch_waker(self, waker: Callable[[], None] | None) -> None:
        """Register the dispatcher's wake hook (an `asyncio.Event.set`)."""
        self._dispatch_waker = waker

    def _wake_dispatcher(self) -> None:
        if self._dispatch_waker is None:
            return
        try:
            self._dispatch_waker()
        except Exception:  # pragma: no cover - a waker must never break a transition
            logger.exception("dispatcher wake hook raised; ignoring")

    def _lock(self, runner_id: str) -> asyncio.Lock:
        lock = self._locks.get(runner_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[runner_id] = lock
        return lock

    # -- lifecycle ------------------------------------------------------------

    async def connect(self, db, websocket: Any, register: RegisterMessage) -> Runner:
        """Accept a registered runner and bring its row up to date.

        Raises DuplicateRunnerConnection when this process already holds a
        socket for the same runner_id.

        `current_step_execution_id` is deliberately NOT cleared: the caller
        hands the row to JobRecoveryService.on_runner_reconnect immediately
        afterwards, and that decision (idle / continue / abort) needs to see
        the step this runner still believes it holds.
        """
        runner_id = register.runner_id or str(uuid4())

        async with self._lock(runner_id):
            if runner_id in self._connections:
                raise DuplicateRunnerConnection(runner_id)

            now = datetime.utcnow()
            websocket_id = str(uuid4())

            result = await db.execute(select(Runner).where(Runner.id == runner_id))
            runner = result.scalar_one_or_none()
            if runner is None:
                runner = Runner(id=runner_id, created_at=now)
                db.add(runner)

            runner.name = register.name or runner.name or runner_id
            runner.runner_type = register.runner_type or DEFAULT_RUNNER_TYPE
            runner.set_labels(normalize_labels(register.labels))
            runner.protocol_version = register.protocol_version
            runner.agent_version = register.agent_version or None
            runner.connected_at = now
            runner.last_heartbeat = now
            runner.websocket_id = websocket_id
            # Re-anchor the projection to the machine's true initial state
            # BEFORE any transition runs. A row left "busy" by a crashed
            # process is a lie about a socket that no longer exists; the
            # machine below starts at DISCONNECTED and the row must agree
            # with it or the very first transition is illegal.
            runner.status = RunnerState.DISCONNECTED.value
            await db.commit()
            await db.refresh(runner)

            # NEVER construct the machine directly at IDLE (failure_01 did,
            # so the machine's own history was a lie and reconnect could
            # never observe in-flight work). Walk it.
            machine = RunnerStateMachine(runner_id=runner_id)
            self._connections[runner_id] = websocket
            self._machines[runner_id] = machine
            self._websocket_ids[runner_id] = websocket_id

            await self._apply_transition(
                db, runner, machine, RunnerState.CONNECTING, reason="register"
            )
            await self._apply_transition(
                db, runner, machine, RunnerState.IDLE, reason="registered"
            )

        logger.info(
            "runner %s connected (type=%s, protocol=%s, agent=%s, ws=%s)",
            runner_id,
            runner.runner_type,
            register.protocol_version,
            register.agent_version or "?",
            websocket_id,
        )
        self._wake_dispatcher()
        return runner

    async def disconnect(self, db, runner_id: str, websocket_id: str | None = None) -> None:
        """Tear down a connection. Never raises.

        This runs from the endpoint's `finally:`. failure_01 threw
        InvalidRunnerTransitionError out of exactly this path (DEAD ->
        DISCONNECTED was not a legal move for it), which skipped BOTH the DB
        update and the requeue - the single most expensive bug in that
        implementation. `runner_state.py` now allows DEAD -> DISCONNECTED,
        and this method additionally refuses to propagate anything.

        A stale websocket_id (a superseded socket closing after its
        replacement registered) is ignored: the incumbent connection is not
        torn down by the ghost's close.
        """
        async with self._lock(runner_id):
            held = self._websocket_ids.get(runner_id)
            if websocket_id is not None and held is not None and held != websocket_id:
                logger.warning(
                    "ignoring disconnect for runner %s from superseded socket %s "
                    "(current %s)",
                    runner_id,
                    websocket_id,
                    held,
                )
                return

            machine = self._machines.pop(runner_id, None)
            self._connections.pop(runner_id, None)
            self._websocket_ids.pop(runner_id, None)

            try:
                result = await db.execute(select(Runner).where(Runner.id == runner_id))
                runner = result.scalar_one_or_none()
                if runner is None:
                    return
                # Cleared BEFORE the transition so the disconnect produces
                # exactly ONE runner_status frame carrying the final truth,
                # not a status frame followed by a fence-clearing frame.
                runner.websocket_id = None
                if machine is not None and machine.state is not RunnerState.DISCONNECTED:
                    await self._apply_transition(
                        db,
                        runner,
                        machine,
                        RunnerState.DISCONNECTED,
                        reason="socket closed",
                    )
                else:
                    runner.status = RunnerState.DISCONNECTED.value
                    await db.commit()
                    await self._broadcast(runner)
            except Exception:
                logger.exception(
                    "runner %s disconnect cleanup failed; connection state was "
                    "already dropped",
                    runner_id,
                )

    async def transition(
        self, db, runner_id: str, to_state: RunnerState, *, reason: str = ""
    ) -> None:
        """Move a runner to a new state: machine -> DB -> UI, in that order.

        Raises InvalidRunnerTransitionError on an illegal move, having
        written NOTHING. A protocol error must be LOUD; a silent DB write
        that the machine never sanctioned is how the two diverge.
        """
        async with self._lock(runner_id):
            machine = self._machines.get(runner_id)
            result = await db.execute(select(Runner).where(Runner.id == runner_id))
            runner = result.scalar_one_or_none()
            if runner is None:
                raise KeyError(f"unknown runner {runner_id}")
            if machine is None:
                # No live connection: the only legal destination is the
                # terminal bookkeeping the death monitor / recovery perform.
                machine = RunnerStateMachine(
                    runner_id=runner_id, initial_state=RunnerState(runner.status)
                )
            await self._apply_transition(db, runner, machine, to_state, reason=reason)

        if to_state is RunnerState.IDLE:
            self._wake_dispatcher()

    async def _apply_transition(
        self,
        db,
        runner: Runner,
        machine: RunnerStateMachine,
        to_state: RunnerState,
        *,
        reason: str = "",
    ) -> None:
        # 1. Machine first. Raises before anything durable happens.
        machine.transition_to(to_state, reason=reason or None)
        # 2. Durable projection.
        runner.status = to_state.value
        if to_state in (RunnerState.IDLE, RunnerState.BUSY):
            runner.last_heartbeat = datetime.utcnow()
        await db.commit()
        await db.refresh(runner)
        # 3. UI. Exactly one runner_status frame per transition.
        await self._broadcast(runner)

    async def heartbeat(self, db, runner_id: str) -> None:
        """Record a heartbeat. The timestamp is stamped HERE, backend-side.

        No timestamp from a runner is ever compared to a backend deadline: a
        clock hours off would otherwise make a dead runner immortal.
        """
        machine = self._machines.get(runner_id)
        if machine is not None:
            machine.update_heartbeat()
        await db.execute(
            update(Runner)
            .where(Runner.id == runner_id)
            .values(last_heartbeat=datetime.utcnow())
        )
        await db.commit()

    # -- messaging ------------------------------------------------------------

    async def send(self, runner_id: str, message: BackendMessage) -> bool:
        """Send one backend frame to a runner. False when it did not land.

        THE fan-out seam: a multi-process deployment replaces this method and
        nothing else.
        """
        websocket = self._connections.get(runner_id)
        if websocket is None:
            logger.warning(
                "cannot send %s to runner %s: no live connection",
                message.type,
                runner_id,
            )
            return False
        try:
            await websocket.send_text(json.dumps(message.to_dict()))
            return True
        except Exception:
            logger.warning(
                "send %s to runner %s failed; dropping the connection",
                message.type,
                runner_id,
                exc_info=True,
            )
            self._connections.pop(runner_id, None)
            return False

    async def drain(self, reason: str = "backend shutting down") -> None:
        """Tell every connected runner to finish up and go away.

        Never raises: shutdown must not be blocked by a socket that is
        already gone.
        """
        for runner_id in list(self._connections):
            await self.send(runner_id, DrainMessage(reason=reason))

    # -- queries --------------------------------------------------------------

    async def find_available(self, db, requirements: dict | None = None) -> list[Runner]:
        """Every idle, connected runner that satisfies `requirements`.

        Returns ALL matches, never one pre-selected winner: the dispatcher
        picks and then does the compare-and-swap. failure_01's
        `find_idle_runner` returned a single runner and was a TOCTOU by
        construction - two dispatch passes could hand the same runner two
        steps between the read and the write.
        """
        result = await db.execute(
            select(Runner)
            .where(
                Runner.status == RunnerState.IDLE.value,
                Runner.current_step_execution_id.is_(None),
            )
            .order_by(Runner.id)
        )
        rows = list(result.scalars().all())
        return [
            runner
            for runner in rows
            # A row can say "idle" while THIS process holds no socket for it
            # (another worker, or a crash before bootstrap). Only a runner we
            # can actually send to is available.
            if runner.id in self._connections and runner.matches_requirements(requirements)
        ]

    def is_connected(self, runner_id: str) -> bool:
        """Does this process hold a live socket for the runner?"""
        return runner_id in self._connections

    def websocket_id(self, runner_id: str) -> str | None:
        """The current connection's fence token (the step gate reads this)."""
        return self._websocket_ids.get(runner_id)

    def machine(self, runner_id: str) -> RunnerStateMachine | None:
        return self._machines.get(runner_id)

    def machines(self) -> Iterable[tuple[str, RunnerStateMachine]]:
        """Every live machine - IDLE included.

        The death monitor iterates ALL of them. That is exactly why
        `runner_state.py` carries IDLE -> DEAD: a runner that connects and
        then silently vanishes must not sit `idle` forever collecting
        assignments. failure_01 death-checked only ASSIGNED/BUSY.
        """
        return list(self._machines.items())

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def snapshot(self, db) -> list[dict]:
        """The API/UI read model: every known runner row, newest state first.

        `connection` is stamped from `_connections`, not from the row: an
        "idle" status left behind by a crashed process is indistinguishable
        from a live one in the DB alone, and a vacuous "runners exist" pass
        is exactly what gate assertion 9 must not accept.
        """
        result = await db.execute(select(Runner).order_by(Runner.id))
        return [self._as_dict(runner) for runner in result.scalars().all()]

    def _as_dict(self, runner: Runner) -> dict:
        """The runner wire shape, for both GET /api/runners and the WS frame.

        ``utc_isoformat``, not ``.isoformat()``: this dict IS the response body
        (the router declares ``response_model=list[dict]``, so the annotated
        ``RunnerRead`` never runs), and the runner panel renders a live
        connection age off ``connected_at``. A naive string there is read as
        local time and the age comes out hours wrong or negative.
        """
        machine = self._machines.get(runner.id)
        return {
            "id": runner.id,
            "name": runner.name,
            "runner_type": runner.runner_type,
            "status": runner.status,
            "labels": runner.get_labels(),
            "current_step_execution_id": runner.current_step_execution_id,
            "protocol_version": runner.protocol_version,
            "agent_version": runner.agent_version,
            "connected_at": utc_isoformat(runner.connected_at),
            "last_heartbeat": utc_isoformat(runner.last_heartbeat),
            "created_at": utc_isoformat(runner.created_at),
            "connection": "websocket" if runner.id in self._connections else "none",
            "current_step_id": machine.current_step_id if machine else None,
        }

    async def _broadcast(self, runner: Runner) -> None:
        """One `runner_status` frame. Never raises into a transition."""
        from app.services.websocket import manager

        try:
            await manager.send_runner_status(self._as_dict(runner))
        except Exception:  # pragma: no cover - the UI must not break state
            logger.exception("runner_status broadcast failed for %s", runner.id)

    # -- startup / shutdown / tests ------------------------------------------

    async def bootstrap(self, db) -> int:
        """Startup: no connection survives a restart, so say so.

        Every row is forced to `disconnected` with `websocket_id = NULL`
        BEFORE any socket can connect. Pretending a connection survived is
        how a fresh backend hands work to a ghost.

        `current_step_execution_id` is left alone here - JobRecoveryService's
        orphan sweep owns requeueing those steps, and clearing the pointer
        first would hide them from it.
        """
        result = await db.execute(
            update(Runner)
            .where(Runner.status != RunnerState.DISCONNECTED.value)
            .values(status=RunnerState.DISCONNECTED.value, websocket_id=None)
        )
        await db.execute(update(Runner).values(websocket_id=None))
        await db.commit()
        count = result.rowcount or 0
        if count:
            logger.info("runner registry bootstrap: %d row(s) marked disconnected", count)
        self._connections.clear()
        self._machines.clear()
        self._websocket_ids.clear()
        return count

    async def reset(self) -> None:
        """Drop all in-memory connection state (R6 test-mode hook).

        The reset endpoint must reset in-memory singletons or the next test
        inherits a socket that no longer exists.
        """
        connections = list(self._connections.values())
        self._connections.clear()
        self._machines.clear()
        self._websocket_ids.clear()
        self._locks.clear()
        for websocket in connections:
            try:
                await websocket.close()
            except Exception:
                pass


#: Process-wide singleton. One registry per backend process, by design.
runner_registry = RunnerRegistry()


def get_runner_registry() -> RunnerRegistry:
    return runner_registry


__all__ = [
    "DuplicateRunnerConnection",
    "InvalidRunnerTransitionError",
    "RunnerRegistry",
    "runner_registry",
    "get_runner_registry",
]
