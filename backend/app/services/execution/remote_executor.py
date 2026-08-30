"""Remote executor - Phase 12.6.

Runs a step on another machine and produces **byte-for-byte the LocalExecutor
event contract** while doing it.

That last clause is the entire design. `pipeline_executor` already knows how
to consume an executor: `_consume_local_events` reads three event type
strings in a fixed order and `_finish_local_step` reconciles the result. If
either of those had to learn what "remote" means, the contract was not met
and every future executor would need the same surgery. So:

    {"type": "status", "status": "preparing"}
    {"type": "status", "status": "running"}          # on ACK, not on send
    {"type": "status", "status": <final>}
    {"type": "result", "status": ..., "exit_code": ..., [error] [timeout_seconds]}

`log` remains part of the executor event contract and is simply never emitted
on this path - exactly as LocalExecutor emits none in control mode. Runner-
origin lines ("[runner] pulling lazyaf-test-runner:dev") arrive on the WS
`log` frame and are written by `step_logs.append_step_logs(source="runner")`,
the SOLE writer of StepRun.logs for both channels (cross-agent contract #6).
Echoing them through this generator as well would be a second path for one
datum, which is the R3 violation this arc exists to remove.

Same strings, same order, same optional result keys, and the same invariant
LocalExecutor guarantees with "the container is removed before the result
event": **by the time `result` is yielded the assignment is fully closed
out** - the runner is back to IDLE (or dead), the StepExecution row is
terminal, and no further wire frame for this step will be honored.

The generator OWNS the step until terminal, exactly as LocalExecutor owns its
container until `wait()` returns. A runner that dies mid-step does not yield
a result; it re-dispatches inside the loop. That is what lets the pipeline
executor stay ignorant of remoteness.

What this class deliberately does NOT have:

- **No idempotency cache.** LocalExecutor's `_completed_executions` guards a
  re-driven generator inside one process. Remote idempotency belongs where
  the duplication can actually happen: the `execution_key` LRU on the agent
  (a re-dispatch of the same key returns the cached outcome instead of
  running twice) and the CAS in the database.
- **No log streaming of container stdout.** The step container POSTs its own
  status, logs, heartbeats, test-results and usage to `/api/steps/*` with the
  step JWT, which is location-independent and therefore works from another
  host with zero new server code. Reimplementing those five channels over the
  WebSocket would be a second ingestion path for datums that 12.2.6 and 12.5
  spent two phases single-sourcing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator

from sqlalchemy import select

from app.models.pipeline import StepExecution, StepExecutionStatus
from app.services.execution.runner_protocol import (
    ACK_TIMEOUT,
    DRAIN_GRACE,
    MAX_ASSIGN_ATTEMPTS,
    NO_RUNNER_TIMEOUT,
    CancelStepMessage,
    ExecuteStepMessage,
    build_execute_step_config,
)
from app.services.execution.runner_state import (
    InvalidRunnerTransitionError,
    RunnerState,
)

logger = logging.getLogger(__name__)

#: Backstop grace over the step's own timeout, mirroring LocalExecutor's
#: CONTROL_MODE_TIMEOUT_GRACE. The in-container runtime remains the ONE
#: timeout owner in both modes; this deadline only catches a runner that
#: stopped speaking without dying (its heartbeats still land, its step never
#: completes). Ordering stays: in-container timeout < executor backstop <
#: pipeline hard deadline.
REMOTE_STEP_TIMEOUT_GRACE = 60


class RemoteExecutor:
    """Executes steps on a connected runner agent over the runner WebSocket."""

    def __init__(
        self,
        registry=None,
        dispatcher=None,
        recovery=None,
        session_factory=None,
    ) -> None:
        self._registry = registry
        self._dispatcher = dispatcher
        self._recovery = recovery
        self._session_factory = session_factory
        #: execution_key -> (step_execution_id, runner_id). The cancel surface
        #: is keyed the way LocalExecutor's is, so `pipeline_executor`'s
        #: deadline path calls `cancel_step(execution_key)` unchanged.
        self._in_flight: dict[str, tuple[str, str]] = {}

    # -- collaborators (late-bound so a test can build one with no app) ------

    @property
    def registry(self):
        if self._registry is None:
            from app.services.execution.runner_registry import runner_registry

            self._registry = runner_registry
        return self._registry

    @property
    def dispatcher(self):
        if self._dispatcher is None:
            from app.services.execution.runner_dispatcher import runner_dispatcher

            self._dispatcher = runner_dispatcher
        return self._dispatcher

    @property
    def recovery(self):
        if self._recovery is None:
            from app.services.execution.job_recovery import get_job_recovery_service

            self._recovery = get_job_recovery_service()
        return self._recovery

    def _session(self):
        """A SHORT-LIVED session. The generator never holds one across a
        wait: a step can run for thirty minutes, and a pooled connection
        parked for that long is a pool exhaustion waiting to happen.

        The fallback is resolved on EVERY call rather than cached, so a
        caller that rebinds `app.database.async_session` (a test harness
        pointing at a temp database) is honored instead of being pinned to
        whatever was installed the first time this executor ran.
        """
        if self._session_factory is not None:
            return self._session_factory()
        from app.database import async_session

        return async_session()

    # -- LocalExecutor interface parity --------------------------------------

    def reset(self) -> None:
        """Test-mode reset hook (R6)."""
        self._in_flight.clear()

    async def image_supports_control_layer(self, image: str) -> bool:
        """Always True: the image lives on the RUNNER's host, not here.

        The backend cannot inspect a label on a daemon it does not talk to,
        and returning False (the honest "I could not check") would silently
        downgrade every remote step to stdout mode - which is exactly the
        kind of silent degradation R1 forbids. The agent's `preflight()`
        reports missing images at registration, and a step whose image is
        absent fails with the same `Image not found: <tag>` message the local
        path produces.
        """
        return True

    async def image_declares_label(self, image: str, label: str) -> bool | None:
        """None = "could not inspect". See `image_supports_control_layer`."""
        return None

    async def find_missing_images(self, images) -> list[str]:
        """Empty: image presence is the runner host's fact, checked there."""
        return []

    # -- the contract ---------------------------------------------------------

    async def execute_step(
        self,
        step_config: dict,
        execution_context: dict,
    ) -> AsyncGenerator[dict, None]:
        """Execute one step on a matching runner. See the module docstring.

        `execution_context` additions over the local path:
            step_execution_id   REQUIRED - the row the CAS assigns and the
                                step JWT authenticates against.
            runner_requirements the parsed `requires:` block (section 2.4).
            remote_config       the fully-built `execute_step.config` from
                                `pipeline_executor._build_remote_execution_config`.
                                When absent it is built here from
                                `build_execute_step_config` - the SOLE
                                producer either way (cross-agent contract #2).
        """
        execution_key = execution_context.get("execution_key", "")
        step_execution_id = execution_context.get("step_execution_id")
        requirements = dict(execution_context.get("runner_requirements") or {})
        timeout = step_config.get("timeout", 300)

        yield {"type": "status", "status": "preparing"}

        if not step_execution_id:
            # Fail loudly rather than guessing: without a StepExecution row
            # there is nothing to compare-and-swap, nothing for the step JWT
            # to authenticate, and no way for the container to report home.
            async for event in self._terminal(
                {
                    "type": "result",
                    "status": "failed",
                    "exit_code": None,
                    "error": (
                        "remote execution requires execution_context"
                        "['step_execution_id']; the dispatcher creates the "
                        "StepExecution row before routing a step remote"
                    ),
                }
            ):
                yield event
            return

        try:
            config = self._build_config(step_config, execution_context)
        except Exception as exc:
            logger.exception("failed to build execute_step config")
            async for event in self._terminal(
                {
                    "type": "result",
                    "status": "failed",
                    "exit_code": None,
                    "error": f"step config error: {exc}",
                }
            ):
                yield event
            return

        attempt = 0
        last_runner_id: str | None = None
        final_result: dict | None = None
        running_yielded = False
        runner_reported = False

        while attempt < MAX_ASSIGN_ATTEMPTS:
            attempt += 1

            await self._arm_step(step_execution_id, requirements)

            try:
                runner = await self.dispatcher.acquire(
                    step_execution_id, requirements, timeout=NO_RUNNER_TIMEOUT
                )
            except Exception as exc:
                # NoRunnerAvailable carries the requirements AND every
                # connected runner's labels - the only two facts an operator
                # can diff to find the typo.
                logger.error("step %s could not be assigned: %s", step_execution_id, exc)
                final_result = {
                    "type": "result",
                    "status": "failed",
                    "exit_code": None,
                    "error": str(exc),
                }
                break

            last_runner_id = runner.id
            self._in_flight[execution_key] = (step_execution_id, runner.id)
            assignment = self.dispatcher.register_assignment(
                step_execution_id, runner.id
            )
            execution_context["runner_id"] = runner.id

            outcome = None
            try:
                sent = await self.registry.send(
                    runner.id,
                    ExecuteStepMessage(
                        step_id=step_execution_id,
                        execution_key=execution_key,
                        config=config,
                    ),
                )
                if not sent:
                    logger.warning(
                        "execute_step to runner %s did not land; requeueing step %s",
                        runner.id,
                        step_execution_id,
                    )
                    await self._fail_runner(
                        runner.id, step_execution_id, "send failed"
                    )
                    continue

                try:
                    await asyncio.wait_for(assignment.ack, timeout=ACK_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(
                        "runner %s did not ACK step %s within %ss; reassigning",
                        runner.id,
                        step_execution_id,
                        ACK_TIMEOUT,
                    )
                    await self._fail_runner(
                        runner.id, step_execution_id, "ACK timeout"
                    )
                    continue

                await self._mark_busy(runner.id, step_execution_id)
                if not running_yielded:
                    yield {"type": "status", "status": "running"}
                    running_yielded = True

                outcome = await self._await_terminal(
                    assignment, timeout + REMOTE_STEP_TIMEOUT_GRACE
                )
            finally:
                self.dispatcher.release_assignment(step_execution_id, runner.id)
                self._in_flight.pop(execution_key, None)

            if outcome is None:
                # Backstop fired: the runner stopped speaking about this step
                # without dying. Cancel on the wire, then fail it here.
                await self._cancel_on_wire(
                    runner.id, step_execution_id, "executor backstop deadline"
                )
                await self._close_out(runner.id, step_execution_id)
                final_result = {
                    "type": "result",
                    "status": "timeout",
                    "exit_code": None,
                    "timeout_seconds": timeout,
                }
                break

            if outcome.requeued:
                # A mid-step death yields NO intermediate result event - the
                # loop simply tries again. This is the whole reason the
                # pipeline executor can stay ignorant of remoteness.
                logger.warning(
                    "step %s was requeued (%s); attempt %d of %d",
                    step_execution_id,
                    outcome.reason,
                    attempt,
                    MAX_ASSIGN_ATTEMPTS,
                )
                continue

            await self._close_out(runner.id, step_execution_id)
            final_result = self._result_from_outcome(outcome, timeout)
            runner_reported = True
            break

        if final_result is None:
            final_result = {
                "type": "result",
                "status": "failed",
                "exit_code": None,
                "error": (
                    f"step was requeued {MAX_ASSIGN_ATTEMPTS} times; "
                    f"last runner: {last_runner_id}"
                ),
            }

        async for event in self._terminal(
            final_result, step_execution_id, force_terminal=not runner_reported
        ):
            yield event

    # -- terminal tail --------------------------------------------------------

    async def _terminal(
        self,
        final_result: dict,
        step_execution_id: str | None = None,
        *,
        force_terminal: bool = True,
    ) -> AsyncGenerator[dict, None]:
        """ONE exit path, mirroring LocalExecutor's terminal tail.

        The StepExecution row is forced terminal ONLY for failures this
        executor determined (no runner matched, the attempt budget ran out,
        the backstop deadline fired, a config error) - otherwise a leaked
        step token could keep writing to a step nobody is watching.

        A runner-REPORTED outcome leaves the row exactly as it is
        (`force_terminal=False`). The control runtime's own POST /status is
        what `_reconcile_control_execution` reads, and a row still sitting at
        ASSIGNED after a `step_complete` is the signal "the container never
        ran a working control runtime" - a real safety property that must not
        be papered over from out here.
        """
        if force_terminal and step_execution_id:
            await self._force_terminal(step_execution_id, final_result)
        yield {"type": "status", "status": final_result["status"]}
        yield final_result

    def _result_from_outcome(self, outcome, timeout: int) -> dict:
        """Map a runner's `step_complete` to the LocalExecutor result shape.

        Exit code is ground truth for step outcome in both modes: it is the
        remote analogue of `container.wait()`. The happy path carries exactly
        {type, status, exit_code} - no `error` key - because that is what
        LocalExecutor emits and the parity test compares KEY SETS.
        """
        exit_code = outcome.exit_code
        if outcome.cancelled:
            return {
                "type": "result",
                "status": "failed",
                "exit_code": exit_code,
                "error": outcome.error or f"cancelled: {outcome.reason}",
            }
        if exit_code == 0 and not outcome.error:
            return {"type": "result", "status": "completed", "exit_code": 0}
        result = {
            "type": "result",
            "status": "failed",
            "exit_code": exit_code,
        }
        if outcome.error:
            result["error"] = outcome.error
        return result

    # -- assignment plumbing --------------------------------------------------

    def _build_config(self, step_config: dict, execution_context: dict) -> dict:
        """The `execute_step.config` payload.

        Prefers the one `pipeline_executor` already built (cross-agent
        contract #2 names it the caller's job, because only it has the
        `generate_step_config` / `generate_agent_config` output). Falls back
        to the same sole producer so this executor is drivable standalone -
        never to a second, divergent builder.
        """
        prebuilt = execution_context.get("remote_config")
        if prebuilt:
            return prebuilt
        return build_execute_step_config(
            step_config,
            execution_context,
            execution_context.get("step_config_file"),
            execution_context.get("agent_config_file"),
        )

    async def _await_terminal(self, assignment, deadline: float):
        """Wait for `step_complete`, a requeue, or the backstop. None = backstop."""
        try:
            return await asyncio.wait_for(assignment.terminal, timeout=deadline)
        except asyncio.TimeoutError:
            return None

    async def _arm_step(self, step_execution_id: str, requirements: dict) -> None:
        """Put the row in the state the CAS can win: PENDING, unassigned.

        Also persists `runner_requirements`, which is what makes a requeued
        step re-matchable after a backend restart - the requirements cannot
        live only in this generator's closure, because this generator is
        exactly what a restart destroys.
        """
        async with self._session() as db:
            execution = (
                await db.execute(
                    select(StepExecution).where(StepExecution.id == step_execution_id)
                )
            ).scalar_one_or_none()
            if execution is None:
                logger.warning(
                    "cannot arm step %s for dispatch: row not found", step_execution_id
                )
                return
            execution.runner_requirements = json.dumps(requirements, sort_keys=True)
            execution.status = StepExecutionStatus.PENDING.value
            execution.runner_id = None
            execution.assigned_at = None
            await db.commit()

    async def _mark_busy(self, runner_id: str, step_execution_id: str) -> None:
        """ASSIGNED -> BUSY on ACK. Owned here, not by the WS endpoint.

        Every transition for one assignment lives in one place; the endpoint
        gates the frame and hands it to the dispatcher, and nothing else.
        """
        async with self._session() as db:
            try:
                await self.registry.transition(
                    db,
                    runner_id,
                    RunnerState.BUSY,
                    reason=f"executing step {step_execution_id}",
                )
            except (InvalidRunnerTransitionError, KeyError) as exc:
                logger.warning(
                    "runner %s could not enter BUSY for step %s: %s",
                    runner_id,
                    step_execution_id,
                    exc,
                )

    async def _fail_runner(
        self, runner_id: str, step_execution_id: str, reason: str
    ) -> None:
        """A runner that did not accept its assignment is dead to us.

        Marks it DEAD (the machine allows ASSIGNED -> DEAD), then runs the
        recovery requeue so the step returns to `pending` and the dispatcher
        can hand it elsewhere. A late ACK from the same runner is dropped by
        the step gate, so no special handling is needed on the wire.
        """
        async with self._session() as db:
            try:
                await self.registry.transition(
                    db, runner_id, RunnerState.DEAD, reason=reason
                )
            except (InvalidRunnerTransitionError, KeyError) as exc:
                logger.info("runner %s could not be marked dead: %s", runner_id, exc)

            from app.models.runner import Runner

            runner = (
                await db.execute(select(Runner).where(Runner.id == runner_id))
            ).scalar_one_or_none()
            if runner is not None:
                await self.recovery.on_runner_death(db, runner)

        # The runner may still be alive and holding a container. Tell it to
        # stop; the step gate makes any reply it sends inert.
        await self._cancel_on_wire(runner_id, step_execution_id, reason)

    async def _cancel_on_wire(
        self, runner_id: str, step_execution_id: str, reason: str
    ) -> None:
        try:
            await self.registry.send(
                runner_id,
                CancelStepMessage(step_id=step_execution_id, reason=reason),
            )
        except Exception:
            logger.debug(
                "cancel_step to runner %s could not be sent", runner_id, exc_info=True
            )

    async def _close_out(self, runner_id: str, step_execution_id: str) -> None:
        """Return the runner to IDLE and clear its step pointer."""
        async with self._session() as db:
            await self.dispatcher.release_runner(
                db, runner_id, step_execution_id, reason="step complete"
            )

    async def _force_terminal(self, step_execution_id: str, final_result: dict) -> None:
        """Mark a row terminal for a failure the EXECUTOR determined.

        Never called on success: see `_terminal`.
        """
        status_map = {
            "timeout": StepExecutionStatus.TIMEOUT.value,
            "cancelled": StepExecutionStatus.CANCELLED.value,
        }
        target = status_map.get(
            final_result.get("status", ""), StepExecutionStatus.FAILED.value
        )
        try:
            async with self._session() as db:
                execution = (
                    await db.execute(
                        select(StepExecution).where(
                            StepExecution.id == step_execution_id
                        )
                    )
                ).scalar_one_or_none()
                if execution is None:
                    return
                if execution.status in (
                    StepExecutionStatus.COMPLETED.value,
                    StepExecutionStatus.FAILED.value,
                    StepExecutionStatus.CANCELLED.value,
                    StepExecutionStatus.TIMEOUT.value,
                ):
                    return
                execution.status = target
                if final_result.get("error") and not execution.error:
                    execution.error = final_result["error"]
                if execution.completed_at is None:
                    execution.completed_at = datetime.utcnow()
                await db.commit()
        except Exception:
            logger.exception(
                "could not force step execution %s terminal", step_execution_id
            )

    # -- cancellation ---------------------------------------------------------

    async def cancel_step(self, execution_key: str) -> bool:
        """Cancel a running remote step. Mirrors LocalExecutor.cancel_step.

        Sends `cancel_step` and waits up to DRAIN_GRACE for the runner's own
        `step_complete`. If none arrives the assignment is forced terminal
        here and the death monitor reaps the runner - the step must not stay
        owned by a machine that has stopped answering.
        """
        in_flight = self._in_flight.get(execution_key)
        if not in_flight:
            return False
        step_execution_id, runner_id = in_flight

        await self._cancel_on_wire(runner_id, step_execution_id, "cancelled by operator")

        assignment = self.dispatcher.assignment(step_execution_id, runner_id)
        if assignment is None:
            return True
        try:
            await asyncio.wait_for(assignment.terminal, timeout=DRAIN_GRACE)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "runner %s did not report completion for cancelled step %s "
                "within %ss; forcing terminal",
                runner_id,
                step_execution_id,
                DRAIN_GRACE,
            )
            from app.services.execution.runner_dispatcher import AssignmentOutcome

            if not assignment.terminal.done():
                assignment.terminal.set_result(
                    AssignmentOutcome(
                        exit_code=None,
                        error="cancelled: runner did not confirm",
                        cancelled=True,
                        reason="cancelled",
                    )
                )
            return True
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:
            logger.exception("cancel of remote step %s failed", step_execution_id)
            return False

    async def cancel_all(self) -> int:
        """Cancel every in-flight remote step. Never raises."""
        cancelled = 0
        for execution_key in list(self._in_flight):
            try:
                if await self.cancel_step(execution_key):
                    cancelled += 1
            except Exception:
                logger.warning(
                    "cancel_all: failed to cancel %s", execution_key, exc_info=True
                )
        return cancelled


__all__ = ["RemoteExecutor", "REMOTE_STEP_TIMEOUT_GRACE"]
