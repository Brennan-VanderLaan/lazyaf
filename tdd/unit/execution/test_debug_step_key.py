"""Breakpoint identity - contract C2, Phase 12.7.

PLAN's API sketch addresses breakpoints by step *id*; failure_01's schema
addresses them by *index*. Neither works for both pipeline formats: a graph
(v2) step has a stable `step_id` and no meaningful index, a legacy (v1) step
has an index and no id. `debug_step_key` is the ONE resolver, and these tests
pin that the gate, the create-endpoint validator and the UI cannot drift into
three different notions of which step is breakpointed.
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
from app.services.execution.debug_state import debug_step_key
from app.services.execution.debug_session_service import debug_session_service


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

    def test_legacy_step_is_addressed_by_index(self):
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=2,
            step_id=None,
            step_name="test",
            status="running",
        )
        assert debug_step_key(step_run) == "2"

    def test_index_zero_is_a_real_key_not_a_falsy_hole(self):
        """Step 0 is the most likely breakpoint; it must key as '0'."""
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id="run",
            step_index=0,
            step_id=None,
            step_name="first",
            status="running",
        )
        assert debug_step_key(step_run) == "0"

    def test_empty_step_id_falls_back_to_index(self):
        """An empty string id is absence, not an id."""
        assert debug_step_key(SimpleNamespace(step_id="", step_index=5)) == "5"

    def test_key_is_always_a_string(self):
        """The column is Text and the JSON list holds strings; an int key
        would compare unequal to everything the row stores."""
        assert isinstance(debug_step_key(SimpleNamespace(step_id=None, step_index=7)), str)


class TestResolveStepKeysForBothFormats:
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

    def test_legacy_pipeline_keys_are_indices(self):
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id="repo",
            name="legacy",
            steps=json.dumps(
                [
                    {"name": "one", "type": "script", "config": {}},
                    {"name": "two", "type": "script", "config": {}},
                ]
            ),
        )
        keys = dict(debug_session_service.resolve_step_keys(pipeline))
        assert set(keys) == {"0", "1"}
        assert keys["1"] == "two"

    def test_empty_pipeline_has_no_keys(self):
        pipeline = Pipeline(id=str(uuid4()), repo_id="repo", name="empty", steps="[]")
        assert debug_session_service.resolve_step_keys(pipeline) == []
