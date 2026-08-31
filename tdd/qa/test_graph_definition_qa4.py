"""QA-4: what the pipeline-graph DEFINITION layer accepts.

``PipelineGraphModel.validate_graph_integrity``
(backend/app/schemas/pipeline.py:52) checks three things: every edge endpoint
exists, entry_points is non-empty, and every entry point exists. Everything
else about a graph is accepted verbatim and only discovered - or not
discovered - at execution time.

Each xfail(strict=True) below asserts the behaviour the definition layer
SHOULD have. They fail today; they will turn into strict XPASS failures the
moment validation is tightened, which is the point.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import api, dead_step, edge, graph, step

pytestmark = pytest.mark.qa4


# ---------------------------------------------------------------------------
# Accepted nonsense (each of these is a QA-4 finding)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-03: a graph containing a CYCLE is accepted. "
        "validate_graph_integrity never checks acyclicity, so the executor - "
        "which assumes a DAG - silently truncates the run instead. See "
        "test_graph_execution_qa4.test_cycle_reports_pass_having_run_one_step."
    ),
)
def test_cycle_is_rejected_at_definition_time(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-cycle-abc",
        "steps_graph": graph(
            [step("a"), step("b"), step("c")],
            [edge("e1", "a", "b"), edge("e2", "b", "c"), edge("e3", "c", "b")],
            ["a"],
        ),
    })
    assert status == 422, f"a cyclic graph should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA4-03: a self-referencing edge (a -> a) is accepted.",
)
def test_self_edge_is_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-self-edge",
        "steps_graph": graph([step("a")], [edge("e1", "a", "a")], ["a"]),
    })
    assert status == 422, f"a self-edge should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-04: a step that no edge reaches and no entry point "
        "names is accepted; the run then reports PASSED having skipped it."
    ),
)
def test_unreachable_step_is_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-orphan",
        "steps_graph": graph([step("a"), step("orphan")], [], ["a"]),
    })
    assert status == 422, f"an unreachable step should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-06: duplicate entry_points are accepted and the "
        "executor dispatches the step once PER duplicate - N StepRuns and N "
        "containers for one step."
    ),
)
def test_duplicate_entry_points_are_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-dup-entry",
        "steps_graph": graph([step("a")], [], ["a", "a", "a"]),
    })
    assert status == 422, f"duplicate entry_points should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-06: two edges between the same pair of steps matching "
        "the same condition are accepted; _handle_graph_step_complete builds "
        "steps_to_execute as a LIST with no de-duplication, so the target "
        "runs twice."
    ),
)
def test_parallel_duplicate_edges_are_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-dup-edges",
        "steps_graph": graph(
            [step("a"), step("b")],
            [edge("e1", "a", "b", "success"), edge("e2", "a", "b", "always")],
            ["a"],
        ),
    })
    assert status == 422, f"duplicate a->b edges should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-13: timeout has no lower bound. -1 is stored, then "
        "spawns a container only to kill it and report 'timed out after -1s'."
    ),
)
@pytest.mark.parametrize("timeout", [-1, -999999999])
def test_negative_timeout_is_rejected(create_pipeline, timeout):
    status, body = create_pipeline({
        "name": f"qa4-timeout{timeout}",
        "steps_graph": graph([step("a", timeout=timeout)], [], ["a"]),
    })
    assert status == 422, f"timeout={timeout} should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-13: timeout has no upper bound either. 999999999s is "
        "~31 years, and nothing else caps a step's lifetime."
    ),
)
def test_absurd_timeout_is_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-timeout-huge",
        "steps_graph": graph([step("a", timeout=999_999_999)], [], ["a"]),
    })
    assert status == 422, f"a 31-year timeout should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-15: PipelineStepV2.id is ignored - the executor keys "
        "everything off the steps dict KEY - so a step whose declared id "
        "disagrees with its key is accepted and silently renamed."
    ),
)
def test_step_key_and_declared_id_must_agree(create_pipeline):
    status, body = create_pipeline({
        "name": "qa4-key-id-mismatch",
        "steps_graph": {
            "steps": {
                "KEY": {
                    "id": "DECLARED",
                    "name": "mismatch",
                    "type": "script",
                    "config": {"command": "echo x"},
                    "timeout": 300,
                }
            },
            "edges": [],
            "entry_points": ["KEY"],
            "version": 2,
        },
    })
    assert status == 422, f"key/id mismatch should be refused, got {status}: {body}"


# QA4-19 FIXED, but NOT in validate_graph_integrity: the pipeline create/update
# schemas now type `name` as the `Name` alias from backend/app/schemas/_strings.py
# (strip_whitespace, min_length=1, max_length=200), so "" is refused by pydantic
# with a 422 on `body.name` before any graph validation runs.
def test_empty_pipeline_name_is_rejected(create_pipeline):
    status, body = create_pipeline({
        "name": "",
        "steps_graph": graph([step("a")], [], ["a"]),
    })
    assert status == 422, f"an empty name should be refused, got {status}: {body}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-14: PATCHing steps_graph onto a pipeline that already "
        "has legacy `steps` leaves BOTH populated and disagreeing. The "
        "executor uses the graph; any reader of `steps` sees the old "
        "definition."
    ),
)
def test_patching_a_graph_clears_the_legacy_steps_array(create_pipeline):
    status, pipeline = create_pipeline({
        "name": "qa4-both-definitions",
        "steps": [{"name": "LEGACY-ONLY", "type": "script", "config": {"command": "echo L"}}],
    })
    assert status == 201, body_or(pipeline)

    status, _ = api("PATCH", f"/api/pipelines/{pipeline['id']}", {
        "steps_graph": graph([step("g")], [], ["g"]),
    })
    assert status == 200

    status, after = api("GET", f"/api/pipelines/{pipeline['id']}")
    assert status == 200
    assert not (after["steps"] and after["steps_graph"]), (
        "pipeline holds two disagreeing definitions at once: "
        f"steps={after['steps']!r} steps_graph keys={list(after['steps_graph']['steps'])}"
    )


def body_or(value):
    return repr(value)[:400]


# ---------------------------------------------------------------------------
# Verified-correct guards (these must keep passing)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,payload_graph",
    [
        (
            "edge to a step that does not exist",
            graph([step("a")], [edge("e1", "a", "ghost")], ["a"]),
        ),
        (
            "entry point naming a step that does not exist",
            graph([step("a")], [], ["ghost"]),
        ),
        (
            "empty entry_points",
            graph([step("a")], [], []),
        ),
        (
            "no steps at all",
            {"steps": {}, "edges": [], "entry_points": [], "version": 2},
        ),
        (
            "step with no id",
            {
                "steps": {"a": {"name": "x", "type": "script", "config": {}}},
                "edges": [],
                "entry_points": ["a"],
                "version": 2,
            },
        ),
        (
            "unknown step type",
            {
                "steps": {"a": {"id": "a", "name": "a", "type": "banana", "config": {}, "timeout": 300}},
                "edges": [],
                "entry_points": ["a"],
                "version": 2,
            },
        ),
        (
            "unknown edge condition",
            graph([step("a"), step("b")], [edge("e", "a", "b", "maybe")], ["a"]),
        ),
        (
            "non-integer timeout",
            graph([step("a", timeout=1.5)], [], ["a"]),
        ),
    ],
)
def test_graph_definition_rejects_these(create_pipeline, label, payload_graph):
    """Guards on validation that already works - regressions here are loud."""
    status, body = create_pipeline({"name": f"qa4-guard", "steps_graph": payload_graph})
    assert status == 422, f"{label} should be refused, got {status}: {body_or(body)}"


def test_run_endpoint_refuses_a_pipeline_with_no_steps(create_pipeline):
    """POST /api/pipelines/{id}/run gates on 'has steps'.

    The repo-YAML run path does NOT (see test_yaml_pipelines_qa4), so this
    guard is what makes that inconsistency a finding rather than a policy.
    """
    status, pipeline = create_pipeline({"name": "qa4-no-steps", "steps": []})
    assert status == 201, body_or(pipeline)
    status, body = api("POST", f"/api/pipelines/{pipeline['id']}/run", {"trigger_type": "manual"})
    assert status == 400, f"expected 400, got {status}: {body_or(body)}"
    assert "no steps" in json_text(body).lower()


def json_text(body) -> str:
    return body if isinstance(body, str) else repr(body)
