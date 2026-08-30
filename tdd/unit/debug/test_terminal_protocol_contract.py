"""BOTH SIDES of the debug-terminal wire contract - C12/C13, Phase 12.7.

This file is the only reason the codec is allowed to exist twice.

The server half lives in `backend/app/services/execution/debug_terminal.py`;
the client half in `cli/lazyaf/debug_protocol.py`, because a published
`lazyaf-cli` wheel installs on a machine with no backend checkout. Two copies
pinned by nothing would drift on the first change; these tests import BOTH and:

1. compare every shared constant, name by name - so renaming a frame type,
   adding a `@`-verb or moving a close code on one side fails here;
2. round-trip every frame type in BOTH directions - server-encoded frames are
   decoded by the client and vice versa, with byte-exact payloads including
   the bytes that are not valid UTF-8 (the exact case raw-text framing would
   have corrupted, C12);
3. compare REJECTIONS - a malformed frame must be refused by both halves,
   because a client that accepts what the server refuses is how a
   half-understood frame reaches a terminal.

R3: one wire contract, one place it is decided. That place is this test.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "cli"))

from app.services.execution import debug_terminal as server  # noqa: E402
from lazyaf import debug_protocol as client  # noqa: E402


#: Every name the two halves must define identically. Listed explicitly
#: rather than derived from `__all__`: the server module also owns the sidecar
#: container lifecycle and the client also owns the local escape key, so
#: comparing whole export lists would compare things that are deliberately
#: one-sided.
SHARED_SCALARS = (
    "PROTOCOL_VERSION",
    "TYPE_STDIN",
    "TYPE_RESIZE",
    "TYPE_COMMAND",
    "TYPE_PING",
    "TYPE_READY",
    "TYPE_STDOUT",
    "TYPE_NOTICE",
    "TYPE_CLOSED",
    "TYPE_PONG",
    "COMMANDS",
    "CLOSE_NORMAL",
    "CLOSE_BAD_TOKEN",
    "CLOSE_NOT_ATTACHABLE",
    "CLOSE_UNKNOWN_SESSION",
    "CLOSE_DUPLICATE_TERMINAL",
    "CLOSE_BOUND_EXCEEDED",
    "MAX_FRAME_BYTES",
    "MAX_OUTBOUND_QUEUE",
    "RATE_WINDOW_SECONDS",
    "RATE_MAX_FRAMES_PER_WINDOW",
    "CONNECTION_MODE_SIDECAR",
)

SHARED_SETS = ("CLIENT_FRAME_TYPES", "SERVER_FRAME_TYPES")


class TestConstantParity:
    """The two halves agree on every value that travels on the wire."""

    @pytest.mark.parametrize("name", SHARED_SCALARS)
    def test_scalar_constants_match(self, name):
        assert hasattr(server, name), f"the server codec lost {name}"
        assert hasattr(client, name), f"the CLI codec lost {name}"
        assert getattr(server, name) == getattr(client, name), (
            f"{name} drifted: server={getattr(server, name)!r} "
            f"client={getattr(client, name)!r}. One wire contract, one value "
            f"(R3)."
        )

    @pytest.mark.parametrize("name", SHARED_SETS)
    def test_frame_type_sets_match(self, name):
        assert set(getattr(server, name)) == set(getattr(client, name))

    def test_no_frame_type_is_defined_on_only_one_side(self):
        """A type either half knows must be a type both halves know.

        Derived from the modules rather than from the list above, so a NEW
        frame type added to one side fails here even though nobody thought to
        extend SHARED_SCALARS.
        """
        def types(module):
            return {
                value
                for name, value in vars(module).items()
                if name.startswith("TYPE_") and isinstance(value, str)
            }

        assert types(server) == types(client)

    def test_command_verbs_are_the_same_tuple_in_the_same_order(self):
        # Order matters: it is what `@help` and the CLI's escape menu print.
        assert server.COMMANDS == client.COMMANDS

    def test_both_refuse_a_frame_larger_than_the_shared_bound(self):
        assert server.MAX_FRAME_BYTES == client.MAX_FRAME_BYTES == 64 * 1024


class TestServerToClient:
    """Frames the SERVER emits decode on the CLIENT."""

    def test_ready(self):
        raw = server.encode_frame(
            server.TYPE_READY,
            mode=server.CONNECTION_MODE_SIDECAR,
            container_id="abc123def456",
        )
        frame = client.decode_frame(raw)
        assert frame["type"] == client.TYPE_READY
        assert frame["mode"] == client.CONNECTION_MODE_SIDECAR
        assert frame["container_id"] == "abc123def456"

    def test_stdout_carries_arbitrary_bytes(self):
        payload = bytes(range(256))
        raw = server.encode_frame(
            server.TYPE_STDOUT, data=server.encode_bytes(payload)
        )
        frame = client.decode_frame(raw)
        assert client.decode_bytes(frame["data"]) == payload

    def test_stdout_carries_bytes_that_are_not_utf8(self):
        """The exact case raw-text framing would have corrupted (C12)."""
        payload = b"\xff\xfe\x00\x80 not utf-8 \x9c"
        raw = server.encode_frame(
            server.TYPE_STDOUT, data=server.encode_bytes(payload)
        )
        assert client.decode_bytes(client.decode_frame(raw)["data"]) == payload

    def test_notice(self):
        raw = server.encode_frame(server.TYPE_NOTICE, text="/workspace is rw")
        assert client.decode_frame(raw)["text"] == "/workspace is rw"

    def test_closed(self):
        raw = server.encode_frame(server.TYPE_CLOSED, reason="resumed")
        frame = client.decode_frame(raw)
        assert frame["type"] == client.TYPE_CLOSED
        assert frame["reason"] == "resumed"

    def test_pong(self):
        assert client.decode_frame(server.encode_frame(server.TYPE_PONG))[
            "type"
        ] == client.TYPE_PONG


class TestClientToServer:
    """Frames the CLIENT emits decode on the SERVER."""

    def test_stdin_carries_arbitrary_bytes(self):
        payload = bytes(range(256))
        raw = client.encode_frame(
            client.TYPE_STDIN, data=client.encode_bytes(payload)
        )
        frame = server.decode_frame(raw)
        assert server.decode_bytes(frame["data"]) == payload

    def test_resize(self):
        raw = client.encode_frame(client.TYPE_RESIZE, cols=120, rows=40)
        frame = server.decode_frame(raw)
        assert (frame["cols"], frame["rows"]) == (120, 40)

    def test_ping(self):
        assert server.decode_frame(client.encode_frame(client.TYPE_PING))[
            "type"
        ] == server.TYPE_PING

    @pytest.mark.parametrize("command", client.COMMANDS)
    def test_every_command_verb_round_trips(self, command):
        raw = client.encode_frame(client.TYPE_COMMAND, command=command)
        assert server.decode_frame(raw)["command"] == command

    def test_the_wire_shape_is_the_same_json_both_ways(self):
        """Byte-identical output, not merely mutually-decodable output.

        A difference here (a key order, a separator, an extra field) is
        invisible to the decoders and visible to anything that logs, hashes or
        diffs a frame.
        """
        payload = b"ls -la /workspace\n"
        assert client.encode_frame(
            client.TYPE_STDIN, data=client.encode_bytes(payload)
        ) == server.encode_frame(
            server.TYPE_STDIN, data=server.encode_bytes(payload)
        )


#: Malformed payloads BOTH halves must refuse. Each is a frame a buggy or
#: hostile peer could actually send.
MALFORMED = {
    "not-json": "{ not json",
    "not-an-object": json.dumps([1, 2, 3]),
    "wrong-version": json.dumps({"v": 99, "type": "stdin", "data": ""}),
    "missing-version": json.dumps({"type": "ping"}),
    "unknown-type": json.dumps({"v": 1, "type": "exec"}),
    "stdin-without-data": json.dumps({"v": 1, "type": "stdin"}),
    "stdin-non-string-data": json.dumps({"v": 1, "type": "stdin", "data": 7}),
    "stdin-not-base64": json.dumps({"v": 1, "type": "stdin", "data": "not!base64"}),
    "stdout-not-base64": json.dumps({"v": 1, "type": "stdout", "data": "@@@"}),
    "resize-missing-rows": json.dumps({"v": 1, "type": "resize", "cols": 80}),
    "resize-zero": json.dumps({"v": 1, "type": "resize", "cols": 0, "rows": 24}),
    "resize-negative": json.dumps({"v": 1, "type": "resize", "cols": 80, "rows": -1}),
    "resize-bool": json.dumps({"v": 1, "type": "resize", "cols": True, "rows": 24}),
    "resize-float": json.dumps({"v": 1, "type": "resize", "cols": 80.5, "rows": 24}),
    "unknown-command": json.dumps({"v": 1, "type": "command", "command": "@rm"}),
    "command-without-verb": json.dumps({"v": 1, "type": "command"}),
}


class TestRejectionParity:
    """A frame one half refuses, the other half refuses too."""

    @pytest.mark.parametrize("case", sorted(MALFORMED), ids=sorted(MALFORMED))
    def test_both_halves_refuse(self, case):
        raw = MALFORMED[case]
        with pytest.raises(server.DebugProtocolError):
            server.decode_frame(raw)
        with pytest.raises(client.DebugProtocolError):
            client.decode_frame(raw)

    def test_both_refuse_a_frame_that_is_not_utf8(self):
        raw = b"\xff\xfe{}"
        with pytest.raises(server.DebugProtocolError):
            server.decode_frame(raw)
        with pytest.raises(client.DebugProtocolError):
            client.decode_frame(raw)

    def test_both_refuse_raw_bytes_in_an_outgoing_data_field(self):
        """C12 enforced at the ENCODER, so a raw-text frame cannot be built."""
        with pytest.raises(server.DebugProtocolError):
            server.encode_frame(server.TYPE_STDOUT, data=b"raw")
        with pytest.raises(client.DebugProtocolError):
            client.encode_frame(client.TYPE_STDIN, data=b"raw")

    def test_both_refuse_to_encode_an_unknown_frame_type(self):
        with pytest.raises(server.DebugProtocolError):
            server.encode_frame("exec", cmd="sh")
        with pytest.raises(client.DebugProtocolError):
            client.encode_frame("exec", cmd="sh")

    def test_both_error_types_are_valueerrors(self):
        """Callers that catch ValueError behave the same against either half."""
        assert issubclass(server.DebugProtocolError, ValueError)
        assert issubclass(client.DebugProtocolError, ValueError)


class TestBase64Parity:
    @pytest.mark.parametrize(
        "payload",
        [b"", b"a", b"\x00", bytes(range(256)), b"\xff" * 1024, "héllo".encode()],
        ids=["empty", "one-byte", "nul", "all-256", "1k-high-bytes", "utf8"],
    )
    def test_encode_bytes_agrees(self, payload):
        assert server.encode_bytes(payload) == client.encode_bytes(payload)

    @pytest.mark.parametrize(
        "payload", [b"", b"\x00\x01\x02", bytes(range(256))],
        ids=["empty", "control", "all-256"],
    )
    def test_decode_bytes_agrees(self, payload):
        encoded = server.encode_bytes(payload)
        assert server.decode_bytes(encoded) == client.decode_bytes(encoded) == payload

    def test_both_refuse_padding_free_garbage(self):
        with pytest.raises(server.DebugProtocolError):
            server.decode_bytes("A")
        with pytest.raises(client.DebugProtocolError):
            client.decode_bytes("A")


class TestClientOnlySurface:
    """The escape key is the CLIENT's, and the server must never see it.

    Stated as a test because the tempting shortcut - sniffing stdin for a
    leading `@` - is exactly what C12 forbids: it corrupts any program that
    legitimately reads `@...`.
    """

    def test_the_escape_byte_is_not_part_of_the_wire_protocol(self):
        assert not hasattr(server, "ESCAPE_BYTE")
        assert client.ESCAPE_BYTE == b"\x1d"

    def test_every_escape_key_maps_to_a_real_wire_command(self):
        for key, command in client.ESCAPE_KEYS.items():
            assert command in server.COMMANDS, (
                f"escape key {key!r} sends {command!r}, which the server's "
                f"command vocabulary does not contain"
            )

    def test_every_wire_command_is_reachable_from_the_keyboard(self):
        """No verb exists that a TTY user cannot invoke."""
        assert set(client.ESCAPE_KEYS.values()) == set(server.COMMANDS)
