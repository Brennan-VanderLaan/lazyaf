"""QA-3: the run-list endpoint collapses under a handful of simultaneous readers.

``GET /api/pipeline-runs`` eager-loads every step run and every step execution
for up to 100 runs, and the engine is created with ``echo=True`` and stock
pool settings:

    backend/app/database.py:15
    engine = create_async_engine(settings.database_url, echo=True)

``echo=True`` logs every statement (twice, through two handlers) on the
request path, and the default async pool is 5 connections + 10 overflow with
a 30 s checkout timeout. With a very modest history - 7 runs / 302 step runs -
a single list request takes 0.6-1.6 s, so ten simultaneous readers serialise
into ~30 s and twenty of them start timing out of the pool:

    sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
    reached, connection timed out, timeout 30.00

which the client sees as a bare ``500 Internal Server Error``. A dashboard
open in two tabs during a demo is enough.

Marked ``slow``: the test builds a history and then deliberately waits out
pool timeouts.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa3_support import (  # noqa: E402
    api,
    ensure_repo,
    fire_together,
    graph_pipeline,
    require_stack,
    start_run,
    status_counts,
)

#: Cheap way to manufacture many step-run rows fast: entry points that fail
#: inside the dispatch call, so a run of 60 steps finishes in a few seconds.
_FAST_FAIL = {"command": "echo hi", "executor": "legacy"}

HISTORY_RUNS = 5
STEPS_PER_RUN = 60
READERS = 20
SINGLE_REQUEST_BUDGET_SECONDS = 1.0


def _build_history(repo_id: str) -> int:
    pipeline_id = graph_pipeline(
        repo_id, [dict(_FAST_FAIL)] * STEPS_PER_RUN, name_prefix="qa3-history"
    )
    for _ in range(HISTORY_RUNS):
        start_run(pipeline_id)
    time.sleep(8)
    status, body = api("GET", "/api/pipeline-runs?limit=100")
    if status != 200 or not isinstance(body, list):
        return 0
    return sum(len(run.get("step_runs", [])) for run in body)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-12 (BLOCKER): with 7 runs / ~300 step runs in the "
        "database, 20 simultaneous GET /api/pipeline-runs return HTTP 500 "
        "(QueuePool checkout timeout). Root causes: echo=True hardcoded at "
        "backend/app/database.py:15 and an unbounded selectinload of "
        "step_runs -> executions in list_all_pipeline_runs."
    ),
)
def test_concurrent_readers_of_the_run_list_do_not_get_500s():
    require_stack()
    repo_id = ensure_repo()
    steps = _build_history(repo_id)
    if steps == 0:  # pragma: no cover - env dependent
        pytest.skip("could not build a run history (stack reset?)")

    results = fire_together(
        READERS, lambda _i: api("GET", "/api/pipeline-runs?limit=100", timeout=120)
    )
    counts = status_counts(results)

    assert counts.get(500, 0) == 0, (
        f"{counts.get(500, 0)}/{READERS} simultaneous readers got HTTP 500 "
        f"with only {steps} step-run rows in the database (codes {counts})"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-13 (MAJOR): a single GET /api/pipeline-runs takes "
        "0.6-1.6 s on a nearly-empty database, which is what turns a handful "
        "of readers into pool exhaustion. echo=True on the engine is a large "
        "part of it."
    ),
)
def test_a_single_run_list_request_is_not_pathologically_slow():
    require_stack()
    repo_id = ensure_repo()
    _build_history(repo_id)

    samples = []
    for _ in range(3):
        started = time.time()
        status, _ = api("GET", "/api/pipeline-runs?limit=100", timeout=120)
        samples.append(time.time() - started)
        assert status == 200

    best = min(samples)
    assert best < SINGLE_REQUEST_BUDGET_SECONDS, (
        f"fastest of 3 run-list requests took {best:.2f}s "
        f"(budget {SINGLE_REQUEST_BUDGET_SECONDS}s); samples={samples}"
    )


def test_health_and_repo_list_survive_the_same_burst_on_a_clean_database():
    """Control case - the collapse above is DATA-driven, not just 'SQLite'.

    On a freshly reset database the very same burst sizes are fine: 100
    simultaneous /health (no database at all) and 100 simultaneous
    /api/repos both answer 200 in about a second. That is what makes the
    run-list finding a finding rather than ambient load.

    Note the flip side, observed while writing this: once the pool IS
    saturated by the run-list burst, /api/repos returns 500 too (84/100).
    The pool is process-wide, so one heavy endpoint takes the whole API down
    with it - hence the reset and cooldown here.
    """
    require_stack()
    status, _ = api("POST", "/api/test/reset", timeout=90)
    assert status == 200
    time.sleep(35)  # let any in-flight pool checkouts time out and return

    health = status_counts(fire_together(100, lambda _i: api("GET", "/health")))
    assert health == {200: 100}, f"/health under load: {health}"

    repos = status_counts(fire_together(100, lambda _i: api("GET", "/api/repos")))
    assert repos == {200: 100}, f"/api/repos on a clean database: {repos}"
