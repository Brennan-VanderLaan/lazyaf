"""The ad-hoc pipeline DEFINITION, written and read back (12.8 P3, lane A5).

Every ad-hoc run in the product - card work, a playground session, an endpoint
probe, an experiment cell - persists an ephemeral `Pipeline` row and then runs
it. Until 12.8 that row carried a v1 `steps` ARRAY; it now carries a v2
`steps_graph`, which is the only shape the executor will have left.

Two things needed covering before that move was safe, and neither had any:

1. **The writer.** Nothing asserted that an ad-hoc definition is one the
   executor can actually dispatch. A definition that fails at run time is a
   card that starts and dies for a reason that reads like an executor bug, so
   the graph these writers produce is checked here against
   `graph_definition_errors` - the executor's own definition-time authority -
   rather than against a shape this file guesses at.

2. **The reader.** `_run_agent_name` reads the agent back off that definition
   and the caller writes it to `card.completed_runner_type` and
   `job.runner_type`. Failing over to `None` there is silent data loss on
   EVERY card (recon 12.8 s6 item 4), which is exactly what a shape change
   under an unpinned reader produces. So the writer and the reader are pinned
   against each other here, on one persisted row, in one test.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.models import Pipeline, Repo  # noqa: E402
from app.services.agent_run import (  # noqa: E402
    TRIGGER_CARD_WORK,
    _run_agent_name,
    adhoc_steps_graph,
)
from app.services.pipeline_executor import graph_definition_errors  # noqa: E402

AGENT_STEP = {
    "id": "agent",
    "name": "Agent work",
    "type": "agent",
    "config": {"agent": "mock", "model": "mock-a"},
    "timeout": 1800,
}
VERIFY_STEP = {
    "id": "verify",
    "name": "Verify",
    "type": "script",
    "config": {"image": "python:3.11", "command": "pytest -q"},
    "timeout": 120,
}


class TestAdhocStepsGraph:
    """The shape `adhoc_steps_graph` writes, and what it refuses."""

    def test_a_single_step_is_one_node_one_entry_point_and_no_edges(self):
        graph = json.loads(adhoc_steps_graph([AGENT_STEP]))

        assert list(graph["steps"]) == ["agent"]
        assert graph["entry_points"] == ["agent"]
        assert graph["edges"] == []
        assert graph["version"] == 2
        node = graph["steps"]["agent"]
        assert node["type"] == "agent"
        assert node["config"]["agent"] == "mock"
        assert node["timeout"] == 1800

    def test_a_chain_is_joined_by_success_edges_ONLY(self):
        """The rule the experiment cell depends on, stated structurally.

        v1 said it with the agent step's `on_failure: "stop"`: a crashed agent
        produced no measurement, so verify must not run and paper a 0% over an
        error. On the graph that rule IS the absence of a failure edge, so
        this asserts the whole edge list rather than the absence of one entry
        - an edge added under `always` would satisfy "no failure edge" and
        still run verify after a crash.
        """
        graph = json.loads(adhoc_steps_graph([AGENT_STEP, VERIFY_STEP]))

        assert list(graph["steps"]) == ["agent", "verify"]
        assert graph["entry_points"] == ["agent"]
        assert [
            (e["from_step"], e["to_step"], e["condition"]) for e in graph["edges"]
        ] == [("agent", "verify", "success")]

    def test_no_node_carries_a_terminal_action(self):
        """No ad-hoc writer has ever emitted `merge:` or `trigger:`.

        Pinned because the fear when the v1 flow keys came off these sites is
        that an EFFECT came off with them. None was there: a card's auto-merge
        is `POST /api/cards/{id}/approve` calling `git_repo_manager.merge_branch`
        and the run-level `TriggerConfig.on_pass` read off `trigger_context`,
        neither of which is a step action. If an ad-hoc writer ever does need
        one, this fails and the reviewer gets to ask why.
        """
        graph = json.loads(adhoc_steps_graph([AGENT_STEP, VERIFY_STEP]))

        for node in graph["steps"].values():
            assert node["actions"] == {"success": [], "failure": [], "always": []}

    def test_the_graph_is_one_the_executor_will_dispatch(self):
        """Checked against the executor's own definition-time authority (R3).

        A hand-written expected-shape assertion cannot catch a graph that
        validates and still cannot run; `graph_definition_errors` is the
        function the executor itself asks.
        """
        for steps in ([AGENT_STEP], [AGENT_STEP, VERIFY_STEP]):
            graph = json.loads(adhoc_steps_graph(steps))
            assert graph_definition_errors(graph) == []

    def test_an_empty_definition_is_refused(self):
        """QA4-08's shape: a stepless definition must not become a pipeline
        that runs, does nothing, and reports PASSED."""
        with pytest.raises(ValueError, match="at least one step"):
            adhoc_steps_graph([])

    def test_duplicate_step_ids_are_refused(self):
        """The graph is keyed BY id, so a duplicate does not collide loudly -
        it silently drops a step. Refuse at the writer instead."""
        with pytest.raises(ValueError, match="unique"):
            adhoc_steps_graph([AGENT_STEP, dict(VERIFY_STEP, id="agent")])

    def test_a_bad_step_type_is_refused_at_the_writer(self):
        """Built through `PipelineGraphModel`, so the writer clears the same
        validator the API boundary clears (R6) - not at dispatch time."""
        with pytest.raises(Exception):
            adhoc_steps_graph([dict(AGENT_STEP, type="banana")])


class TestRunAgentNameReadsTheGraph:
    """`_run_agent_name` feeds `card.completed_runner_type` / `job.runner_type`."""

    def _run(self):
        return SimpleNamespace(id=str(uuid4()))

    def test_the_agent_resolves_off_a_graph_definition(self):
        assert (
            _run_agent_name(self._run(), adhoc_steps_graph([AGENT_STEP]))
            == "mock"
        )

    def test_it_finds_the_agent_node_among_script_nodes(self):
        """The steps are a MAPPING now, so "the agent step" is found by TYPE
        and never by position - an experiment cell puts a `verify` script in
        the same graph."""
        graph = adhoc_steps_graph([
            dict(VERIFY_STEP, id="setup"),
            dict(AGENT_STEP, config={"agent": "claude-code"}),
            VERIFY_STEP,
        ])
        assert _run_agent_name(self._run(), graph) == "claude-code"

    def test_a_script_only_definition_resolves_to_None(self):
        """An endpoint probe has no agent step. Legitimately None, silently."""
        assert _run_agent_name(self._run(), adhoc_steps_graph([VERIFY_STEP])) is None

    @pytest.mark.parametrize(
        "definition", [None, "", "not json at all", "[]", '{"steps": []}']
    )
    def test_an_unreadable_definition_is_None_AND_a_warning(self, definition, caplog):
        """The two ways this returns None are not the same thing.

        "No agent step" is normal. "The definition would not read" is a defect
        that costs every card its `completed_runner_type` with no error
        anywhere, which is how a shape change like 12.8's stays invisible.
        `'[]'` is in here on purpose: it is exactly what the old v1 reader
        would have seen once the writers stopped filling the array.
        """
        with caplog.at_level("WARNING"):
            assert _run_agent_name(self._run(), definition) is None
        assert any("agent that ran is unknown" in r.message for r in caplog.records)


class TestAdhocWriterPersistsAGraph:
    """Writer and reader pinned against ONE persisted row."""

    async def _start(self, db_session, monkeypatch):
        from app.services import agent_run, pipeline_executor as pe_module

        repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}",
                    default_branch="main", is_ingested=True)
        db_session.add(repo)
        await db_session.commit()

        captured = {}

        async def _fake_start(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="run-123")

        monkeypatch.setattr(
            pe_module.pipeline_executor, "start_pipeline", _fake_start
        )
        await agent_run.start_adhoc_agent_run(
            db_session,
            repo,
            trigger_type=TRIGGER_CARD_WORK,
            trigger_ref=str(uuid4()),
            agent="mock",
            task={"title": "Add the widget", "description": "twice"},
            work_branch="lazyaf/abc",
        )
        return await db_session.get(Pipeline, captured["pipeline"].id)

    async def test_card_work_persists_a_dispatchable_graph(
        self, db_session, monkeypatch
    ):
        pipeline = await self._start(db_session, monkeypatch)

        assert pipeline.steps_graph, "card work persisted no definition at all"
        graph = json.loads(pipeline.steps_graph)
        assert graph_definition_errors(graph) == []
        assert list(graph["steps"]) == ["agent"]
        assert graph["steps"]["agent"]["type"] == "agent"
        assert graph["steps"]["agent"]["config"]["agent"] == "mock"

    async def test_the_agent_reads_back_off_the_row_that_was_written(
        self, db_session, monkeypatch
    ):
        """The loop that `card.completed_runner_type` rides on, closed.

        Two halves that must agree - `start_adhoc_agent_run` writes the
        definition, `_run_agent_name` reads it - and nothing else in the tree
        asserts they agree below the T3 e2e.
        """
        pipeline = await self._start(db_session, monkeypatch)

        assert _run_agent_name(SimpleNamespace(id="run-123"),
                               pipeline.steps_graph) == "mock"
