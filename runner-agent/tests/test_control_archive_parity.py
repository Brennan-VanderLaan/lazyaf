"""Contract tests against the BACKEND source - cross-agent contracts #2 and #9.

Test contract item 7 (section 8, Agent D). This package deliberately COPIES two
things from the backend rather than importing them, because a runner host must
not need ``backend/app`` on its PYTHONPATH and a shared installable package
would drag the backend's whole dependency tree onto every runner node:

1. ``build_control_archive`` (~30 lines of tar building), and
2. the wire constants the agent has to agree with.

A deliberate copy is only defensible with an instrument that fails when it
drifts. That instrument is this file.

It does NOT import the backend. It PARSES it: the backend's function is
extracted from ``local_executor.py`` by AST and executed in an isolated
namespace containing only ``io``/``json``/``tarfile``. That keeps these tests
runnable from a bare runner checkout with no fastapi, no sqlalchemy and no
docker daemon - and, critically, keeps them UNCONDITIONAL. There is no
importorskip and no try/except ImportError anywhere in this file: a missing or
renamed backend function is a FAILURE, not a skip. (failure_01's removal test
self-skipped the moment its target disappeared and then stayed green over a
system that could no longer execute anything.)
"""
from __future__ import annotations

import ast
import io
import json
import tarfile
from pathlib import Path
from typing import Sequence

import pytest

from lazyaf_runner import client as client_module
from lazyaf_runner import session as session_module
from lazyaf_runner.control_archive import (
    CONTROL_CONFIG_DIR,
    build_control_archive,
    control_files_to_entries,
)

from conftest import REPO_ROOT

LOCAL_EXECUTOR = REPO_ROOT / "backend" / "app" / "services" / "execution" / "local_executor.py"
RUNNER_PROTOCOL = REPO_ROOT / "backend" / "app" / "services" / "execution" / "runner_protocol.py"


def _extract(path: Path, functions: set[str], constants: set[str]) -> dict:
    """Execute only the named top-level defs/assignments from ``path``.

    Deliberately narrow: pulling one function out by AST cannot accidentally
    import fastapi, and it fails loudly if the function moves.
    """
    assert path.exists(), f"{path} is missing - the backend module moved"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    body: list = []
    found_functions: set[str] = set()
    found_constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            body.append(node)
            found_functions.add(node.name)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & constants:
                body.append(node)
                found_constants |= names & constants

    missing_f = functions - found_functions
    missing_c = constants - found_constants
    assert not missing_f, f"{path.name} no longer defines {sorted(missing_f)}"
    assert not missing_c, f"{path.name} no longer defines {sorted(missing_c)}"

    namespace: dict = {
        "io": io,
        "json": json,
        "tarfile": tarfile,
        "Sequence": Sequence,
        "__name__": "backend_extract",
    }
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def backend_local_executor() -> dict:
    return _extract(
        LOCAL_EXECUTOR,
        functions={"build_control_archive"},
        constants={"CONTROL_CONFIG_DIR"},
    )


@pytest.fixture(scope="module")
def backend_protocol_constants() -> dict:
    return _extract(
        RUNNER_PROTOCOL,
        functions=set(),
        constants={
            "PROTOCOL_VERSION",
            "REGISTRATION_TIMEOUT",
            "ACK_TIMEOUT",
            "HEARTBEAT_INTERVAL",
            "DEATH_TIMEOUT",
            "DRAIN_GRACE",
            "MAX_MESSAGE_BYTES",
            "MAX_LOG_LINES_PER_MESSAGE",
            "MAX_LOG_LINE_BYTES",
        },
    )


# ---------------------------------------------------------------------------
# The tar
# ---------------------------------------------------------------------------

SINGLE = [("0f1c9d5e.json", {"step_id": "0f1c9d5e", "auth_token": "jwt", "command": "pytest"})]
PAIR = SINGLE + [("agent.0f1c9d5e.json", {"provider": "claude", "prompt": "do the thing"})]


@pytest.mark.parametrize("files", [[], SINGLE, PAIR], ids=["empty", "step-only", "step+agent"])
def test_archives_are_byte_identical(backend_local_executor, files) -> None:
    theirs = backend_local_executor["build_control_archive"](files)
    ours = build_control_archive(files)
    assert ours == theirs, (
        "the agent's control archive drifted from local_executor's. Both sides "
        "put_archive into a container the same image entrypoint chowns; a "
        "different mode, path or member order means remote steps see a "
        "different filesystem than local ones."
    )


def test_control_config_dir_matches(backend_local_executor) -> None:
    assert CONTROL_CONFIG_DIR == backend_local_executor["CONTROL_CONFIG_DIR"]


def test_archive_member_shape(backend_local_executor) -> None:
    """Spelled out, so a byte-equality failure is diagnosable."""
    data = build_control_archive(PAIR)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == [
        ".control",
        ".control/0f1c9d5e.json",
        ".control/agent.0f1c9d5e.json",
    ]
    assert members[0].isdir() and members[0].mode == 0o700
    assert all(m.mode == 0o600 for m in members[1:])
    # No uid/gid/mtime stamped on either side: the image entrypoint's chown
    # owns in-container readability, and a timestamp would make the two
    # builders' output differ by wall clock.
    assert all(m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in members)


def test_member_order_follows_insertion_not_sorting() -> None:
    """The backend emits the step config first, the agent config second. Sorting
    here would put `agent.<id>.json` first and break byte parity."""
    entries = control_files_to_entries(
        {
            "/workspace/.control/zzz.json": {"n": 1},
            "/workspace/.control/aaa.json": {"n": 2},
        }
    )
    assert [name for name, _ in entries] == ["zzz.json", "aaa.json"]


def test_control_file_path_outside_the_root_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        control_files_to_entries({"/etc/passwd": {}})
    assert "/workspace/.control/" in str(excinfo.value)

    with pytest.raises(ValueError):
        control_files_to_entries({"/workspace/.control/nested/x.json": {}})


# ---------------------------------------------------------------------------
# The wire constants
# ---------------------------------------------------------------------------

def test_protocol_version_matches(backend_protocol_constants) -> None:
    assert client_module.PROTOCOL_VERSION == backend_protocol_constants["PROTOCOL_VERSION"]


def test_registration_timeout_matches(backend_protocol_constants) -> None:
    assert (
        client_module.REGISTRATION_TIMEOUT
        == backend_protocol_constants["REGISTRATION_TIMEOUT"]
    )


def test_log_batching_constants_match(backend_protocol_constants) -> None:
    assert (
        session_module.MAX_LOG_LINES_PER_MESSAGE
        == backend_protocol_constants["MAX_LOG_LINES_PER_MESSAGE"]
    )
    assert (
        session_module.MAX_LOG_LINE_BYTES
        == backend_protocol_constants["MAX_LOG_LINE_BYTES"]
    )


def test_drain_grace_matches(backend_protocol_constants) -> None:
    assert session_module.DRAIN_GRACE == backend_protocol_constants["DRAIN_GRACE"]


def test_a_full_log_frame_can_never_exceed_the_backends_message_limit(
    backend_protocol_constants,
) -> None:
    """The backend DROPS oversized frames. A batch the agent considers normal
    must not be one the backend refuses, or runner-origin logs vanish silently
    exactly when a step is at its noisiest."""
    assert (
        session_module.MAX_LOG_FRAME_BYTES
        < backend_protocol_constants["MAX_MESSAGE_BYTES"]
    )


def test_ack_budget_leaves_room_for_the_agents_ack_path(
    backend_protocol_constants,
) -> None:
    """The agent ACKs before invoking the orchestrator (see
    test_session_concurrency.test_ack_precedes_execution), so the only work
    inside the ACK budget is parsing one frame. This asserts the budget exists
    and is a real number rather than silently absent."""
    assert backend_protocol_constants["ACK_TIMEOUT"] > 0
    assert (
        backend_protocol_constants["ACK_TIMEOUT"]
        < backend_protocol_constants["DEATH_TIMEOUT"]
    )


def test_heartbeat_interval_is_comfortably_inside_the_death_timeout(
    backend_protocol_constants,
) -> None:
    """The agent takes its interval from `registered`, so it cannot drift - but
    if the SERVER's two numbers ever cross, every runner dies between beats."""
    assert (
        backend_protocol_constants["HEARTBEAT_INTERVAL"] * 2
        <= backend_protocol_constants["DEATH_TIMEOUT"]
    )


def test_cancel_exit_code_is_the_documented_one() -> None:
    from lazyaf_runner.orchestrator.docker_orch import EXIT_CANCELLED

    assert session_module.EXIT_CANCELLED == 143
    assert EXIT_CANCELLED == 143
