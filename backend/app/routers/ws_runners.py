"""The runner WebSocket endpoint - Phase 12.6 (`/ws/runner`).

This is the ONLY socket a runner agent speaks. It carries runner and
assignment concerns and nothing else (section 1.6): the step container keeps
POSTing its own status / logs / heartbeat / test-results / usage to
`/api/steps/*` with the step JWT, exactly as on the local path, because that
token is location-independent. Reimplementing those five channels over this
socket would be a second ingestion path for two channels that 12.2.6 and
12.5 just single-sourced.

Four properties this module exists to hold:

1. **Auth happens at the HTTP upgrade, before `accept()`.** The contract
   suite forbids a required `token` field on `register`, so auth is a
   transport concern. A socket that fails the header/query check is closed
   without ever being accepted - the failure is visible in the handshake
   rather than one frame later.

2. **One DB session per MESSAGE, never one per connection.** A runner
   connection is a multi-HOUR object; a session held across it is a pooled
   connection pinned for hours and a transaction snapshot that ages into
   nonsense. Every handler opens its own session and closes it.

3. **The step gate (cross-agent contract #7).** Every step-scoped inbound
   frame - `ack`, `log`, `step_complete` - is dropped with a WARN unless
   BOTH `step_execution.runner_id == connection.runner_id` AND
   `runner.websocket_id == connection.websocket_id`. A late ACK from a
   runner already declared dead, a `step_complete` from a superseded
   connection, a `log` for a step that moved on: all become inert facts
   instead of corrupting state. This one rule closes the
   reconnect-vs-reassign race.

4. **The `finally:` never raises.** failure_01 threw
   `InvalidRunnerTransitionError` out of exactly this path, which skipped
   BOTH the DB update and the requeue - the single most expensive bug in
   that implementation.

Log routing: the `log` frame carries RUNNER-origin lines only - the lines a
step container cannot emit because it does not exist yet or failed to start
("[runner] pulling lazyaf-test-runner:dev", "[runner] ERROR: docker daemon
unreachable"). They are written through `append_step_logs(source="runner")`,
the SAME writer the `/logs` router uses (cross-agent contract #6), so there
is one writer of `StepRun.logs` and two callers. The agent emits them only
BEFORE `container.start()` and AFTER the container exits, so the two streams
cannot overlap in time and append order is real order.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.config import get_settings
from app.models.pipeline import StepExecution
from app.models.runner import Runner
from app.services.execution import runner_protocol as protocol
from app.services.execution.job_recovery import get_job_recovery_service
from app.services.execution.runner_dispatcher import get_runner_dispatcher
from app.services.execution.runner_protocol import (
    CancelStepMessage,
    ErrorMessage,
    PingMessage,
    PongMessage,
    RegisteredMessage,
    is_supported_protocol_version,
    parse_runner_message,
    unsupported_version_message,
    validate_runner_message,
)
from app.services.execution.runner_registry import (
    DuplicateRunnerConnection,
    get_runner_registry,
)
from app.services.execution.runner_state import RunnerState
from app.services.execution.step_logs import SOURCE_RUNNER, append_step_logs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runners"])

#: Query parameter carrying the shared enrollment secret, for clients that
#: cannot set headers on a WebSocket handshake (browsers, mostly).
TOKEN_QUERY_PARAM = "token"


def get_runner_session_factory():
    """The async_sessionmaker every handler opens its OWN session from.

    A dependency rather than a module import so tests can bind the endpoint
    to their own engine. It returns the FACTORY, not a session: the whole
    point is that no session outlives a single message.
    """
    from app.database import async_session

    return async_session


@dataclass
class RunnerPrincipal:
    """What the handshake proves. Deliberately thin.

    A shared enrollment secret does NOT bind an identity, so `runner_id` is
    client-asserted and the real guards live elsewhere: duplicate-connection
    rejection (4004) and the step gate. Per-runner JWTs are the named
    upgrade path (`runner_token.py` ships with tests and no default-path
    caller); enabling them changes this function and nothing else.
    """

    runner_id: str | None = None
    scope: str = "enroll"
    #: True when neither the header nor the query carried a secret, so the
    #: last-resort `register.token` channel must supply one.
    deferred: bool = False


def _presented_secret(websocket: WebSocket) -> str | None:
    """The secret offered at the upgrade, header first, then query."""
    header = websocket.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    query = websocket.query_params.get(TOKEN_QUERY_PARAM)
    if query:
        return query
    return None


def verify_runner_secret(presented: str | None) -> bool:
    """Constant-time comparison against `settings.runner_auth_secret`."""
    if not presented:
        return False
    expected = get_settings().runner_auth_secret
    return hmac.compare_digest(str(presented), str(expected))


async def authenticate_runner_connection(websocket: WebSocket) -> RunnerPrincipal | None:
    """Authenticate the upgrade. None means "refuse, do not accept".

    Order (section 1.3): `Authorization: Bearer` header, then `?token=`,
    then - and only when neither is present - `register.token`, which the
    caller checks once the first frame arrives.
    """
    presented = _presented_secret(websocket)
    if presented is None:
        return RunnerPrincipal(deferred=True)
    if not verify_runner_secret(presented):
        return None
    return RunnerPrincipal()


class RunnerConnection:
    """One runner socket, from upgrade to teardown."""

    def __init__(self, websocket: WebSocket, session_factory) -> None:
        self.websocket = websocket
        self.session_factory = session_factory
        self.registry = get_runner_registry()
        self.dispatcher = get_runner_dispatcher()
        self.recovery = get_job_recovery_service()
        self.runner_id: str | None = None
        self.websocket_id: str | None = None
        self._closed = False
        self._window_start = time.monotonic()
        self._window_count = 0

    # -- transport ------------------------------------------------------------

    async def send(self, message) -> bool:
        """Send one backend frame. Never raises - a dead socket is not an
        error the receive loop has to handle twice."""
        try:
            await self.websocket.send_text(json.dumps(message.to_dict()))
            return True
        except Exception:
            logger.debug(
                "send %s to runner %s failed (socket gone)",
                getattr(message, "type", "?"),
                self.runner_id,
                exc_info=True,
            )
            return False

    async def close(self, code: int, reason: str = "") -> None:
        """Close once, ever. Both the watchdog and the receive loop can
        reach here and a double close raises out of starlette."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            logger.debug("closing runner socket %s failed", self.runner_id, exc_info=True)

    async def refuse(self, code: int, message: str, error_code: str) -> None:
        """Send a FATAL error frame, then close. Post-accept only."""
        await self.send(ErrorMessage(message=message, code=error_code, fatal=True))
        await self.close(code, reason=error_code)

    # -- back-pressure (section 1.7) -----------------------------------------

    def _over_budget(self) -> bool:
        """More than INBOUND_BUDGET_MESSAGES in a rolling window.

        A runner that floods loses its work (its step is requeued by the
        ordinary disconnect path) rather than the backend losing its event
        loop.
        """
        now = time.monotonic()
        if now - self._window_start >= protocol.INBOUND_BUDGET_WINDOW:
            self._window_start = now
            self._window_count = 0
        self._window_count += 1
        return self._window_count > protocol.INBOUND_BUDGET_MESSAGES

    # -- the step gate (cross-agent contract #7) ------------------------------

    async def gated_execution(self, db, step_id: str, frame: str):
        """The StepExecution this connection is allowed to speak about.

        None means "drop this frame". Both halves of the fence are checked:
        the step must be assigned to THIS runner, and the runner row's
        `websocket_id` must be THIS connection's. The second half is what
        makes a superseded socket's frames inert even though it carries the
        right runner_id.
        """
        if not step_id or not self.runner_id:
            return None

        execution = (
            await db.execute(
                select(StepExecution).where(StepExecution.id == step_id)
            )
        ).scalar_one_or_none()
        if execution is None:
            logger.warning(
                "dropping %s from runner %s: step execution %s does not exist",
                frame,
                self.runner_id,
                step_id,
            )
            return None
        if execution.runner_id != self.runner_id:
            logger.warning(
                "dropping %s for step %s from runner %s: the step belongs to %r",
                frame,
                step_id,
                self.runner_id,
                execution.runner_id,
            )
            return None

        runner = (
            await db.execute(select(Runner).where(Runner.id == self.runner_id))
        ).scalar_one_or_none()
        if runner is None or runner.websocket_id != self.websocket_id:
            logger.warning(
                "dropping %s for step %s from a superseded connection of runner "
                "%s (socket %s, current %s)",
                frame,
                step_id,
                self.runner_id,
                self.websocket_id,
                getattr(runner, "websocket_id", None),
            )
            return None
        return execution

    # -- registration ---------------------------------------------------------

    async def _read_frame(self, timeout: float) -> str | None:
        """One text frame, or None on timeout. WebSocketDisconnect escapes."""
        try:
            return await asyncio.wait_for(
                self.websocket.receive_text(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    async def register(self, principal: RunnerPrincipal) -> bool:
        """Drive the registration handshake. False means the socket is closed."""
        try:
            raw = await self._read_frame(protocol.REGISTRATION_TIMEOUT)
        except WebSocketDisconnect:
            return False
        if raw is None:
            await self.refuse(
                protocol.CLOSE_REGISTRATION_TIMEOUT,
                f"no register frame within {protocol.REGISTRATION_TIMEOUT}s",
                "registration_timeout",
            )
            return False

        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            await self.refuse(
                protocol.CLOSE_INVALID_REGISTRATION,
                f"register frame is not valid JSON: {exc}",
                "invalid_registration",
            )
            return False
        if not isinstance(data, dict):
            await self.refuse(
                protocol.CLOSE_INVALID_REGISTRATION,
                "register frame must be a JSON object",
                "invalid_registration",
            )
            return False

        errors = validate_runner_message(data)
        if errors:
            await self.refuse(
                protocol.CLOSE_INVALID_REGISTRATION,
                "; ".join(errors),
                "invalid_registration",
            )
            return False
        if data.get("type") != "register":
            await self.refuse(
                protocol.CLOSE_INVALID_REGISTRATION,
                f"expected a register frame, got {data.get('type')!r}",
                "invalid_registration",
            )
            return False

        # Last-resort auth channel, used ONLY when the handshake carried no
        # secret at all. This one necessarily happens after accept(): a
        # WebSocket cannot carry an application frame before it is accepted.
        if principal.deferred and not verify_runner_secret(data.get("token")):
            await self.refuse(
                protocol.CLOSE_AUTH_FAILED, "authentication failed", "auth"
            )
            return False

        offered = data.get("protocol_version")
        if not is_supported_protocol_version(offered):
            await self.refuse(
                protocol.CLOSE_UNSUPPORTED_VERSION,
                unsupported_version_message(offered),
                "protocol_version",
            )
            return False

        register = parse_runner_message(data)

        async with self.session_factory() as db:
            try:
                runner = await self.registry.connect(db, self.websocket, register)
            except DuplicateRunnerConnection as exc:
                # The INCUMBENT wins and is left untouched. First connection
                # wins is what stops a second process - honest restart or
                # not - from silently stealing an in-flight assignment.
                await self.refuse(
                    protocol.CLOSE_DUPLICATE_CONNECTION,
                    str(exc),
                    "duplicate_connection",
                )
                return False

            # Only NOW does this connection own a runner id: everything
            # before this point must not run the teardown path, or a refused
            # impostor would disconnect the incumbent.
            self.runner_id = runner.id
            self.websocket_id = runner.websocket_id
            resume = await self.recovery.on_runner_reconnect(db, runner)

        action = resume.get("action", protocol.RESUME_IDLE)
        resume_step_id = resume.get("step_id")
        await self.send(
            RegisteredMessage(
                runner_id=self.runner_id,
                resume_action=action,
                resume_step_id=resume_step_id,
            )
        )
        if action == protocol.RESUME_ABORT and resume_step_id:
            # The abort half of the reconnect protocol: the step was
            # reassigned while this runner was away, so its in-flight
            # container must die rather than race the new owner.
            await self.send(
                CancelStepMessage(step_id=resume_step_id, reason="reassigned")
            )
        elif action == protocol.RESUME_CONTINUE and resume_step_id:
            await self._resume_busy(resume_step_id)
        return True

    async def _resume_busy(self, step_id: str) -> None:
        """A reconnecting runner that still owns its step is BUSY, not IDLE.

        `connect()` always walks a fresh machine DISCONNECTED -> CONNECTING
        -> IDLE, which is right for the machine's history and wrong for what
        this runner is actually doing. Walking it on to BUSY costs two
        transitions and buys three things: a truthful panel, a legal
        IDLE-transition for `dispatcher.release_runner` when the step
        finishes, and one less way for `find_available` to be the only thing
        standing between a busy runner and a second assignment.

        Never raises: a failed cosmetic transition must not fail a
        registration that already succeeded.
        """
        try:
            async with self.session_factory() as db:
                await self.registry.transition(
                    db, self.runner_id, RunnerState.ASSIGNED, reason="resume"
                )
                await self.registry.transition(
                    db, self.runner_id, RunnerState.BUSY, reason="resume"
                )
        except Exception:
            logger.warning(
                "could not resume runner %s to busy for step %s; the "
                "current_step_execution_id fence still stops a second "
                "assignment",
                self.runner_id,
                step_id,
                exc_info=True,
            )

    # -- the receive loop -----------------------------------------------------

    async def serve(self) -> None:
        """Read frames until the socket dies. Never raises."""
        while True:
            try:
                raw = await self._read_frame(protocol.RECEIVE_TIMEOUT)
            except WebSocketDisconnect:
                return
            except RuntimeError:
                # starlette raises this once the socket is already gone.
                return
            if raw is None:
                # A read timeout is a KEEPALIVE, never a death verdict:
                # RECEIVE_TIMEOUT < DEATH_TIMEOUT is deliberate and the
                # watchdog is the sole authority on death.
                if not await self.send(PingMessage()):
                    return
                continue

            if len(raw.encode("utf-8", "ignore")) > protocol.MAX_MESSAGE_BYTES:
                # DROPPED, never closed: one huge line must not kill a live
                # step.
                await self.send(
                    ErrorMessage(
                        message=(
                            f"frame exceeds {protocol.MAX_MESSAGE_BYTES} bytes "
                            "and was dropped"
                        ),
                        code="too_large",
                    )
                )
                continue

            if self._over_budget():
                await self.refuse(
                    protocol.CLOSE_BACK_PRESSURE,
                    (
                        f"more than {protocol.INBOUND_BUDGET_MESSAGES} frames in "
                        f"{protocol.INBOUND_BUDGET_WINDOW}s"
                    ),
                    "rate",
                )
                return

            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                await self._bad_frame(f"frame is not valid JSON: {exc}")
                continue
            if not isinstance(data, dict):
                await self._bad_frame("frame must be a JSON object")
                continue

            errors = validate_runner_message(data)
            if errors:
                # Mid-session: an error frame, and the connection STAYS
                # OPEN. One malformed frame must never kill a live step.
                await self._bad_frame("; ".join(errors))
                continue

            try:
                await self.handle(parse_runner_message(data))
            except Exception:
                logger.exception(
                    "handling %s from runner %s failed; connection kept",
                    data.get("type"),
                    self.runner_id,
                )
                await self._bad_frame(f"handler failed for {data.get('type')!r}")

    async def _bad_frame(self, message: str) -> None:
        await self.send(ErrorMessage(message=message, code="invalid_message"))
        logger.warning("runner %s sent a bad frame: %s", self.runner_id, message)

    async def handle(self, message) -> None:
        """Dispatch one validated frame. Each handler owns its OWN session."""
        kind = message.type

        if kind == "heartbeat":
            async with self.session_factory() as db:
                await self.registry.heartbeat(db, self.runner_id)
            await self.send(PongMessage())
            return

        if kind == "ping":
            await self.send(PongMessage())
            return

        if kind == "ack":
            async with self.session_factory() as db:
                execution = await self.gated_execution(db, message.step_id, "ack")
            if execution is None:
                return
            self.dispatcher.notify_ack(message.step_id, self.runner_id)
            return

        if kind == "log":
            async with self.session_factory() as db:
                execution = await self.gated_execution(db, message.step_id, "log")
                if execution is None:
                    return
                lines = _clamp_log_lines(message.lines)
                if not lines:
                    return
                await append_step_logs(db, execution, lines, source=SOURCE_RUNNER)
            return

        if kind == "step_complete":
            async with self.session_factory() as db:
                execution = await self.gated_execution(
                    db, message.step_id, "step_complete"
                )
            if execution is None:
                return
            self.dispatcher.notify_complete(
                message.step_id,
                self.runner_id,
                message.exit_code,
                message.error,
            )
            return

        # `register` is only legal as the first frame; anything else that
        # parsed is a protocol error the connection survives.
        await self._bad_frame(f"unexpected {kind!r} frame mid-session")

    # -- the death watchdog (section 2.7) -------------------------------------

    async def watch_for_death(self) -> None:
        """Mark a silent runner DEAD, requeue its step, close the socket.

        Runs for IDLE connections too. That is exactly why `runner_state.py`
        carries IDLE -> DEAD: a runner that connects and then silently
        vanishes must not sit `idle` forever collecting assignments
        (failure_01 death-checked only ASSIGNED/BUSY).

        Every timeout is evaluated on BACKEND time - `last_heartbeat` is
        stamped at receipt in the registry, never taken from the wire - so a
        runner whose clock is hours off cannot make itself immortal.
        """
        while True:
            await asyncio.sleep(protocol.DEATH_MONITOR_INTERVAL)
            if self.runner_id is None:
                continue
            machine = self.registry.machine(self.runner_id)
            if machine is None:
                return  # the connection is already torn down
            if machine.is_alive(protocol.DEATH_TIMEOUT):
                continue

            logger.warning(
                "runner %s missed heartbeats for %ss: marking dead",
                self.runner_id,
                protocol.DEATH_TIMEOUT,
            )
            async with self.session_factory() as db:
                try:
                    await self.registry.transition(
                        db,
                        self.runner_id,
                        RunnerState.DEAD,
                        reason="heartbeat timeout",
                    )
                except Exception:
                    logger.exception(
                        "could not transition runner %s to dead", self.runner_id
                    )
                runner = (
                    await db.execute(
                        select(Runner).where(Runner.id == self.runner_id)
                    )
                ).scalar_one_or_none()
                if runner is not None:
                    await self.recovery.on_runner_death(db, runner)
            await self.close(protocol.CLOSE_NORMAL, reason="heartbeat timeout")
            return

    # -- teardown -------------------------------------------------------------

    async def teardown(self) -> None:
        """Drop the connection and requeue whatever it was holding.

        NEVER raises. This runs from the endpoint's `finally:`, and
        failure_01 threw `InvalidRunnerTransitionError` out of exactly here -
        skipping both the DB update and the requeue.
        """
        if self.runner_id is None:
            return
        try:
            async with self.session_factory() as db:
                await self.registry.disconnect(db, self.runner_id, self.websocket_id)
                runner = (
                    await db.execute(
                        select(Runner).where(Runner.id == self.runner_id)
                    )
                ).scalar_one_or_none()
                if runner is not None:
                    await self.recovery.on_runner_disconnect(db, runner)
        except Exception:
            logger.exception("runner %s teardown failed", self.runner_id)


def _clamp_log_lines(lines) -> list[str]:
    """Apply the per-frame line and per-line byte budgets (section 1.7)."""
    clamped: list[str] = []
    for line in list(lines or [])[: protocol.MAX_LOG_LINES_PER_MESSAGE]:
        text = str(line)
        if len(text.encode("utf-8", "ignore")) > protocol.MAX_LOG_LINE_BYTES:
            text = text[: protocol.MAX_LOG_LINE_BYTES] + "...[truncated]"
        clamped.append(text)
    return clamped


@router.websocket("/ws/runner")
async def runner_websocket(
    websocket: WebSocket,
    session_factory=Depends(get_runner_session_factory),
) -> None:
    """The runner socket. See the module docstring for what it carries."""
    principal = await authenticate_runner_connection(websocket)
    if principal is None:
        # Refused BEFORE accept(), so the failure is visible in the
        # handshake rather than one frame later. The tradeoff, stated: a
        # WebSocket cannot carry an application frame before it is accepted,
        # so the `error{code:"auth"}` body the spec sketches travels in the
        # close REASON instead. Making it a frame would require accepting an
        # unauthenticated socket first, which section 1.3 forbids outright.
        await websocket.close(code=protocol.CLOSE_AUTH_FAILED, reason="auth")
        return

    await websocket.accept()
    connection = RunnerConnection(websocket, session_factory)
    watchdog: asyncio.Task | None = None
    try:
        if not await connection.register(principal):
            return
        watchdog = asyncio.create_task(
            connection.watch_for_death(), name=f"runner-death:{connection.runner_id}"
        )
        await connection.serve()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("runner socket %s crashed", connection.runner_id)
    finally:
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        # The requeue lives here, in the ONE place every exit path passes
        # through, and `teardown` swallows everything: failure_01 threw
        # InvalidRunnerTransitionError out of exactly this block, skipping
        # both the DB update and the requeue.
        await connection.teardown()
        await connection.close(protocol.CLOSE_NORMAL)


__all__ = [
    "router",
    "RunnerConnection",
    "RunnerPrincipal",
    "authenticate_runner_connection",
    "get_runner_session_factory",
    "verify_runner_secret",
]
