"""Message dispatch for one live connection - Phase 12.6, sections 1.7 and 4.3/4.4.

The session owns everything that happens between ``registered`` and the socket
closing: the receive loop, the heartbeat task, the bounded outbound log queue,
step execution, cancellation, the idempotency LRU and drain.

Three shape decisions, each of them a named failure_01 defect:

* **Step execution runs in ``asyncio.create_task``, never inline in the receive
  loop.** Inline execution meant ``cancel_step``, ``drain`` and ``ping`` were
  unserviceable for the entire duration of a step - the agent looked dead
  exactly when it was busiest.
* **Telemetry never blocks execution and never wedges the socket.** The log
  sink appends to a bounded deque and returns; a sender task drains it. On
  overflow the OLDEST lines go and one synthetic line reports the loss, the
  same hard rule the 12.3 log budget follows.
* **A second ``execute_step`` while busy is answered ``error{code:"busy"}`` and
  is NOT acked**, so the backend's ACK timeout reassigns it cleanly rather than
  the agent silently dropping or serializing it.

Log prefixing note: the agent sends RAW lines. The ``[runner] `` prefix is
applied backend-side by ``append_step_logs(source="runner")`` - the sole writer
of ``StepRun.logs`` for both channels (cross-agent contract #6). Prefixing here
too would double it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from typing import Protocol, Sequence

from .config import RunnerConfig
from .orchestrator.base import StepOrchestrator
from .types import StepAssignment, StepOutcome

logger = logging.getLogger(__name__)

#: Bounded outbound log buffer (section 1.7). Lines, not frames.
AGENT_OUTBOUND_QUEUE = 1000
#: Never more than this many lines in one `log` frame (mirrors the backend's
#: MAX_LOG_LINES_PER_MESSAGE; pinned by tests/test_control_archive_parity.py).
MAX_LOG_LINES_PER_MESSAGE = 500
#: Longer lines are truncated with a visible marker (mirrors the backend).
MAX_LOG_LINE_BYTES = 16_384
#: Hard ceiling on ONE frame, well under the backend's MAX_MESSAGE_BYTES
#: (1 MiB) so a full batch of maximum-length lines can never be refused.
MAX_LOG_FRAME_BYTES = 512 * 1024
#: Flush cadence for a partially-filled batch.
LOG_FLUSH_INTERVAL = 1.0
#: Last N execution_key -> outcome, for the reconnect-after-reassign case.
IDEMPOTENCY_CACHE_SIZE = 32
#: 12.6 has no two-step runner state (section 10); raising this is a design
#: change, not a constant bump.
MAX_CONCURRENT_STEPS = 1
#: Grace given to a running step on `drain` before the socket closes anyway.
DRAIN_GRACE = 30
#: Grace given to a running step when the SOCKET drops under it.
DISCONNECT_KILL_GRACE = 10

TRUNCATION_MARKER = "...[truncated]"

#: Exit code for an agent-side fault the container never got to express.
EXIT_AGENT_ERROR = 1
#: Cancellation (128 + SIGTERM), pinned by the spec (section 4.4).
EXIT_CANCELLED = 143


class TransportClosed(Exception):
    """The underlying socket is gone. Not an error: the client reconnects."""


class FatalProtocolError(Exception):
    """The backend refused us permanently (auth, unsupported version).

    Retrying is pointless and harmful: failure_01 reconnect-looped on a
    permanently-invalid registration every 5s forever. The client exits
    non-zero with the server's own message instead.
    """


class Transport(Protocol):
    """The two-and-a-half methods the session needs from a WebSocket."""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


def truncate_line(line: str) -> str:
    """Cap one log line at MAX_LOG_LINE_BYTES with a visible marker."""
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_LOG_LINE_BYTES:
        return line
    keep = MAX_LOG_LINE_BYTES - len(TRUNCATION_MARKER.encode("utf-8"))
    return encoded[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


class RunnerSession:
    """One connection's worth of behavior."""

    def __init__(
        self,
        config: RunnerConfig,
        orchestrator: StepOrchestrator,
        transport: Transport,
        registered: dict,
        *,
        log_flush_interval: float = LOG_FLUSH_INTERVAL,
        drain_grace: float = DRAIN_GRACE,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator
        self._transport = transport
        self._registered = registered or {}
        self._log_flush_interval = log_flush_interval
        self._drain_grace = drain_grace

        self._heartbeat_interval = float(self._registered.get("heartbeat_interval") or 10)

        self._closed = asyncio.Event()
        self._draining = False
        self._close_code = 1000
        self._close_reason = ""

        self._current: StepAssignment | None = None
        self._cancel: asyncio.Event | None = None
        self._cancel_reason: str = ""
        self._step_task: asyncio.Task | None = None
        #: The step we held when the socket dropped. `_current` is cleared
        #: as the step finishes, so it cannot answer "what were we doing"
        #: after teardown - which is exactly what `register.resume` needs.
        self._abandoned_step_id: str | None = None
        self._side_tasks: set[asyncio.Task] = set()

        self._results: "OrderedDict[str, StepOutcome]" = OrderedDict()

        self._log_buffer: deque = deque()
        self._log_dropped = 0
        self._log_seq = 0
        self._log_wake = asyncio.Event()
        self._log_step_id: str | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def current_step_id(self) -> str | None:
        """The assignment this session holds, if any."""
        return self._current.step_id if self._current is not None else None

    @property
    def resume_step_id(self) -> str | None:
        """What the NEXT `register.resume` should name.

        The live assignment if there is one, otherwise the assignment that was
        in flight when the socket dropped. Never a claim that the work survived
        - the container is killed on disconnect - only the id the backend needs
        to reconcile its own view.
        """
        if self._current is not None:
            return self._current.step_id
        return self._abandoned_step_id

    async def send(self, message: dict) -> None:
        """Put one frame on the wire. The session owns the socket, so anything
        that needs to speak on this connection goes through here rather than
        keeping a second reference to the transport."""
        await self._send(message)

    async def serve(self) -> None:
        """Run the connection until the socket closes or we drain.

        Raises :class:`FatalProtocolError` when the backend says never retry.
        """
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="lazyaf-heartbeat")
        sender = asyncio.create_task(self._log_sender_loop(), name="lazyaf-log-sender")
        try:
            await self._receive_loop()
        finally:
            self._closed.set()
            self._log_wake.set()
            for task in (heartbeat, sender):
                task.cancel()
            await asyncio.gather(heartbeat, sender, return_exceptions=True)
            await self._abandon_running_step()
            for task in list(self._side_tasks):
                task.cancel()
            if self._side_tasks:
                await asyncio.gather(*self._side_tasks, return_exceptions=True)
            try:
                await self._transport.close(self._close_code, self._close_reason)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------
    async def _receive_loop(self) -> None:
        while not self._closed.is_set():
            try:
                raw = await self._transport.recv()
            except TransportClosed:
                logger.info("Runner socket closed by peer")
                return
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("Discarding unparseable frame from backend")
                continue
            if not isinstance(message, dict):
                logger.warning("Discarding non-object frame from backend")
                continue
            await self._dispatch(message)

    async def _dispatch(self, message: dict) -> None:
        handler = {
            "execute_step": self._on_execute_step,
            "cancel_step": self._on_cancel_step,
            "cleanup_workspace": self._on_cleanup_workspace,
            "drain": self._on_drain,
            "ping": self._on_ping,
            "pong": self._on_pong,
            "error": self._on_error,
            "registered": self._on_late_registered,
        }.get(str(message.get("type") or ""))
        if handler is None:
            # A frame type we do not know is NOT fatal: the backend may speak a
            # newer optional message and the connection must survive it.
            logger.warning("Ignoring unknown backend message type %r", message.get("type"))
            return
        await handler(message)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    async def _on_execute_step(self, message: dict) -> None:
        step_id = str(message.get("step_id") or "")
        execution_key = str(message.get("execution_key") or "")
        config = message.get("config") or {}
        assignment = StepAssignment(step_id=step_id, execution_key=execution_key, config=config)

        if self._draining:
            await self._send(
                {
                    "type": "error",
                    "code": "draining",
                    "message": "runner is draining and is not accepting assignments",
                    "fatal": False,
                }
            )
            return

        if self._current is not None:
            # NOT acked on purpose: the backend's ACK timeout reassigns the
            # step to a free runner, which is the correct outcome. Acking and
            # then queueing would hide a scheduling bug behind extra latency.
            await self._send(
                {
                    "type": "error",
                    "code": "busy",
                    "message": (
                        f"runner is already executing step {self._current.step_id}; "
                        f"MAX_CONCURRENT_STEPS={MAX_CONCURRENT_STEPS}"
                    ),
                    "fatal": False,
                }
            )
            return

        # Idempotency (section 4.4): a reconnect-after-reassign, or a backend
        # restart re-dispatching the same execution_key, must not run the work
        # twice. ACK first so the backend does not reassign, then answer from
        # the cache without touching the orchestrator.
        cached = self._results.get(execution_key)
        if cached is not None:
            self._results.move_to_end(execution_key)
            logger.info(
                "execute_step %s repeats execution_key %s - answering from cache",
                step_id,
                execution_key,
            )
            await self._send({"type": "ack", "step_id": step_id})
            await self._send(
                {
                    "type": "step_complete",
                    "step_id": step_id,
                    "exit_code": cached.exit_code,
                    "error": cached.error,
                }
            )
            return

        logger.info("Assignment accepted: %s", assignment.redacted_summary())
        self._current = assignment
        self._cancel = asyncio.Event()
        self._cancel_reason = ""
        self._log_step_id = step_id
        await self._send({"type": "ack", "step_id": step_id})
        self._step_task = asyncio.create_task(
            self._run_step(assignment), name=f"lazyaf-step-{step_id}"
        )

    async def _on_cancel_step(self, message: dict) -> None:
        step_id = str(message.get("step_id") or "")
        reason = str(message.get("reason") or "")
        if self._current is None or self._current.step_id != step_id:
            logger.info("Ignoring cancel for step %s: not the assignment we hold", step_id)
            return
        logger.info("Cancelling step %s: %s", step_id, reason or "<no reason>")
        self._cancel_reason = reason
        if self._cancel is not None:
            self._cancel.set()
        cancel_running = getattr(self._orchestrator, "cancel_running", None)
        if cancel_running is not None:
            self._spawn(cancel_running(self._current.execution_key))

    async def _on_cleanup_workspace(self, message: dict) -> None:
        retain_key = str(message.get("retain_key") or "")
        if not retain_key:
            return
        # Off the receive loop: reaping a volume is a docker round trip and
        # the loop must keep serving cancel/drain while it happens.
        self._spawn(self._orchestrator.cleanup_workspace(retain_key))

    async def _on_drain(self, message: dict) -> None:
        reason = str(message.get("reason") or "")
        if self._draining:
            return
        self._draining = True
        logger.info("Backend asked this runner to drain: %s", reason or "<no reason>")
        self._spawn(self._drain_then_close())

    async def _on_ping(self, _message: dict) -> None:
        await self._send({"type": "pong"})

    async def _on_pong(self, _message: dict) -> None:
        return None

    async def _on_late_registered(self, _message: dict) -> None:
        logger.debug("Ignoring duplicate 'registered' frame")

    async def _on_error(self, message: dict) -> None:
        text = str(message.get("message") or "")
        code = message.get("code")
        if message.get("fatal"):
            raise FatalProtocolError(f"{code or 'error'}: {text}")
        logger.warning("Backend error frame (%s): %s", code or "-", text)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------
    async def _run_step(self, assignment: StepAssignment) -> None:
        cancel = self._cancel or asyncio.Event()
        try:
            outcome = await self._orchestrator.run_step(
                assignment, on_log=self._log_sink, cancel=cancel
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Orchestrator raised for step %s", assignment.step_id)
            outcome = StepOutcome(EXIT_AGENT_ERROR, f"runner error: {exc}")

        if cancel.is_set() and outcome.exit_code == EXIT_CANCELLED:
            outcome = StepOutcome(
                EXIT_CANCELLED, f"cancelled: {self._cancel_reason or 'no reason given'}"
            )

        self._remember(assignment.execution_key, outcome)
        # Logs BEFORE the terminal frame: a `step_complete` that overtakes the
        # explanation of the failure it reports is a log nobody can read.
        await self._flush_logs()
        self._current = None
        self._cancel = None
        self._log_step_id = None
        await self._send(
            {
                "type": "step_complete",
                "step_id": assignment.step_id,
                "exit_code": outcome.exit_code,
                "error": outcome.error,
            }
        )

    def _remember(self, execution_key: str, outcome: StepOutcome) -> None:
        if not execution_key:
            return
        self._results[execution_key] = outcome
        self._results.move_to_end(execution_key)
        while len(self._results) > IDEMPOTENCY_CACHE_SIZE:
            self._results.popitem(last=False)

    async def _abandon_running_step(self) -> None:
        """The socket died under a live step: kill it rather than orphan it.

        A container that keeps running after its connection is gone is exactly
        the split-brain the backend's step gate has to defend against; ending
        it here means the reassignment the backend is about to make is clean.
        """
        task = self._step_task
        if task is None or task.done():
            return
        if self._current is not None:
            self._abandoned_step_id = self._current.step_id
        if self._cancel is not None:
            self._cancel_reason = "connection lost"
            self._cancel.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=DISCONNECT_KILL_GRACE)
        except Exception:  # timeout, or the step task itself blew up - never raise from teardown
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _drain_then_close(self) -> None:
        task = self._step_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._drain_grace)
            except asyncio.TimeoutError:
                logger.warning("Drain grace expired with a step still running")
            except Exception:
                pass
        self._close_code = 1000
        self._close_reason = "drained"
        self._closed.set()
        try:
            await self._transport.close(1000, "drained")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Logs (section 1.7)
    # ------------------------------------------------------------------
    def _log_sink(self, lines: Sequence[str]) -> None:
        """Non-blocking sink handed to the orchestrator.

        Drops the OLDEST lines on overflow: the tail of a step's runner-origin
        output is what explains a failure, and blocking the orchestrator to
        preserve the head would let telemetry stall execution.
        """
        for line in lines:
            if len(self._log_buffer) >= AGENT_OUTBOUND_QUEUE:
                self._log_buffer.popleft()
                self._log_dropped += 1
            self._log_buffer.append(truncate_line(str(line)))
        self._log_wake.set()

    async def _log_sender_loop(self) -> None:
        while not self._closed.is_set():
            try:
                await asyncio.wait_for(self._log_wake.wait(), timeout=self._log_flush_interval)
            except asyncio.TimeoutError:
                pass
            self._log_wake.clear()
            await self._flush_logs()

    async def _flush_logs(self) -> None:
        step_id = self._log_step_id
        if step_id is None:
            # Nothing owns these lines: without a step_id the backend has no
            # StepRun to append them to.
            self._log_buffer.clear()
            self._log_dropped = 0
            return
        while self._log_buffer or self._log_dropped:
            if self._log_dropped:
                dropped, self._log_dropped = self._log_dropped, 0
                self._log_buffer.appendleft(
                    f"WARNING: {dropped} log lines dropped (back-pressure)"
                )
            batch: list[str] = []
            budget = MAX_LOG_FRAME_BYTES
            while self._log_buffer and len(batch) < MAX_LOG_LINES_PER_MESSAGE:
                line = self._log_buffer[0]
                cost = len(line.encode("utf-8", errors="replace")) + 1
                if batch and cost > budget:
                    break
                budget -= cost
                batch.append(self._log_buffer.popleft())
            if not batch:
                break
            self._log_seq += 1
            await self._send(
                {
                    "type": "log",
                    "step_id": step_id,
                    "lines": batch,
                    "seq": self._log_seq,
                }
            )

    # ------------------------------------------------------------------
    # Heartbeat + send
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """Sends immediately, then every ``registered.heartbeat_interval``.

        Runs DURING step execution: a runner mid-step is exactly the runner
        whose death matters most, and a heartbeat that pauses for the duration
        of a long step is a heartbeat that declares every long step dead.
        """
        while not self._closed.is_set():
            try:
                await self._send({"type": "heartbeat"})
            except TransportClosed:
                return
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=self._heartbeat_interval)
                return
            except asyncio.TimeoutError:
                continue

    async def _send(self, message: dict) -> None:
        try:
            await self._transport.send(json.dumps(message))
        except TransportClosed:
            self._closed.set()
            raise
        except Exception as exc:
            logger.warning("Failed to send %s frame: %s", message.get("type"), exc)
            self._closed.set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)


__all__ = [
    "AGENT_OUTBOUND_QUEUE",
    "DRAIN_GRACE",
    "EXIT_AGENT_ERROR",
    "EXIT_CANCELLED",
    "IDEMPOTENCY_CACHE_SIZE",
    "LOG_FLUSH_INTERVAL",
    "MAX_CONCURRENT_STEPS",
    "MAX_LOG_LINES_PER_MESSAGE",
    "MAX_LOG_LINE_BYTES",
    "FatalProtocolError",
    "RunnerSession",
    "Transport",
    "TransportClosed",
    "truncate_line",
]
