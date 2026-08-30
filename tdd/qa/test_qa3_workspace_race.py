"""QA-3 finding 1: parallel steps of one run destroy each other's workspace.

A v2 graph with two parallel entry points shares ONE workspace volume named
after the run (``lazyaf-ws-<run_id>``). Both step tasks call
``WorkspaceService.get_or_create`` at once. The second one finds the first
one's row still in ``creating`` state, classifies it as a stranded row from a
crash, and force-removes the volume while the first step's population
container still has it mounted. Docker answers 409 and the raw client error
is written into the step's ``error`` field.

Evidence in the backend log for every failing run:

    WARNING app.services.workspace_service - Replacing stale workspace row
            <id> (status=creating) for run <run_id>

Root cause read: backend/app/services/workspace_service.py:312-320 treats
ANY non-READY/IN_USE/CLEANING row as replaceable. ``creating`` needs an age
threshold (or a wait on the volume lock) before it counts as stranded - a
freshly created row means a sibling step is mid-provision, not a crash.
"""
from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa3_support import (  # noqa: E402
    ensure_repo,
    graph_pipeline,
    require_stack,
    start_run,
    step_errors,
    wait_terminal,
)

#: Observed failure rate for a 2-way parallel graph is roughly 1 in 3, so a
#: single trial would be a coin flip. Ten trials make a clean run improbable
#: (~1.5%) while keeping the test around a minute.
TRIALS = 10


def _trial(repo_id: str):
    """Run one 2-way parallel graph of trivially-passing steps.

    Returns (status, errors) or None when the stack was reset mid-flight.
    """
    pipeline_id = graph_pipeline(
        repo_id,
        [{"command": "echo ok"}, {"command": "echo ok"}],
        name_prefix="qa3-wsrace",
    )
    run_id = start_run(pipeline_id)
    body = wait_terminal(run_id)
    if body is None:
        return None
    return body.get("status"), step_errors(body)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-1 (BLOCKER): two parallel entry points share one "
        "workspace volume; the second step's get_or_create force-removes the "
        "volume the first step is still populating, so the run fails with a "
        "raw Docker 409 'volume is in use'. ~1 run in 3."
    ),
)
def test_parallel_entry_points_do_not_destroy_the_shared_workspace():
    require_stack()
    repo_id = ensure_repo()

    failures = []
    attempted = 0
    for _ in range(TRIALS):
        outcome = _trial(repo_id)
        if outcome is None:
            continue
        attempted += 1
        status, errors = outcome
        if status != "passed":
            failures.append((status, errors))

    if attempted == 0:  # pragma: no cover - env dependent
        pytest.skip("stack was reset under every trial; nothing observed")

    assert not failures, (
        f"{len(failures)}/{attempted} runs of a 2-way parallel graph of "
        f"`echo ok` steps did not pass: {failures}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-2 (MAJOR): when the workspace race fires, the raw "
        "Docker API error (URL, API version, container id) is surfaced "
        "verbatim as the step error a user reads in the UI."
    ),
)
def test_step_errors_never_leak_raw_docker_client_text():
    require_stack()
    repo_id = ensure_repo()

    leaked = []
    for _ in range(TRIALS):
        outcome = _trial(repo_id)
        if outcome is None:
            continue
        _status, errors = outcome
        for error in errors:
            if "http+docker://" in error or "Client Error for" in error:
                leaked.append(error)

    assert not leaked, (
        "raw Docker client errors reached the step error field: " f"{leaked[:2]}"
    )
