"""Debug session lifecycle - Phase 12.7.

failure_01 shipped a service with the right API surface and three fatal
holes. Each is named here with the fix, because "rebuilt" is not a design:

| failure_01 bug | Fix |
|---|---|
| `create_debug_rerun` never started the run | `create()` drives `start_pipeline` and inserts the session row through an `on_run_created` hook that fires AFTER the run row commits and BEFORE the first step dispatches, so the gate cannot race a step past a breakpoint. |
| `resume` ended the session, killing multi-breakpoint | `resume()` goes to `PENDING`. Only abort, timeout and run completion reach a terminal state. |
| the timeout monitor was never started | There is no monitor task. **The paused gate is the timeout owner** - it already holds the deadline and re-arms on every wake, so there is no task to leak and nothing for `reset()` to strand. |
| in-memory / DB dual truth | **The row is the truth; the `asyncio.Event` is a wakeup only.** Every wake re-reads the row, so a lost signal costs <=5s of latency and never a hang. |

The gate (`gate()`) is called as the FIRST statement of
`PipelineExecutor._run_executor_step`, which matters for four reasons spelled
out there. The one worth repeating: at a breakpoint there is no
`StepExecution` row yet, so a paused step has no `timeout_at`, no
`last_heartbeat`, and nothing for `recover_orphaned_executions` to reap.
Heartbeat suspension at a breakpoint is achieved by PLACEMENT, not by a flag
(contract C3).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.debug import DebugSession
from app.models.pipeline import PipelineRun, StepRun
from app.services.execution.debug_state import (
    DebugState,
    DebugStateMachine,
    InvalidDebugTransitionError,
    TERMINAL_STATES,
    debug_step_key,
)
from app.services.websocket import manager

logger = logging.getLogger(__name__)

#: The WS frame type the UI listens for. One constant, so the broadcast and
#: the frontend store cannot drift apart.
WS_DEBUG_SESSION_STATUS = "debug_session_status"

#: `PipelineRun.trigger_type` for a debug re-run. Stamped ONLY here.
TRIGGER_TYPE_DEBUG_RERUN = "debug_rerun"

#: Longest a single `wait_for` inside the pause loop may arm for. The DB row
#: is the truth and the event is only a wakeup, so a lost signal degrades to
#: at most this much latency - never a hang (contract C7).
GATE_POLL_SECONDS = 5.0

#: How long abort/run-completion waits for a paused gate to release its
#: workspace pin before proceeding anyway (loudly). See `await_gate_release`.
GATE_TEARDOWN_WAIT_SECONDS = 10.0

DEFAULT_TIMEOUT_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 14400

# R1: a session never reaches a terminal state without saying why.
END_REASON_ABORTED = "aborted by user"
END_REASON_TIMEOUT = "timed out at breakpoint"
END_REASON_PIPELINE_COMPLETED = "pipeline completed"
END_REASON_RUN_CANCELLED = "run cancelled"
END_REASON_RESTARTED = "backend restarted while paused"


class DebugSessionError(RuntimeError):
    """A debug operation the caller asked for that cannot be honoured.

    The router turns this into a 4xx with the message verbatim: every one of
    them is a fact the operator needs ("session already ended", "unknown
    breakpoint key 'build'"), never an internal detail.
    """


class DebugGateOutcome(str, Enum):
    """What the gate tells `_run_executor_step` to do next."""

    #: Not breakpointed, or the pause ended with a resume - run the step.
    RESUME = "resume"
    #: The pause timed out - fail the step through the ORDINARY completion
    #: path (`_finish_local_step`). No new terminal path exists for debug runs.
    FAILED = "failed"
    #: The session was aborted - `cancel_run` already owns every row, so the
    #: step task returns without touching anything.
    ABORTED = "aborted"


@dataclass
class DebugGateResult:
    outcome: DebugGateOutcome
    error: str | None = None
    #: True when this call actually held a step at a breakpoint (as opposed
    #: to the zero-cost "no session / not breakpointed" pass-through). Tests
    #: assert on it so "the gate returned RESUME" and "the gate never paused"
    #: cannot be confused for one another.
    paused: bool = False


def _json_list(raw: str | None) -> list[str]:
    """Parse a JSON list column into a list of strings, never raising.

    A corrupt blob must not wedge a run: it is logged and read as empty,
    which degrades to "no breakpoints" rather than to a crash inside the
    executor's step task.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("debug session carried a non-JSON list column: %r", raw[:120])
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


class _DebugLogTarget:
    """Minimal `StepExecution` stand-in for `step_logs.append_step_logs`.

    That function reads exactly one attribute - `step_run_id` - and is the
    SOLE writer of `StepRun.logs` (contract C11). Routing the gate's notice
    line through it keeps that "one writer" property instead of opening a
    second append path.

    The line is appended VERBATIM (the `container` source's semantics), which
    is what a pre-formatted `[debug] ...` line wants. A dedicated
    `SOURCE_DEBUG` constant in `step_logs.py` would be tidier and is listed
    as a requested integrator edit; it would not change a byte of output.
    """

    __slots__ = ("step_run_id",)

    def __init__(self, step_run_id: str) -> None:
        self.step_run_id = step_run_id


class DebugSessionService:
    """The debug session's whole lifecycle. One instance, `reset()`-able."""

    def __init__(self) -> None:
        #: debug_session_id -> wakeup. NOT state: every wake re-reads the row
        #: (contract C6). Recreated by `reset()` because asyncio primitives
        #: bind to the loop that first awaits them.
        self._events: dict[str, asyncio.Event] = {}
        #: Bumped by reset(). A parked gate captures it on entry and bails on
        #: the next wake if it changed, so the test harness's drain never has
        #: to cancel a gate as a straggler (which would be a hard cancel
        #: mid-await, exactly what reset() is written to avoid).
        self._generation = 0
        #: PipelineExecutor seam. None means the process-wide singleton;
        #: tests that build their own executor point this at it so the
        #: service and the executor under test are the same object.
        self._executor = None
        #: debug_session_id -> one Event per gate currently holding a
        #: WORKSPACE PIN for that session. Abort and run-completion wait on
        #: these before letting `_cleanup_workspace` run: the pin must be
        #: released and the sidecar removed FIRST (contract C9), or cleanup
        #: force-releases a "leaked" use_count and then asks docker to remove
        #: a volume a running sidecar still mounts.
        self._teardowns: dict[str, list[asyncio.Event]] = {}

    # -- plumbing ------------------------------------------------------------

    def _pipeline_executor(self):
        """The executor this service drives. Lazily imported (no cycle)."""
        if self._executor is not None:
            return self._executor
        from app.services.pipeline_executor import pipeline_executor

        return pipeline_executor

    def _event(self, session_id: str) -> asyncio.Event:
        event = self._events.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._events[session_id] = event
        return event

    def _wake(self, session_id: str) -> None:
        """Poke a paused gate. Best-effort by design: the row already
        carries the decision, so a missed poke costs GATE_POLL_SECONDS."""
        event = self._events.get(session_id)
        if event is not None:
            event.set()

    async def reset(self) -> None:
        """Test-mode reset hook (routers/test_api.py registry).

        There is no background task to drain - the paused gate is the sole
        timeout owner - so this only drops the wakeup registry. Any gate
        still parked is woken first so it re-reads a row the reset endpoint
        is about to delete and returns, rather than sitting out its poll.
        """
        self._generation += 1
        for event in self._events.values():
            event.set()
        self._events = {}
        for pending in self._teardowns.values():
            for event in pending:
                event.set()
        self._teardowns = {}

    # -- reads ---------------------------------------------------------------

    async def get(self, db: AsyncSession, session_id: str) -> DebugSession | None:
        result = await db.execute(
            select(DebugSession).where(DebugSession.id == session_id)
        )
        return result.scalar_one_or_none()

    async def get_for_run(
        self, db: AsyncSession, run_id: str, *, active_only: bool = True
    ) -> DebugSession | None:
        """The run's debug session. `pipeline_run_id` is UNIQUE, so at most one."""
        stmt = select(DebugSession).where(DebugSession.pipeline_run_id == run_id)
        if active_only:
            stmt = stmt.where(
                DebugSession.status.notin_([s.value for s in TERMINAL_STATES])
            )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self, db: AsyncSession) -> list[DebugSession]:
        result = await db.execute(
            select(DebugSession)
            .where(DebugSession.status.notin_([s.value for s in TERMINAL_STATES]))
            .order_by(DebugSession.created_at.desc())
        )
        return list(result.scalars().all())

    # -- state transitions ---------------------------------------------------

    def _apply(
        self, session: DebugSession, to_state: DebugState, reason: str
    ) -> None:
        """Drive the row's state through the machine and persist the history.

        Rehydrating the machine from `state_history` on every transition is
        what makes the column real (failure_01 declared it and never wrote
        it) and what makes an illegal transition a loud
        `InvalidDebugTransitionError` instead of a silent column overwrite.
        """
        machine = self._machine_for(session)
        machine.transition(to_state, reason=reason)
        session.status = to_state.value
        session.state_history = json.dumps(machine.to_dict())

    def _machine_for(self, session: DebugSession) -> DebugStateMachine:
        raw = session.state_history
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = None
            if isinstance(data, dict) and "state" in data and "created_at" in data:
                machine = DebugStateMachine.from_dict(data)
                # The row is the truth: if something wrote `status` without
                # going through the machine, trust the row.
                if machine.state.value != session.status:
                    machine._state = DebugState(session.status)
                return machine
        return DebugStateMachine(initial_state=DebugState(session.status))

    # -- create --------------------------------------------------------------

    def resolve_step_keys(self, pipeline) -> list[tuple[str, str]]:
        """Every (key, name) a breakpoint may name, for THIS pipeline.

        Uses the same `debug_step_key` the gate does (contract C2), fed by a
        shim carrying the step's id/index - so the validator cannot drift
        from the runtime.

        12.8: the graph is the only definition, so a pipeline that has none
        is REFUSED here rather than answered with an empty vocabulary. The
        empty list was the quiet version of this: a debug re-run created with
        no breakpoints against a definition that can address no step is a
        session that watches a run it can never stop, and nothing anywhere
        would have said so (R1).
        """
        from app.services.pipeline_executor import parse_steps_graph

        graph = parse_steps_graph(pipeline.steps_graph)
        steps = (graph or {}).get("steps") or {}
        if not steps:
            raise DebugSessionError(
                f"pipeline {getattr(pipeline, 'name', None) or pipeline.id} has "
                "no graph definition, so no step can be addressed by a "
                "breakpoint. Re-save the pipeline to materialize its graph."
            )
        keys: list[tuple[str, str]] = []
        for step_id, step in steps.items():
            shim = _StepKeyShim(step_id=step_id, step_index=0)
            keys.append((debug_step_key(shim), step.get("name") or step_id))
        return keys

    async def create(
        self,
        db: AsyncSession,
        *,
        original_run: PipelineRun,
        pipeline,
        repo,
        breakpoints: list[str],
        use_original_commit: bool = True,
        commit_sha: str | None = None,
        branch: str | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[DebugSession, PipelineRun]:
        """Start a debug re-run and return (session, new run).

        Ordering, and it is the whole point: the session row is inserted from
        an `on_run_created` hook that `start_pipeline` fires AFTER the run row
        is committed and BEFORE any step is dispatched. Insert it after
        `start_pipeline` returns and the first step's gate may already have
        run; insert it before and there is no run id to point at.

        `trigger_context` is REBUILT, never copied (contract C10): only
        `branch` and `commit_sha` carry over. `on_pass`, `on_fail`, `card_id`
        and everything else are dropped, so a debug re-run can never merge a
        branch and can never walk a card to in_review.
        """
        known = {key for key, _name in self.resolve_step_keys(pipeline)}
        unknown = [key for key in breakpoints if key not in known]
        if unknown:
            raise DebugSessionError(
                "unknown breakpoint step key(s): "
                + ", ".join(sorted(unknown))
                + ". Known keys: "
                + (", ".join(sorted(known)) or "(none)")
            )

        original_context: dict[str, Any] = {}
        if original_run.trigger_context:
            try:
                original_context = json.loads(original_run.trigger_context) or {}
            except (json.JSONDecodeError, TypeError):
                original_context = {}

        if use_original_commit:
            new_branch = original_context.get("branch")
            new_commit = original_context.get("commit_sha")
        else:
            new_branch = branch or original_context.get("branch")
            new_commit = commit_sha

        trigger_context: dict[str, Any] = {}
        if new_branch:
            trigger_context["branch"] = new_branch
        if new_commit:
            trigger_context["commit_sha"] = new_commit

        clamped_timeout = min(
            max(int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), 60),
            MAX_TIMEOUT_SECONDS,
        )

        session_id = str(uuid4())
        created: dict[str, DebugSession] = {}

        async def _insert_session(hook_db: AsyncSession, run: PipelineRun) -> None:
            debug_session = DebugSession(
                id=session_id,
                pipeline_run_id=run.id,
                original_run_id=original_run.id,
                status=DebugState.PENDING.value,
                breakpoints=json.dumps(list(breakpoints)),
                hit_breakpoints=json.dumps([]),
                timeout_seconds=clamped_timeout,
                max_timeout_seconds=MAX_TIMEOUT_SECONDS,
                created_at=datetime.utcnow(),
                state_history=json.dumps(
                    DebugStateMachine(initial_state=DebugState.PENDING).to_dict()
                ),
            )
            hook_db.add(debug_session)
            await hook_db.commit()
            await hook_db.refresh(debug_session)
            created["session"] = debug_session
            # Arm the wakeup before any gate can park on it.
            self._event(session_id)

        pipeline_run = await self._pipeline_executor().start_pipeline(
            db,
            pipeline,
            repo,
            trigger_type=TRIGGER_TYPE_DEBUG_RERUN,
            trigger_ref=original_run.id,
            trigger_context=trigger_context or None,
            on_run_created=_insert_session,
        )

        debug_session = created.get("session")
        if debug_session is None:  # pragma: no cover - defensive
            raise DebugSessionError(
                "start_pipeline did not invoke the debug session hook; the "
                "re-run started WITHOUT breakpoints and must not be reported "
                "as a debug session"
            )
        await self._broadcast(db, debug_session)
        logger.info(
            "Debug session %s created for run %s (%d breakpoint(s), re-running %s)",
            session_id[:8],
            pipeline_run.id[:8],
            len(breakpoints),
            original_run.id[:8],
        )
        return debug_session, pipeline_run

    # -- the gate ------------------------------------------------------------

    async def gate(
        self, session_factory, run_id: str, step_run_id: str, mode
    ) -> DebugGateResult:
        """Hold a step before it dispatches, if a breakpoint names it.

        Session discipline (contract C4): this method opens and closes its own
        SHORT sessions and holds none across the pause. A multi-hour pause
        pinning a pooled connection - and aging a transaction snapshot into
        nonsense - is exactly the failure mode `ws_runners.py` calls out.

        Zero-cost for an ordinary run: one indexed SELECT that finds nothing.
        """
        db = session_factory()
        try:
            session = await self.get_for_run(db, run_id, active_only=True)
            if session is None:
                return DebugGateResult(DebugGateOutcome.RESUME)
            step_run = await db.get(StepRun, step_run_id)
            if step_run is None:
                # The executor's own context load will fail loudly right
                # after this; the gate must not invent a pause for a step
                # that does not exist.
                return DebugGateResult(DebugGateOutcome.RESUME)
            key = debug_step_key(step_run)
            if key not in _json_list(session.breakpoints):
                return DebugGateResult(DebugGateOutcome.RESUME)
            if key in _json_list(session.hit_breakpoints):
                return DebugGateResult(DebugGateOutcome.RESUME)
            session_id = session.id
            step_index = step_run.step_index
            step_name = step_run.step_name
        finally:
            await db.close()

        executor_value = getattr(mode, "value", str(mode))
        workspace_id: str | None = None
        if executor_value != "remote":
            workspace_id = await self._pin_workspace(session_factory, run_id)

        released = asyncio.Event()
        self._teardowns.setdefault(session_id, []).append(released)
        try:
            armed = await self._arm_pause(
                session_factory,
                session_id,
                key=key,
                step_run_id=step_run_id,
                step_index=step_index,
                step_name=step_name,
                executor_value=executor_value,
            )
            if armed is not None:
                return armed
            outcome = await self._wait_at_breakpoint(session_factory, session_id)
        finally:
            await self._teardown_pause(session_factory, session_id, workspace_id)
            holders = self._teardowns.get(session_id)
            if holders is not None and released in holders:
                holders.remove(released)
                if not holders:
                    self._teardowns.pop(session_id, None)
            released.set()
        return outcome

    async def await_gate_release(
        self, session_id: str, timeout: float = GATE_TEARDOWN_WAIT_SECONDS
    ) -> bool:
        """Block until every gate holding a pin for this session has let go.

        Contract C9's ordering is only real if the caller WAITS for it.
        `abort()` and `end_for_run()` both run immediately before something
        that calls `_cleanup_workspace`, and cleanup force-releases a
        "leaked" use_count with a warning and then asks docker to remove a
        volume the sidecar may still mount. Setting the wakeup and hoping the
        gate task gets scheduled first is not an ordering guarantee.

        Bounded and loud: a gate that does not let go inside the budget is
        logged, and the caller proceeds anyway - a debug session must never
        be able to wedge a run's teardown.
        """
        holders = list(self._teardowns.get(session_id) or ())
        if not holders:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in holders)),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.error(
                "Debug session %s: %d breakpoint gate(s) did not release the "
                "workspace pin within %.1fs; proceeding with teardown, so "
                "workspace cleanup may report a leaked use_count",
                session_id[:8],
                len(holders),
                timeout,
            )
            return False

    async def _pin_workspace(self, session_factory, run_id: str) -> str | None:
        """Hold a durable REFCOUNT on the run's workspace for the pause (C8).

        `acquire` keeps its exclusive lock only long enough to bump
        `use_count` and commit, so the pause holds a refcount, not a lock -
        and that refcount is what keeps the volume alive (the workspace state
        machine refuses CLEANING while `use_count > 0`). For the FIRST step of
        a run it is also what makes the sidecar useful at all: without it a
        breakpoint on step 0 would attach to a volume that does not exist yet.

        Pins the DEFAULT LANE (no `worker_key`), matching the volume
        `debug_terminal.workspace_volume_name` mounts into the sidecar. This
        is also the reason `use_count` stays an integer after M13-1: the pin
        lands on a lane a paused step is ALREADY holding, so the count
        legitimately reaches 2 and the release order is not guaranteed - a
        boolean would let the first releaser reap a volume a live sidecar
        still has mounted.
        """
        db = session_factory()
        try:
            result = await db.execute(
                select(PipelineRun).where(PipelineRun.id == run_id)
            )
            pipeline_run = result.scalar_one_or_none()
            if pipeline_run is None:
                return None
            from app.models.pipeline import Pipeline
            from app.models.repo import Repo

            pipeline = (
                await db.execute(
                    select(Pipeline).where(Pipeline.id == pipeline_run.pipeline_id)
                )
            ).scalar_one_or_none()
            if pipeline is None:
                return None
            repo = (
                await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
            ).scalar_one_or_none()
            if repo is None:
                return None
            context: dict[str, Any] = {}
            if pipeline_run.trigger_context:
                try:
                    context = json.loads(pipeline_run.trigger_context) or {}
                except (json.JSONDecodeError, TypeError):
                    context = {}
            workspace_service = self._pipeline_executor()._get_workspace_service()
            workspace = await workspace_service.get_or_create(
                db,
                run_id,
                repo.id,
                context.get("branch") or repo.default_branch,
                context.get("commit_sha"),
            )
            await workspace_service.acquire(db, workspace.id)
            return workspace.id
        except Exception:
            logger.error(
                "Debug gate could not pin the workspace for run %s; the pause "
                "continues WITHOUT a sidecar-visible volume",
                run_id[:8],
                exc_info=True,
            )
            return None
        finally:
            await db.close()

    async def _arm_pause(
        self,
        session_factory,
        session_id: str,
        *,
        key: str,
        step_run_id: str,
        step_index: int,
        step_name: str | None,
        executor_value: str,
    ) -> DebugGateResult | None:
        """Move the session into WAITING_AT_BP, waiting our turn if needed.

        One pause at a time per run, stated rather than assumed: two
        breakpointed steps of a parallel graph cannot both hold the single
        session at a breakpoint, so the second waits for the first to resume
        (the session returns to PENDING) and then takes its own pause. It
        polls the SAME bounded wait the pause loop uses, so nothing new can
        hang.

        Returns None once the pause is armed, or a terminal result when the
        session ended while we were waiting our turn.
        """
        generation = self._generation
        while True:
            if self._generation != generation:
                return DebugGateResult(DebugGateOutcome.ABORTED, paused=True)
            db = session_factory()
            try:
                session = await self.get(db, session_id)
                if session is None or session.is_terminal():
                    return self._terminal_result(session)
                if session.status == DebugState.PENDING.value:
                    now = datetime.utcnow()
                    self._apply(
                        session,
                        DebugState.WAITING_AT_BP,
                        reason=f"breakpoint hit at step {key}",
                    )
                    session.current_step_key = key
                    session.current_step_index = step_index
                    session.current_step_name = step_name
                    session.current_step_executor = executor_value
                    session.breakpoint_hit_at = now
                    session.expires_at = now + timedelta(
                        seconds=session.timeout_seconds
                    )
                    hit = _json_list(session.hit_breakpoints)
                    if key not in hit:
                        hit.append(key)
                    session.hit_breakpoints = json.dumps(hit)
                    # The notice line rides the SAME transaction as the
                    # status change (it commits this session), so anyone who
                    # can see WAITING_AT_BP can also see the reason in the
                    # step's own log. Two commits would leave a window where
                    # the UI says "paused" and the log view says nothing.
                    await self._notice(db, step_run_id, session_id, step_name)
                    await db.commit()
                    await db.refresh(session)
                    await self._broadcast(db, session)
                    return None
            except InvalidDebugTransitionError:
                logger.exception(
                    "Debug session %s refused the breakpoint transition; the "
                    "step runs WITHOUT pausing rather than wedging",
                    session_id[:8],
                )
                return DebugGateResult(DebugGateOutcome.RESUME)
            finally:
                await db.close()
            await self._sleep_until_poked(session_id, GATE_POLL_SECONDS)

    async def _wait_at_breakpoint(
        self, session_factory, session_id: str
    ) -> DebugGateResult:
        """Park until the row says otherwise, or the deadline passes.

        THE TIMEOUT OWNER (contract C7). No background task exists: this loop
        already holds the deadline and re-arms `min(remaining, 5s)` on every
        iteration, so `/extend` is just a row update plus a poke, and a lost
        poke costs at most one poll.
        """
        generation = self._generation
        while True:
            if self._generation != generation:
                return DebugGateResult(DebugGateOutcome.ABORTED, paused=True)
            db = session_factory()
            try:
                session = await self.get(db, session_id)
                if session is None:
                    return DebugGateResult(
                        DebugGateOutcome.ABORTED, paused=True
                    )
                if session.is_terminal():
                    return self._terminal_result(session)
                if session.status == DebugState.PENDING.value:
                    return DebugGateResult(DebugGateOutcome.RESUME, paused=True)
                expires_at = session.expires_at
                if expires_at is not None and expires_at <= datetime.utcnow():
                    self._apply(
                        session, DebugState.TIMEOUT, reason=END_REASON_TIMEOUT
                    )
                    session.end_reason = END_REASON_TIMEOUT
                    session.ended_at = datetime.utcnow()
                    await db.commit()
                    await db.refresh(session)
                    await self._broadcast(db, session)
                    return DebugGateResult(
                        DebugGateOutcome.FAILED,
                        error=(
                            f"debug session timed out at breakpoint "
                            f"{session.current_step_key!r} after "
                            f"{session.timeout_seconds}s"
                        ),
                        paused=True,
                    )
                remaining = (
                    (expires_at - datetime.utcnow()).total_seconds()
                    if expires_at is not None
                    else GATE_POLL_SECONDS
                )
            finally:
                await db.close()
            await self._sleep_until_poked(
                session_id, max(0.0, min(remaining, GATE_POLL_SECONDS))
            )

    def _terminal_result(self, session: DebugSession | None) -> DebugGateResult:
        """A terminal row, mapped to what the step task must do.

        ENDED means abort or run completion - either way `cancel_run` /
        `_complete_pipeline` already owns every row, so the step task returns
        and touches nothing. TIMEOUT means the step fails through the
        ORDINARY completion path.
        """
        if session is not None and session.status == DebugState.TIMEOUT.value:
            return DebugGateResult(
                DebugGateOutcome.FAILED,
                error=session.end_reason or END_REASON_TIMEOUT,
                paused=True,
            )
        return DebugGateResult(DebugGateOutcome.ABORTED, paused=True)

    async def _sleep_until_poked(self, session_id: str, timeout: float) -> None:
        event = self._event(session_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=max(timeout, 0.01))
        except asyncio.TimeoutError:
            return
        finally:
            event.clear()

    async def _notice(
        self,
        db: AsyncSession,
        step_run_id: str,
        session_id: str,
        step_name: str | None,
    ) -> None:
        """One line into `StepRun.logs`, through the SOLE writer (C11).

        A `StepRun` sits RUNNING while its step is paused (dispatch committed
        and broadcast it before the task ran), so the plain log view has to
        say why on its own.

        Written on the CALLER's session, inside the same transaction as the
        pause itself: two commits would leave a window in which the UI says
        "paused at a breakpoint" and the step's log view says nothing.
        """
        try:
            from app.services.execution.step_logs import (
                SOURCE_CONTAINER,
                append_step_logs,
            )

            await append_step_logs(
                db,
                _DebugLogTarget(step_run_id),
                [
                    f"[debug] paused before step {step_name or '?'!r} - "
                    f"join: lazyaf debug attach {session_id}\n"
                ],
                source=SOURCE_CONTAINER,
            )
        except Exception:
            logger.warning(
                "Debug gate could not write its notice line for step %s",
                step_run_id,
                exc_info=True,
            )

    async def _teardown_pause(
        self, session_factory, session_id: str, workspace_id: str | None
    ) -> None:
        """End-of-pause cleanup, in the ONE order that works (contract C9).

        Sidecar removal MUST precede the workspace release, because
        `cancel_run` / `_complete_pipeline` call `_cleanup_workspace` and
        docker refuses to remove a volume a running container still mounts.
        """
        db = session_factory()
        try:
            session = await self.get(db, session_id)
            container_id = session.sidecar_container_id if session else None
            from app.services.execution.debug_terminal import debug_terminal_service

            await debug_terminal_service.remove_sidecar(session_id, container_id)
            if session is not None and session.sidecar_container_id:
                session.sidecar_container_id = None
                session.connection_mode = None
                await db.commit()
            if workspace_id is not None:
                try:
                    await self._pipeline_executor()._get_workspace_service().release(
                        db, workspace_id
                    )
                except Exception:
                    logger.error(
                        "Debug gate could not release the workspace pin for "
                        "session %s (audit_orphans is the backstop)",
                        session_id[:8],
                        exc_info=True,
                    )
        except Exception:
            logger.exception("Debug pause teardown failed for %s", session_id[:8])
        finally:
            await db.close()

    # -- operator actions ----------------------------------------------------

    async def resume(
        self, db: AsyncSession, session_id: str, *, clear_remaining: bool = False
    ) -> tuple[DebugSession, str | None]:
        """Release a paused step. Goes to PENDING, NEVER to ENDED.

        This one line is the multi-breakpoint fix: failure_01 ended the
        session here, so the second breakpoint had no live session to pause
        into and simply never fired.
        """
        session = await self._require_active(db, session_id)
        if session.status not in (
            DebugState.WAITING_AT_BP.value,
            DebugState.CONNECTED.value,
        ):
            raise DebugSessionError(
                f"debug session {session_id} is {session.status}, not paused "
                "at a breakpoint"
            )
        if clear_remaining:
            session.breakpoints = json.dumps([])
        self._apply(
            session,
            DebugState.PENDING,
            reason="resumed (run to completion)" if clear_remaining else "resumed",
        )
        session.current_step_key = None
        session.current_step_index = None
        session.current_step_name = None
        session.current_step_executor = None
        session.breakpoint_hit_at = None
        session.connected_at = None
        session.expires_at = None
        await db.commit()
        await db.refresh(session)
        self._wake(session_id)
        await self._broadcast(db, session)
        pending = self.pending_breakpoints(session)
        return session, (pending[0] if pending else None)

    async def abort(self, db: AsyncSession, session_id: str) -> DebugSession:
        """End the session AND cancel its run.

        Teardown order (C9): terminal state -> close terminal -> remove
        sidecar -> release pin -> `cancel_run`. The pin release lives in the
        paused gate's `finally`, which runs before `cancel_run` can reach
        `_cleanup_workspace` because the gate is woken first.
        """
        session = await self._require_active(db, session_id)
        run_id = session.pipeline_run_id
        self._apply(session, DebugState.ENDED, reason=END_REASON_ABORTED)
        session.end_reason = END_REASON_ABORTED
        session.ended_at = datetime.utcnow()
        await db.commit()
        await db.refresh(session)
        self._wake(session_id)
        await self._broadcast(db, session)

        from app.services.execution.debug_terminal import debug_terminal_service

        await debug_terminal_service.remove_sidecar(
            session_id, session.sidecar_container_id
        )
        # C9: the pin must be gone before cancel_run reaches
        # _cleanup_workspace. Waiting is the only thing that makes that an
        # ordering rather than a hope.
        #
        # Commit first so this session holds NO transaction - not even the
        # read one `refresh` reopened - while we wait. On a rollback-journal
        # SQLite a lingering reader blocks the gate's release commit, and the
        # wait would then burn its whole budget on a lock we hold ourselves.
        await db.commit()
        await self.await_gate_release(session_id)

        # selectinload, not a later lazy access (R5): `cancel_run` walks
        # `pipeline_run.step_runs` after its own commit+refresh, and an
        # unloaded collection there raises MissingGreenlet out of the abort
        # path - the exact async-lazy-load bomb the salvage audit's landmine
        # 7 names.
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        pipeline_run = result.scalar_one_or_none()
        if pipeline_run is not None:
            from app.models.pipeline import RunStatus

            if pipeline_run.status == RunStatus.RUNNING.value:
                await self._pipeline_executor().cancel_run(db, pipeline_run)
        return session

    async def extend(
        self, db: AsyncSession, session_id: str, additional_minutes: int
    ) -> tuple[DebugSession, bool]:
        """Move the pause deadline. `max_timeout_seconds` is the hard ceiling."""
        session = await self._require_active(db, session_id)
        if session.expires_at is None:
            raise DebugSessionError(
                f"debug session {session_id} is not paused at a breakpoint; "
                "there is no deadline to extend"
            )
        anchor = session.breakpoint_hit_at or session.created_at
        ceiling = anchor + timedelta(seconds=session.max_timeout_seconds)
        requested = session.expires_at + timedelta(minutes=additional_minutes)
        clamped = requested > ceiling
        session.expires_at = min(requested, ceiling)
        await db.commit()
        await db.refresh(session)
        self._wake(session_id)
        await self._broadcast(db, session)
        return session, clamped

    async def mark_connected(
        self, db: AsyncSession, session_id: str, container_id: str, mode: str
    ) -> DebugSession:
        session = await self._require_active(db, session_id)
        if session.status == DebugState.WAITING_AT_BP.value:
            self._apply(session, DebugState.CONNECTED, reason="terminal attached")
        session.connected_at = datetime.utcnow()
        session.sidecar_container_id = container_id
        session.connection_mode = mode
        await db.commit()
        await db.refresh(session)
        await self._broadcast(db, session)
        return session

    async def mark_disconnected(
        self, db: AsyncSession, session_id: str
    ) -> DebugSession | None:
        session = await self.get(db, session_id)
        if session is None or session.is_terminal():
            return session
        if session.status == DebugState.CONNECTED.value:
            self._apply(
                session, DebugState.WAITING_AT_BP, reason="terminal detached"
            )
            await db.commit()
            await db.refresh(session)
            await self._broadcast(db, session)
        return session

    async def end_for_run(
        self, db: AsyncSession, run_id: str, reason: str
    ) -> DebugSession | None:
        """End the run's session when the RUN ends. Never leaves it dangling.

        Called from `_complete_pipeline` and `cancel_run`. A session that ends
        this way reports any breakpoint that never fired, because an
        unreachable breakpoint (its step's upstream failed) is a visible fact
        rather than silence.
        """
        session = await self.get_for_run(db, run_id, active_only=True)
        if session is None:
            return None
        never_hit = self.pending_breakpoints(session)
        end_reason = reason
        if never_hit:
            end_reason = (
                f"{reason}; {len(never_hit)} breakpoint(s) never reached: "
                + ", ".join(never_hit)
            )[:255]
        try:
            self._apply(session, DebugState.ENDED, reason=end_reason)
        except InvalidDebugTransitionError:
            logger.warning(
                "Debug session %s could not be ended from %s; forcing ENDED "
                "so the run does not leave a live session behind",
                session.id[:8],
                session.status,
            )
            session.status = DebugState.ENDED.value
        session.end_reason = end_reason
        session.ended_at = datetime.utcnow()
        await db.commit()
        await db.refresh(session)
        self._wake(session.id)
        await self._broadcast(db, session)

        from app.services.execution.debug_terminal import debug_terminal_service

        await debug_terminal_service.remove_sidecar(
            session.id, session.sidecar_container_id
        )
        # The caller (_complete_pipeline / cancel_run) calls
        # _cleanup_workspace next; C9 again. Same reason for the commit: hold
        # no transaction while waiting on a coroutine that needs to write.
        await db.commit()
        await self.await_gate_release(session.id)
        return session

    async def sweep_paused_sessions(self, db: AsyncSession) -> int:
        """Startup sweep (contract C20): no half-alive debug runs.

        A paused gate is an in-process task; a restart kills it and the run
        can never continue. Each non-terminal session is ended with
        `backend restarted while paused`, its run is failed with that same
        message, and every stray sidecar is removed. Honest beats hopeful.
        """
        from app.models.pipeline import RunStatus

        sessions = await self.list_active(db)
        for session in sessions:
            try:
                self._apply(session, DebugState.ENDED, reason=END_REASON_RESTARTED)
            except InvalidDebugTransitionError:
                session.status = DebugState.ENDED.value
            session.end_reason = END_REASON_RESTARTED
            session.ended_at = datetime.utcnow()
            run = (
                await db.execute(
                    select(PipelineRun).where(
                        PipelineRun.id == session.pipeline_run_id
                    )
                )
            ).scalar_one_or_none()
            if run is not None and run.status == RunStatus.RUNNING.value:
                run.status = RunStatus.FAILED.value
                run.completed_at = datetime.utcnow()
                # The paused StepRun is still RUNNING - its owning task died
                # with the process. Leaving it RUNNING under a FAILED run is
                # exactly the half-alive state this sweep exists to prevent,
                # so it is failed with the same reason.
                stranded = (
                    await db.execute(
                        select(StepRun).where(
                            StepRun.pipeline_run_id == run.id,
                            StepRun.status == RunStatus.RUNNING.value,
                        )
                    )
                ).scalars().all()
                for step_run in stranded:
                    step_run.status = RunStatus.FAILED.value
                    step_run.completed_at = datetime.utcnow()
                    step_run.error = END_REASON_RESTARTED
        if sessions:
            await db.commit()
            logger.warning(
                "Startup swept %d debug session(s) paused across a restart",
                len(sessions),
            )
        from app.services.execution.debug_terminal import debug_terminal_service

        await debug_terminal_service.sweep_orphan_sidecars(set())
        return len(sessions)

    async def _require_active(
        self, db: AsyncSession, session_id: str
    ) -> DebugSession:
        session = await self.get(db, session_id)
        if session is None:
            raise DebugSessionError(f"debug session {session_id} not found")
        if session.is_terminal():
            raise DebugSessionError(
                f"debug session {session_id} already ended "
                f"({session.end_reason or session.status})"
            )
        return session

    # -- projection ----------------------------------------------------------

    @staticmethod
    def pending_breakpoints(session: DebugSession) -> list[str]:
        hit = set(_json_list(session.hit_breakpoints))
        return [key for key in _json_list(session.breakpoints) if key not in hit]

    def attachability(self, session: DebugSession) -> tuple[bool, str | None]:
        """(attach_available, reason). The reason is ALWAYS set when False.

        R1: a remote-step pause is still a real pause, and it says so instead
        of quietly offering a terminal onto the wrong (or no) volume.
        """
        from app.services.execution.debug_terminal import REMOTE_ATTACH_REASON

        if session.is_terminal():
            return False, f"session has ended ({session.end_reason or session.status})"
        if not session.is_at_breakpoint():
            return False, "the run is not paused at a breakpoint"
        if session.current_step_executor == "remote":
            return False, REMOTE_ATTACH_REASON
        return True, None

    def to_dict(self, session: DebugSession, *, logs: str = "") -> dict:
        """The ONE projection of a session row (R3).

        The GET response, the WS frame and the CLI all read this, so the UI
        and the API cannot drift into two vocabularies the way failure_01's
        frontend fixtures and backend enum did in a single commit.
        """
        attach_available, reason = self.attachability(session)
        current_step = None
        if session.current_step_key is not None:
            current_step = {
                "key": session.current_step_key,
                "name": session.current_step_name or "",
                "index": session.current_step_index or 0,
                "type": "",
            }
        return {
            "id": session.id,
            "pipeline_run_id": session.pipeline_run_id,
            "original_run_id": session.original_run_id,
            "status": session.status,
            "current_step": current_step,
            "commit": {"sha": "", "message": "", "branch": ""},
            "runtime": {
                "host": session.current_step_executor or "local",
                "orchestrator": "docker",
                "image": "",
                "image_sha": None,
            },
            "logs": logs,
            "join_command": f"lazyaf debug attach {session.id}",
            "expires_at": session.expires_at,
            "created_at": session.created_at,
            "ended_at": session.ended_at,
            "breakpoints": _json_list(session.breakpoints),
            "breakpoints_hit": _json_list(session.hit_breakpoints),
            "breakpoints_pending": self.pending_breakpoints(session),
            "attach_available": attach_available,
            "attach_unavailable_reason": reason,
            "connection_mode": session.connection_mode,
            "end_reason": session.end_reason,
        }

    async def _broadcast(self, db: AsyncSession, session: DebugSession) -> None:
        """Publish `debug_session_status`.

        Explicit two-argument call into the manager's `broadcast`, not a
        `*args` helper: failure_01's breakpoint hook died on a `broadcast()`
        arity TypeError that its tests had AsyncMocked away, so the call site
        stays visible and typed here.
        """
        payload = self.to_dict(session)
        payload = {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in payload.items()
        }
        try:
            await manager.broadcast(WS_DEBUG_SESSION_STATUS, payload)
        except Exception:
            logger.warning(
                "debug_session_status broadcast failed for %s", session.id[:8],
                exc_info=True,
            )


@dataclass
class _StepKeyShim:
    """A step definition addressed the way a StepRun is, for key validation."""

    step_id: str | None
    step_index: int


debug_session_service = DebugSessionService()


__all__ = [
    "DebugGateOutcome",
    "DebugGateResult",
    "DebugSessionError",
    "DebugSessionService",
    "END_REASON_ABORTED",
    "END_REASON_PIPELINE_COMPLETED",
    "END_REASON_RESTARTED",
    "END_REASON_RUN_CANCELLED",
    "END_REASON_TIMEOUT",
    "GATE_POLL_SECONDS",
    "TRIGGER_TYPE_DEBUG_RERUN",
    "WS_DEBUG_SESSION_STATUS",
    "debug_session_service",
]
