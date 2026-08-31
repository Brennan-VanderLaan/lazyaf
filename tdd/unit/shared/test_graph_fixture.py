"""The graph fixture helper is a SECOND rendering of the v1->v2 rules.

`tdd/shared/factories/pipelines.linear_graph` deliberately does not call
`app.schemas.pipeline.array_to_graph` (see that module's docstring: coupling
every fixture in the tree to the production converter means a converter defect
makes every fixture wrong in the same direction).

That independence is only worth having if something checks the two agree. This
file is that something: for every shape both accept, the fixture's graph must
equal the converter's graph, field for field. When they diverge, one of them is
wrong and this test says which shape found it.

It also pins the refusals, because a fixture helper that quietly renders a
malformed pipeline produces a red run whose cause is nowhere near the test.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.factories.pipelines import (  # noqa: E402
    GraphFixtureError,
    graph_json,
    graph_pipeline_payload,
    linear_graph,
)

from app.schemas.pipeline import (  # noqa: E402
    PipelineGraphModel,
    PipelineStepConfig,
    array_to_graph,
)


def converted(steps: list[dict]) -> dict:
    """The production converter's answer for the same step list."""
    return array_to_graph(
        [PipelineStepConfig.model_validate(s) for s in steps]
    ).model_dump(mode="json")


# -----------------------------------------------------------------------------
# The two renderings agree
# -----------------------------------------------------------------------------

AGREEING_SHAPES = {
    "single_step": [
        {"name": "Only", "type": "script", "config": {"command": "echo hi"}},
    ],
    "three_step_chain": [
        {"name": "Lint", "type": "script", "config": {"command": "lint"}},
        {"name": "Test", "type": "script", "config": {"command": "test"}},
        {"name": "Build", "type": "script", "config": {"command": "build"},
         "on_success": "stop"},
    ],
    "on_failure_next_carries_on": [
        {"name": "Flaky", "type": "script", "config": {"command": "flake"},
         "on_failure": "next"},
        {"name": "After", "type": "script", "config": {"command": "after"}},
    ],
    "authored_ids": [
        {"id": "tier1", "name": "T1", "type": "script", "config": {"command": "t1"}},
        {"id": "tier2", "name": "T2", "type": "script", "config": {"command": "t2"},
         "on_success": "stop"},
    ],
    "merge_on_the_final_step": [
        {"name": "Gate", "type": "script", "config": {"command": "gate"}},
        {"name": "Ship", "type": "script", "config": {"command": "ship"},
         "on_success": "merge:main", "on_failure": "stop"},
    ],
    "trigger_on_failure_mid_array": [
        {"name": "Tests", "type": "script", "config": {"command": "pytest"},
         "on_failure": "trigger:card-abc"},
        {"name": "Report", "type": "script", "config": {"command": "report"},
         "on_success": "stop"},
    ],
    "timeout_and_continuation": [
        {"name": "Long", "type": "script", "config": {"command": "sleep"},
         "timeout": 1800, "continue_in_context": True},
        {"name": "Next", "type": "agent", "config": {"agent": "mock"},
         "on_success": "stop"},
    ],
}


@pytest.mark.parametrize("shape", sorted(AGREEING_SHAPES))
def test_fixture_matches_the_production_converter(shape):
    steps = AGREEING_SHAPES[shape]

    assert linear_graph(steps) == converted(steps), (
        f"{shape}: the fixture helper and array_to_graph disagree. One of the "
        f"two is wrong - the whole reason the fixture is an independent "
        f"rendering is that this comparison can catch it."
    )


@pytest.mark.parametrize("shape", sorted(AGREEING_SHAPES))
def test_fixture_output_is_a_valid_graph(shape):
    """Every fixture graph must clear the same schema bar a user's graph does."""
    graph = PipelineGraphModel.model_validate(linear_graph(AGREEING_SHAPES[shape]))

    assert graph.version == 2
    assert graph.entry_points


def test_explicit_ids_override_the_step_dicts():
    steps = [
        {"name": "A", "type": "script", "config": {}},
        {"name": "B", "type": "script", "config": {}, "on_success": "stop"},
    ]

    graph = linear_graph(steps, ids=["first", "second"])

    assert list(graph["steps"]) == ["first", "second"]
    assert graph["entry_points"] == ["first"]
    assert graph["edges"][0]["from_step"] == "first"
    assert graph["edges"][0]["to_step"] == "second"


def test_actions_carry_the_effect_and_the_edge_survives():
    """v1 fired the effect and then ran the next step; both must be rendered."""
    steps = [
        {"name": "Tests", "type": "script", "config": {},
         "on_failure": "trigger:card-1"},
        {"name": "After", "type": "script", "config": {}, "on_success": "stop"},
    ]

    graph = linear_graph(steps)

    assert graph["steps"]["step_0"]["actions"]["failure"] == ["trigger:card-1"]
    assert graph["steps"]["step_0"]["actions"]["success"] == []
    conditions = {(e["from_step"], e["condition"]) for e in graph["edges"]}
    assert ("step_0", "failure") in conditions, "the effect must not eat the edge"
    assert ("step_0", "success") in conditions, "on_success defaults to next"


def test_last_step_action_emits_no_edge():
    steps = [{"name": "Ship", "type": "script", "config": {},
              "on_success": "merge:main"}]

    graph = linear_graph(steps)

    assert graph["steps"]["step_0"]["actions"]["success"] == ["merge:main"]
    assert graph["edges"] == []


# -----------------------------------------------------------------------------
# The refusals
# -----------------------------------------------------------------------------

def test_empty_step_list_refuses():
    with pytest.raises(GraphFixtureError, match="empty step list"):
        linear_graph([])


def test_unknown_key_refuses_naming_it():
    with pytest.raises(GraphFixtureError, match="timout"):
        linear_graph([{"name": "A", "type": "script", "timout": 5}])


def test_missing_type_refuses():
    with pytest.raises(GraphFixtureError, match="'type'"):
        linear_graph([{"name": "A"}])


def test_duplicate_ids_refuse():
    with pytest.raises(GraphFixtureError, match="both resolve to id 'dup'"):
        linear_graph([
            {"id": "dup", "name": "A", "type": "script"},
            {"id": "dup", "name": "B", "type": "script", "on_success": "stop"},
        ])


def test_unknown_action_refuses():
    with pytest.raises(GraphFixtureError, match="continue"):
        linear_graph([{"name": "A", "type": "script", "on_success": "continue"}])


def test_retired_trigger_pipeline_refuses():
    with pytest.raises(GraphFixtureError, match="trigger:pipeline:"):
        linear_graph([
            {"name": "A", "type": "script", "on_success": "trigger:pipeline:p1"},
        ])


def test_mid_list_stop_that_orphans_the_tail_refuses():
    """A fixture that builds an unreachable tail is a red run with a wrong cause."""
    with pytest.raises(GraphFixtureError, match="unreachable"):
        linear_graph([
            {"name": "A", "type": "script", "on_success": "stop", "on_failure": "stop"},
            {"name": "B", "type": "script"},
        ])


def test_the_converter_refuses_the_same_orphaned_tail():
    """The two renderings must refuse the same shapes, not just accept them."""
    from app.schemas.pipeline import ArrayConversionError

    orphaned = [
        {"name": "A", "type": "script", "on_success": "stop", "on_failure": "stop"},
        {"name": "B", "type": "script"},
    ]

    with pytest.raises(ArrayConversionError):
        converted(orphaned)


# -----------------------------------------------------------------------------
# The persist / payload wrappers
# -----------------------------------------------------------------------------

def test_graph_json_round_trips_through_the_column_shape():
    import json

    steps = [{"name": "A", "type": "script", "config": {"command": "echo"}}]

    assert json.loads(graph_json(steps)) == linear_graph(steps)


def test_graph_pipeline_payload_carries_only_the_graph():
    """Both `steps` and `steps_graph` in one body is a 422 (12.8 §4.4)."""
    payload = graph_pipeline_payload(
        [{"name": "A", "type": "script", "config": {}}], name="p"
    )

    assert "steps" not in payload
    assert payload["steps_graph"]["version"] == 2
    assert payload["name"] == "p"
