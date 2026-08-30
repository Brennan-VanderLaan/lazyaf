"""Connection lifecycle - Phase 12.6, section 4.3.

``RunnerClient.run()`` loops ``connect -> register -> serve``, reconnecting with
exponential backoff and FULL jitter. Each of the four behaviors below is a
named failure_01 defect:

* **Jittered, capped backoff, reset on ``registered``.** A flat 5s retry is a
  reconnect storm waiting for a backend restart: N runners resynchronize on the
  first failed attempt and hammer the backend in lockstep, keeping it from
  finishing startup. ``random.uniform(0, min(30, 2 ** attempt))`` spreads them.
* **A timeout on the ``registered`` wait.** Blocking forever on ``recv()``
  after sending ``register`` turns a backend that accepted the socket and then
  wedged into a runner that never reconnects and never reports.
* **``error{fatal: true}`` exits instead of retrying.** Auth failures and
  unsupported protocol versions do not heal; retrying one every 5s forever is
  a fleet DDoSing its own backend over a typo'd secret.
* **The transport is injectable.** ``connector`` is a seam so the lifecycle is
  testable against a fake transport without a socket, a port, or a sleep.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Awaitable, Callable

from . import __version__
from .config import ConfigError, RunnerConfig
from .orchestrator.base import StepOrchestrator, merge_labels
from .session import FatalProtocolError, RunnerSession, Transport, TransportClosed

logger = logging.getLogger(__name__)

#: Wire version this agent speaks. Mirrors the backend's PROTOCOL_VERSION;
#: pinned by tests/test_control_archive_parity.py.
PROTOCOL_VERSION = 1
#: The agent must see `registered` within this long or give up and retry.
REGISTRATION_TIMEOUT = 10
#: Ceiling on the jittered reconnect delay.
BACKOFF_CAP = 30
#: How long to wait for the TCP + WS upgrade.
CONNECT_TIMEOUT = 15

#: Exit codes from `run()`.
EXIT_OK = 0
EXIT_FATAL = 2


class RegistrationRejected(Exception):
    """The backend refused this registration, but retrying may work."""


class WebSocketTransport:
    """Adapts a ``websockets`` connection to the session's Transport protocol.

    Its whole job is turning library-specific close exceptions into
    :class:`TransportClosed`, so nothing above this line imports ``websockets``
    and a different client library is a one-class change.
    """

    def __init__(self, connection) -> None:
        self._ws = connection

    async def send(self, data: str) -> None:
        try:
            await self._ws.send(data)
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc

    async def recv(self) -> str:
        try:
            raw = await self._ws.recv()
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return raw

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code, reason)
        except Exception:
            pass


async def connect_websocket(url: str, *, headers: dict) -> Transport:
    """Open the real WebSocket. The default ``connector``."""
    from websockets.asyncio.client import connect

    connection = await connect(
        url,
        additional_headers=headers,
        open_timeout=CONNECT_TIMEOUT,
        # The agent sends application-level heartbeats on the interval the
        # backend dictates; a second, library-level keepalive with its own
        # independent timeout is exactly the drift this phase removed.
        ping_interval=None,
        max_size=None,
    )
    return WebSocketTransport(connection)


Connector = Callable[..., Awaitable[Transport]]


class RunnerClient:
    """Connects, registers, and hands the socket to a :class:`RunnerSession`."""

    def __init__(
        self,
        config: RunnerConfig,
        orchestrator: StepOrchestrator,
        *,
        connector: Connector | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator
        self._connector = connector or connect_websocket
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._max_attempts = max_attempts
        self._stop = asyncio.Event()
        #: The step this agent was executing when a connection dropped. Its
        #: container is already dead (the session kills on disconnect); the id
        #: travels on the next `register.resume` purely so the backend can
        #: reconcile, never as a claim that the work is still running.
        self._resume_step_id: str | None = None
        self.attempts = 0
        self.delays: list[float] = []

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def next_delay(self, attempt: int) -> float:
        """Full jitter: ``uniform(0, min(cap, 2 ** attempt))``.

        FULL jitter, not "backoff plus a little noise": the point is to
        decorrelate a fleet, and a narrow jitter band around a common base
        delay still reconnects N runners inside the same few hundred
        milliseconds.
        """
        return self._rng.uniform(0.0, min(float(BACKOFF_CAP), float(2 ** attempt)))

    # ------------------------------------------------------------------
    async def run(self) -> int:
        """connect -> serve, forever, with backoff. Returns a process exit code."""
        try:
            self._config.validate()
        except ConfigError as exc:
            logger.error("%s", exc)
            return EXIT_FATAL

        logger.info("Runner agent %s starting: %s", __version__, self._config.redacted())

        attempt = 0
        while not self._stop.is_set():
            if self._max_attempts is not None and self.attempts >= self._max_attempts:
                break
            self.attempts += 1
            registered = False
            try:
                registered = await self._connect_and_serve()
            except FatalProtocolError as exc:
                logger.error("Backend refused this runner permanently: %s", exc)
                return EXIT_FATAL
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Runner connection failed: %s", exc)

            if self._stop.is_set():
                break
            # Reset ONLY on a registration that actually succeeded. Resetting
            # on any completed attempt would turn a backend that accepts and
            # immediately drops sockets into a tight reconnect loop.
            attempt = 0 if registered else attempt + 1
            delay = self.next_delay(attempt)
            self.delays.append(delay)
            logger.info("Reconnecting in %.2fs (attempt %d)", delay, attempt)
            await self._sleep(delay)
        return EXIT_OK

    # ------------------------------------------------------------------
    async def _connect_and_serve(self) -> bool:
        """One connection. Returns True if registration succeeded."""
        url = self._config.ws_url
        headers = {"Authorization": f"Bearer {self._config.token}"}
        logger.info("Connecting to %s", url)
        transport = await self._connector(url, headers=headers)

        registered_payload: dict | None = None
        try:
            await transport.send(json.dumps(self.register_payload()))
            registered_payload = await self._await_registered(transport)
            self._on_registered(registered_payload)
            session = RunnerSession(
                self._config, self._orchestrator, transport, registered_payload
            )
            await self._handle_resume(session, registered_payload)
            try:
                await session.serve()
            finally:
                self._resume_step_id = session.resume_step_id
            return True
        finally:
            if registered_payload is None:
                try:
                    await transport.close(1000, "registration failed")
                except Exception:
                    pass

    async def _await_registered(self, transport: Transport) -> dict:
        deadline_error = (
            f"backend did not answer register within {REGISTRATION_TIMEOUT}s"
        )
        while True:
            try:
                raw = await asyncio.wait_for(transport.recv(), timeout=REGISTRATION_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise RegistrationRejected(deadline_error) from exc
            except TransportClosed as exc:
                raise RegistrationRejected(f"socket closed during registration: {exc}") from exc
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "registered":
                return message
            if kind == "error":
                text = str(message.get("message") or "")
                code = message.get("code")
                if message.get("fatal"):
                    raise FatalProtocolError(f"{code or 'error'}: {text}")
                raise RegistrationRejected(f"{code or 'error'}: {text}")
            # `pong` and anything else before `registered` is noise, not a
            # failure: keep waiting until the deadline says otherwise.
            logger.debug("Ignoring %r frame while awaiting registered", kind)

    def _on_registered(self, message: dict) -> None:
        their_version = message.get("protocol_version")
        if their_version is not None and int(their_version) != PROTOCOL_VERSION:
            # The backend already ACCEPTED us, so this is informational, not a
            # reason to disconnect - it just explains any shape surprise later.
            logger.warning(
                "Backend speaks protocol version %s, this agent speaks %s",
                their_version,
                PROTOCOL_VERSION,
            )
        logger.info(
            "Registered as %s (heartbeat=%ss death=%ss resume=%s)",
            message.get("runner_id"),
            message.get("heartbeat_interval"),
            message.get("death_timeout"),
            message.get("resume_action"),
        )

    async def _handle_resume(self, session: RunnerSession, message: dict) -> None:
        """Reconcile the backend's reconnect verdict with what we actually hold.

        The session kills its container the moment a socket drops, so this
        agent never genuinely CONTINUES a step across a reconnect. Saying so
        explicitly - with a terminal `step_complete` the backend already knows
        how to process - is what keeps a "continue" verdict from stranding a
        StepExecution in `running` forever with nothing on the other end.
        """
        action = str(message.get("resume_action") or "idle")
        step_id = message.get("resume_step_id") or self._resume_step_id
        self._resume_step_id = None
        if action == "continue" and step_id:
            logger.warning(
                "Backend expects this runner to continue step %s, but its "
                "container did not survive the disconnect - reporting it terminal",
                step_id,
            )
            await session.send(
                {
                    "type": "step_complete",
                    "step_id": str(step_id),
                    "exit_code": 143,
                    "error": "cancelled: container did not survive the runner reconnect",
                }
            )
        elif action == "abort" and step_id:
            logger.info("Backend aborted step %s; nothing to clean up locally", step_id)

    # ------------------------------------------------------------------
    def register_payload(self) -> dict:
        labels = merge_labels(self._config.labels, self._orchestrator.capabilities())
        payload = {
            "type": "register",
            "runner_id": self._config.runner_id,
            "name": self._config.name,
            "runner_type": self._config.runner_type,
            "labels": labels,
            "protocol_version": PROTOCOL_VERSION,
            "agent_version": __version__,
        }
        if self._resume_step_id:
            payload["resume"] = {"step_id": self._resume_step_id}
        return payload


__all__ = [
    "BACKOFF_CAP",
    "CONNECT_TIMEOUT",
    "EXIT_FATAL",
    "EXIT_OK",
    "PROTOCOL_VERSION",
    "REGISTRATION_TIMEOUT",
    "RegistrationRejected",
    "RunnerClient",
    "WebSocketTransport",
    "connect_websocket",
]
