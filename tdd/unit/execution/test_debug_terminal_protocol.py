"""Debug terminal frame codec - contracts C12, C15, C17, Phase 12.7.

The backend half of the terminal wire contract. The CLI half lives in
`cli/lazyaf/debug_protocol.py` (lane B) and is pinned against THIS module by
`tdd/unit/debug/test_terminal_protocol_contract.py` - one codec, two callers
(R3).

Two things these tests exist to make impossible:

- **Raw text payloads.** failure_01's sketch put terminal bytes straight into
  a JSON string field. A terminal emits arbitrary bytes; the first `less` on
  a binary file would have produced an encoding error or, worse, silently
  mangled output. `data` is base64 or the frame is refused.
- **`@`-commands sniffed out of stdin.** Scanning the byte stream for a
  leading `@` corrupts any program that legitimately reads `@...`. Commands
  are their own frame type.
"""
import json
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.execution import debug_terminal as protocol


class TestFrameRoundTrip:
    def test_stdin_round_trips_arbitrary_bytes(self):
        payload = bytes(range(256))
        raw = protocol.encode_frame(
            protocol.TYPE_STDIN, data=protocol.encode_bytes(payload)
        )
        frame = protocol.decode_frame(raw)
        assert protocol.decode_bytes(frame["data"]) == payload

    def test_stdout_round_trips_invalid_utf8(self):
        """The exact case raw-text framing would have corrupted."""
        payload = b"\xff\xfe\x00broken"
        raw = protocol.encode_frame(
            protocol.TYPE_STDOUT, data=protocol.encode_bytes(payload)
        )
        assert protocol.decode_bytes(protocol.decode_frame(raw)["data"]) == payload

    def test_every_frame_carries_the_protocol_version(self):
        for frame_type in sorted(
            protocol.CLIENT_FRAME_TYPES | protocol.SERVER_FRAME_TYPES
        ):
            extra = {}
            if frame_type in (protocol.TYPE_STDIN, protocol.TYPE_STDOUT):
                extra["data"] = protocol.encode_bytes(b"x")
            elif frame_type == protocol.TYPE_RESIZE:
                extra = {"cols": 80, "rows": 24}
            elif frame_type == protocol.TYPE_COMMAND:
                extra = {"command": "@status"}
            raw = protocol.encode_frame(frame_type, **extra)
            assert json.loads(raw)["v"] == protocol.PROTOCOL_VERSION

    def test_resize_round_trips(self):
        frame = protocol.decode_frame(
            protocol.encode_frame(protocol.TYPE_RESIZE, cols=120, rows=40)
        )
        assert (frame["cols"], frame["rows"]) == (120, 40)

    def test_ready_carries_mode_and_container(self):
        frame = protocol.decode_frame(
            protocol.encode_frame(
                protocol.TYPE_READY, mode="sidecar", container_id="abc123"
            )
        )
        assert frame["mode"] == "sidecar"
        assert frame["container_id"] == "abc123"


class TestFrameRefusals:
    def test_raw_bytes_in_data_is_a_programming_error(self):
        """C12: `encode_frame` refuses raw bytes rather than guessing."""
        with pytest.raises(protocol.DebugProtocolError):
            protocol.encode_frame(protocol.TYPE_STDOUT, data=b"raw")

    def test_non_base64_data_is_refused_at_decode(self):
        raw = json.dumps({"v": 1, "type": "stdin", "data": "not base64!!"})
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame(raw)

    def test_missing_data_field_is_refused(self):
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame(json.dumps({"v": 1, "type": "stdin"}))

    def test_unknown_frame_type_is_refused(self):
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame(json.dumps({"v": 1, "type": "exec"}))

    def test_wrong_protocol_version_is_refused_by_number(self):
        with pytest.raises(protocol.DebugProtocolError) as exc:
            protocol.decode_frame(json.dumps({"v": 99, "type": "ping"}))
        assert "99" in str(exc.value)

    def test_non_json_is_refused(self):
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame("not json at all")

    def test_json_array_is_refused(self):
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame("[1, 2, 3]")

    def test_unknown_command_is_refused_and_names_the_vocabulary(self):
        with pytest.raises(protocol.DebugProtocolError) as exc:
            protocol.decode_frame(
                json.dumps({"v": 1, "type": "command", "command": "@rm-rf"})
            )
        assert "@resume" in str(exc.value)

    @pytest.mark.parametrize("bad", [0, -1, "80", True, None])
    def test_resize_needs_positive_ints(self, bad):
        with pytest.raises(protocol.DebugProtocolError):
            protocol.decode_frame(
                json.dumps({"v": 1, "type": "resize", "cols": bad, "rows": 24})
            )


class TestCommandsAreTheirOwnFrameType:
    def test_all_four_verbs_are_accepted(self):
        for command in protocol.COMMANDS:
            frame = protocol.decode_frame(
                protocol.encode_frame(protocol.TYPE_COMMAND, command=command)
            )
            assert frame["command"] == command

    def test_at_prefixed_stdin_stays_stdin(self):
        """An `@resume` a program legitimately reads must NOT become a command."""
        raw = protocol.encode_frame(
            protocol.TYPE_STDIN, data=protocol.encode_bytes(b"@resume\n")
        )
        frame = protocol.decode_frame(raw)
        assert frame["type"] == protocol.TYPE_STDIN
        assert "command" not in frame


class TestBoundsAndRefusalText:
    def test_every_close_code_is_distinct(self):
        codes = [
            protocol.CLOSE_NORMAL,
            protocol.CLOSE_BAD_TOKEN,
            protocol.CLOSE_NOT_ATTACHABLE,
            protocol.CLOSE_UNKNOWN_SESSION,
            protocol.CLOSE_DUPLICATE_TERMINAL,
            protocol.CLOSE_BOUND_EXCEEDED,
        ]
        assert len(set(codes)) == len(codes)

    def test_shell_refusal_names_the_reason(self):
        """C17: `--shell` is an error with a reason, not a silent downgrade."""
        assert "pre-step breakpoint" in protocol.SHELL_REFUSED_REASON
        assert "--sidecar" in protocol.SHELL_REFUSED_REASON

    def test_remote_refusal_names_the_reason(self):
        """C16: the remote deferral is stated, never a silent degrade."""
        assert "remote runner" in protocol.REMOTE_ATTACH_REASON

    def test_bounds_are_declared_not_implicit(self):
        assert protocol.MAX_FRAME_BYTES == 64 * 1024
        assert protocol.MAX_OUTBOUND_QUEUE == 256
        assert protocol.RATE_MAX_FRAMES_PER_WINDOW > 0


class TestSidecarAddressing:
    def test_sidecar_mounts_the_run_s_own_workspace_volume(self):
        """The sidecar must see the SAME volume the resumed step will."""
        from app.services.workspace.state_machine import generate_volume_name

        run_id = "11111111-2222-3333-4444-555555555555"
        assert protocol.workspace_volume_name(run_id) == generate_volume_name(run_id)

    def test_sidecar_image_is_overridable_but_defaulted(self, monkeypatch):
        monkeypatch.delenv(protocol.SIDECAR_IMAGE_ENV, raising=False)
        assert protocol.sidecar_image() == protocol.DEFAULT_SIDECAR_IMAGE
        monkeypatch.setenv(protocol.SIDECAR_IMAGE_ENV, "custom:tag")
        assert protocol.sidecar_image() == "custom:tag"
