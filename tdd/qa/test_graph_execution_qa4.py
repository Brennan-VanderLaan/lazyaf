"""QA-4: what the graph EXECUTOR does with the nonsense the schema accepted.

Two mechanisms explain almost everything in this file.

1. ``PipelineExecutor._handle_graph_step_complete``
   (backend/app/services/pipeline_executor.py:3340) completes the run as soon
   as nothing is active and nothing new was dispatched, WITHOUT ever checking
   that the graph was actually covered:

       if not active_ids:
           if len(completed_ids) >= total_steps:  ... complete
           elif not steps_to_execute:             ... complete   # <-- here

   A cycle, an unreachable step, or a typo'd on_success all land on that
   ``elif`` and the run is stamped PASSED with a fraction of its steps run.

2. ``_handle_graph_step_complete`` calls ``_execute_graph_step``
   (pipeline_executor.py:1534), which on a routing failure calls
   ``_handle_graph_step_complete`` again - synchronously, in the caller's
   stack. Chain length therefore equals Python recursion depth.
"""

import time

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import (
    api,
    chain_graph,
    dead_step,
    edge,
    graph,
    run_to_completion,
    start_run,
    step,
)

pytestmark = pytest.mark.qa4


# ---------------------------------------------------------------------------
# BLOCKER: unbounded recursion on a long chain
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-01 (BLOCKER): _handle_graph_step_complete and "
        "_execute_graph_step recurse into each other once per step whenever a "
        "step fails synchronously (e.g. the realistic 'executor: legacy' "
        "stale-config mistake). Around 170-500 steps this raises "
        "RecursionError inside the request handler: POST /run answers a bare "
        "500 and the PipelineRun is abandoned in status='running' with "
        "completed_at=null forever."
    ),
)
def test_long_chain_does_not_blow_the_python_stack(create_pipeline):
    status, pipeline = create_pipeline({"name": "qa4-chain-500", "steps_graph": chain_graph(500)})
    assert status == 201, repr(pipeline)[:300]

    status, run = start_run(pipeline["id"], timeout=300)
    assert status == 200, (
        f"POST /run returned {status} ({str(run)[:120]}) - a 500 here is the "
        "RecursionError escaping the request handler"
    )

    time.sleep(2)
    status, body = api("GET", f"/api/pipeline-runs/{run['id']}")
    assert status == 200
    assert body["status"] != "running", (
        "run abandoned mid-traversal: status='running' with no active steps "
        f"({len(body['active_step_ids'])} active, "
        f"{len(body['completed_step_ids'])}/{body['steps_total']} completed) - "
        "nothing will ever finish it"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-05: start_pipeline documents that it 'returns as soon "
        "as the run row exists and the entry steps are dispatched', but on "
        "the synchronous-failure path POST /run walks the WHOLE graph before "
        "answering. Measured: 170 steps = 27s, 180 steps = 43s. Any proxy or "
        "browser in front of this times out."
    ),
)
def test_run_endpoint_returns_promptly_for_a_large_graph(create_pipeline):
    status, pipeline = create_pipeline({"name": "qa4-chain-150", "steps_graph": chain_graph(150)})
    assert status == 201, repr(pipeline)[:300]

    started = time.time()
    status, run = start_run(pipeline["id"], timeout=300)
    elapsed = time.time() - started
    assert status == 200, f"{status} {str(run)[:200]}"
    assert elapsed < 5.0, (
        f"POST /api/pipelines/{{id}}/run blocked for {elapsed:.1f}s walking a "
        "150-step graph instead of returning after dispatching entry points"
    )


# ---------------------------------------------------------------------------
# BLOCKER: false green
# ---------------------------------------------------------------------------

@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-03 (BLOCKER): entry a -> b -> c -> b. Step b's "
        "_all_upstream_satisfied sees upstream [a, c]; c can never complete "
        "first, so b never becomes ready. Nothing is active and nothing was "
        "dispatched, so the run is stamped PASSED with 1 of 3 steps run. A "
        "green CI badge for a pipeline that ran one third of itself."
    ),
)
def test_cycle_reports_pass_having_run_one_step(create_pipeline, seeded_repo_id):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-exec-cycle",
        "steps_graph": graph(
            [step("a", "echo A"), step("b", "echo B"), step("c", "echo C")],
            [edge("e1", "a", "b"), edge("e2", "b", "c"), edge("e3", "c", "b")],
            ["a"],
        ),
    })
    if run["status"] == "failed" and not run["completed_step_ids"]:
        pytest.skip("step 0 never ran (workspace/docker unavailable in this environment)")

    assert not (run["status"] == "passed" and run["steps_completed"] < run["steps_total"]), (
        f"run reported {run['status']} with only "
        f"{run['steps_completed']}/{run['steps_total']} steps executed "
        f"(completed_step_ids={run['completed_step_ids']})"
    )


@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-04: a step no edge reaches is counted in steps_total "
        "but never executed, and the run still reports PASSED - '1/2 steps, "
        "passed'."
    ),
)
def test_unreachable_step_does_not_produce_a_green_run(create_pipeline, seeded_repo_id):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-exec-orphan",
        "steps_graph": graph([step("a", "echo A"), step("orphan", "echo NEVER")], [], ["a"]),
    })
    if run["status"] == "failed" and not run["completed_step_ids"]:
        pytest.skip("step 0 never ran (workspace/docker unavailable in this environment)")

    assert not (run["status"] == "passed" and run["steps_completed"] < run["steps_total"]), (
        f"run reported {run['status']} with only "
        f"{run['steps_completed']}/{run['steps_total']} steps executed"
    )


@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-02 (BLOCKER): on_success/on_failure are free-text "
        "strings in PipelineStepConfig and PipelineStepYaml. _handle_action "
        "(pipeline_executor.py:3543) logs 'Unknown action, treating as stop' "
        "and completes the run with success=step_success - so a ONE-CHARACTER "
        "typo ('nextt') stops a 3-step pipeline after step 1 and reports "
        "PASSED. Nothing surfaces the typo to the user."
    ),
)
def test_typo_in_on_success_does_not_produce_a_green_truncated_run(create_pipeline, seeded_repo_id):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-typo-action",
        "steps": [
            {"name": "one", "type": "script", "config": {"command": "echo ONE"}, "on_success": "nextt"},
            {"name": "two", "type": "script", "config": {"command": "echo TWO"}},
            {"name": "three", "type": "script", "config": {"command": "echo THREE"}},
        ],
    })
    if run["status"] == "failed" and run["steps_completed"] == 0:
        pytest.skip("step 0 never ran (workspace/docker unavailable in this environment)")

    assert not (run["status"] == "passed" and run["steps_completed"] < run["steps_total"]), (
        f"a typo'd on_success produced {run['status']} with "
        f"{run['steps_completed']}/{run['steps_total']} steps run"
    )


# ---------------------------------------------------------------------------
# MAJOR: duplicate dispatch
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-06: duplicate entry_points are each dispatched. "
        "start_pipeline loops `for step_id in entry_points` with no de-dup, "
        "so ['a','a','a'] creates THREE StepRuns - all at step_index 0, all "
        "with their own container - for one step."
    ),
)
def test_duplicate_entry_points_dispatch_the_step_once(create_pipeline):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-dup-entry-exec",
        "steps_graph": graph([dead_step("a")], [], ["a", "a", "a"]),
    }, seconds=60)
    assert len(run["step_runs"]) == 1, (
        f"step 'a' was dispatched {len(run['step_runs'])} times "
        f"(indices {[s['step_index'] for s in run['step_runs']]})"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-06: two edges a->b matching the same condition both "
        "append 'b' to steps_to_execute (a list, never de-duplicated - "
        "pipeline_executor.py:3405-3420), and the in-loop 'already active?' "
        "guard reads a snapshot taken before dispatch. b runs twice."
    ),
)
def test_duplicate_edges_dispatch_the_target_once(create_pipeline):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-dup-edge-exec",
        "steps_graph": graph(
            [dead_step("a"), dead_step("b")],
            [edge("e1", "a", "b", "always"), edge("e2", "a", "b", "always")],
            ["a"],
        ),
    }, seconds=60)
    b_runs = [s for s in run["step_runs"] if s["step_id"] == "b"]
    assert len(b_runs) == 1, f"step 'b' was dispatched {len(b_runs)} times"


@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-07: with two a->b edges the run is stamped PASSED "
        "while the SECOND 'b' StepRun is still 'running'. Its container keeps "
        "going against a workspace _complete_pipeline has already cleaned up, "
        "and the UI shows a finished green run with a live spinner in it."
    ),
)
def test_run_is_not_completed_while_a_step_is_still_running(create_pipeline, seeded_repo_id):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-dup-edge-live",
        "steps_graph": graph(
            [step("a", "echo A"), step("b", "sleep 8; echo B")],
            [edge("e1", "a", "b", "success"), edge("e2", "a", "b", "always")],
            ["a"],
        ),
    }, seconds=400)
    if run["status"] == "failed" and run["steps_completed"] == 0:
        pytest.skip("step 0 never ran (workspace/docker unavailable in this environment)")

    unfinished = [s for s in run["step_runs"] if s["status"] in ("running", "pending")]
    assert not unfinished, (
        f"run is terminal ({run['status']}) while "
        f"{[(s['step_id'], s['status']) for s in unfinished]} are still going"
    )


# ---------------------------------------------------------------------------
# MAJOR: no cap on parallel fan-out
# ---------------------------------------------------------------------------

@pytest.mark.heavy
@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-12: nothing in the executor caps how many graph steps "
        "run at once (no semaphore anywhere in pipeline_executor.py or "
        "local_executor.py). A fan-out of N makes N containers land on the "
        "docker socket simultaneously - and in the shipped compose files that "
        "socket is the HOST daemon, shared with every other stack. Measured: "
        "a 20-way fan-out put all 20 into active_step_ids at once."
    ),
)
def test_fanout_is_capped(create_pipeline, seeded_repo_id):
    width = 20
    status, pipeline = create_pipeline({
        "name": f"qa4-fanout-{width}",
        "steps_graph": graph(
            [step("root", "echo ROOT")] + [step(f"f{i}", "sleep 20") for i in range(width)],
            [edge(f"e{i}", "root", f"f{i}") for i in range(width)],
            ["root"],
        ),
    })
    assert status == 201, repr(pipeline)[:300]
    status, run = start_run(pipeline["id"])
    assert status == 200

    peak = 0
    deadline = time.time() + 300
    while time.time() < deadline:
        status, body = api("GET", f"/api/pipeline-runs/{run['id']}")
        if status != 200 or not isinstance(body, dict) or "status" not in body:
            pytest.skip("QA sandbox was reset mid-test")
        peak = max(peak, len(body["active_step_ids"]))
        if body["status"] not in ("running", "pending"):
            break
        time.sleep(1.5)

    if peak == 0:
        pytest.skip("no step ever became active (workspace/docker unavailable)")
    assert peak < width, f"{peak} of {width} fan-out steps were in flight simultaneously"


# ---------------------------------------------------------------------------
# MAJOR: timeout has no bounds
# ---------------------------------------------------------------------------

@pytest.mark.containers
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-13: timeout=-1 survives validation, and the local "
        "executor's deadline (local_executor.py:890) is already in the past "
        "when the container starts - so LazyAF creates a container purely to "
        "kill it and reports 'step timed out after -1s' to the user."
    ),
)
def test_negative_timeout_never_reaches_the_container(create_pipeline, seeded_repo_id):
    run = run_to_completion(create_pipeline, {
        "name": "qa4-timeout-negative",
        "steps_graph": graph([step("a", "echo A", timeout=-1)], [], ["a"]),
    }, seconds=400)
    errors = " ".join(s["error"] or "" for s in run["step_runs"])
    assert "-1s" not in errors, f"user-visible negative duration in error text: {errors!r}"


# ---------------------------------------------------------------------------
# Verified-correct guards
# ---------------------------------------------------------------------------

def test_fan_in_diamond_runs_every_step_exactly_once(create_pipeline):
    """a -> {b, c} -> d. Correct today; this guards the fan-in join."""
    run = run_to_completion(create_pipeline, {
        "name": "qa4-diamond",
        "steps_graph": graph(
            [dead_step("a"), dead_step("b"), dead_step("c"), dead_step("d")],
            [
                edge("e1", "a", "b", "always"),
                edge("e2", "a", "c", "always"),
                edge("e3", "b", "d", "always"),
                edge("e4", "c", "d", "always"),
            ],
            ["a"],
        ),
    }, seconds=60)
    ran = sorted(s["step_id"] for s in run["step_runs"])
    assert ran == ["a", "b", "c", "d"], ran
    assert sorted(run["completed_step_ids"]) == ["a", "b", "c", "d"]


def test_two_entry_points_joining_on_one_step_run_it_once(create_pipeline):
    """Two independent entry points converge on z; z must run exactly once."""
    run = run_to_completion(create_pipeline, {
        "name": "qa4-two-entries",
        "steps_graph": graph(
            [dead_step("a"), dead_step("b"), dead_step("z")],
            [edge("e1", "a", "z", "always"), edge("e2", "b", "z", "always")],
            ["a", "b"],
        ),
    }, seconds=60)
    z_runs = [s for s in run["step_runs"] if s["step_id"] == "z"]
    assert len(z_runs) == 1, f"z ran {len(z_runs)} times"


def test_a_wedged_run_can_still_be_cancelled(create_pipeline):
    """Cancel is the only escape from QA4-01's abandoned run - keep it working."""
    run = run_to_completion(create_pipeline, {
        "name": "qa4-cancel-guard",
        "steps_graph": graph([dead_step("a")], [], ["a"]),
    }, seconds=60)
    status, body = api("POST", f"/api/pipeline-runs/{run['id']}/cancel")
    # Already terminal: the endpoint must answer deterministically, not 500.
    assert status in (200, 400), f"{status} {str(body)[:200]}"


@pytest.mark.parametrize("trigger_type", ["card_work", "playground"])
def test_adhoc_trigger_types_stay_reserved(create_pipeline, trigger_type):
    """Guard on the 12.5 routing-key gate - it holds, and it must keep holding."""
    status, pipeline = create_pipeline({
        "name": f"qa4-trigger-{trigger_type}",
        "steps_graph": graph([dead_step("a")], [], ["a"]),
    })
    assert status == 201
    status, body = api(
        "POST", f"/api/pipelines/{pipeline['id']}/run",
        {"trigger_type": trigger_type, "trigger_ref": "some-card"},
    )
    assert status == 400, f"{status} {str(body)[:200]}"


def test_unknown_trigger_type_is_refused(create_pipeline):
    status, pipeline = create_pipeline({
        "name": "qa4-trigger-bogus",
        "steps_graph": graph([dead_step("a")], [], ["a"]),
    })
    assert status == 201
    status, _ = api("POST", f"/api/pipelines/{pipeline['id']}/run", {"trigger_type": "not_a_trigger"})
    assert status == 422
