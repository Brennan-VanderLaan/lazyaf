"""Breakpoint identity - contract C2, Phase 12.7, narrowed by 12.8.

PLAN's API sketch addressed breakpoints by step *id*; failure_01's schema
addressed them by *index*, and 12.7 shipped `debug_step_key` covering both
because both pipeline formats were live. **12.8 retires the v1 array, so a
step IS its `step_id` and an index is no longer an address.**

What survives of the fallback is not v1 support. A graph run writes two rows
that carry no `step_id` and are not steps at all - `_verify_graph_coverage`'s
`pipeline graph` defect row and `_spawn_fix_card`'s `trigger:` marker - and
the marker deliberately carries a REAL step's `step_index`. These tests pin
that neither can take a real step's key, that the validator offers graph step
ids and nothing else, and that a pipeline with no graph is refused out loud
rather than answered with an empty breakpoint vocabulary.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.pipeline import Pipeline, StepRun
from app.services.execution.debug_state import NON_STEP_KEY_PREFIX, debug_step_key
from app.services.execution.debug_session_service import (
    DebugSessionError,
    debug_session_service,
)


def _graph(*ids: str) -> str:
    """A linear graph over `ids`, as the column stores it."""
    return json.dumps(
        {
            "version": 2,
            "entry_points": [ids[0]],
            "steps": {
                step_id: {"name": step_id.title(), "type": "script", "config": {}}
                for step_id in ids
            },
            "edges": [
                {
                    "id": f"edge_{i}_success",
                    "from_step": ids[i],
                    "to_step": ids[i + 1],
                    "condition": "success",
                }
                for i in range(len(ids) - 1)
            ],
        }
    )


class TestDebugStepKey:
    def test_graph_step_is_addressed_by_step_id(self):
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=3,
            step_id="build",
            step_name="Build",
            status="running",
        )
        assert debug_step_key(step_run) == "build"

    def test_a_numeric_step_id_is_an_id_not_an_index(self):
        """`array_to_graph` honours author-supplied ids since 12.8, so `"0"`
        is a legal step id. It must key as itself - and step 0 is the most
        likely breakpoint, so a falsy-string hole here would be silent."""
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=7,
            step_id="0",
            step_name="first",
            status="running",
        )
        assert debug_step_key(step_run) == "0"

    def test_key_is_always_a_string(self):
        """The column is Text and the JSON list holds strings; an int key
        would compare unequal to everything the row stores."""
        assert isinstance(debug_step_key(SimpleNamespace(step_id=None, step_index=7)), str)


class TestRowsThatAreNotSteps:
    """The narrowed fallback. Both producers of a `step_id=None` StepRun, and
    the rule that covers them: a row with no `step_id` is not a step, and
    nothing can breakpoint it."""

    def test_the_graph_defect_row_is_not_addressable(self):
        """`_verify_graph_coverage` writes exactly this row when the graph is
        structurally broken with nothing left unrun: no step_id, the name
        `pipeline graph`, and an index one past the last step. It is a
        verdict about the graph, so it gets no step's identity."""
        defect_row = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=2,  # len(step_ids), for a two-step graph
            step_id=None,
            step_name="pipeline graph",
            status="failed",
            error="the pipeline graph is structurally invalid: ...",
        )
        key = debug_step_key(defect_row)
        assert key.startswith(NON_STEP_KEY_PREFIX)
        assert key != "2"

    def test_the_fix_card_marker_cannot_take_a_real_steps_key(self):
        """Correction to §5.1: `step_id=None` has TWO producers, and the
        `trigger:` marker is the dangerous one - it carries the spawning
        step's own `step_index` on purpose. With an index fallback a marker
        at index 0 in a graph whose entry step is legally named `"0"` would
        answer to that step's breakpoint. It must not."""
        real_step = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=0,
            step_id="0",
            step_name="first",
            status="failed",
        )
        marker = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=0,  # the SAME index - deliberate, it is a sub-step
            step_id=None,  # and deliberately no id
            step_name="[Fix] repair the build",
            status="running",
        )
        assert debug_step_key(real_step) == "0"
        assert debug_step_key(marker) != debug_step_key(real_step)
        assert debug_step_key(marker).startswith(NON_STEP_KEY_PREFIX)

    def test_an_empty_step_id_is_absence_not_an_id(self):
        key = debug_step_key(SimpleNamespace(step_id="", step_index=5))
        assert key == f"{NON_STEP_KEY_PREFIX}5"

    def test_a_non_step_key_is_never_offered_as_a_breakpoint(self):
        """The rule has to hold at the validator too, or a key nothing can
        dispatch would still be acceptable at create time."""
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id="repo",
            name="graph",
            steps_graph=_graph("lint", "build"),
        )
        keys = [key for key, _name in debug_session_service.resolve_step_keys(pipeline)]
        assert keys
        assert not any(key.startswith(NON_STEP_KEY_PREFIX) for key in keys)


class TestResolveStepKeys:
    """The create-endpoint validator derives its known-key set from the SAME
    resolver, fed by the pipeline definition."""

    def test_graph_pipeline_keys_are_step_ids(self):
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id="repo",
            name="graph",
            steps_graph=json.dumps(
                {
                    "entry_points": ["lint"],
                    "steps": {
                        "lint": {"name": "Lint", "type": "script"},
                        "build": {"name": "Build", "type": "script"},
                    },
                    "edges": [],
                }
            ),
        )
        keys = dict(debug_session_service.resolve_step_keys(pipeline))
        assert set(keys) == {"lint", "build"}
        assert keys["build"] == "Build"

    def test_a_pipeline_with_no_graph_is_refused_naming_itself(self):
        """12.8: the graph is the only definition. Returning `[]` here made a
        pipeline nothing could address look like a pipeline with no steps -
        and a create with no breakpoints would then have started a debug
        session that could never stop the run."""
        pipeline = Pipeline(id=str(uuid4()), repo_id="repo", name="arrays-only")
        with pytest.raises(DebugSessionError) as exc:
            debug_session_service.resolve_step_keys(pipeline)
        assert "arrays-only" in str(exc.value)
        assert "no graph definition" in str(exc.value)

    def test_an_empty_graph_is_refused_the_same_way(self):
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id="repo",
            name="empty",
            steps_graph=json.dumps({"entry_points": [], "steps": {}, "edges": []}),
        )
        with pytest.raises(DebugSessionError):
            debug_session_service.resolve_step_keys(pipeline)
