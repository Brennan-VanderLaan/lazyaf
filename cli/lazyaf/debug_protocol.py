"""The CLI half of the debug-terminal wire contract - Phase 12.7 (C12/C13).

The server half is
`backend/app/services/execution/debug_terminal.py`. This module is its
mirror, and `tdd/unit/debug/test_terminal_protocol_contract.py` imports BOTH
and cross-round-trips every frame type in both directions, so the two cannot
drift (R3).

Why a mirror and not an import: a published `lazyaf-cli` wheel installs on a
laptop that has no backend checkout, no FastAPI and no SQLAlchemy. The codec
therefore exists twice, and the drift risk that buys is paid off by a test
that fails the moment one side changes a constant, a validation rule, or a
frame shape. "Two copies pinned by a test" is a decision; two copies pinned
by nothing is the bug this file's contract test exists to prevent.

Stdlib only, on purpose: importable with `websockets` absent, so
`lazyaf debug attach --print-credential` and every unit test keep working on
a base install.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

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

CLOSE_NORMAL = 1000
CLOSE_BAD_TOKEN = 4401
CLOSE_NOT_ATTACHABLE = 4403
CLOSE_UNKNOWN_SESSION = 4404
CLOSE_DUPLICATE_TERMINAL = 4004
CLOSE_BOUND_EXCEEDED = 4009

#: Human sentences for the close codes the server can answer an upgrade with.
#: The server always sends a reason; this map is what the CLI prints when a
#: proxy or an old server drops the reason on the floor, so a refusal is
#: never a bare number (R1).
CLOSE_CODE_MEANINGS = {
    CLOSE_NORMAL: "the terminal closed normally",
    CLOSE_DUPLICATE_TERMINAL: "a terminal is already attached to this debug session",
    CLOSE_BOUND_EXCEEDED: "a protocol bound was exceeded (frame size, rate or output backlog)",
    CLOSE_BAD_TOKEN: "missing or invalid join token - mint a new one with `lazyaf debug attach`",
    CLOSE_NOT_ATTACHABLE: "this session cannot be attached to right now",
    CLOSE_UNKNOWN_SESSION: "unknown debug session",
}

MAX_FRAME_BYTES = 64 * 1024
MAX_OUTBOUND_QUEUE = 256
RATE_WINDOW_SECONDS = 1.0
RATE_MAX_FRAMES_PER_WINDOW = 200

CONNECTION_MODE_SIDECAR = "sidecar"


class DebugProtocolError(ValueError):
    """A frame that does not satisfy the contract.

    Distinct from ValueError so the terminal client can tell "the server sent
    something I do not understand" from any other failure, and print the
    former rather than a traceback.
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


def decode_frame(raw: "str | bytes") -> dict:
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
            f"(this client speaks v{PROTOCOL_VERSION})"
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


def close_reason(code: "int | None", reason: "str | None") -> str:
    """One sentence for a close, whatever the server managed to send.

    A refusal happens BEFORE the WebSocket is accepted, so its reason can only
    travel in the close frame - and an intermediary that drops the reason must
    not turn a stated refusal into a bare number.
    """
    text = (reason or "").strip()
    if text:
        return f"{text} (code {code})" if code is not None else text
    if code in CLOSE_CODE_MEANINGS:
        return f"{CLOSE_CODE_MEANINGS[code]} (code {code})"
    return f"the server closed the terminal with code {code}"


# ---------------------------------------------------------------------------
# The escape key (client-side only - the server never sees it)
# ---------------------------------------------------------------------------

#: Ctrl-] , the same escape `telnet` uses. NOT `@` sniffed out of the byte
#: stream: scanning stdin for a leading `@` corrupts any program that
#: legitimately reads `@...`, which is exactly why commands are their own
#: frame type on the wire (C12).
ESCAPE_BYTE = b"\x1d"

#: escape + key -> what the client does. One table, so `@help` and the code
#: cannot disagree about which keys exist.
ESCAPE_KEYS: dict[bytes, str] = {
    b"r": "@resume",
    b"a": "@abort",
    b"s": "@status",
    b"h": "@help",
    b"?": "@help",
}

ESCAPE_DETACH_KEYS = (b"d", b"\x04")  # d / Ctrl-D: leave the shell running

ESCAPE_HELP = (
    "Ctrl-] then: r resume · a abort · s status · h help · d detach "
    "(leaves the session paused) · Ctrl-] sends a literal Ctrl-]"
)


class EscapeDecoder:
    """Splits local keystrokes into stdin bytes, commands and a detach.

    A tiny state machine rather than an inline `if`, because it has to hold
    across chunk boundaries: a read can end exactly on the escape byte, and a
    client that forgot that would send the escape to the shell and then
    swallow the next real keystroke.
    """

    def __init__(self) -> None:
        self._armed = False

    @property
    def armed(self) -> bool:
        """True when the previous chunk ended on the escape byte."""
        return self._armed

    def feed(self, data: bytes) -> list:
        """-> list of ("stdin", bytes) / ("command", str) / ("detach", key)
        / ("unknown", key) actions, in order."""
        actions: list = []
        pending = bytearray()
        for index in range(len(data)):
            byte = data[index : index + 1]
            if self._armed:
                self._armed = False
                if byte == ESCAPE_BYTE:
                    pending += ESCAPE_BYTE  # doubled: send one literal
                elif byte in ESCAPE_KEYS:
                    if pending:
                        actions.append(("stdin", bytes(pending)))
                        pending = bytearray()
                    actions.append(("command", ESCAPE_KEYS[byte]))
                elif byte in ESCAPE_DETACH_KEYS:
                    if pending:
                        actions.append(("stdin", bytes(pending)))
                        pending = bytearray()
                    actions.append(("detach", byte))
                else:
                    if pending:
                        actions.append(("stdin", bytes(pending)))
                        pending = bytearray()
                    # Never silently eaten: an unknown escape key says so.
                    actions.append(("unknown", byte))
                continue
            if byte == ESCAPE_BYTE:
                self._armed = True
                continue
            pending += byte
        if pending:
            actions.append(("stdin", bytes(pending)))
        return actions


__all__ = [
    "CLIENT_FRAME_TYPES",
    "CLOSE_BAD_TOKEN",
    "CLOSE_BOUND_EXCEEDED",
    "CLOSE_CODE_MEANINGS",
    "CLOSE_DUPLICATE_TERMINAL",
    "CLOSE_NORMAL",
    "CLOSE_NOT_ATTACHABLE",
    "CLOSE_UNKNOWN_SESSION",
    "COMMANDS",
    "CONNECTION_MODE_SIDECAR",
    "DebugProtocolError",
    "ESCAPE_BYTE",
    "ESCAPE_DETACH_KEYS",
    "ESCAPE_HELP",
    "ESCAPE_KEYS",
    "EscapeDecoder",
    "MAX_FRAME_BYTES",
    "MAX_OUTBOUND_QUEUE",
    "PROTOCOL_VERSION",
    "RATE_MAX_FRAMES_PER_WINDOW",
    "RATE_WINDOW_SECONDS",
    "SERVER_FRAME_TYPES",
    "TYPE_CLOSED",
    "TYPE_COMMAND",
    "TYPE_NOTICE",
    "TYPE_PING",
    "TYPE_PONG",
    "TYPE_READY",
    "TYPE_RESIZE",
    "TYPE_STDIN",
    "TYPE_STDOUT",
    "close_reason",
    "decode_bytes",
    "decode_frame",
    "encode_bytes",
    "encode_frame",
]
