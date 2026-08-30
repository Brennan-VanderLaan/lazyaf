"""QA-3: a pipeline run keeps executing after it has reached a terminal state.

``start_pipeline`` dispatches every graph entry point in a loop and never
re-checks whether the run is still running between iterations
(backend/app/services/pipeline_executor.py:1198-1206). When an entry point
fails SYNCHRONOUSLY - a routing error, e.g. a pipeline definition carrying
the pre-12.6 ``executor: legacy`` key - the fan-in check in
``_handle_graph_step_complete`` sees no active steps, completes the run, and
the loop then dispatches the NEXT entry point into an already-finished run.

``_complete_pipeline`` itself has no "already terminal" guard, so it runs
once per entry point: N ``pipeline_run_status: failed`` broadcasts, N
workspace cleanups, and N evaluations of the run's on_pass/on_fail trigger
action.

What a viewer sees over the websocket for a 4-entry-point pipeline:

    pipeline_run_status  running
    step_run_status      s0 running -> failed
    pipeline_run_status  FAILED          <- run is terminal here
    step_run_status      s1 running      <- ...and still starting steps
    pipeline_run_status  FAILED
    step_run_status      s2 running
    ... (5 terminal broadcasts in total)
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa3_support import (  # noqa: E402
    BASE_URL,
    TERMINAL,
    ensure_repo,
    get_run,
    graph_pipeline,
    require_stack,
    start_run,
)

#: A step config the router refuses, so the step fails inside the dispatch
#: call rather than in a background task. This is a real definition a user can
#: still have: `executor: legacy` was valid before Phase 12.6.
ROUTE_ERROR_STEP = {"command": "echo hi", "executor": "legacy"}

#: Enough entry points that the dispatch loop spans several seconds. The run
#: goes terminal on the FIRST one, so a REST poller (which is itself slow -
#: see the run-list finding) still has time to catch the run mid-dispatch.
ENTRY_POINTS = 12


def _collect_events(run_id_box: dict, pipeline_id: str, seconds: float = 25.0):
    """Subscribe to /ws, start the run, and return the ordered event stream.

    The websocket is the only vantage point that can see this: REST polling
    cannot, because the executor's dispatch loop holds the database and the
    poller's own requests queue behind it, so a GET only lands after the run
    has finished dispatching everything.
    """
    import asyncio
    import json

    websockets = pytest.importorskip(
        "websockets", reason="websocket client needed to observe run event ordering"
    )

    ws_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    async def run():
        events = []
        async with websockets.connect(ws_url, open_timeout=20) as socket:
            run_id_box["id"] = start_run(pipeline_id)
            deadline = asyncio.get_event_loop().time() + seconds
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if events:
                        break
                    continue
                try:
                    events.append(json.loads(raw))
                except ValueError:
                    continue
        return events

    return asyncio.run(run())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-10 (MAJOR): a pipeline run that has already broadcast "
        "a terminal status keeps dispatching its remaining entry points, so "
        "'step running' events arrive after 'run failed', and the run "
        "broadcasts its terminal status once per entry point. "
        "backend/app/services/pipeline_executor.py:1198-1206 (no terminal "
        "re-check in the entry-point loop) and :838 (_complete_pipeline has "
        "no already-terminal guard)."
    ),
)
def test_no_step_starts_after_the_run_broadcasts_a_terminal_status():
    require_stack()
    repo_id = ensure_repo()
    pipeline_id = graph_pipeline(
        repo_id, [dict(ROUTE_ERROR_STEP)] * ENTRY_POINTS, name_prefix="qa3-terminal"
    )

    box: dict = {}
    events = _collect_events(box, pipeline_id)
    run_id = box.get("id")
    if not run_id:  # pragma: no cover - env dependent
        pytest.skip("run never started")

    terminal_index = None
    terminal_broadcasts = 0
    late_step_starts = []

    for index, event in enumerate(events):
        payload = event.get("payload") or {}
        if (
            event.get("type") == "pipeline_run_status"
            and payload.get("id") == run_id
            and payload.get("status") in TERMINAL
        ):
            terminal_broadcasts += 1
            if terminal_index is None:
                terminal_index = index
        if (
            event.get("type") == "step_run_status"
            and payload.get("pipeline_run_id") == run_id
            and payload.get("status") == "running"
            and terminal_index is not None
        ):
            late_step_starts.append(payload.get("step_index"))

    if terminal_index is None:  # pragma: no cover - env dependent
        pytest.skip("no terminal broadcast observed for the run")

    assert not late_step_starts and terminal_broadcasts == 1, (
        f"run broadcast a terminal status {terminal_broadcasts} time(s) and "
        f"then started step(s) {late_step_starts} afterwards"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-11 (MAJOR): the same run is 'completed' once per "
        "entry point, so steps_completed / current_step never catch up - a "
        "finished 4-step run reports current_step=0 while completed_step_ids "
        "lists all four."
    ),
)
def test_a_finished_run_reports_a_coherent_step_position():
    require_stack()
    repo_id = ensure_repo()
    pipeline_id = graph_pipeline(
        repo_id, [dict(ROUTE_ERROR_STEP)] * ENTRY_POINTS, name_prefix="qa3-position"
    )
    run_id = start_run(pipeline_id)

    deadline = time.time() + 90
    body = None
    while time.time() < deadline:
        status, body = get_run(run_id)
        if status == 404:  # pragma: no cover - env dependent
            pytest.skip("stack was reset mid-test")
        if status == 200 and body.get("status") in TERMINAL:
            break
        time.sleep(0.5)
    time.sleep(5)
    status, body = get_run(run_id)
    if status != 200:  # pragma: no cover - env dependent
        pytest.skip("stack was reset mid-test")

    completed_ids = body.get("completed_step_ids") or []
    assert body.get("current_step") == len(completed_ids) - 1 or not completed_ids, (
        f"finished run reports current_step={body.get('current_step')} while "
        f"{len(completed_ids)} of {body.get('steps_total')} steps completed "
        f"({completed_ids})"
    )
