"""Debug terminal: the sidecar's frame codec and its container lifecycle - 12.7.

failure_01 shipped this file as a TODO stub with zero callers ("Implement
full terminal I/O bridging"). This is the rebuild, and it holds four
properties the stub could not:

1. **Payloads are base64** (contract C12). A terminal emits arbitrary bytes;
   failure_01's sketch put raw text in a JSON frame and would have corrupted
   on the first non-UTF-8 byte. `stdin`/`stdout` carry base64 and nothing
   else, and the codec REFUSES a frame whose data is not decodable rather
   than passing mojibake through.

2. **`@`-commands are their own frame type**, never sniffed out of the byte
   stream. Scanning stdin for a leading `@` corrupts any program that
   legitimately reads `@...`; the CLI reserves Ctrl-] as its escape and
   sends a `command` frame.

3. **The codec has ONE definition** (contract C13). It lives here, in a
   module that imports nothing from `docker`, `fastapi` or `websockets` at
   import time, so the CLI-side contract test can import it in any
   environment. The docker-facing service below is a separate class in the
   same file; nothing in the codec touches it.

4. **No blocking read ever reaches the event loop** (R5). `exec_run(...,
   socket=True)` hands back a blocking socket, so it is drained by a daemon
   thread that pushes chunks into an `asyncio.Queue` via
   `loop.call_soon_threadsafe` - the same pattern `LocalExecutor._pump`
   already uses for log streaming.

**Sidecar, not shell.** A breakpoint is a PRE-step gate: it fires before the
step container is created, so there is nothing to exec into. `--shell` is an
error naming that reason (contract C17), not a silent downgrade - see
`SHELL_REFUSED_REASON`.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire protocol (contract C12/C13)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1

# client -> server
TYPE_STDIN = "stdin"
TYPE_RESIZE = "resize"
TYPE_COMMAND = "command"
TYPE_PING = "ping"

# server -> client
TYPE_READY = "ready"
TYPE_STDOUT = "stdout"
TYPE_NOTICE = "notice"
TYPE_CLOSED = "closed"
TYPE_PONG = "pong"

CLIENT_FRAME_TYPES = frozenset({TYPE_STDIN, TYPE_RESIZE, TYPE_COMMAND, TYPE_PING})
SERVER_FRAME_TYPES = frozenset(
    {TYPE_READY, TYPE_STDOUT, TYPE_NOTICE, TYPE_CLOSED, TYPE_PONG}
)

#: The four verbs a `command` frame may carry. Every one is ALSO a plain HTTP
#: subcommand, so controlling a session never depends on having a TTY.
COMMANDS = ("@resume", "@abort", "@status", "@help")

# Close codes. 1000 is the normal close.
CLOSE_NORMAL = 1000
CLOSE_BAD_TOKEN = 4401
CLOSE_NOT_ATTACHABLE = 4403
CLOSE_UNKNOWN_SESSION = 4404
CLOSE_DUPLICATE_TERMINAL = 4004
CLOSE_BOUND_EXCEEDED = 4009

# Bounds (contract C15: every one of these closes the socket with a reason;
# nothing is silently dropped or truncated).
MAX_FRAME_BYTES = 64 * 1024
MAX_OUTBOUND_QUEUE = 256
RATE_WINDOW_SECONDS = 1.0
RATE_MAX_FRAMES_PER_WINDOW = 200

CONNECTION_MODE_SIDECAR = "sidecar"

SHELL_REFUSED_REASON = (
    "no step container exists at a pre-step breakpoint - the step has not "
    "started. Use --sidecar to inspect the workspace it is about to run "
    "against."
)

REMOTE_ATTACH_REASON = (
    "terminal attach is not available for steps running on a remote runner "
    "(12.7 ships local attach only)"
)


class DebugProtocolError(ValueError):
    """A frame that does not satisfy the contract.

    A distinct exception so the WS endpoint can answer with the right close
    code and a reason, instead of a generic 500 or - worse - forwarding a
    half-understood frame.
    """


def encode_frame(frame_type: str, **fields: Any) -> str:
    """Serialize one frame to the text payload that goes on the wire.

    `data` fields are expected to be base64 ALREADY (use `encode_bytes`);
    passing raw bytes here is a programming error and raises.
    """
    if frame_type not in CLIENT_FRAME_TYPES and frame_type not in SERVER_FRAME_TYPES:
        raise DebugProtocolError(f"unknown frame type {frame_type!r}")
    if isinstance(fields.get("data"), (bytes, bytearray)):
        raise DebugProtocolError(
            "frame 'data' must be base64 text - call encode_bytes() first "
            "(contract C12)"
        )
    payload = {"v": PROTOCOL_VERSION, "type": frame_type}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return json.dumps(payload, separators=(",", ":"))


def decode_frame(raw: str | bytes) -> dict:
    """Parse and VALIDATE one wire frame.

    Raises DebugProtocolError - never returns a partially-understood frame.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DebugProtocolError(f"frame is not UTF-8 text: {exc}") from exc
    try:
        frame = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DebugProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise DebugProtocolError("frame must be a JSON object")
    if frame.get("v") != PROTOCOL_VERSION:
        raise DebugProtocolError(
            f"unsupported protocol version {frame.get('v')!r} "
            f"(this backend speaks v{PROTOCOL_VERSION})"
        )
    frame_type = frame.get("type")
    if frame_type not in CLIENT_FRAME_TYPES and frame_type not in SERVER_FRAME_TYPES:
        raise DebugProtocolError(f"unknown frame type {frame_type!r}")

    if frame_type in (TYPE_STDIN, TYPE_STDOUT):
        data = frame.get("data")
        if not isinstance(data, str):
            raise DebugProtocolError(f"{frame_type} frame needs a base64 'data' string")
        # Decode eagerly: a frame that cannot round-trip is refused here
        # rather than corrupting the terminal downstream.
        decode_bytes(data)
    elif frame_type == TYPE_RESIZE:
        for field in ("cols", "rows"):
            value = frame.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise DebugProtocolError(f"resize frame needs a positive int {field!r}")
    elif frame_type == TYPE_COMMAND:
        command = frame.get("command")
        if command not in COMMANDS:
            raise DebugProtocolError(
                f"unknown command {command!r} (known: {', '.join(COMMANDS)})"
            )
    return frame


def encode_bytes(data: bytes) -> str:
    """Raw terminal bytes -> the base64 text a `data` field carries."""
    return base64.b64encode(bytes(data)).decode("ascii")


def decode_bytes(data: str) -> bytes:
    """The base64 text a `data` field carries -> raw terminal bytes."""
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise DebugProtocolError(f"'data' is not valid base64: {exc}") from exc


# ---------------------------------------------------------------------------
# Sidecar container lifecycle
# ---------------------------------------------------------------------------

#: The sidecar image. Built from `images/debug-sidecar/` (lane B) FROM
#: lazyaf-base, so it inherits uid 1000 - a root sidecar would leave
#: root-owned files in the workspace that the resumed step (uid 1000) trips
#: over.
SIDECAR_IMAGE_ENV = "LAZYAF_DEBUG_SIDECAR_IMAGE"
DEFAULT_SIDECAR_IMAGE = "lazyaf-debug-sidecar:dev"

LABEL_TYPE = "lazyaf.type"
LABEL_TYPE_VALUE = "debug-sidecar"
LABEL_SESSION = "lazyaf.debug-session"
LABEL_RUN = "lazyaf.pipeline-run"


def sidecar_image() -> str:
    """The image name a sidecar is spawned from."""
    return os.getenv(SIDECAR_IMAGE_ENV) or DEFAULT_SIDECAR_IMAGE


def workspace_volume_name(pipeline_run_id: str) -> str:
    """The run's workspace volume - the SAME name the executor mounts.

    Imported lazily from the workspace state machine so this module stays
    importable without the SQLAlchemy stack (the CLI-side contract test).
    """
    from app.services.workspace.state_machine import generate_volume_name

    return generate_volume_name(pipeline_run_id)


@dataclass
class TerminalStream:
    """One attached terminal: an inbound queue plus the socket to write to."""

    container_id: str
    queue: asyncio.Queue
    _socket: Any
    _closed: bool = False

    async def write(self, data: bytes) -> None:
        """Send bytes to the container's stdin without blocking the loop."""
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(self._write_sync, bytes(data))

    def _write_sync(self, data: bytes) -> None:
        sock = getattr(self._socket, "_sock", self._socket)
        sock.sendall(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.close()
        except Exception:  # pragma: no cover - socket already dead
            logger.debug("debug terminal socket close failed", exc_info=True)


class DebugTerminalService:
    """Owns sidecar containers and the threads that pump their I/O.

    Deliberately NOT a per-session task registry: the session row is the
    truth, and this class holds only what cannot live in a row (a live socket
    and its pump thread). `reset()` tears both down for the test harness.
    """

    def __init__(self) -> None:
        self._client = None
        #: debug_session_id -> TerminalStream. At most ONE terminal per
        #: session (a second attach is refused 4004 rather than interleaving
        #: two keyboards into one shell).
        self._streams: dict[str, TerminalStream] = {}

    # -- docker seam ---------------------------------------------------------

    def _docker(self):
        """Lazily construct the docker client (loud on failure, never dark)."""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    # -- lifecycle -----------------------------------------------------------

    def has_terminal(self, session_id: str) -> bool:
        return session_id in self._streams

    async def ensure_sidecar(
        self, session_id: str, pipeline_run_id: str, existing_container_id: str | None
    ) -> str:
        """Return the session's sidecar container id, creating it on demand.

        Created LAZILY on first attach - a paused step that nobody looks at
        never pays for a container - and removed only at session end, so a
        dropped CLI reconnects into the same shell host.
        """
        from starlette.concurrency import run_in_threadpool

        if existing_container_id:
            alive = await run_in_threadpool(self._sync_is_running, existing_container_id)
            if alive:
                return existing_container_id
            logger.warning(
                "debug sidecar %s for session %s is gone; spawning a new one",
                existing_container_id[:12],
                session_id[:8],
            )
        return await run_in_threadpool(
            self._sync_create_sidecar, session_id, pipeline_run_id
        )

    def _sync_is_running(self, container_id: str) -> bool:
        try:
            container = self._docker().containers.get(container_id)
        except Exception:
            return False
        return container.status == "running"

    def _sync_create_sidecar(self, session_id: str, pipeline_run_id: str) -> str:
        from app.config import get_settings

        settings = get_settings()
        volume = workspace_volume_name(pipeline_run_id)
        container = self._docker().containers.run(
            sidecar_image(),
            command=["sleep", "infinity"],
            detach=True,
            # Read-WRITE, deliberately: the point of a debug re-run is to poke
            # at the workspace and resume. The MOTD and the CLI banner both
            # say so.
            volumes={volume: {"bind": "/workspace", "mode": "rw"}},
            working_dir="/workspace",
            # settings.container_network, NOT failure_01's network_mode="host"
            # (wrong under the compose stack and needlessly wide).
            network=settings.container_network,
            labels={
                LABEL_TYPE: LABEL_TYPE_VALUE,
                LABEL_SESSION: session_id,
                LABEL_RUN: pipeline_run_id,
            },
            name=f"lazyaf-debug-{session_id[:12]}",
        )
        logger.info(
            "Spawned debug sidecar %s for session %s on volume %s",
            container.id[:12],
            session_id[:8],
            volume,
        )
        return container.id

    async def attach(self, session_id: str, container_id: str) -> TerminalStream:
        """Exec a login shell in the sidecar and start pumping its output."""
        from starlette.concurrency import run_in_threadpool

        if session_id in self._streams:
            raise RuntimeError("duplicate terminal")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_OUTBOUND_QUEUE)
        socket = await run_in_threadpool(self._sync_exec, container_id)
        stream = TerminalStream(container_id=container_id, queue=queue, _socket=socket)
        self._streams[session_id] = stream

        def _put(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # loop gone - the consumer already left
            except asyncio.QueueFull:
                # C15: NEVER silently drop bytes. The consumer sees the
                # overflow marker and closes 4009.
                try:
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("overflow", b"")
                    )
                except Exception:
                    pass

        def _pump() -> None:
            sock = getattr(socket, "_sock", socket)
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    _put(("data", chunk))
            except Exception as exc:
                _put(("error", exc))
            finally:
                _put(("eof", b""))

        threading.Thread(
            target=_pump, name=f"lazyaf-debug-term-{session_id[:8]}", daemon=True
        ).start()
        return stream

    def _sync_exec(self, container_id: str):
        container = self._docker().containers.get(container_id)
        _exec_id, socket = container.exec_run(
            cmd=["/bin/bash", "-l"],
            stdin=True,
            tty=True,
            socket=True,
            user="1000:1000",
            workdir="/workspace",
        )
        return socket

    async def detach(self, session_id: str) -> None:
        """Drop the attached terminal (the sidecar container survives)."""
        stream = self._streams.pop(session_id, None)
        if stream is not None:
            stream.close()

    async def remove_sidecar(self, session_id: str, container_id: str | None) -> None:
        """Stop and remove the sidecar. Step 3 of the teardown order (C9).

        MUST run before the workspace pin is released: docker refuses to
        remove a volume a running container still mounts, and getting this
        backwards leaves a volume only `audit_orphans` eventually reaps.
        """
        from starlette.concurrency import run_in_threadpool

        await self.detach(session_id)
        if not container_id:
            return
        try:
            await run_in_threadpool(self._sync_remove, container_id)
        except Exception:
            logger.warning(
                "Failed to remove debug sidecar %s for session %s "
                "(the startup sweep is the backstop)",
                container_id[:12],
                session_id[:8],
                exc_info=True,
            )

    def _sync_remove(self, container_id: str) -> None:
        container = self._docker().containers.get(container_id)
        container.remove(force=True)

    async def sweep_orphan_sidecars(self, live_session_ids: set[str]) -> int:
        """Remove sidecars whose session is terminal or gone (contract C20).

        Called at startup next to `recover_orphaned_executions`: a backend
        restart kills the paused gate task, so any sidecar still running
        belongs to a session nothing will ever end.
        """
        from starlette.concurrency import run_in_threadpool

        try:
            containers = await run_in_threadpool(self._sync_list_sidecars)
        except Exception:
            logger.warning("debug sidecar sweep could not list containers", exc_info=True)
            return 0
        removed = 0
        for container_id, session_id in containers:
            if session_id in live_session_ids:
                continue
            try:
                await run_in_threadpool(self._sync_remove, container_id)
                removed += 1
            except Exception:
                logger.warning(
                    "debug sidecar sweep could not remove %s", container_id[:12],
                    exc_info=True,
                )
        if removed:
            logger.info("debug sidecar sweep removed %d orphan container(s)", removed)
        return removed

    def _sync_list_sidecars(self) -> list[tuple[str, str]]:
        containers = self._docker().containers.list(
            all=True, filters={"label": f"{LABEL_TYPE}={LABEL_TYPE_VALUE}"}
        )
        return [
            (c.id, (c.labels or {}).get(LABEL_SESSION, ""))
            for c in containers
        ]

    async def reset(self) -> None:
        """Test-mode reset hook: drop every attached terminal."""
        for session_id in list(self._streams):
            stream = self._streams.pop(session_id, None)
            if stream is not None:
                stream.close()


debug_terminal_service = DebugTerminalService()


__all__ = [
    "CLIENT_FRAME_TYPES",
    "CLOSE_BAD_TOKEN",
    "CLOSE_BOUND_EXCEEDED",
    "CLOSE_DUPLICATE_TERMINAL",
    "CLOSE_NORMAL",
    "CLOSE_NOT_ATTACHABLE",
    "CLOSE_UNKNOWN_SESSION",
    "COMMANDS",
    "CONNECTION_MODE_SIDECAR",
    "DebugProtocolError",
    "DebugTerminalService",
    "MAX_FRAME_BYTES",
    "MAX_OUTBOUND_QUEUE",
    "PROTOCOL_VERSION",
    "RATE_MAX_FRAMES_PER_WINDOW",
    "RATE_WINDOW_SECONDS",
    "REMOTE_ATTACH_REASON",
    "SERVER_FRAME_TYPES",
    "SHELL_REFUSED_REASON",
    "TYPE_CLOSED",
    "TYPE_COMMAND",
    "TYPE_NOTICE",
    "TYPE_PING",
    "TYPE_PONG",
    "TYPE_READY",
    "TYPE_RESIZE",
    "TYPE_STDIN",
    "TYPE_STDOUT",
    "TerminalStream",
    "debug_terminal_service",
    "decode_bytes",
    "decode_frame",
    "encode_bytes",
    "encode_frame",
    "sidecar_image",
    "workspace_volume_name",
]
