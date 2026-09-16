"""The debug-terminal wire contract - C12/C13, Phase 12.7; as DATA since the Go CLI.

The codec is defined ONCE, in `backend/app/services/execution/debug_terminal.py`.
Every client is a copy of it, and a copy pinned by nothing drifts on the first
change. Two mechanisms pin the copies, and this file holds the Python half of
both:

1. THE CORPUS (go-cli plan section 3). The server EMITS its contract
   (`export_contract()`), `scripts/gen_debug_terminal_corpus.py` writes it to
   `tdd/contracts/debug_terminal.v1.json`, and every client - the Go binary
   in `cli/internal/debugproto` - is tested against that file. Only the
   server can write it. This file proves two things about it:

   - it is FRESH: a regeneration in memory equals the committed bytes, so a
     constant that moved without a regenerate goes red HERE first, with the
     command that fixes it (`TestCorpusIsCurrent`);
   - the server SATISFIES it: its encoder reproduces every wire string, its
     decoder accepts every frame and refuses every malformed one, its base64
     agrees. A hand-edited corpus the server itself cannot satisfy is caught
     even before anyone runs the generator (`TestServerSatisfiesItsOwnCorpus`).

2. THE CROSS-IMPORT (until the deletion commit). The Python client in
   `cli/lazyaf/debug_protocol.py` is still what users install, so it is still
   imported here and compared name by name, frame by frame, refusal by
   refusal (`TestConstantParity` .. `TestClientOnlySurface`). While the
   Python client passes the corpus, the corpus is a faithful transcription of
   the wire that ships TODAY - which is what the Go port is held to. The
   deletion commit drops these classes and the `from lazyaf import
   debug_protocol` line; nothing in the corpus half depends on them.

R3: one wire contract, one place it is decided. That place is the server
module; this file is where a copy is caught drifting from it.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "cli"))

from app.services.execution import debug_terminal as server  # noqa: E402
from lazyaf import debug_protocol as client  # noqa: E402

GENERATOR = REPO_ROOT / "scripts" / "gen_debug_terminal_corpus.py"
CORPUS = REPO_ROOT / "tdd" / "contracts" / f"debug_terminal.v{server.PROTOCOL_VERSION}.json"


def _load_generator():
    """The generator as a module, so the test renders with ITS code path.

    Loaded by path (it is a script, not a package) and given the module the
    backend actually runs, so `render_corpus` here and `render_corpus` in the
    script are the same function over the same constants.
    """
    spec = importlib.util.spec_from_file_location("lazyaf_gen_debug_terminal_corpus", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("lazyaf_gen_debug_terminal_corpus", module)
    spec.loader.exec_module(module)
    return module


# Loaded at import so the frame / malformed / base64 cases can be
# parametrized by name: gotestsum-style, one testcase per corpus entry, so a
# corpus that silently shrinks lowers T1's executed count (R4 for free). A
# missing file is a collection error, which is louder than a skip.
CORPUS_TEXT = CORPUS.read_text(encoding="utf-8")
CORPUS_DATA = json.loads(CORPUS_TEXT)

FRAME_CASES = {case["name"]: case for case in CORPUS_DATA["frames"]}
MALFORMED_CASES = {
    case["name"]: case for case in CORPUS_DATA["malformed"] if "name" in case
}
ENCODER_REFUSALS = [case for case in CORPUS_DATA["malformed"] if "name" not in case]
B64_ENCODE = {v["bytes_hex"] or "empty": v for v in CORPUS_DATA["base64"]["encode"]}
B64_DECODE = {v["bytes_hex"] or "empty": v for v in CORPUS_DATA["base64"]["decode"]}


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


def _server_value(name):
    """A server constant in its JSON shape (tuples become lists)."""
    value = getattr(server, name)
    return list(value) if isinstance(value, tuple) else value


# ===========================================================================
# 1. THE CORPUS
# ===========================================================================


class TestCorpusIsCurrent:
    """The committed file IS a fresh export. This is what fails when a server
    constant moves and nobody regenerated."""

    def test_committed_file_equals_a_fresh_export(self):
        generator = _load_generator()
        fresh = generator.render_corpus(server)
        assert CORPUS_TEXT == fresh, (
            "corpus stale - run python scripts/gen_debug_terminal_corpus.py and commit"
        )

    def test_the_generator_check_mode_agrees(self):
        """`--check` is what a developer and the acceptance gate run; it must
        see the same currency this test sees, through its own loader."""
        generator = _load_generator()
        assert generator.main(["--check"]) == 0

    def test_the_file_is_named_for_the_protocol_version(self):
        # `.v1` is PROTOCOL_VERSION. A v2 is a NEW file both consumers opt
        # into, never this one rewritten to mean something else.
        assert CORPUS.name == f"debug_terminal.v{server.PROTOCOL_VERSION}.json"
        assert CORPUS_DATA["constants"]["scalars"]["PROTOCOL_VERSION"] == server.PROTOCOL_VERSION

    def test_the_corpus_holds_exactly_the_shared_scalars(self):
        """The exporter's name list and this file's name list are the same
        22 names; a name dropped from either side is visible here."""
        assert set(CORPUS_DATA["constants"]["scalars"]) == set(SHARED_SCALARS)
        assert set(server.CONTRACT_SCALARS) == set(SHARED_SCALARS)
        assert set(CORPUS_DATA["constants"]["sets"]) == set(SHARED_SETS)

    def test_the_corpus_is_written_by_the_generator_only(self):
        """The docstring promise, checked: the script says it is the only writer."""
        text = GENERATOR.read_text(encoding="utf-8")
        assert "ONLY WRITER" in text
        assert "hand edit" in text


class TestServerSatisfiesItsOwnCorpus:
    """An exporter that lies cannot produce a green file: every case in the
    corpus is replayed through the server's own codec."""

    @pytest.mark.parametrize("name", sorted(FRAME_CASES), ids=sorted(FRAME_CASES))
    def test_encoder_reproduces_the_wire_byte_for_byte(self, name):
        case = FRAME_CASES[name]
        fields = dict(case["fields"])
        assert server.encode_frame(case["type"], **fields) == case["wire"]

    @pytest.mark.parametrize("name", sorted(FRAME_CASES), ids=sorted(FRAME_CASES))
    def test_decoder_yields_the_listed_fields(self, name):
        case = FRAME_CASES[name]
        frame = server.decode_frame(case["wire"])
        assert frame["v"] == server.PROTOCOL_VERSION
        assert frame["type"] == case["type"]
        for key, value in case["fields"]:
            if value is None:
                # A None field is DROPPED by the encoder and therefore absent
                # after decode - never present as null.
                assert key not in frame, f"{name}: None field {key!r} reached the wire"
            else:
                assert frame[key] == value
        listed = {key for key, value in case["fields"] if value is not None}
        assert set(frame) == {"v", "type"} | listed, "the wire carries an unlisted field"

    @pytest.mark.parametrize(
        "name",
        sorted(n for n, c in FRAME_CASES.items() if "data_hex" in c),
        ids=sorted(n for n, c in FRAME_CASES.items() if "data_hex" in c),
    )
    def test_data_frames_carry_the_stated_bytes(self, name):
        case = FRAME_CASES[name]
        frame = server.decode_frame(case["wire"])
        assert server.decode_bytes(frame["data"]) == bytes.fromhex(case["data_hex"])

    def test_every_direction_is_one_of_the_two(self):
        directions = {case["direction"] for case in FRAME_CASES.values()}
        assert directions == {"server->client", "client->server"}
        for case in FRAME_CASES.values():
            expected = (
                server.SERVER_FRAME_TYPES
                if case["direction"] == "server->client"
                else server.CLIENT_FRAME_TYPES
            )
            assert case["type"] in expected, f"{case['name']} travels the wrong way"

    def test_every_frame_type_has_at_least_one_case(self):
        """A frame type with no case is a frame type no client is tested on."""
        covered = {case["type"] for case in FRAME_CASES.values()}
        assert covered == set(server.CLIENT_FRAME_TYPES | server.SERVER_FRAME_TYPES)

    def test_every_command_verb_has_a_case_in_order(self):
        verbs = [
            case["fields"][0][1]
            for case in CORPUS_DATA["frames"]
            if case["type"] == server.TYPE_COMMAND
        ]
        assert verbs == list(server.COMMANDS)

    def test_the_none_dropping_case_is_present(self):
        """The one case that proves a None field never reaches the wire."""
        case = FRAME_CASES["closed-without-reason"]
        assert case["fields"] == [["reason", None]]
        assert case["wire"] == '{"v":1,"type":"closed"}'

    @pytest.mark.parametrize("name", sorted(MALFORMED_CASES), ids=sorted(MALFORMED_CASES))
    def test_decoder_refuses_every_malformed_case(self, name):
        case = MALFORMED_CASES[name]
        raw = bytes.fromhex(case["raw_hex"]) if "raw_hex" in case else case["raw"]
        with pytest.raises(server.DebugProtocolError):
            server.decode_frame(raw)

    def test_the_malformed_table_is_the_modules_own(self):
        """The corpus lists MALFORMED verbatim and in order, plus the
        not-UTF-8 frame: a new validation rule is added to the module's
        table and reaches every client through the corpus."""
        listed = [(c["name"], c["raw"]) for c in CORPUS_DATA["malformed"] if "raw" in c]
        assert listed == list(server.MALFORMED.items())
        assert len(server.MALFORMED) == 16
        assert MALFORMED_CASES["not-utf8"]["raw_hex"] == server.MALFORMED_NOT_UTF8.hex()

    def test_encoder_refuses_what_the_corpus_says_it_refuses(self):
        assert ENCODER_REFUSALS == [
            {"encode_unknown_type": "exec"},
            {"encode_raw_bytes_in_data": True},
        ]
        with pytest.raises(server.DebugProtocolError):
            server.encode_frame(ENCODER_REFUSALS[0]["encode_unknown_type"], cmd="sh")
        with pytest.raises(server.DebugProtocolError):
            server.encode_frame(server.TYPE_STDOUT, data=b"raw")

    @pytest.mark.parametrize("key", sorted(B64_ENCODE), ids=sorted(B64_ENCODE))
    def test_base64_encode_vectors(self, key):
        vector = B64_ENCODE[key]
        assert server.encode_bytes(bytes.fromhex(vector["bytes_hex"])) == vector["text"]

    @pytest.mark.parametrize("key", sorted(B64_DECODE), ids=sorted(B64_DECODE))
    def test_base64_decode_vectors(self, key):
        vector = B64_DECODE[key]
        assert server.decode_bytes(vector["text"]) == bytes.fromhex(vector["bytes_hex"])

    def test_base64_reject_vectors(self):
        # A PROPERTY, not a restated list: every vector the corpus says is
        # rejected must be refused by the server that emitted it, and the one
        # short symbol "A" must be among them. An exact-list pin here would
        # fail the moment the corpus gains a vector (it did: the CR/LF forms
        # Go's Strict() decoder would otherwise accept, V1-2) without proving
        # anything about the codec.
        rejects = CORPUS_DATA["base64"]["reject"]
        assert "A" in rejects
        assert len(rejects) >= 1
        for text in CORPUS_DATA["base64"]["reject"]:
            with pytest.raises(server.DebugProtocolError):
                server.decode_bytes(text)

    def test_base64_vectors_include_the_hard_cases(self):
        """Empty, NUL, every byte value, 1 KiB of high bytes, and UTF-8 text:
        the vectors a base64 that is 'almost right' gets wrong."""
        encoded = {bytes.fromhex(v["bytes_hex"]) for v in CORPUS_DATA["base64"]["encode"]}
        assert {b"", b"a", b"\x00", bytes(range(256)), b"\xff" * 1024, "héllo".encode()} <= encoded

    @pytest.mark.parametrize("name", SHARED_SCALARS)
    def test_every_scalar_is_the_servers_value(self, name):
        assert CORPUS_DATA["constants"]["scalars"][name] == _server_value(name)

    @pytest.mark.parametrize("name", SHARED_SETS)
    def test_every_set_is_the_servers_set_sorted(self, name):
        assert CORPUS_DATA["constants"]["sets"][name] == sorted(getattr(server, name))

    def test_reasons_are_the_servers_sentences(self):
        reasons = CORPUS_DATA["constants"]["reasons"]
        assert reasons == {
            "SHELL_REFUSED_REASON": server.SHELL_REFUSED_REASON,
            "REMOTE_ATTACH_REASON": server.REMOTE_ATTACH_REASON,
        }


class TestDerivedTypes:
    def test_every_TYPE_star_is_in_the_corpus(self):
        """Derived from the module rather than from SHARED_SCALARS, so a NEW
        frame type added to the server fails here even though nobody thought
        to extend the list - and the corpus goes stale in the same breath."""
        derived = sorted(
            value
            for name, value in vars(server).items()
            if name.startswith("TYPE_") and isinstance(value, str)
        )
        assert CORPUS_DATA["constants"]["derived_types"] == derived
        # And every derived type is a member of exactly one of the two sets.
        for value in derived:
            assert (value in server.CLIENT_FRAME_TYPES) != (value in server.SERVER_FRAME_TYPES)


class TestEscapeSurfaceIsClientOnly:
    def test_the_server_has_no_escape_byte(self):
        """The escape key is the CLIENT's, and the server must never see it.

        Stated as a test because the tempting shortcut - sniffing stdin for a
        leading `@` - is exactly what C12 forbids: it corrupts any program
        that legitimately reads `@...`. The corpus carries no escape byte
        either; a client's escape table is its own, pinned by its own tests
        against `constants.scalars.COMMANDS`.
        """
        assert not hasattr(server, "ESCAPE_BYTE")
        assert "ESCAPE_BYTE" not in json.dumps(CORPUS_DATA)


# ===========================================================================
# 2. THE CROSS-IMPORT - kept until the deletion commit (go-cli plan 3.3)
# ===========================================================================


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

    @pytest.mark.parametrize("name", SHARED_SCALARS)
    def test_the_client_holds_the_corpus_value(self, name):
        """The Python client passes the corpus, so the corpus pins what the
        wire actually is today - the standard the Go client is held to."""
        value = getattr(client, name)
        value = list(value) if isinstance(value, tuple) else value
        assert CORPUS_DATA["constants"]["scalars"][name] == value


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

    @pytest.mark.parametrize(
        "name",
        sorted(n for n, c in FRAME_CASES.items() if c["direction"] == "server->client"),
    )
    def test_the_client_decodes_every_server_case_in_the_corpus(self, name):
        case = FRAME_CASES[name]
        frame = client.decode_frame(case["wire"])
        for key, value in case["fields"]:
            if value is None:
                assert key not in frame
            else:
                assert frame[key] == value
        if "data_hex" in case:
            assert client.decode_bytes(frame["data"]) == bytes.fromhex(case["data_hex"])


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

    @pytest.mark.parametrize("name", sorted(FRAME_CASES), ids=sorted(FRAME_CASES))
    def test_the_client_encoder_reproduces_every_corpus_wire(self, name):
        """The Python client's encoder is generic, so every corpus case in
        BOTH directions is a free byte-exact pin - the same bar the Go
        encoder's `TestContractFrames` is held to."""
        case = FRAME_CASES[name]
        assert client.encode_frame(case["type"], **dict(case["fields"])) == case["wire"]


class TestRejectionParity:
    """A frame one half refuses, the other half refuses too."""

    @pytest.mark.parametrize(
        "case", sorted(server.MALFORMED), ids=sorted(server.MALFORMED)
    )
    def test_both_halves_refuse(self, case):
        raw = server.MALFORMED[case]
        with pytest.raises(server.DebugProtocolError):
            server.decode_frame(raw)
        with pytest.raises(client.DebugProtocolError):
            client.decode_frame(raw)

    def test_both_refuse_a_frame_that_is_not_utf8(self):
        raw = server.MALFORMED_NOT_UTF8
        assert raw == b"\xff\xfe{}"
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

    def test_every_escape_verb_is_in_the_corpus(self):
        """The Go client's escape table is pinned to `constants.scalars.COMMANDS`;
        the Python client's is pinned to the same list here."""
        assert set(client.ESCAPE_KEYS.values()) == set(
            CORPUS_DATA["constants"]["scalars"]["COMMANDS"]
        )
