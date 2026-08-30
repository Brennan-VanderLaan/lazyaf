"""`lazyaf debug` - the terminal client (cli/lazyaf/debug_cmd.py) - Phase 12.7.

Before this file, `debug attach` minted a credential, printed it, and said so
plainly. Honest, and still a gap. These tests pin the client that closes it,
and they drive the REAL protocol driver - `run_terminal` - against a socket
that speaks the REAL SERVER codec (`app.services.execution.debug_terminal`).
Nothing inside the code under test is doubled: the two things replaced are
the websocket (a scripted object with `send`/`recv`) and the console (a
scripted object with `next_input`/`write_output`), which is exactly the seam
`debug_cmd` was factored into three layers to expose.

That matters because the interesting bugs live in the middle: an escape
sequence split across two reads, a `@`-verb sniffed out of stdin, a close
code whose reason an intermediary dropped, a `websockets` version whose
header kwarg is spelled differently. Every one of those is a test here.

`rich` is not installed in the backend test environment (the CLI ships its
own dependency set), so `lazyaf.cli` is imported behind a minimal stub - the
same idiom `test_cli_tests_reconcile.py` uses, for the same reason: the
refusals are genuinely exercised rather than skipped (R4).
"""
import asyncio
import io
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_DIR = REPO_ROOT / "cli"
BACKEND_DIR = REPO_ROOT / "backend"

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 path
    import tomli as tomllib

pytest.importorskip("click", reason="cli/ requires click")

for _path in (str(CLI_DIR), str(BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import click  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from app.services.execution import debug_terminal as wire  # noqa: E402
from lazyaf import debug_cmd, debug_protocol as proto  # noqa: E402


def _rich_stub() -> dict:
    """Minimal rich stand-in: Console.print writes plain text to stdout."""

    class _Console:
        def __init__(self, *args, **kwargs):
            pass

        def print(self, *args, **kwargs):
            print(" ".join(str(a) for a in args))

    class _Panel(str):
        def __new__(cls, renderable="", *args, **kwargs):
            return super().__new__(cls, str(renderable))

        @classmethod
        def fit(cls, renderable="", *args, **kwargs):
            return cls(renderable)

    rich = types.ModuleType("rich")
    console_mod = types.ModuleType("rich.console")
    console_mod.Console = _Console
    panel_mod = types.ModuleType("rich.panel")
    panel_mod.Panel = _Panel
    rich.console = console_mod
    rich.panel = panel_mod
    return {"rich": rich, "rich.console": console_mod, "rich.panel": panel_mod}


def load_cli():
    """The LIVE `lazyaf.cli` module, imported behind the rich stub if needed.

    A FUNCTION, not a module-level import, and that is load-bearing.
    `test_cli_tests_reconcile.py` pops `lazyaf.cli` out of `sys.modules` and
    re-imports it at ITS collection time, which happens after this file's.
    A module-level `import lazyaf.cli as cli_module` here would therefore
    leave every test in this file monkeypatching a module object that
    `debug_cmd.attach`'s lazy `from lazyaf.cli import ...` no longer
    resolves to - the patch lands on an orphan and the command reaches the
    real network. Resolving through `sys.modules` AT TEST TIME is what makes
    the two files order-independent.
    """
    module = sys.modules.get("lazyaf.cli")
    if module is not None:
        return module
    saved = {
        name: sys.modules.get(name)
        for name in ("rich", "rich.console", "rich.panel")
    }
    sys.modules.update(_rich_stub())
    try:
        import lazyaf.cli as module  # noqa: E402
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


@pytest.fixture
def cli_module():
    """The live `lazyaf.cli`, resolved when the test runs (see load_cli)."""
    return load_cli()


# -----------------------------------------------------------------------------
# Scripted seams
# -----------------------------------------------------------------------------


class ScriptedSocket:
    """A websocket that speaks the REAL SERVER codec.

    Inbound frames are built with `debug_terminal.encode_frame`; outbound
    frames are VALIDATED with `debug_terminal.decode_frame`, so a client frame
    the real server would refuse fails the test here rather than in
    production. When the script runs dry `recv()` parks forever, exactly like
    a real idle terminal.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(wire.decode_frame(text))

    async def recv(self) -> str:
        if self.script:
            await asyncio.sleep(0)
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await asyncio.Event().wait()  # idle terminal
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True

    def frames_of(self, frame_type: str) -> list[dict]:
        return [f for f in self.sent if f["type"] == frame_type]


class ScriptedIO:
    """A console whose keystrokes are a list and whose screen is a bytearray."""

    def __init__(self, chunks=(), size=(120, 40)):
        self.chunks = list(chunks)
        self.output = bytearray()
        self._size = size
        self.park_at_end = False

    async def next_input(self):
        if self.chunks:
            await asyncio.sleep(0)
            return self.chunks.pop(0)
        if self.park_at_end:
            await asyncio.Event().wait()
        return None

    def write_output(self, data: bytes) -> None:
        self.output += data

    def size(self):
        return self._size


def ready(container_id: str = "c0ffee123456") -> str:
    return wire.encode_frame(
        wire.TYPE_READY,
        mode=wire.CONNECTION_MODE_SIDECAR,
        container_id=container_id,
    )


def stdout(payload: bytes) -> str:
    return wire.encode_frame(wire.TYPE_STDOUT, data=wire.encode_bytes(payload))


def notice(text: str) -> str:
    return wire.encode_frame(wire.TYPE_NOTICE, text=text)


def closed(reason: str) -> str:
    return wire.encode_frame(wire.TYPE_CLOSED, reason=reason)


# -----------------------------------------------------------------------------
# URL derivation
# -----------------------------------------------------------------------------


class TestTerminalUrl:
    def test_http_becomes_ws(self):
        assert debug_cmd.terminal_url("http://localhost:8000", "abc") == (
            "ws://localhost:8000/api/debug/abc/terminal?mode=sidecar"
        )

    def test_https_becomes_wss(self):
        assert debug_cmd.terminal_url("https://lazyaf.example", "abc").startswith(
            "wss://lazyaf.example/"
        )

    def test_trailing_slash_does_not_double(self):
        assert "//api/debug" not in debug_cmd.terminal_url("http://h:8000/", "abc")

    def test_an_explicit_ws_url_is_accepted(self):
        assert debug_cmd.terminal_url("ws://h:8000", "abc").startswith("ws://h:8000/")

    def test_a_schemeless_server_url_is_refused_not_guessed(self):
        """R1: guessing a scheme is how a terminal credential ends up on the
        wire unencrypted."""
        with pytest.raises(ValueError) as exc:
            debug_cmd.terminal_url("localhost:8000", "abc")
        assert "scheme" in str(exc.value)

    def test_the_token_never_enters_the_url(self):
        url = debug_cmd.terminal_url("http://localhost:8000", "abc")
        assert "token" not in url, (
            "the join credential belongs in the Authorization header - a query "
            "string lands in proxy logs and shell history"
        )


# -----------------------------------------------------------------------------
# The escape decoder (client-side only; the server never sees Ctrl-])
# -----------------------------------------------------------------------------


class TestEscapeDecoder:
    def test_plain_bytes_pass_through_untouched(self):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"ls -la\n") == [("stdin", b"ls -la\n")]

    def test_an_at_sign_in_the_byte_stream_is_just_input(self):
        """The whole reason commands are their own frame type (C12).

        A program reading `@resume` from its own stdin must receive it.
        """
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"@resume\n") == [("stdin", b"@resume\n")]

    @pytest.mark.parametrize(
        "key,command",
        [(b"r", "@resume"), (b"a", "@abort"), (b"s", "@status"), (b"h", "@help"), (b"?", "@help")],
    )
    def test_escape_plus_key_becomes_a_command(self, key, command):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"\x1d" + key) == [("command", command)]

    def test_doubled_escape_sends_one_literal_escape_byte(self):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"\x1d\x1d") == [("stdin", b"\x1d")]

    def test_escape_split_across_two_reads_still_works(self):
        """The bug a naive per-chunk scan ships with: a read that ends exactly
        on the escape byte sends it to the shell and eats the next keystroke."""
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"echo hi\x1d") == [("stdin", b"echo hi")]
        assert decoder.armed is True
        assert decoder.feed(b"r") == [("command", "@resume")]

    def test_input_before_and_after_a_command_keeps_its_order(self):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"ab\x1dscd") == [
            ("stdin", b"ab"),
            ("command", "@status"),
            ("stdin", b"cd"),
        ]

    @pytest.mark.parametrize("key", [b"d", b"\x04"])
    def test_detach_keys(self, key):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"\x1d" + key) == [("detach", key)]

    def test_an_unknown_escape_key_is_reported_not_swallowed(self):
        decoder = proto.EscapeDecoder()
        assert decoder.feed(b"\x1dz") == [("unknown", b"z")]


# -----------------------------------------------------------------------------
# The protocol driver
# -----------------------------------------------------------------------------


class TestRunTerminal:
    async def test_a_full_attach_round_trip(self):
        socket = ScriptedSocket([ready(), notice("/workspace is rw"), stdout(b"hello\n")])
        console = ScriptedIO([b"ls -la\n", b"\x1dr"])
        notices: list[str] = []

        result = await debug_cmd.run_terminal(socket, console, notice=notices.append)

        # The shell's output reached the screen, byte-exact.
        assert bytes(console.output) == b"hello\n"
        # The keystrokes reached the wire as stdin, and the escape as a command.
        stdin_frames = socket.frames_of(wire.TYPE_STDIN)
        assert [wire.decode_bytes(f["data"]) for f in stdin_frames] == [b"ls -la\n"]
        assert [f["command"] for f in socket.frames_of(wire.TYPE_COMMAND)] == ["@resume"]
        assert result.commands == ["@resume"]
        # The banner and the ready line are client chatter, not shell output.
        assert any("/workspace is rw" in n for n in notices)
        assert any("c0ffee123456" in n for n in notices)

    async def test_the_initial_window_size_is_announced(self):
        socket = ScriptedSocket()
        console = ScriptedIO(size=(200, 50))

        await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        resize = socket.frames_of(wire.TYPE_RESIZE)
        assert len(resize) == 1
        assert (resize[0]["cols"], resize[0]["rows"]) == (200, 50)

    async def test_no_resize_is_sent_when_the_size_is_unknown(self):
        """A pipe has no window. Sending cols=0 would be a frame the server
        refuses; inventing 80x24 would be a lie about the terminal."""
        socket = ScriptedSocket()
        console = ScriptedIO(size=None)

        await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert socket.frames_of(wire.TYPE_RESIZE) == []

    async def test_arbitrary_output_bytes_survive(self):
        payload = bytes(range(256))
        socket = ScriptedSocket([stdout(payload)])
        console = ScriptedIO([])

        await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert bytes(console.output) == payload

    async def test_a_closed_frame_ends_the_attach_with_the_servers_reason(self):
        socket = ScriptedSocket([ready(), closed("resumed")])
        console = ScriptedIO([], size=(80, 24))
        console.park_at_end = True

        result = await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert result.exit_code == 0
        assert result.reason == "resumed"

    async def test_local_eof_ends_the_attach(self):
        socket = ScriptedSocket([ready()])
        console = ScriptedIO([b"whoami\n"])

        result = await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert result.exit_code == 0
        assert "EOF" in result.reason

    async def test_detach_leaves_the_session_paused_and_says_so(self):
        socket = ScriptedSocket([ready()])
        console = ScriptedIO([b"\x1dd"])
        console.park_at_end = True

        result = await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert result.exit_code == 0
        assert "still paused" in result.reason
        assert "lazyaf debug resume" in result.reason
        # A detach must NOT resume: no command frame reached the wire.
        assert socket.frames_of(wire.TYPE_COMMAND) == []

    async def test_an_undecodable_server_frame_is_reported_not_dropped(self):
        """R1: a frame we cannot understand is surfaced, and the terminal
        keeps running - a silently swallowed frame is invisible corruption."""
        socket = ScriptedSocket(
            [
                '{"v":99,"type":"stdout","data":""}',
                stdout(b"still here"),
                closed("the sidecar shell exited"),
            ]
        )
        console = ScriptedIO([])
        console.park_at_end = True  # the SERVER ends this one, not local EOF
        notices: list[str] = []

        await debug_cmd.run_terminal(socket, console, notice=notices.append)

        assert any("[protocol]" in n and "99" in n for n in notices)
        assert bytes(console.output) == b"still here"

    async def test_an_unknown_escape_key_is_reported_and_sends_nothing(self):
        socket = ScriptedSocket([ready()])
        console = ScriptedIO([b"\x1dz"])
        notices: list[str] = []

        await debug_cmd.run_terminal(socket, console, notice=notices.append)

        assert socket.frames_of(wire.TYPE_STDIN) == []
        assert socket.frames_of(wire.TYPE_COMMAND) == []
        assert any("unknown escape key" in n for n in notices)

    async def test_a_dropped_connection_is_reported_with_its_close_reason(self):
        class Closed(Exception):
            code = wire.CLOSE_BOUND_EXCEEDED
            reason = "frame exceeds 65536 bytes"

        socket = ScriptedSocket([Closed()])
        console = ScriptedIO([])
        console.park_at_end = True

        result = await debug_cmd.run_terminal(socket, console, notice=lambda _t: None)

        assert result.exit_code == 1
        assert "frame exceeds 65536 bytes" in result.reason
        assert str(wire.CLOSE_BOUND_EXCEEDED) in result.reason


# -----------------------------------------------------------------------------
# Close-code handling (a refusal happens BEFORE accept: the reason can only
# travel in the close frame)
# -----------------------------------------------------------------------------


class TestCloseDetails:
    def test_reads_the_modern_rcvd_shape(self):
        exc = types.SimpleNamespace(
            rcvd=types.SimpleNamespace(code=4401, reason="missing or invalid join token")
        )
        assert debug_cmd.close_details(exc) == (4401, "missing or invalid join token")

    def test_reads_the_legacy_flat_shape(self):
        exc = types.SimpleNamespace(rcvd=None, code=4403, reason="remote step")
        assert debug_cmd.close_details(exc) == (4403, "remote step")

    def test_a_plain_exception_yields_nothing_rather_than_inventing_a_code(self):
        assert debug_cmd.close_details(RuntimeError("boom")) == (None, None)

    def test_a_rejected_handshake_reports_its_http_status(self):
        """The shape the endpoint's refusal ACTUALLY takes.

        `routers/debug.py` closes before `accept()`, and Starlette turns that
        into a plain HTTP 403 during the handshake - the close reason it wrote
        never leaves the process. websockets >=13 raises `InvalidStatus`
        carrying a `.response`.
        """
        exc = types.SimpleNamespace(response=types.SimpleNamespace(status_code=403))
        assert debug_cmd.handshake_status(exc) == 403

    def test_the_legacy_handshake_shape_is_read_too(self):
        assert debug_cmd.handshake_status(types.SimpleNamespace(status_code=401)) == 401

    def test_a_close_code_is_not_mistaken_for_a_handshake_status(self):
        assert debug_cmd.handshake_status(RuntimeError("boom")) is None

    def test_the_upgrade_refusal_names_where_the_reason_can_be_read(self):
        """R1: the CLI never invents a reason it was not given, and never
        leaves the operator with a bare number."""
        text = debug_cmd.UPGRADE_REFUSED.format(status=403, session="sess-1")
        assert "403" in text
        assert "lazyaf debug status sess-1" in text
        assert "expired" in text

    @pytest.mark.parametrize("code", sorted(proto.CLOSE_CODE_MEANINGS))
    def test_every_close_code_has_a_sentence_when_the_reason_is_dropped(self, code):
        """An intermediary that strips the reason must not turn a stated
        refusal into a bare number."""
        text = proto.close_reason(code, "")
        assert str(code) in text
        assert len(text) > len(str(code)) + 8

    def test_the_servers_reason_wins_when_it_survives(self):
        assert proto.close_reason(4403, "remote steps do not attach").startswith(
            "remote steps do not attach"
        )

    def test_every_close_code_the_server_can_send_has_a_meaning(self):
        server_codes = {
            value
            for name, value in vars(wire).items()
            if name.startswith("CLOSE_") and isinstance(value, int)
        }
        assert server_codes <= set(proto.CLOSE_CODE_MEANINGS), (
            "the server gained a close code the CLI cannot explain: "
            f"{sorted(server_codes - set(proto.CLOSE_CODE_MEANINGS))}"
        )


# -----------------------------------------------------------------------------
# The console
# -----------------------------------------------------------------------------


class TestConsoleIO:
    async def test_a_pipe_is_line_buffered_and_says_so(self):
        """R1: a line-buffered stream that claimed to be a raw TTY would break
        every full-screen program in a way the operator cannot see."""
        console = debug_cmd.ConsoleIO(
            stdin=io.BytesIO(b"one\ntwo\n"), stdout=io.StringIO()
        )
        console.start()
        try:
            assert console.mode == "line-buffered"
            assert await console.next_input() == b"one\n"
            assert await console.next_input() == b"two\n"
            assert await console.next_input() is None
        finally:
            console.stop()

    def test_output_reaches_a_binary_stream_byte_exact(self):
        class _Out:
            def __init__(self):
                self.buffer = io.BytesIO()

            def flush(self):
                pass

        out = _Out()
        console = debug_cmd.ConsoleIO(stdin=io.BytesIO(), stdout=out)
        console.write_output(bytes(range(256)))
        assert out.buffer.getvalue() == bytes(range(256))

    def test_size_is_a_positive_pair_or_none(self):
        console = debug_cmd.ConsoleIO(stdin=io.BytesIO(), stdout=io.StringIO())
        size = console.size()
        assert size is None or (size[0] > 0 and size[1] > 0)


# -----------------------------------------------------------------------------
# The click surface
# -----------------------------------------------------------------------------


@pytest.fixture
def no_http(monkeypatch, cli_module):
    """Fail loudly if a refusal path ever reaches the network."""

    def _boom(*args, **kwargs):
        raise AssertionError("attach must refuse BEFORE calling the API")

    monkeypatch.setattr(cli_module, "_debug_request", _boom)


class TestAttachCommand:
    def test_shell_is_refused_with_the_reason_never_downgraded(self, no_http):
        """C17: `--shell` is an error naming why, not a silent fall back to
        the sidecar."""
        result = CliRunner().invoke(debug_cmd.attach, ["sess-1", "--shell"])
        assert result.exit_code == 2
        assert "no step container exists at a pre-step breakpoint" in result.output
        assert "--sidecar" in result.output

    def test_the_refusal_text_is_the_servers_word_for_word(self):
        """One sentence, three surfaces (API, WS close, CLI). Drift here is
        an operator reading two different explanations of one rule."""
        assert debug_cmd.SHELL_REFUSED == wire.SHELL_REFUSED_REASON

    def test_an_unattachable_session_is_refused_with_the_api_reason(
        self, monkeypatch, cli_module
    ):
        """C16: a remote-step pause says why out loud rather than silently
        attaching to the wrong volume."""
        reason = "terminal attach is not available for steps running on a remote runner"

        def _request(method, path, server, **kwargs):
            assert path == "/api/debug/sess-1"
            return {"attach_available": False, "attach_unavailable_reason": reason}

        monkeypatch.setattr(cli_module, "_debug_request", _request)
        result = CliRunner().invoke(debug_cmd.attach, ["sess-1"])
        assert result.exit_code == 2
        assert reason in result.output

    def test_print_credential_mints_without_opening_a_socket(
        self, monkeypatch, cli_module
    ):
        calls: list[str] = []

        def _request(method, path, server, **kwargs):
            calls.append(f"{method} {path}")
            if path.endswith("/join-token"):
                return {"token": "jwt-value", "expires_at": "2026-08-30T12:00:00"}
            return {"attach_available": True}

        monkeypatch.setattr(cli_module, "_debug_request", _request)
        monkeypatch.setattr(cli_module, "get_server_url", lambda: "http://localhost:8000")

        def _no_socket(*args, **kwargs):
            raise AssertionError("--print-credential must not open a websocket")

        monkeypatch.setattr(debug_cmd, "attach_socket", _no_socket)

        result = CliRunner().invoke(debug_cmd.attach, ["sess-1", "--print-credential"])

        assert result.exit_code == 0, result.output
        assert calls == ["GET /api/debug/sess-1", "POST /api/debug/sess-1/join-token"]
        assert "jwt-value" in result.output
        assert "ws://localhost:8000/api/debug/sess-1/terminal?mode=sidecar" in result.output
        assert debug_cmd.BANNER in result.output

    def test_the_interactive_path_runs_end_to_end_under_the_click_runner(
        self, monkeypatch, cli_module
    ):
        """The whole command body, with only the SOCKET replaced.

        This is the test that catches wiring bugs the protocol tests cannot:
        `ConsoleIO.start()` binds its reader thread to the RUNNING loop, so
        starting it outside `asyncio.run` raises before a single frame is
        sent. Under CliRunner stdin is a pipe, which also exercises the
        line-buffered path and its stated warning.
        """
        captured = {}

        def _request(method, path, server, **kwargs):
            if path.endswith("/join-token"):
                return {"token": "jwt-value", "expires_at": "2026-08-30T12:00:00"}
            return {"attach_available": True}

        async def _fake_socket(url, token, console_io, *, notice=print, session_id=""):
            captured["url"] = url
            captured["token"] = token
            captured["mode"] = console_io.mode
            captured["session_id"] = session_id
            assert await console_io.next_input() is None, "stdin should be at EOF"
            return debug_cmd.TerminalResult(0, "resumed")

        monkeypatch.setattr(cli_module, "_debug_request", _request)
        monkeypatch.setattr(cli_module, "get_server_url", lambda: "http://localhost:8000")
        monkeypatch.setattr(debug_cmd, "attach_socket", _fake_socket)

        result = CliRunner().invoke(debug_cmd.attach, ["sess-1"], input="")

        assert result.exit_code == 0, result.output
        assert captured["url"].endswith("/api/debug/sess-1/terminal?mode=sidecar")
        assert captured["token"] == "jwt-value"
        assert captured["session_id"] == "sess-1"
        assert captured["mode"] == "line-buffered"
        assert "not a TTY" in result.output
        assert "resumed" in result.output

    def test_register_replaces_the_placeholder_attach_on_the_group(self):
        """The one wiring line `cli.py` needs, asserted rather than assumed."""

        @click.group()
        def group():
            pass

        @group.command("attach")
        def placeholder():
            pass

        assert group.commands["attach"] is placeholder
        debug_cmd.register(group)
        assert group.commands["attach"] is debug_cmd.attach

    def test_the_shipped_cli_group_exposes_every_debug_verb(self, cli_module):
        verbs = set(cli_module.debug.commands)
        assert {"rerun", "list", "status", "attach", "resume", "abort", "extend"} <= verbs

    def test_the_pending_wiring_line_produces_a_working_interactive_attach(
        self, monkeypatch, cli_module
    ):
        """Proof for the ONE edit `cli.py` still needs.

        `cli/lazyaf/cli.py` is not this agent's file, so the two lines that
        install the interactive client are a requested edit:

            from lazyaf.debug_cmd import register as _register_debug_terminal
            _register_debug_terminal(debug)

        Rather than describe them, this applies them at runtime and drives
        `lazyaf debug attach` through the TOP-LEVEL group, so the integrator
        is landing a change that is already proven, and the placeholder is
        restored afterwards.
        """
        placeholder = cli_module.debug.commands["attach"]
        assert placeholder is not debug_cmd.attach, (
            "the wiring already landed - delete this test and the requested "
            "edit note with it"
        )

        opened = {}

        async def _fake_socket(url, token, console_io, *, notice=print, session_id=""):
            opened["url"] = url
            assert await console_io.next_input() is None
            return debug_cmd.TerminalResult(0, "resumed")

        monkeypatch.setattr(
            cli_module,
            "_debug_request",
            lambda method, path, server, **kw: (
                {"token": "jwt-value", "expires_at": "later"}
                if path.endswith("/join-token")
                else {"attach_available": True}
            ),
        )
        monkeypatch.setattr(cli_module, "get_server_url", lambda: "http://localhost:8000")
        monkeypatch.setattr(debug_cmd, "attach_socket", _fake_socket)

        debug_cmd.register(cli_module.debug)
        try:
            result = CliRunner().invoke(
                cli_module.cli, ["debug", "attach", "sess-1"], input=""
            )
        finally:
            cli_module.debug.commands["attach"] = placeholder

        assert result.exit_code == 0, result.output
        assert opened["url"].endswith("/api/debug/sess-1/terminal?mode=sidecar")
        assert "resumed" in result.output


class TestWebsocketsExtra:
    def test_absence_is_a_stated_error_naming_the_install_command(self, monkeypatch):
        """R1: no silent degrade to the credential-printing path."""
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def _no_websockets(name, *args, **kwargs):
            if name.startswith("websockets"):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _no_websockets)
        with pytest.raises(click.ClickException) as exc:
            debug_cmd._load_websockets()
        assert debug_cmd.TERMINAL_EXTRA_INSTALL in str(exc.value)
        assert "--print-credential" in str(exc.value)

    def test_the_extra_is_declared_and_bounded_on_both_ends(self):
        """A published wheel's install footprint is a decision; an unpinned
        major can break installs of an already-published wheel."""
        from packaging.requirements import Requirement

        data = tomllib.loads((CLI_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        extras = data["project"]["optional-dependencies"]
        assert "terminal" in extras, (
            "the interactive terminal's dependency must be DECLARED - an "
            "undeclared import is a runtime surprise, not a distribution"
        )
        requirement = Requirement(extras["terminal"][0])
        assert requirement.name == "websockets"
        operators = {spec.operator for spec in requirement.specifier}
        assert ">=" in operators, "websockets has no lower bound"
        assert operators & {"<", "<=", "=="}, "websockets has no upper bound"

    def test_the_extra_is_not_also_a_core_dependency(self):
        """Stated deviation: `websockets` is an EXTRA. Every debug verb except
        the interactive shell is plain HTTP, and tdd/unit/packaging pins the
        core dependency set so that growing a published wheel stays
        deliberate."""
        data = tomllib.loads((CLI_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        assert not any(
            dep.startswith("websockets") for dep in data["project"]["dependencies"]
        )
