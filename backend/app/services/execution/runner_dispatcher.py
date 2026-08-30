"""Runner dispatcher - Phase 12.6.

The single arbiter that hands pending remote steps to idle runners, and the
rendezvous where the WebSocket endpoint's inbound frames meet the
`RemoteExecutor` generator that is waiting for them.

**This module is the fix for failure_01's fatal structural gap.** That
implementation requeued steps to `pending` on runner death and then had
nothing that ever looked at `pending` again: every requeue silently stranded
the pipeline. There was no dispatcher at all.

Three properties, in the order they matter:

1. **Event-driven, with a self-healing tick.** The loop wakes on an
   `asyncio.Event` set by (a) a RemoteExecutor that needs a runner, (b) the
   registry when a runner reaches IDLE, (c) JobRecovery after any requeue,
   and (d) a `DISPATCH_SWEEP_INTERVAL` safety tick so a *missed* wake costs
   15 seconds instead of stranding a pipeline forever. Polling was never the
   problem; polling as the ONLY mechanism was.

2. **Assignment is a database compare-and-swap, and the DB is the arbiter.**
   `UPDATE ... WHERE status='pending' AND runner_id IS NULL` with
   `rowcount != 1` meaning "someone else won" is the only acceptable
   double-assign detection (cross-agent contract #8). An in-process
   `asyncio.Lock` keeps one backend from racing itself; the CAS is what makes
   it correct across restarts and, later, across processes. failure_01's
   `find_idle_runner` returned a single pre-selected runner and was a TOCTOU
   by construction.

3. **Waiters, not orphans.** The loop only assigns steps that a live
   `RemoteExecutor` generator is currently waiting for. A `pending` row whose
   generator died with a backend restart is NOT re-dispatched here - handing
   it to a runner would execute work whose result nobody consumes. Those rows
   are logged as strays and reaped by `JobRecoveryService.recover_orphaned_steps`
   plus the run-level orphan sweep. The DB query still drives the ORDER
   (oldest `created_at` first), which is exactly why `runner_requirements` is
   a durable column rather than a dispatch-closure variable.

Who transitions what (stated once, because a split ownership here is how
split-brain gets in):

    dispatcher   IDLE -> ASSIGNED     (the CAS, then registry.transition)
    executor     ASSIGNED -> BUSY     (on ACK)
    executor     BUSY -> IDLE         (on terminal, via release_runner)
    recovery     * -> DEAD/DISCONNECTED and the requeue
    ws endpoint  nothing - it gates the frame and calls notify_*

SINGLE-WORKER LIMIT: the waiter table and the assignment futures are
per-process, exactly like `RunnerRegistry._connections`. Same stated limit,
same seam.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update

from app.models.pipeline import StepExecution, StepExecutionStatus
from app.models.runner import Runner
from app.services.execution.runner_protocol import (
    DISPATCH_SWEEP_INTERVAL,
    NO_RUNNER_TIMEOUT,
)
from app.services.execution.runner_state import (
    InvalidRunnerTransitionError,
    RunnerState,
)

logger = logging.getLogger(__name__)


class NoRunnerAvailable(Exception):
    """No connected runner satisfied a step's `requires:` inside the budget.

    The message names BOTH the requirements and the labels of every connected
    runner, because the overwhelmingly likely cause is a typo and the only
    thing an operator can act on is the difference between the two. A
    `requires:` nobody can satisfy must fail loudly at
    `NO_RUNNER_TIMEOUT`; hanging forever is indistinguishable from a hung
    backend, which is how failure_01's pipelines "just stopped".
    """

    def __init__(self, requirements: dict, connected: list[dict]):
        self.requirements = dict(requirements or {})
        self.connected = list(connected)
        if connected:
            fleet = "; ".join(
                f"{r.get('id')}(type={r.get('runner_type')}, "
                f"labels={json.dumps(r.get('labels') or {}, sort_keys=True)})"
                for r in connected
            )
        else:
            fleet = "none"
        super().__init__(
            f"no runner matched {json.dumps(self.requirements, sort_keys=True)} "
            f"within {NO_RUNNER_TIMEOUT}s; connected runners: {fleet}"
        )


@dataclass
class AssignmentOutcome:
    """How one assignment of one step to one runner ended.

    `requeued` is NOT a terminal outcome for the step - it means the runner
    went away and the step went back to `pending`, so the executor should
    re-dispatch rather than yield a result. That distinction is what lets
    `pipeline_executor` stay completely ignorant of remoteness: a mid-step
    runner death produces no intermediate `result` event, exactly as a local
    container restart would produce none.
    """

    exit_code: int | None = None
    error: str | None = None
    requeued: bool = False
    cancelled: bool = False
    reason: str = ""


@dataclass
class Assignment:
    """The rendezvous between the WS endpoint and one waiting executor.

    Keyed by `(step_id, runner_id)`, never `step_id` alone: failure_01 keyed
    its ACK future on the step id, so a re-assignment of the same step after
    an ACK timeout collided with the future of the assignment that timed out.
    """

    step_id: str
    runner_id: str
    ack: asyncio.Future = field(repr=False)
    terminal: asyncio.Future = field(repr=False)


@dataclass
class _Waiter:
    """A RemoteExecutor generator currently asking for a runner."""

    step_id: str
    requirements: dict
    future: asyncio.Future = field(repr=False)
    wake: asyncio.Event = field(repr=False)
    deadline: float = 0.0
    sequence: int = 0


class RunnerDispatcher:
    """Assigns pending remote steps to idle runners; owns the ACK/terminal futures."""

    def __init__(
        self,
        registry=None,
        recovery=None,
        session_factory=None,
    ) -> None:
        self._registry = registry
        self._recovery = recovery
        self._session_factory = session_factory

        self._event = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._waiters: dict[str, _Waiter] = {}
        self._assignments: dict[tuple[str, str], Assignment] = {}
        self._task: asyncio.Task | None = None
        self._sequence = 0
        self._stray_logged: set[str] = set()

    # -- collaborators (late-bound so tests can build one with no app) --------

    @property
    def registry(self):
        if self._registry is None:
            from app.services.execution.runner_registry import runner_registry

            self._registry = runner_registry
        return self._registry

    @property
    def recovery(self):
        if self._recovery is None:
            from app.services.execution.job_recovery import get_job_recovery_service

            self._recovery = get_job_recovery_service()
        return self._recovery

    def _session(self):
        """A session for one dispatch pass.

        The fallback is resolved on EVERY call rather than cached, so a
        harness that rebinds `app.database.async_session` is honored rather
        than pinned to whatever was installed on the first pass.
        """
        if self._session_factory is not None:
            return self._session_factory()
        from app.database import async_session

        return async_session()

    # -- lifecycle ------------------------------------------------------------

    def install_hooks(self) -> None:
        """Wire the registry and recovery service to this dispatcher.

        Callbacks, not imports: the registry and the recovery service must
        both stay importable (and testable) with no dispatcher in the
        process at all.
        """
        self.registry.set_dispatch_waker(lambda: self.wake("runner idle"))
        self.recovery.set_requeue_hook(self.on_step_requeued)

    async def start(self, session_factory=None) -> None:
        """Start the dispatch loop. Idempotent."""
        if session_factory is not None:
            self._session_factory = session_factory
        self.install_hooks()
        self._ensure_running()

    def _ensure_running(self) -> None:
        """Start the loop lazily.

        `acquire()` calls this so a dispatcher used directly (a unit test, a
        script) behaves identically to one started from the lifespan. A
        waiter that silently never gets serviced is the exact class of bug
        this phase exists to remove.
        """
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop: nothing to start
            return
        self._task = loop.create_task(self._loop(), name="runner-dispatcher")

    async def stop(self) -> None:
        """Cancel the loop. Never raises."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("dispatcher loop raised on shutdown")

    async def reset(self) -> None:
        """Test-mode reset hook (R6): drop waiters and assignments."""
        await self.stop()
        for waiter in list(self._waiters.values()):
            if not waiter.future.done():
                waiter.future.cancel()
        self._waiters.clear()
        for assignment in list(self._assignments.values()):
            for fut in (assignment.ack, assignment.terminal):
                if not fut.done():
                    fut.cancel()
        self._assignments.clear()
        self._stray_logged.clear()
        # REBUILD, never merely clear. An asyncio.Event binds itself to the
        # loop it is first awaited on, so a cleared-but-kept Event makes the
        # NEXT loop's `_wait_tick` raise "bound to a different event loop" and
        # kill the dispatcher task on its first tick. That is invisible in
        # production (one process, one loop) and fatal everywhere a reset is
        # followed by fresh work: the R6 test-mode reset endpoint, and every
        # test after the first in a module.
        self._event = asyncio.Event()

    # -- waking ---------------------------------------------------------------

    def wake(self, reason: str = "") -> None:
        """Poke the loop. Sync, so it is safe from any callback."""
        logger.debug("dispatcher wake: %s", reason or "unspecified")
        self._event.set()
        for waiter in list(self._waiters.values()):
            waiter.wake.set()

    # -- the executor-facing API ---------------------------------------------

    async def acquire(
        self,
        step_id: str,
        requirements: dict | None = None,
        *,
        timeout: float = NO_RUNNER_TIMEOUT,
    ) -> Runner:
        """Block until a runner is assigned to `step_id`, or fail loudly.

        Raises NoRunnerAvailable after `timeout` seconds with a message
        naming the requirements and the whole connected fleet.
        """
        loop = asyncio.get_running_loop()
        self._sequence += 1
        waiter = _Waiter(
            step_id=step_id,
            requirements=dict(requirements or {}),
            future=loop.create_future(),
            wake=asyncio.Event(),
            deadline=loop.time() + timeout,
            sequence=self._sequence,
        )
        self._waiters[step_id] = waiter
        self._ensure_running()
        self.wake(f"step {step_id} waiting for a runner")
        try:
            return await waiter.future
        finally:
            if self._waiters.get(step_id) is waiter:
                del self._waiters[step_id]

    def register_assignment(self, step_id: str, runner_id: str) -> Assignment:
        """Open the ACK/terminal rendezvous for one assignment."""
        loop = asyncio.get_running_loop()
        assignment = Assignment(
            step_id=step_id,
            runner_id=runner_id,
            ack=loop.create_future(),
            terminal=loop.create_future(),
        )
        self._assignments[(step_id, runner_id)] = assignment
        return assignment

    def release_assignment(self, step_id: str, runner_id: str) -> None:
        """Close the rendezvous. Late frames for it become inert facts."""
        self._assignments.pop((step_id, runner_id), None)

    def assignment(self, step_id: str, runner_id: str) -> Assignment | None:
        return self._assignments.get((step_id, runner_id))

    # -- the WS-endpoint-facing API ------------------------------------------
    #
    # The endpoint applies the step gate FIRST (cross-agent contract #7:
    # step.runner_id == connection.runner_id AND runner.websocket_id ==
    # connection.websocket_id) and then calls one of these. The
    # (step_id, runner_id) key is a second, cheaper layer of the same fence:
    # a frame for an assignment this process is not waiting on returns False
    # and changes nothing.

    def notify_ack(self, step_id: str, runner_id: str) -> bool:
        """The runner accepted the assignment. True if anyone was waiting."""
        assignment = self._assignments.get((step_id, runner_id))
        if assignment is None or assignment.ack.done():
            logger.warning(
                "dropping ack for step %s from runner %s: no open assignment",
                step_id,
                runner_id,
            )
            return False
        assignment.ack.set_result(True)
        return True

    def notify_complete(
        self,
        step_id: str,
        runner_id: str,
        exit_code: int,
        error: str | None = None,
    ) -> bool:
        """The runner reported a terminal outcome. True if anyone was waiting."""
        assignment = self._assignments.get((step_id, runner_id))
        if assignment is None or assignment.terminal.done():
            logger.warning(
                "dropping step_complete for step %s from runner %s "
                "(exit_code=%s): no open assignment",
                step_id,
                runner_id,
                exit_code,
            )
            return False
        if not assignment.ack.done():
            # A runner that completes without ever ACKing still did the work.
            # Unblocking the ACK wait here is strictly better than letting the
            # ACK timeout reassign a step that is already finished.
            assignment.ack.set_result(True)
        assignment.terminal.set_result(
            AssignmentOutcome(exit_code=exit_code, error=error)
        )
        return True

    def on_step_requeued(
        self, step_id: str, runner_id: str | None, reason: str = ""
    ) -> None:
        """JobRecoveryService's hook: a step went back to `pending`.

        Resolves the owning assignment as `requeued` (so the executor
        re-dispatches instead of yielding a result) and wakes the loop.
        """
        if runner_id is not None:
            assignment = self._assignments.get((step_id, runner_id))
            self._resolve_requeued(assignment, reason)
        else:
            for key, assignment in list(self._assignments.items()):
                if key[0] == step_id:
                    self._resolve_requeued(assignment, reason)
        self.wake(f"step {step_id} requeued ({reason})")

    def _resolve_requeued(self, assignment: Assignment | None, reason: str) -> None:
        if assignment is None:
            return
        if not assignment.ack.done():
            assignment.ack.set_result(False)
        if not assignment.terminal.done():
            assignment.terminal.set_result(
                AssignmentOutcome(requeued=True, reason=reason)
            )

    # -- assignment (the CAS) -------------------------------------------------

    async def claim(
        self, db, step_id: str, requirements: dict | None = None
    ) -> Runner | None:
        """One compare-and-swap attempt. Returns the winning Runner, or None.

        None means either "no connected runner matches" or "another claimer
        took this step / this runner first" - both are ordinary, and neither
        writes anything.
        """
        async with self._claim_lock:
            candidates = await self.registry.find_available(db, requirements)
            for runner in candidates:
                if not await self._compare_and_swap(db, step_id, runner.id):
                    continue
                try:
                    await self.registry.transition(
                        db,
                        runner.id,
                        RunnerState.ASSIGNED,
                        reason=f"assigned step {step_id}",
                    )
                except (InvalidRunnerTransitionError, KeyError) as exc:
                    # The durable CAS won but the in-memory machine refuses:
                    # the connection changed underneath us. Undo the CAS
                    # rather than leave a step assigned to a runner that will
                    # never be told about it.
                    logger.error(
                        "assignment of step %s to runner %s committed but the "
                        "state machine rejected it (%s); rolling the CAS back",
                        step_id,
                        runner.id,
                        exc,
                    )
                    await self._undo_assignment(db, step_id, runner.id)
                    continue
                await db.refresh(runner)
                logger.info(
                    "assigned step %s to runner %s (requirements=%s)",
                    step_id,
                    runner.id,
                    json.dumps(dict(requirements or {}), sort_keys=True),
                )
                return runner
        return None

    async def _compare_and_swap(self, db, step_id: str, runner_id: str) -> bool:
        """The two guarded UPDATEs, in one transaction. rowcount != 1 loses.

        Cross-agent contract #8. Both predicates matter: the step must still
        be PENDING with no runner, and the runner must still be IDLE with no
        step. Either half failing means someone else got there first, and the
        correct response is to roll back and move on - never to "fix" the row.
        """
        now = datetime.utcnow()
        step_res = await db.execute(
            update(StepExecution)
            .where(
                StepExecution.id == step_id,
                StepExecution.status == StepExecutionStatus.PENDING.value,
                StepExecution.runner_id.is_(None),
            )
            .values(
                status=StepExecutionStatus.ASSIGNED.value,
                runner_id=runner_id,
                assigned_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if step_res.rowcount != 1:
            await db.rollback()
            logger.debug(
                "CAS lost the step: %s is no longer pending/unassigned", step_id
            )
            return False

        runner_res = await db.execute(
            update(Runner)
            .where(
                Runner.id == runner_id,
                Runner.status == RunnerState.IDLE.value,
                Runner.current_step_execution_id.is_(None),
            )
            .values(
                status=RunnerState.ASSIGNED.value,
                current_step_execution_id=step_id,
            )
            .execution_options(synchronize_session=False)
        )
        if runner_res.rowcount != 1:
            await db.rollback()
            logger.debug(
                "CAS lost the runner: %s is no longer idle/free; step %s stays pending",
                runner_id,
                step_id,
            )
            return False

        await db.commit()
        return True

    async def _undo_assignment(self, db, step_id: str, runner_id: str) -> None:
        """Reverse a committed CAS. Only used when the machine rejects it."""
        await db.execute(
            update(StepExecution)
            .where(StepExecution.id == step_id, StepExecution.runner_id == runner_id)
            .values(
                status=StepExecutionStatus.PENDING.value,
                runner_id=None,
                assigned_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(Runner)
            .where(Runner.id == runner_id, Runner.current_step_execution_id == step_id)
            .values(current_step_execution_id=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    async def release_runner(
        self, db, runner_id: str, step_id: str, *, reason: str = ""
    ) -> None:
        """Close an assignment out: clear the pointer, walk the runner to IDLE.

        Called by the executor once the step is terminal, BEFORE it yields
        its `result` event - that ordering is the remote half of
        LocalExecutor's "the container is gone before the result" invariant.

        Never raises: a runner that disconnected during its own last step is
        the common case, and a failed transition must not turn a completed
        step into a crashed one.
        """
        try:
            await db.execute(
                update(Runner)
                .where(
                    Runner.id == runner_id,
                    Runner.current_step_execution_id == step_id,
                )
                .values(current_step_execution_id=None)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        except Exception:
            logger.exception(
                "failed to clear step pointer for runner %s (step %s)",
                runner_id,
                step_id,
            )

        try:
            await self.registry.transition(
                db, runner_id, RunnerState.IDLE, reason=reason or "step complete"
            )
        except (InvalidRunnerTransitionError, KeyError) as exc:
            # DISCONNECTED/DEAD -> IDLE is illegal by design: a runner that
            # died as it finished stays dead until it reconnects.
            logger.info(
                "runner %s not returned to idle after step %s: %s",
                runner_id,
                step_id,
                exc,
            )
        except Exception:
            logger.exception(
                "unexpected failure returning runner %s to idle", runner_id
            )

    # -- the loop -------------------------------------------------------------

    async def _loop(self) -> None:
        logger.info(
            "runner dispatcher started (self-heal tick %ss)", DISPATCH_SWEEP_INTERVAL
        )
        try:
            while True:
                await self._wait_tick()
                try:
                    await self._dispatch_pass()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A dispatch pass that raises must not kill the loop: the
                    # next tick retries. A dead dispatcher strands every
                    # remote pipeline, which is exactly the failure this
                    # module exists to prevent.
                    logger.exception("dispatch pass failed; retrying on the next tick")
        except asyncio.CancelledError:
            logger.info("runner dispatcher stopped")
            raise

    async def _wait_tick(self) -> None:
        """Wait for a wake, a waiter deadline, or the self-heal tick.

        The floor of 10ms is not cosmetic: a waiter whose future is already
        resolved stays in `_waiters` until its own coroutine resumes and
        clears it, and an unfloored `max(0.0, ...)` would spin the loop at
        full speed through that window. Done waiters are excluded from the
        deadline scan for the same reason.
        """
        timeout = float(DISPATCH_SWEEP_INTERVAL)
        loop = asyncio.get_running_loop()
        deadlines = [
            w.deadline for w in self._waiters.values() if not w.future.done()
        ]
        if deadlines:
            timeout = max(0.01, min(timeout, min(deadlines) - loop.time()))
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()

    async def _dispatch_pass(self) -> None:
        """One ordered attempt to satisfy every waiting step."""
        if not self._waiters:
            return
        async with self._session() as db:
            for step_id in await self._dispatch_order(db):
                waiter = self._waiters.get(step_id)
                if waiter is None or waiter.future.done():
                    continue
                try:
                    runner = await self.claim(db, step_id, waiter.requirements)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One unclaimable step must not stall the queue behind
                    # it, and must not skip the expiry sweep below - a
                    # dispatcher that stops expiring waiters turns every
                    # failed claim into the "hangs forever" failure mode
                    # NO_RUNNER_TIMEOUT exists to eliminate.
                    logger.exception("claim of step %s failed; skipping it", step_id)
                    await db.rollback()
                    continue
                if runner is not None and not waiter.future.done():
                    waiter.future.set_result(runner)
            await self._expire_waiters(db)

    async def _dispatch_order(self, db) -> list[str]:
        """Waiting step ids, oldest `created_at` first.

        The ORDER comes from the database - which is precisely why
        `runner_requirements` is a durable column: a requeued step must be
        re-matchable and re-prioritized after a restart, so its requirements
        cannot live only in a dispatch closure.

        A `pending` row with requirements and NO waiter is a stray: its
        executor generator died with a backend restart. It is logged once and
        left alone - assigning it would run work whose result nobody
        consumes. `JobRecoveryService.recover_orphaned_steps` and the
        run-level orphan sweep own those rows.
        """
        result = await db.execute(
            select(StepExecution.id)
            .where(
                StepExecution.status == StepExecutionStatus.PENDING.value,
                StepExecution.runner_requirements.is_not(None),
            )
            .order_by(StepExecution.created_at)
        )
        ordered = [row[0] for row in result.all()]

        known = set(ordered)
        for step_id in ordered:
            if step_id not in self._waiters and step_id not in self._stray_logged:
                self._stray_logged.add(step_id)
                logger.warning(
                    "pending remote step %s has no live executor waiting for it "
                    "(backend restart?); leaving it to the orphan sweep",
                    step_id,
                )
        self._stray_logged &= known

        # Waiters whose row is not (yet) visible as pending still get served,
        # after the DB-ordered ones, in registration order. Liveness first.
        extra = sorted(
            (w for sid, w in self._waiters.items() if sid not in known),
            key=lambda w: w.sequence,
        )
        return ordered + [w.step_id for w in extra]

    async def _expire_waiters(self, db) -> None:
        """Fail every waiter past its NO_RUNNER_TIMEOUT budget."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        expired = [
            w
            for w in self._waiters.values()
            if not w.future.done() and w.deadline <= now
        ]
        if not expired:
            return
        connected = await self.connected_runners(db)
        for waiter in expired:
            logger.error(
                "step %s found no runner matching %s within the budget",
                waiter.step_id,
                json.dumps(waiter.requirements, sort_keys=True),
            )
            waiter.future.set_exception(
                NoRunnerAvailable(waiter.requirements, connected)
            )

    async def connected_runners(self, db) -> list[dict]:
        """Snapshot rows for runners this process actually holds a socket to.

        Used only to make `NoRunnerAvailable` actionable. It reads the
        registry's `connection` marker rather than the row's status, because
        an `idle` row left behind by a crashed process is not a runner
        anybody can be handed work.
        """
        try:
            return [
                row
                for row in await self.registry.snapshot(db)
                if row.get("connection") == "websocket"
            ]
        except Exception:  # pragma: no cover - diagnostics must not raise
            logger.exception("could not snapshot runners for the no-runner message")
            return []


#: Process-wide singleton. One dispatcher per backend process, by design.
runner_dispatcher = RunnerDispatcher()


def get_runner_dispatcher() -> RunnerDispatcher:
    return runner_dispatcher


__all__ = [
    "Assignment",
    "AssignmentOutcome",
    "NoRunnerAvailable",
    "RunnerDispatcher",
    "get_runner_dispatcher",
    "runner_dispatcher",
]
