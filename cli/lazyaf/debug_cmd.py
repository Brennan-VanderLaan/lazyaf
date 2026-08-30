"""`lazyaf debug attach` - the raw-TTY terminal client - Phase 12.7.

Before this module, `attach` minted a join credential, printed it, and said
plainly that it did not open a shell. That was an honest gap; this closes it.

The shape, and why:

**Three layers, so two of them are testable without a TTY or a network.**
`run_terminal()` drives the protocol against anything with `send`/`recv`/
`close` and anything with `next_input`/`write_output`/`size`. The websocket
and the raw console are the outermost layer only, which is how
`tdd/unit/scripts/test_cli_debug.py` can drive a FULL attach - ready, notice,
stdout, keystrokes, `@resume`, close - against a socket that speaks the REAL
codec, with no doubles inside the code under test.

**The codec is `lazyaf.debug_protocol`, pinned to the server's by
`tdd/unit/debug/test_terminal_protocol_contract.py`** (C13). Nothing in this
file builds a frame by hand.

**`websockets` is an OPTIONAL extra, and its absence is LOUD.** See
`_load_websockets`: a base install that types `lazyaf debug attach` gets one
sentence naming the exact install command, never a traceback and never a
silent degrade to printing the credential. `--print-credential` is the
explicitly-asked-for non-interactive path and needs no extra at all.

**Ctrl-] is the escape, and `@`-verbs are their own frame type.** Sniffing
stdin for a leading `@` would corrupt any program that legitimately reads
`@...`; the decoder lives in `debug_protocol.EscapeDecoder` and is pure.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading

import click

from lazyaf import debug_protocol as proto

#: The install line printed when the extra is missing. One constant, so the
#: error text and the docs cannot drift.
TERMINAL_EXTRA_INSTALL = 'pip install "lazyaf-cli[terminal]"'

WEBSOCKETS_MISSING = (
    "the interactive terminal needs the `websockets` package, which is an "
    "OPTIONAL extra of lazyaf-cli. Install it with:\n"
    f"    {TERMINAL_EXTRA_INSTALL}\n"
    "Or run `lazyaf debug attach <id> --print-credential` to mint the "
    "credential without opening a shell."
)

SHELL_REFUSED = (
    "no step container exists at a pre-step breakpoint - the step has not "
    "started. Use --sidecar to inspect the workspace it is about to run "
    "against."
)

BANNER = (
    "/workspace is mounted READ-WRITE: edits there are seen by the resumed "
    "step."
)


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------


def terminal_url(server_url: str, session_id: str, mode: str = proto.CONNECTION_MODE_SIDECAR) -> str:
    """The ws:// URL of a session's terminal socket.

    The join token is NOT put in the query string even though the endpoint
    accepts it there: query strings land in proxy logs and shell history. The
    client presents it in an `Authorization: Bearer` header, which the
    endpoint prefers over the query parameter.
    """
    base = (server_url or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    elif not base.startswith(("ws://", "wss://")):
        raise ValueError(
            f"server URL {server_url!r} has no http(s):// or ws(s):// scheme"
        )
    return f"{base}/api/debug/{session_id}/terminal?mode={mode}"


# ---------------------------------------------------------------------------
# The websockets seam
# ---------------------------------------------------------------------------


def _load_websockets():
    """Return (connect, header_kwarg) or raise a click error naming the fix.

    Two import paths on purpose: `websockets.asyncio.client` is the modern
    (>=13) API whose header kwarg is `additional_headers`, and
    `websockets.client` is the legacy one whose kwarg is `extra_headers`. A
    client that guessed would break on half the versions in the wild, and
    guessing WRONG would surface as an auth failure rather than as a version
    problem.
    """
    try:
        from websockets.asyncio.client import connect  # type: ignore

        return connect, "additional_headers"
    except ImportError:
        pass
    try:
        from websockets.client import connect  # type: ignore

        return connect, "extra_headers"
    except ImportError as exc:
        raise click.ClickException(WEBSOCKETS_MISSING) from exc


#: What the CLI says when the server refuses the UPGRADE itself.
#:
#: The endpoint refuses before `accept()` (contract C14) and puts its reason
#: in the close frame - but a WebSocket that was never accepted has no close
#: frame: Starlette turns a pre-accept close into a plain HTTP 403 during the
#: handshake, and the sentence the server wrote is dropped on the floor. The
#: client cannot invent it, so it says exactly that and names where the
#: reason CAN be read. Anything vaguer would be the CLI pretending to know
#: why (R1).
UPGRADE_REFUSED = (
    "the server refused the terminal upgrade (HTTP {status}). A rejected "
    "WebSocket handshake carries no reason, so read it from the session "
    "itself:\n"
    "    lazyaf debug status {session}\n"
    "Most often the join credential expired - they are short-lived and "
    "re-mintable, so run `lazyaf debug attach` again for a fresh one."
)


def handshake_status(exc) -> "int | None":
    """The HTTP status a rejected WebSocket handshake came back with.

    Version-tolerant for the same reason `close_details` is: websockets >=13
    raises `InvalidStatus` carrying a `.response`, older releases raise
    `InvalidStatusCode` carrying `.status_code` directly.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def close_details(exc) -> tuple:
    """(code, reason) out of a websockets ConnectionClosed, version-tolerant.

    websockets >=12 carries the peer's close frame on `.rcvd`; older releases
    expose `.code` / `.reason` directly. A refusal at the upgrade travels
    ONLY in the close frame, so failing to read it here would turn every
    stated refusal into "connection closed".
    """
    received = getattr(exc, "rcvd", None)
    if received is not None:
        return getattr(received, "code", None), getattr(received, "reason", None)
    return getattr(exc, "code", None), getattr(exc, "reason", None)


# ---------------------------------------------------------------------------
# Local console I/O
# ---------------------------------------------------------------------------


class ConsoleIO:
    """Raw local keyboard/screen, drained by a thread (never on the loop).

    A blocking `read` on stdin is exactly the thing R5 forbids on the event
    loop, so a daemon thread pushes chunks into an `asyncio.Queue` through
    `loop.call_soon_threadsafe` - the same pattern the server side uses to
    drain the container socket.

    Three input backends, and the CLI SAYS which one it got (R1: a
    line-buffered fallback that pretended to be a raw TTY would break every
    curses program in a way the operator could not see):

    - POSIX tty: `termios` raw mode, byte-exact.
    - Windows console: `msvcrt`, byte-exact, with the arrow/function keys
      translated to the ANSI sequences a Linux shell expects.
    - not a tty (a pipe, CI): line-buffered stdin, stated on attach.
    """

    def __init__(self, stdin=None, stdout=None):
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._restore = None
        self.mode = "unknown"

    # -- lifecycle -----------------------------------------------------------

    def is_tty(self) -> bool:
        try:
            return bool(self._stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        if self.is_tty() and os.name != "nt":
            self.mode = "raw-posix"
            self._enter_posix_raw()
            reader = self._read_posix
        elif self.is_tty() and os.name == "nt":
            self.mode = "raw-windows"
            self._enable_windows_vt()
            reader = self._read_windows
        else:
            self.mode = "line-buffered"
            reader = self._read_lines
        self._thread = threading.Thread(
            target=reader, name="lazyaf-debug-stdin", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._restore is not None:
            self._restore()
            self._restore = None

    # -- the async surface ---------------------------------------------------

    async def next_input(self) -> "bytes | None":
        """Next chunk of local keystrokes, or None at EOF."""
        assert self._queue is not None, "start() first"
        return await self._queue.get()

    def write_output(self, data: bytes) -> None:
        buffer = getattr(self._stdout, "buffer", None)
        if buffer is None:  # a text-only stream (captured output in tests)
            self._stdout.write(data.decode("utf-8", "replace"))
        else:
            buffer.write(data)
        self._stdout.flush()

    def size(self) -> "tuple[int, int] | None":
        try:
            columns, lines = shutil.get_terminal_size()
        except (OSError, ValueError):
            return None
        return (columns, lines) if columns > 0 and lines > 0 else None

    # -- backends ------------------------------------------------------------

    def _put(self, item) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass  # the loop is gone; the consumer already left

    def _enter_posix_raw(self) -> None:
        import termios
        import tty

        fd = self._stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setraw(fd)

        def _restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

        self._restore = _restore

    def _enable_windows_vt(self) -> None:
        """Turn on ANSI escape handling for the Windows console.

        Best effort AND stated: without it the shell's colour codes print as
        literal `[0m` garbage. If it fails we say so on attach rather than
        letting the operator wonder why the output looks broken.
        """
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            self.mode = "raw-windows (no ANSI output)"

    def _read_posix(self) -> None:
        fd = self._stdin.fileno()
        try:
            while not self._stop.is_set():
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                self._put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._put(None)

    #: Windows console special keys -> the ANSI sequences a Linux shell reads.
    WINDOWS_SPECIAL = {
        "H": b"\x1b[A",  # up
        "P": b"\x1b[B",  # down
        "M": b"\x1b[C",  # right
        "K": b"\x1b[D",  # left
        "G": b"\x1b[H",  # home
        "O": b"\x1b[F",  # end
        "S": b"\x1b[3~",  # delete
    }

    def _read_windows(self) -> None:
        import msvcrt

        try:
            while not self._stop.is_set():
                if not msvcrt.kbhit():
                    self._stop.wait(0.02)
                    continue
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    special = msvcrt.getwch()
                    mapped = self.WINDOWS_SPECIAL.get(special)
                    if mapped:
                        self._put(mapped)
                    continue
                if char == "\r":
                    char = "\n"
                self._put(char.encode("utf-8"))
        except Exception:
            pass
        finally:
            self._put(None)

    def _read_lines(self) -> None:
        stream = getattr(self._stdin, "buffer", self._stdin)
        try:
            while not self._stop.is_set():
                line = stream.readline()
                if not line:
                    break
                self._put(line if isinstance(line, bytes) else line.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            self._put(None)


# ---------------------------------------------------------------------------
# The protocol driver
# ---------------------------------------------------------------------------


class TerminalResult:
    """What an attach ended as. Held as a value so the CLI, and a test, read
    the same thing rather than parsing printed output."""

    def __init__(self, exit_code: int, reason: str, commands: "list[str] | None" = None):
        self.exit_code = exit_code
        self.reason = reason
        self.commands = commands or []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TerminalResult(exit_code={self.exit_code}, reason={self.reason!r})"


async def run_terminal(socket, io, *, notice=print) -> TerminalResult:
    """Bridge a local console and one debug terminal socket.

    `socket` needs `send(text)` / `recv() -> str` / `close()`; `io` needs
    `next_input()` / `write_output(bytes)` / `size()`. Both are duck-typed on
    purpose: the whole protocol - including the `@`-verbs and the escape
    handling - is then exercisable without a TTY and without a network, which
    is what makes `test_cli_debug.py` a real test of this code rather than of
    a mock.

    Returns when the server closes, the shell exits, the user detaches, or
    local input reaches EOF. Never raises for a protocol-level refusal: a
    stated refusal is an outcome, not a crash.
    """
    size = io.size()
    if size is not None:
        await socket.send(
            proto.encode_frame(proto.TYPE_RESIZE, cols=size[0], rows=size[1])
        )

    decoder = proto.EscapeDecoder()
    sent_commands: list[str] = []
    finished: asyncio.Future = asyncio.get_running_loop().create_future()

    def _finish(result: TerminalResult) -> None:
        if not finished.done():
            result.commands = sent_commands
            finished.set_result(result)

    async def _inbound() -> None:
        while True:
            try:
                raw = await socket.recv()
            except Exception as exc:  # includes ConnectionClosed
                code, reason = close_details(exc)
                if code is None and reason is None:
                    _finish(TerminalResult(1, f"terminal connection failed: {exc}"))
                else:
                    _finish(
                        TerminalResult(
                            0 if code == proto.CLOSE_NORMAL else 1,
                            proto.close_reason(code, reason),
                        )
                    )
                return
            if raw is None:
                _finish(TerminalResult(1, "the server closed the terminal"))
                return
            try:
                frame = proto.decode_frame(raw)
            except proto.DebugProtocolError as exc:
                # R1: a frame we cannot understand is REPORTED, never dropped.
                notice(f"[protocol] {exc}")
                continue
            kind = frame["type"]
            if kind == proto.TYPE_STDOUT:
                io.write_output(proto.decode_bytes(frame["data"]))
            elif kind == proto.TYPE_READY:
                notice(
                    f"[attached] sidecar {str(frame.get('container_id') or '')[:12]} "
                    f"- {proto.ESCAPE_HELP}"
                )
            elif kind == proto.TYPE_NOTICE:
                notice(f"[lazyaf] {frame.get('text', '')}")
            elif kind == proto.TYPE_CLOSED:
                _finish(TerminalResult(0, frame.get("reason") or "terminal closed"))
                return

    async def _outbound() -> None:
        while True:
            chunk = await io.next_input()
            if chunk is None:
                _finish(TerminalResult(0, "local input reached EOF"))
                return
            for action, value in decoder.feed(chunk):
                if action == "stdin":
                    await socket.send(
                        proto.encode_frame(
                            proto.TYPE_STDIN, data=proto.encode_bytes(value)
                        )
                    )
                elif action == "command":
                    sent_commands.append(value)
                    await socket.send(
                        proto.encode_frame(proto.TYPE_COMMAND, command=value)
                    )
                elif action == "detach":
                    _finish(
                        TerminalResult(
                            0,
                            "detached - the session is still paused; "
                            "`lazyaf debug resume` when you are done",
                        )
                    )
                    return
                elif action == "unknown":
                    notice(
                        f"[lazyaf] unknown escape key {value!r}. "
                        f"{proto.ESCAPE_HELP}"
                    )

    tasks = [asyncio.create_task(_inbound()), asyncio.create_task(_outbound())]
    try:
        return await finished
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def attach_socket(
    url: str, token: str, io, *, notice=print, session_id: str = "<session>"
) -> TerminalResult:
    """Open the terminal socket and hand it to `run_terminal`.

    Two refusal shapes, and neither may reach the operator as a traceback:
    a close code (the socket was accepted, then closed) and a rejected
    handshake (it never was). Both come back as a `TerminalResult` carrying a
    sentence; anything else is re-raised, because an unrecognised failure
    must not be dressed up as a stated refusal.
    """
    connect, header_kwarg = _load_websockets()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with connect(url, **{header_kwarg: headers}) as socket:
            return await run_terminal(socket, io, notice=notice)
    except Exception as exc:
        code, reason = close_details(exc)
        if code is not None or reason is not None:
            return TerminalResult(1, proto.close_reason(code, reason))
        status = handshake_status(exc)
        if status is not None:
            return TerminalResult(
                1, UPGRADE_REFUSED.format(status=status, session=session_id)
            )
        raise


# ---------------------------------------------------------------------------
# The click command
# ---------------------------------------------------------------------------


@click.command("attach")
@click.argument("session_id")
@click.option(
    "--sidecar/--shell",
    "sidecar",
    default=True,
    help="Sidecar is the only mode at a breakpoint (see below).",
)
@click.option(
    "--print-credential",
    "print_credential",
    is_flag=True,
    help="Mint and print the join credential instead of opening a shell.",
)
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def attach(session_id, sidecar, print_credential, server):
    """Open an interactive shell on a paused session's sidecar.

    `--shell` is REFUSED, not downgraded: a breakpoint is a pre-step gate, so
    the step container does not exist yet. Use the sidecar to inspect the
    workspace the step is about to run against.

    Ctrl-] then r/a/s/h drives @resume / @abort / @status / @help; every one
    of those is also a plain `lazyaf debug` subcommand, so controlling a
    session never depends on having a TTY.
    """
    from lazyaf.cli import _debug_request, console, get_server_url

    if not sidecar:
        console.print(f"[red]Error:[/red] {SHELL_REFUSED}")
        sys.exit(2)

    session = _debug_request("GET", f"/api/debug/{session_id}", server)
    if not session.get("attach_available"):
        console.print(
            "[red]Error:[/red] cannot attach: "
            f"{session.get('attach_unavailable_reason') or 'unknown reason'}"
        )
        sys.exit(2)

    data = _debug_request("POST", f"/api/debug/{session_id}/join-token", server)
    server_url = server or get_server_url()
    url = terminal_url(server_url, session_id)

    if print_credential:
        console.print()
        console.print(
            f"token:   [cyan]{data['token']}[/cyan]\n"
            f"expires: {data['expires_at']}\n"
            f"socket:  {url}\n\n"
            f"[yellow]{BANNER}[/yellow]"
        )
        return

    console.print(f"[dim]connecting to {url}[/dim]")
    console.print(f"[yellow]{BANNER}[/yellow]")

    async def _attach() -> TerminalResult:
        # `ConsoleIO.start()` binds the reader thread to the RUNNING loop, so
        # it has to happen inside `asyncio.run`, not before it.
        io = ConsoleIO()
        io.start()
        if io.mode == "line-buffered":
            # Stated, never silent: a pipe cannot be a raw TTY, and a
            # full-screen program will misbehave in ways the operator has to
            # know about up front.
            console.print(
                "[yellow]stdin is not a TTY[/yellow] - keystrokes are sent a "
                "line at a time and full-screen programs will not render "
                "correctly."
            )
        try:
            return await attach_socket(
                url,
                data["token"],
                io,
                notice=_stderr_notice,
                session_id=session_id,
            )
        finally:
            io.stop()

    result = asyncio.run(_attach())
    console.print(f"\n[dim]{result.reason}[/dim]")
    if result.exit_code:
        sys.exit(result.exit_code)


def _stderr_notice(text: str) -> None:
    """Client-side chatter goes to STDERR so it never lands in a redirect of
    the shell's own output."""
    print(text, file=sys.stderr, flush=True)


def register(debug_group) -> None:
    """Install the interactive `attach` onto `lazyaf debug`.

    Replaces the credential-printing placeholder in `cli.py` (whose help text
    names this module). Explicit rather than an import side effect, so the
    wiring is one greppable line at the bottom of `cli.py`.
    """
    debug_group.add_command(attach, "attach")


__all__ = [
    "BANNER",
    "ConsoleIO",
    "SHELL_REFUSED",
    "TERMINAL_EXTRA_INSTALL",
    "TerminalResult",
    "UPGRADE_REFUSED",
    "WEBSOCKETS_MISSING",
    "attach",
    "attach_socket",
    "close_details",
    "handshake_status",
    "register",
    "run_terminal",
    "terminal_url",
]
