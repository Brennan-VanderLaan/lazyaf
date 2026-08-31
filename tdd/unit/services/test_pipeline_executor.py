"""
Unit tests for PipelineExecutor service.

These tests verify the pipeline execution logic including:
- Terminal NODE actions on the graph path (12.8)
- Graph edge dispatch for every condition (success/failure/always)
- The run verdict, which is the StepRuns' and never an action's
- Step state transitions
- start_pipeline's refusal to run a pipeline with no graph definition

12.8 P5 DELETED the v1 array fork, and with it the classes that drove it:
`TestParseSteps`, `TestPipelineExecutorActionHandlers` and
`TestStepBranchingLogic`. Nothing was dropped on the floor - each claim
either moved to the surviving surface or was already there:

  parse_steps                       -> tdd/unit/schemas/test_pipeline_schemas.py
                                       (`_GRAPH_JSON`, the PipelineRead parse
                                       tests, converted at P3)
  the closed action vocabulary      -> test_graph_pipeline_schemas.py
  (`describe_step_action`)             ::TestDescribeTerminalActionVocabulary
  on_success: next                  -> TestGraphEdgeConditions
                                       ::test_a_success_edge_fires_when_the_source_passes
  on_success/on_failure: stop       -> TestTheRunVerdictIsTheStepRuns (below)
  trigger:{card} / merge:{branch}   -> TestTheFixCardAction,
                                       TestAnActionNeverCompletesThePipeline
  on_failure: next continues        -> TestTheRunVerdictIsTheStepRuns
                                       ::test_a_failure_edge_that_continues_still_ends_the_run_failed
                                       (and the verdict INVERTS - plan 1.8)

`TestStepBranchingLogic`'s five tests were `step = {...}; assert
step.get("on_success", "next") == "next"` - tests of `dict.get`, with no
production surface at either end (plan 7.5). The one claim among them that
named real behaviour (`on_failure: next` continues past a failed step) is the
converted test named above, which now drives the real executor and pins the
verdict change that conversion brings.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas.pipeline import TERMINAL_ACTION_PREFIXES
from app.services.pipeline_executor import (
    LocalStepContextError,
    PipelineExecutor,
    describe_terminal_action,
    parse_steps_graph,
    pipeline_run_to_ws_dict,
    step_run_to_ws_dict,
)
from app.models import Card, Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import ExecutorMode, RunStatus


class TestPipelineRunToWsDict:
    """Tests for pipeline_run_to_ws_dict conversion function."""

    def test_converts_basic_fields(self):
        """Should convert all basic pipeline run fields."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.pipeline_id = "pipeline-456"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 1
        mock_run.steps_completed = 1
        mock_run.steps_total = 3
        mock_run.started_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_run.completed_at = None
        mock_run.created_at = datetime(2024, 1, 15, 9, 55, 0)

        result = pipeline_run_to_ws_dict(mock_run)

        assert result["id"] == "run-123"
        assert result["pipeline_id"] == "pipeline-456"
        assert result["status"] == "running"
        assert result["trigger_type"] == "manual"
        assert result["trigger_ref"] is None
        assert result["current_step"] == 1
        assert result["steps_completed"] == 1
        assert result["steps_total"] == 3
        assert result["started_at"] == "2024-01-15T10:00:00"
        assert result["completed_at"] is None

    def test_handles_none_timestamps(self):
        """Should handle None timestamps gracefully."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.pipeline_id = "pipeline-456"
        mock_run.status = RunStatus.PENDING.value
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = None
        mock_run.completed_at = None
        mock_run.created_at = None

        result = pipeline_run_to_ws_dict(mock_run)

        assert result["started_at"] is None
        assert result["completed_at"] is None
        assert result["created_at"] is None


class TestStepRunToWsDict:
    """Tests for step_run_to_ws_dict conversion function."""

    def test_converts_basic_fields(self):
        """Should convert all basic step run fields."""
        mock_step = MagicMock()
        mock_step.id = "step-123"
        mock_step.pipeline_run_id = "run-456"
        mock_step.step_index = 0
        mock_step.step_name = "Test Step"
        mock_step.status = RunStatus.RUNNING.value
        mock_step.job_id = "job-789"
        mock_step.error = None
        mock_step.started_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_step.completed_at = None

        result = step_run_to_ws_dict(mock_step)

        assert result["id"] == "step-123"
        assert result["pipeline_run_id"] == "run-456"
        assert result["step_index"] == 0
        assert result["step_name"] == "Test Step"
        assert result["status"] == "running"
        assert result["job_id"] == "job-789"
        assert result["error"] is None

    def test_handles_none_timestamps(self):
        """Should handle None timestamps gracefully."""
        mock_step = MagicMock()
        mock_step.id = "step-123"
        mock_step.pipeline_run_id = "run-456"
        mock_step.step_index = 0
        mock_step.step_name = "Test Step"
        mock_step.status = RunStatus.PENDING.value
        mock_step.job_id = None
        mock_step.error = None
        mock_step.started_at = None
        mock_step.completed_at = None

        result = step_run_to_ws_dict(mock_step)

        assert result["started_at"] is None
        assert result["completed_at"] is None


class TestPipelineExecutorStartPipeline:
    """Tests for PipelineExecutor.start_pipeline method.

    The two tests here were the v1 array pair (`steps = "[]"` marks the run
    PASSED; `steps = '[{...}]'` dispatches index 0). 12.8 P5 converts both:
    the definition is a graph, and the first of the two INVERTS - see
    `test_a_pipeline_with_no_graph_definition_is_refused_not_passed`.
    """

    pytestmark = pytest.mark.asyncio

    async def test_a_pipeline_with_no_graph_definition_is_refused_not_passed(
        self, db_session, graph_executor, quiet_manager
    ):
        """THE VACUOUS PASS, INVERTED (plan 1.6c / QA4-08).

        The v1 twin of this test asserted that a pipeline with `steps = "[]"`
        "should immediately mark as passed" - a run that did nothing,
        reporting green, which every downstream gate then trusts. That was
        only ever tolerable because `parse_steps` swallowed a missing
        definition to `[]` and there was no other answer available.

        There is now: the graph is the only definition, so no graph is no
        pipeline. The run row still exists (callers get a PipelineRun, the
        websocket still sees it appear and go terminal) and it ends FAILED
        with the reason on a row a user actually looks at.
        """
        repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True)
        db_session.add(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="Not Authored Yet",
            steps="[]",
            steps_graph=None,
        )
        db_session.add(pipeline)
        await db_session.commit()

        run = await graph_executor.start_pipeline(
            db=db_session, pipeline=pipeline, repo=repo
        )

        assert run.status == RunStatus.FAILED.value
        assert run.completed_at is not None

        rows = await _step_rows(db_session, run)
        assert len(rows) == 1, "the refusal must leave exactly one row to look at"
        assert rows[0].status == RunStatus.FAILED.value
        assert rows[0].step_id is None
        assert "no runnable definition" in rows[0].error
        assert "Not Authored Yet" in rows[0].error

    async def test_an_unparseable_graph_is_refused_the_same_way(
        self, db_session, graph_executor, quiet_manager
    ):
        """`parse_steps_graph` swallows a JSONDecodeError to None (a known
        dark channel, pinned in test_pipeline_schemas.py and owned by
        migration 0014). Whatever the cause, the run must not be green: this
        pins that the swallow lands in the REFUSAL and not in a pass."""
        repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True)
        db_session.add(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="Corrupt",
            steps="[]",
            steps_graph="{not json",
        )
        db_session.add(pipeline)
        await db_session.commit()

        run = await graph_executor.start_pipeline(
            db=db_session, pipeline=pipeline, repo=repo
        )

        assert run.status == RunStatus.FAILED.value
        rows = await _step_rows(db_session, run)
        assert [r.status for r in rows] == [RunStatus.FAILED.value]

    async def test_an_empty_graph_still_passes_and_the_guard_is_named(
        self, db_session, graph_executor, quiet_manager
    ):
        """A graph object with ZERO steps is a vacuous pass HERE, and that is
        deliberate - `_verify_graph_coverage` has nothing to be uncovered.

        It is pinned rather than fixed because the guard belongs at the
        boundary, not the executor: `array_to_graph` refuses an empty array,
        `PipelineGraphModel` refuses empty `entry_points`, and
        `POST /api/pipelines/{id}/run` refuses a pipeline with no steps. This
        shape can only be reached by writing the column directly. The test
        exists so the behaviour is a recorded decision with its owners named,
        not something a reader discovers from a green run.
        """
        empty = {"steps": {}, "edges": [], "entry_points": [], "version": 2}
        repo, pipeline, _ = await _graph_rows(db_session, empty)

        run = await graph_executor.start_pipeline(
            db=db_session, pipeline=pipeline, repo=repo
        )

        assert run.status == RunStatus.PASSED.value
        assert run.steps_total == 0

    async def test_start_pipeline_dispatches_the_entry_point(
        self, db_session, graph_executor, quiet_manager
    ):
        """The graph twin of "executes the first step": entry points are what
        dispatch, and the run stays RUNNING while they do."""
        graph_dict = _graph(
            [_node("a"), _node("b")],
            [_graph_edge("e", "a", "b")],
            ["a"],
        )
        repo, pipeline, _ = await _graph_rows(db_session, graph_dict)

        dispatched = []
        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            run = await graph_executor.start_pipeline(
                db=db_session, pipeline=pipeline, repo=repo
            )

        assert run.status == RunStatus.RUNNING.value
        assert dispatched == ["a"], "only the entry point dispatches"
        assert run.steps_total == 2

    async def test_every_entry_point_dispatches_in_parallel(
        self, db_session, graph_executor, quiet_manager
    ):
        """v1 had one start; a graph has as many as it declares."""
        graph_dict = _graph(
            [_node("a"), _node("b"), _node("z")],
            [
                _graph_edge("e1", "a", "z", "always"),
                _graph_edge("e2", "b", "z", "always"),
            ],
            ["a", "b"],
        )
        repo, pipeline, _ = await _graph_rows(db_session, graph_dict)

        dispatched = []
        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            run = await graph_executor.start_pipeline(
                db=db_session, pipeline=pipeline, repo=repo
            )

        assert dispatched == ["a", "b"]
        assert sorted(json.loads(run.active_step_ids)) == ["a", "b"], (
            "both entry points must be RESERVED before either is dispatched"
        )


class TestPipelineExecutorCancelRun:
    """Tests for PipelineExecutor.cancel_run method."""

    @pytest.fixture
    def executor(self):
        return PipelineExecutor()

    @pytest.mark.asyncio
    async def test_cancel_run_marks_cancelled(self, executor):
        """Cancelling a run should mark it as cancelled."""
        mock_db = AsyncMock()
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.completed_at = None
        mock_run.step_runs = []
        mock_run.pipeline_id = "pipeline-456"
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = datetime.utcnow()
        mock_run.created_at = datetime.utcnow()

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()
            mock_manager.send_step_run_status = AsyncMock()

            result = await executor.cancel_run(mock_db, mock_run)

            assert result.status == RunStatus.CANCELLED.value
            assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_cancel_run_cancels_running_steps(self, executor):
        """Cancelling a run should cancel any running step runs."""
        mock_db = AsyncMock()

        mock_step_run = MagicMock()
        mock_step_run.id = "step-123"
        mock_step_run.status = RunStatus.RUNNING.value
        mock_step_run.completed_at = None
        mock_step_run.error = None
        mock_step_run.job_id = None
        mock_step_run.pipeline_run_id = "run-123"
        mock_step_run.step_index = 0
        mock_step_run.step_name = "Test Step"
        mock_step_run.started_at = datetime.utcnow()

        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.completed_at = None
        mock_run.step_runs = [mock_step_run]
        mock_run.pipeline_id = "pipeline-456"
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = datetime.utcnow()
        mock_run.created_at = datetime.utcnow()

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()
            mock_manager.send_step_run_status = AsyncMock()

            await executor.cancel_run(mock_db, mock_run)

            assert mock_step_run.status == RunStatus.CANCELLED.value
            assert mock_step_run.completed_at is not None
            assert mock_step_run.error == "Cancelled by user"


# =============================================================================
# 12.8 - TERMINAL NODE ACTIONS ON THE GRAPH PATH
#
# v1's `on_success` / `on_failure` string carried two things in one word: FLOW
# (`next`, `stop`) and EFFECT (`merge:{branch}`, `trigger:{card_id}`). The
# graph expresses flow with edges, so what is left on a node is pure effect -
# `PipelineStepV2.actions`, a LIST per condition, because one string could
# never say "merge AND spawn a fix card".
#
# The gap this closes: `array_to_graph` emitted an edge only for the literal
# string "next" and DROPPED `merge:` / `trigger:` in silence, and
# `_handle_action` - the only implementation of either - was reachable from
# the array branch alone. So the graph could not merge a branch or spawn a fix
# card at all, and nothing said so.
#
# These run against a REAL session and REAL rows (R6): the subject is what
# gets written, in what order, and what the run's verdict is afterwards, and a
# mock session would prove none of the three. The v1 path above is untouched
# and still green - both paths exist side by side until the capability is
# provably present in the graph.
#
# Section 1.4 calls three properties mandatory; they are the first three
# classes here:
#
#   1. an action NEVER completes the pipeline
#   2. an action that cannot be performed FAILS THE RUN, rather than letting
#      the graph carry on over a side effect that did not happen (R1)
#   3. a `merge:` lands BEFORE any downstream step reads the branch
# =============================================================================


def _node(step_id, *, actions=None, **overrides):
    """A graph node. `actions=None` omits the key entirely - which is how
    every graph written before 12.8 looks, and it must keep working."""
    node = {
        "id": step_id,
        "name": step_id,
        "type": "script",
        "config": {"command": "echo x"},
        "timeout": 300,
    }
    if actions is not None:
        node["actions"] = actions
    node.update(overrides)
    return node


def _graph_edge(edge_id, from_step, to_step, condition="success"):
    return {
        "id": edge_id,
        "from_step": from_step,
        "to_step": to_step,
        "condition": condition,
    }


def _graph(nodes, edges, entry_points):
    return {
        "steps": {n["id"]: n for n in nodes},
        "edges": edges,
        "entry_points": entry_points,
        "version": 2,
    }


def _actions(*, success=(), failure=(), always=()):
    return {
        "success": list(success),
        "failure": list(failure),
        "always": list(always),
    }


async def _graph_rows(db, graph_dict, *, trigger_context=None):
    """(repo, pipeline, run) for a graph pipeline mid-flight."""
    repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True)
    db.add(repo)
    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name="graph pipeline",
        steps="[]",
        steps_graph=json.dumps(graph_dict),
    )
    db.add(pipeline)
    run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status=RunStatus.RUNNING.value,
        trigger_type="push",
        trigger_context=(
            json.dumps(trigger_context) if trigger_context is not None else None
        ),
        steps_completed=0,
        steps_total=len(graph_dict["steps"]),
        completed_step_ids=json.dumps([]),
        active_step_ids=json.dumps([]),
    )
    db.add(run)
    await db.commit()
    return repo, pipeline, run


async def _finished_step_run(db, run, graph_dict, step_id, *, passed, started_at=None):
    """The terminal StepRun a completed graph step leaves behind."""
    step_ids = list(graph_dict["steps"])
    when = started_at or datetime.utcnow()
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=step_ids.index(step_id),
        step_id=step_id,
        step_name=step_id,
        status=RunStatus.PASSED.value if passed else RunStatus.FAILED.value,
        executor=ExecutorMode.LOCAL.value,
        started_at=when,
        completed_at=when,
    )
    db.add(step_run)
    await db.commit()
    await db.refresh(step_run)
    return step_run


async def _step_rows(db, run):
    result = await db.execute(
        StepRun.__table__.select().where(
            StepRun.__table__.c.pipeline_run_id == run.id
        )
    )
    return list(result.fetchall())


class _FakeGit:
    """Records merges instead of performing them. `events` is shared with the
    dispatch recorder so ORDER across the two can be asserted."""

    def __init__(self, *, merge_success=True, events=None):
        self.merges = []
        self.merge_success = merge_success
        self.events = events if events is not None else []

    def merge_branch(self, repo_id, source_branch, target_branch):
        self.merges.append((repo_id, source_branch, target_branch))
        self.events.append(("merge", target_branch))
        if self.merge_success:
            return {"success": True, "message": "merged"}
        return {"success": False, "error": "conflict"}

    def delete_directory_from_branch(self, repo_id, branch, directory):
        return {"success": True}


def _recording_dispatch(dispatched, events=None):
    """Stands in for `_execute_step`: records the fan-out without
    running anything, so the reserved batch stays active and the run stays
    RUNNING - exactly the state a real in-flight fan-out is in."""

    async def dispatch(_db, _run, _pipeline, _repo, _graph, step_id, *args, **kwargs):
        dispatched.append(step_id)
        if events is not None:
            events.append(("dispatch", step_id))

    return dispatch


@pytest.fixture
def graph_executor():
    return PipelineExecutor()


@pytest.fixture
def quiet_manager():
    """The websocket manager is not the subject, but the broadcasts are real
    awaits and must not become coroutines nobody awaits."""
    with patch(
        "app.services.pipeline_executor.manager", new_callable=MagicMock
    ) as mock:
        mock.send_pipeline_run_status = AsyncMock()
        mock.send_step_run_status = AsyncMock()
        mock.publish_step_update = AsyncMock()
        mock.send_job_status = AsyncMock()
        yield mock


@pytest.fixture
def fake_git(monkeypatch):
    git = _FakeGit()
    monkeypatch.setattr("app.services.pipeline_executor.git_repo_manager", git)
    return git


@pytest.fixture
def spawned_card_runs(monkeypatch):
    """Captures `agent_run.start_card_work` - the ad-hoc run a `trigger:`
    action dispatches. `_spawn_fix_card` imports it inside its body, so the
    patch lands on the module attribute."""
    calls = []

    async def fake_start_card_work(db, card, repo, *, job_id, **kwargs):
        calls.append(SimpleNamespace(card=card, repo=repo, job_id=job_id))
        return SimpleNamespace(id=str(uuid4()))

    monkeypatch.setattr(
        "app.services.agent_run.start_card_work", fake_start_card_work
    )
    return calls


async def _template_card(db, repo):
    card = Card(
        id=str(uuid4()),
        repo_id=repo.id,
        title="Fix the failing step",
        description="",
        status="todo",
        runner_type="mock",
        step_type="agent",
        step_config=json.dumps({"command": "echo fix"}),
    )
    db.add(card)
    await db.commit()
    return card


class TestAnActionNeverCompletesThePipeline:
    """Property 1 of section 1.4.

    v1's `_merge_branch` ended in `_complete_pipeline(success=True)` on two of
    its three paths, and `_execute_step`'s past-the-end branch on a third.
    Those are three FALSE GREENS: a merge on the last step reported the whole
    run PASSED without ever asking whether the other steps did. Actions are
    effects; the verdict stays `_verify_graph_coverage` +
    `_check_all_steps_passed`, always.
    """

    pytestmark = pytest.mark.asyncio

    async def test_a_merge_on_a_mid_graph_node_leaves_the_run_running(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph(
            [_node("a", actions=_actions(success=["merge:main"])), _node("b")],
            [_graph_edge("e1", "a", "b")],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        dispatched = []
        completions = []
        real_complete = graph_executor._complete_pipeline

        async def counting_complete(db, pipeline_run, success):
            completions.append(success)
            return await real_complete(db, pipeline_run, success=success)

        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ), patch.object(graph_executor, "_complete_pipeline", new=counting_complete):
            await graph_executor._handle_step_complete(
                db_session, run, pipeline, repo, g, "a", True, None,
                step_run=step_run,
            )

        assert fake_git.merges == [(repo.id, "feature-x", "main")]
        assert dispatched == ["b"], "the fan-out did not happen after the effect"
        assert completions == [], "the action completed the run - v1's defect"
        assert run.status == RunStatus.RUNNING.value

    async def test_a_merge_on_the_last_node_does_not_rescue_a_failed_run(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        """The sharpest form of property 1.

        `a` FAILED, its failure edge ran `b`, and `b` merges on success. v1
        would have called `_complete_pipeline(success=True)` from inside the
        merge (last step, nothing after it) and reported the run PASSED with a
        failed step in it. The graph asks `_check_all_steps_passed`.
        """
        g = _graph(
            [_node("a"), _node("b", actions=_actions(success=["merge:main"]))],
            [_graph_edge("e1", "a", "b", "failure")],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        await _finished_step_run(db_session, run, g, "a", passed=False)
        b_run = await _finished_step_run(db_session, run, g, "b", passed=True)
        run.completed_step_ids = json.dumps(["a"])
        await db_session.commit()

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "b", True, None, step_run=b_run
        )

        assert fake_git.merges == [(repo.id, "feature-x", "main")]
        assert run.status == RunStatus.FAILED.value, (
            "a run containing a FAILED step reported PASSED because a merge "
            "completed it - the v1 false green"
        )


class TestAnActionThatCannotBePerformedFailsTheRun:
    """Property 2 of section 1.4 (R1).

    A `merge:` that did not land leaves every downstream step reading the
    wrong branch; a `trigger:` that did not spawn leaves nobody fixing what
    failed. Both look exactly like success from the outside, which is why the
    run fails and the reason is written where a user looks.
    """

    pytestmark = pytest.mark.asyncio

    async def test_an_unresolvable_branch_fails_the_run_and_never_fans_out(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph(
            [_node("a", actions=_actions(success=["merge:main"])), _node("b")],
            [_graph_edge("e1", "a", "b")],
            ["a"],
        )
        # No trigger context and no job on the step: nothing names a branch.
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        dispatched = []
        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            await graph_executor._handle_step_complete(
                db_session, run, pipeline, repo, g, "a", True, None,
                step_run=step_run,
            )

        assert fake_git.merges == [], "the merge was attempted anyway"
        assert dispatched == [], (
            "the graph fanned out over a side effect that did not happen"
        )
        assert run.status == RunStatus.FAILED.value

    async def test_the_refusal_names_the_step_the_action_and_the_reason(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert step_run.status == RunStatus.FAILED.value
        assert "'a'" in step_run.error
        assert "merge:main" in step_run.error
        assert "could not resolve the source branch" in step_run.error

    async def test_a_failed_merge_writes_the_reason_on_the_step(
        self, db_session, graph_executor, quiet_manager, monkeypatch
    ):
        """v1's merge-FAILURE branch logged and failed the run with no error
        written anywhere - a red run with nothing red in it, and no way to
        find out why (adversarial review item 13). Every false return from
        `_run_terminal_action` now writes the reason onto the step."""
        git = _FakeGit(merge_success=False)
        monkeypatch.setattr("app.services.pipeline_executor.git_repo_manager", git)

        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert git.merges == [(repo.id, "feature-x", "main")]
        assert run.status == RunStatus.FAILED.value
        assert "conflict" in step_run.error
        assert "merge:main" in step_run.error

    async def test_a_fix_card_that_cannot_be_spawned_fails_the_run(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph(
            [_node("a", actions=_actions(failure=["trigger:no-such-card"]))],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=False)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", False, None, step_run=step_run
        )

        assert run.status == RunStatus.FAILED.value
        assert "template card no-such-card not found" in step_run.error

    async def test_an_existing_step_error_is_appended_to_never_replaced(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        """The common case is a FAILING step firing an `on_failure` action, so
        the step usually already carries the real cause."""
        g = _graph(
            [_node("a", actions=_actions(failure=["trigger:no-such-card"]))],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=False)
        step_run.error = "exit code 1: the suite is red"
        await db_session.commit()

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", False, None, step_run=step_run
        )

        assert "the suite is red" in step_run.error
        assert "no-such-card" in step_run.error

    @pytest.mark.parametrize("action", ["next", "stop"])
    async def test_flow_words_are_refused_as_node_actions(
        self, db_session, graph_executor, quiet_manager, fake_git, action
    ):
        """`next` and `stop` are FLOW, and flow on a graph is an edge. A node
        asking for them is asking for something this dispatcher cannot do, and
        quietly ignoring it would be a second silent path."""
        g = _graph([_node("a", actions=_actions(success=[action]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert run.status == RunStatus.FAILED.value
        assert "FLOW" in step_run.error
        assert "edge" in step_run.error

    async def test_trigger_pipeline_is_refused_and_never_becomes_a_card(
        self, db_session, graph_executor, quiet_manager, fake_git, spawned_card_runs
    ):
        """The retirement check has to beat the `trigger:` prefix. Otherwise
        `trigger:pipeline:p1` is accepted as "a card whose id is `pipeline:p1`",
        which looks like a working action and spawns nothing."""
        g = _graph(
            [_node("a", actions=_actions(success=["trigger:pipeline:p1"]))],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert spawned_card_runs == []
        assert run.status == RunStatus.FAILED.value
        assert "retired" in step_run.error

    async def test_an_unknown_action_is_refused(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions(always=["mrege:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert run.status == RunStatus.FAILED.value
        assert "unknown node action" in step_run.error
        assert "'mrege:main'" in step_run.error

    async def test_a_second_action_does_not_run_after_the_first_refuses(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph(
            [
                _node(
                    "a",
                    actions=_actions(success=["mrege:main", "merge:release"]),
                )
            ],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert fake_git.merges == [], (
            "an action ran after the run had already been failed"
        )


class TestAMergeLandsBeforeTheFanOut:
    """Property 3 of section 1.4.

    Ordering is the whole point of firing actions where they are fired: a
    downstream step that reads the merged branch before the merge lands reads
    the pre-merge tree and passes on the wrong code, silently.
    """

    pytestmark = pytest.mark.asyncio

    async def test_the_merge_happens_before_the_downstream_step_dispatches(
        self, db_session, graph_executor, quiet_manager, monkeypatch
    ):
        events = []
        git = _FakeGit(events=events)
        monkeypatch.setattr("app.services.pipeline_executor.git_repo_manager", git)

        g = _graph(
            [
                _node("a", actions=_actions(success=["merge:main"])),
                _node("b"),
                _node("c"),
            ],
            [_graph_edge("e1", "a", "b"), _graph_edge("e2", "a", "c")],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        dispatched = []
        with patch.object(
            graph_executor,
            "_execute_step",
            new=_recording_dispatch(dispatched, events),
        ):
            await graph_executor._handle_step_complete(
                db_session, run, pipeline, repo, g, "a", True, None,
                step_run=step_run,
            )

        assert events == [
            ("merge", "main"),
            ("dispatch", "b"),
            ("dispatch", "c"),
        ], events

    async def test_the_condition_actions_run_before_the_always_actions(
        self, db_session, graph_executor, quiet_manager, monkeypatch
    ):
        """Stated order, not an accident of dict iteration: a node can say
        "merge on success, and always merge the release branch" and get both,
        in that sequence."""
        events = []
        git = _FakeGit(events=events)
        monkeypatch.setattr("app.services.pipeline_executor.git_repo_manager", git)

        g = _graph(
            [
                _node(
                    "a",
                    actions=_actions(
                        success=["merge:main"], always=["merge:release"]
                    ),
                )
            ],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert events == [("merge", "main"), ("merge", "release")]
        assert run.status == RunStatus.PASSED.value


class TestTheFixCardAction:
    """`trigger:{card_id}` on the graph. The v1 KNOWN LIMITATION - "when the
    triggering step is the LAST one, continuing past it completes the run
    PASSED even though the action fired from on_failure" - evaporates here,
    because continuation is an edge and the verdict is
    `_check_all_steps_passed`."""

    pytestmark = pytest.mark.asyncio

    async def test_a_failure_action_spawns_the_fix_and_the_run_ends_failed(
        self, db_session, graph_executor, quiet_manager, fake_git, spawned_card_runs
    ):
        g = _graph([_node("a", actions=None)], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        template = await _template_card(db_session, repo)
        g["steps"]["a"]["actions"] = _actions(failure=[f"trigger:{template.id}"])
        pipeline.steps_graph = json.dumps(g)
        await db_session.commit()
        step_run = await _finished_step_run(db_session, run, g, "a", passed=False)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", False, None, step_run=step_run
        )

        assert len(spawned_card_runs) == 1
        cloned = spawned_card_runs[0].card
        assert cloned.title == f"[Pipeline Fix] {template.title}"
        assert cloned.branch_name.startswith("lazyaf/")
        assert run.status == RunStatus.FAILED.value, (
            "the fix card rescued the verdict of the step that needed fixing"
        )

    async def test_the_marker_step_run_is_terminal_and_carries_no_step_id(
        self, db_session, graph_executor, quiet_manager, fake_git, spawned_card_runs
    ):
        """The marker is how a triggered card shows up in the run. It carries
        no job_id (a second row at this index claiming the step's job would
        poison merge-source resolution) and no step_id - which is what keeps
        it out of `_graph_step_outcomes` and out of `_latest_step_run_for`."""
        g = _graph([_node("a")], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        template = await _template_card(db_session, repo)
        g["steps"]["a"]["actions"] = _actions(failure=[f"trigger:{template.id}"])
        step_run = await _finished_step_run(db_session, run, g, "a", passed=False)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", False, None, step_run=step_run
        )

        rows = await _step_rows(db_session, run)
        markers = [r for r in rows if r.step_name.startswith("[Fix]")]
        assert len(markers) == 1
        assert markers[0].status == RunStatus.PASSED.value
        assert markers[0].error is None
        assert markers[0].job_id is None
        assert markers[0].step_id is None

        outcomes = await graph_executor._graph_step_outcomes(db_session, run)
        assert outcomes == {"a": False}, (
            "the fix-card marker voted on the graph's verdict"
        )


class TestTheStepRowAnActionIsAttributedTo:
    """Adversarial review item 2: `_handle_step_complete` held no
    StepRun, and `StepRun.step_id == completed_step_id` matches a retry's
    second row. Both halves are pinned here."""

    pytestmark = pytest.mark.asyncio

    async def test_a_caller_with_no_row_resolves_one_by_step_id(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None
        )

        assert fake_git.merges == [(repo.id, "feature-x", "main")]
        assert run.status == RunStatus.PASSED.value

    async def test_a_retried_step_is_attributed_to_its_newest_row(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        older = await _finished_step_run(
            db_session, run, g, "a", passed=False,
            started_at=datetime(2024, 1, 15, 10, 0, 0),
        )
        newer = await _finished_step_run(
            db_session, run, g, "a", passed=True,
            started_at=datetime(2024, 1, 15, 11, 0, 0),
        )

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None
        )

        await db_session.refresh(older)
        await db_session.refresh(newer)
        assert newer.error and "could not resolve" in newer.error
        assert older.error is None, "the refusal landed on an arbitrary row"

    async def test_the_fix_card_marker_is_never_chosen_as_a_steps_row(
        self, db_session, graph_executor
    ):
        g = _graph([_node("a")], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        real = await _finished_step_run(db_session, run, g, "a", passed=False)
        db_session.add(
            StepRun(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                step_index=0,
                step_name="[Fix] something",
                status=RunStatus.PASSED.value,
                started_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
        await db_session.commit()

        found = await graph_executor._latest_step_run_for(db_session, run, "a")
        assert found.id == real.id

    async def test_actions_with_no_row_to_blame_refuse_loudly(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        """A completed graph step always has a row, so this is an invariant
        violation - and firing blind would leave the effect unattributable.
        Refusing silently would be the same dark hole from the other side."""
        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None
        )

        assert fake_git.merges == []
        assert run.status == RunStatus.FAILED.value
        rows = await _step_rows(db_session, run)
        assert len(rows) == 1
        assert rows[0].status == RunStatus.FAILED.value
        assert rows[0].step_id == "a"
        assert "no step row" in rows[0].error
        assert "'merge:main'" in rows[0].error


class TestTerminalActionEdgeCases:
    pytestmark = pytest.mark.asyncio

    async def test_a_pre_12_8_graph_node_fires_nothing(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        """Purely additive: a graph written before this phase has no `actions`
        key at all, and `(step.get("actions") or {}).get(condition) or []`
        must read that as "nothing to do", not raise and not guess."""
        g = _graph(
            [_node("a"), _node("b")], [_graph_edge("e1", "a", "b")], ["a"]
        )
        assert "actions" not in g["steps"]["a"]
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        dispatched = []
        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            await graph_executor._handle_step_complete(
                db_session, run, pipeline, repo, g, "a", True, None,
                step_run=step_run,
            )

        assert fake_git.merges == []
        assert dispatched == ["b"]
        assert run.status == RunStatus.RUNNING.value

    async def test_an_empty_actions_block_fires_nothing(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions())], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert fake_git.merges == []
        assert run.status == RunStatus.PASSED.value

    async def test_the_untaken_condition_does_not_fire(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph(
            [
                _node(
                    "a",
                    actions=_actions(
                        success=["merge:main"], failure=["merge:hotfix"]
                    ),
                )
            ],
            [],
            ["a"],
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=False)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", False, None, step_run=step_run
        )

        assert fake_git.merges == [(repo.id, "feature-x", "hotfix")]

    async def test_source_equal_to_target_skips_the_merge_without_failing(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        g = _graph([_node("a", actions=_actions(success=["merge:main"]))], [], ["a"])
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "main"}
        )
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "a", True, None, step_run=step_run
        )

        assert fake_git.merges == []
        assert run.status == RunStatus.PASSED.value

    async def test_a_routing_failure_still_fires_the_failure_actions(
        self, db_session, graph_executor, quiet_manager, fake_git
    ):
        """The rule, stated (adversarial review item 15): a node that never
        STARTED still fires its `failure` actions. That is v1's behaviour
        exactly - `_execute_step`'s route-error branch applies `on_failure` -
        and it is the honest reading of what the author asked for. Firing for
        one class of failure and silently skipping another is the dark
        behaviour this phase exists to remove."""
        g = _graph(
            [_node("a", actions=_actions(failure=["merge:main"]))], [], ["a"]
        )
        repo, pipeline, run = await _graph_rows(
            db_session, g, trigger_context={"branch": "feature-x"}
        )

        with patch.object(
            graph_executor, "_decide_route", side_effect=RuntimeError("no route")
        ):
            await graph_executor._execute_step(
                db_session, run, pipeline, repo, g, "a"
            )

        assert fake_git.merges == [(repo.id, "feature-x", "main")]
        assert run.status == RunStatus.FAILED.value


def test_the_dispatcher_and_the_schema_share_one_definition():
    """R3. The schema validator and the executor's dispatcher call the SAME
    function, so a typo is a 422 at the boundary and a named run failure at
    run time - and neither can drift into accepting what the other refuses."""
    from app.schemas.pipeline import describe_terminal_action as schema_side

    assert describe_terminal_action is schema_side


class TestGraphEdgeConditions:
    """Recon L28-06: no test in ANY tier has ever executed a failure edge or
    an always edge - `success` is the only condition that has ever been
    dispatched. Retiring v1 makes edges the SOLE expression of failure
    routing, and the dogfood pipeline (all `on_failure: stop`) cannot catch a
    defect there. This is that coverage, and it is a hard prerequisite for the
    retirement rather than a nice-to-have."""

    pytestmark = pytest.mark.asyncio

    BRANCHY = _graph(
        [_node("a"), _node("ok"), _node("recover"), _node("cleanup")],
        [
            _graph_edge("e1", "a", "ok", "success"),
            _graph_edge("e2", "a", "recover", "failure"),
            _graph_edge("e3", "a", "cleanup", "always"),
        ],
        ["a"],
    )

    async def _complete_a(self, db, executor, success):
        repo, pipeline, run = await _graph_rows(db, self.BRANCHY)
        step_run = await _finished_step_run(
            db, run, self.BRANCHY, "a", passed=success
        )
        dispatched = []
        with patch.object(
            executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            await executor._handle_step_complete(
                db, run, pipeline, repo, self.BRANCHY, "a", success, None,
                step_run=step_run,
            )
        return run, dispatched

    async def test_a_failure_edge_fires_when_the_source_fails(
        self, db_session, graph_executor, quiet_manager
    ):
        run, dispatched = await self._complete_a(db_session, graph_executor, False)
        assert "recover" in dispatched

    async def test_a_success_edge_does_not_fire_when_the_source_fails(
        self, db_session, graph_executor, quiet_manager
    ):
        run, dispatched = await self._complete_a(db_session, graph_executor, False)
        assert "ok" not in dispatched
        assert sorted(dispatched) == ["cleanup", "recover"]

    async def test_a_success_edge_fires_when_the_source_passes(
        self, db_session, graph_executor, quiet_manager
    ):
        run, dispatched = await self._complete_a(db_session, graph_executor, True)
        assert "ok" in dispatched

    async def test_a_failure_edge_does_not_fire_when_the_source_passes(
        self, db_session, graph_executor, quiet_manager
    ):
        run, dispatched = await self._complete_a(db_session, graph_executor, True)
        assert "recover" not in dispatched
        assert sorted(dispatched) == ["cleanup", "ok"]

    async def test_an_always_edge_fires_on_both_outcomes(
        self, db_session, graph_executor, quiet_manager
    ):
        _, on_pass = await self._complete_a(db_session, graph_executor, True)
        _, on_fail = await self._complete_a(db_session, graph_executor, False)
        assert "cleanup" in on_pass
        assert "cleanup" in on_fail

    async def test_a_graph_steps_index_is_its_position_in_the_steps_dict(
        self, db_session, graph_executor, quiet_manager
    ):
        """Nothing asserted this, and the websocket frame, the state machine
        and the execution key `{run}:{index}:{step_run}` all depend on it."""
        g = _graph(
            [_node("first"), _node("second"), _node("third")],
            [
                _graph_edge("e1", "first", "second"),
                _graph_edge("e2", "second", "third"),
            ],
            ["first"],
        )
        repo, pipeline, run = await _graph_rows(db_session, g)
        captured = {}

        async def fake_dispatch(db, pipeline_run, **kwargs):
            captured.update(kwargs)
            return (
                StepRun(
                    id=str(uuid4()),
                    pipeline_run_id=pipeline_run.id,
                    step_index=kwargs["step_index"],
                    step_id=kwargs["step_id"],
                    step_name=kwargs["step_name"],
                    status=RunStatus.RUNNING.value,
                ),
                ExecutorMode.LOCAL,
                None,
            )

        with patch.object(graph_executor, "_dispatch_step_run", new=fake_dispatch):
            await graph_executor._execute_step(
                db_session, run, pipeline, repo, g, "third"
            )

        assert captured["step_id"] == "third"
        assert captured["step_index"] == 2


# =============================================================================
# THE RUN VERDICT - what v1's `stop` / `next` words used to decide
#
# On the array path the WORD decided the run: `stop` completed it with the
# step's own verdict and `next` moved on. Three classes of false green came
# out of that, and the third is a behaviour CHANGE this phase ships rather
# than a bug it fixes (plan 1.8):
#
#   `on_failure: next` completed the run PASSED even though a step FAILED.
#
# On a graph, continuation is an EDGE and the verdict is
# `_verify_graph_coverage` + `_check_all_steps_passed`. A run containing a
# failed step ends FAILED, whatever the edges did afterwards. These are the
# converted `TestPipelineExecutorActionHandlers` stop/next claims, driven
# against the real executor and real rows instead of against a mock's
# `assert_called_once_with`.
# =============================================================================


class TestTheRunVerdictIsTheStepRuns:
    pytestmark = pytest.mark.asyncio

    SOLO = _graph([_node("only")], [], ["only"])

    #: a -FAILURE-> b. The graph spelling of v1's `on_failure: "next"`: the
    #: run carries on past a failed step, deliberately, to a cleanup or a
    #: reporting step. v1 then reported the whole run PASSED.
    CONTINUE_PAST_FAILURE = _graph(
        [_node("a"), _node("b")],
        [_graph_edge("e", "a", "b", "failure")],
        ["a"],
    )

    async def test_a_terminal_node_that_passes_ends_the_run_passed(
        self, db_session, graph_executor, quiet_manager
    ):
        """v1: `on_success: "stop"` with a passing step. The graph spelling
        is "no outgoing edge", and the verdict is the StepRun's."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _finished_step_run(
            db_session, run, self.SOLO, "only", passed=True
        )

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, self.SOLO, "only", True, None,
            step_run=step_run,
        )

        assert run.status == RunStatus.PASSED.value
        assert run.completed_at is not None

    async def test_a_terminal_node_that_fails_ends_the_run_failed(
        self, db_session, graph_executor, quiet_manager
    ):
        """v1: `on_failure: "stop"`."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _finished_step_run(
            db_session, run, self.SOLO, "only", passed=False
        )

        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, self.SOLO, "only", False, None,
            step_run=step_run,
        )

        assert run.status == RunStatus.FAILED.value
        assert run.completed_at is not None

    async def test_a_failure_edge_that_continues_still_ends_the_run_failed(
        self, db_session, graph_executor, quiet_manager
    ):
        """THE BEHAVIOUR CHANGE, PINNED (plan 1.8, risk 9).

        `TestLocalFailurePaths::test_linear_on_failure_next_continues_past_
        failed_local_step` pinned the v1 answer and said so in a comment:
        "Behavior-compat with main's legacy linear semantics... (graph
        pipelines DO check all steps)". There was no graph equivalent
        anywhere - this is it, and it deliberately records the OPPOSITE
        verdict, because a run containing a failed step reporting PASSED is
        the false green this whole phase exists to remove.

        Note what does NOT change: the continuation still happens. `b` runs.
        Only the verdict differs.
        """
        g = self.CONTINUE_PAST_FAILURE
        repo, pipeline, run = await _graph_rows(db_session, g)
        a_run = await _finished_step_run(db_session, run, g, "a", passed=False)

        dispatched = []
        with patch.object(
            graph_executor, "_execute_step", new=_recording_dispatch(dispatched)
        ):
            await graph_executor._handle_step_complete(
                db_session, run, pipeline, repo, g, "a", False, None,
                step_run=a_run,
            )

        assert dispatched == ["b"], "the failure edge must still carry on"
        assert run.status == RunStatus.RUNNING.value, (
            "the run must not be stamped terminal while `b` is in flight"
        )

        b_run = await _finished_step_run(db_session, run, g, "b", passed=True)
        await graph_executor._handle_step_complete(
            db_session, run, pipeline, repo, g, "b", True, None,
            step_run=b_run,
        )

        assert run.status == RunStatus.FAILED.value, (
            "a run containing a FAILED step ends FAILED even when every edge "
            "it took succeeded - v1 reported PASSED here"
        )


# =============================================================================
# THE FORK THAT GETS MISSED - `_on_step_complete_locked`
#
# The JOB-callback path is reached from `job_callback`, never from
# `_run_executor_step`, so it did not receive the local path's two-way flag:
# it recomputed `graph and step_run.step_id` itself and kept its own `else:`
# into the v1 action handler. That `else:` was the LAST caller of the entire
# v1 family and had no test pointing at it - deleting the array by following
# the flag alone would have left it alive, dispatching an action vocabulary
# nothing else could reach.
#
# P5 turns it into a REFUSAL. These tests are what stops it quietly becoming a
# fallback again: a callback that cannot be continued must FAIL the run, not
# leave it RUNNING with nobody left to advance it.
# =============================================================================


def _completed_job(**overrides):
    """The shape `on_step_complete` reads off a Job (it is passed one, never
    queried for one)."""
    job = SimpleNamespace(
        id=str(uuid4()),
        status="completed",
        tests_run=0,
        tests_passed=0,
        logs="all good\n",
        error=None,
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


async def _running_step_run(db, run, *, step_id, step_index=0, step_name="step"):
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=step_index,
        step_id=step_id,
        step_name=step_name,
        status=RunStatus.RUNNING.value,
        executor=ExecutorMode.LOCAL.value,
        started_at=datetime.utcnow(),
    )
    db.add(step_run)
    await db.commit()
    await db.refresh(step_run)
    return step_run


class TestTheJobCallbackHasNoArrayFallback:
    pytestmark = pytest.mark.asyncio

    SOLO = _graph([_node("only")], [], ["only"])

    async def test_a_callback_for_a_pipeline_with_no_graph_fails_the_run(
        self, db_session, graph_executor, quiet_manager
    ):
        repo = Repo(
            id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True
        )
        db_session.add(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="graphless",
            steps=json.dumps([{"name": "Step 1", "type": "script"}]),
            steps_graph=None,
        )
        db_session.add(pipeline)
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            steps_total=1,
            completed_step_ids=json.dumps([]),
            active_step_ids=json.dumps([]),
        )
        db_session.add(run)
        await db_session.commit()
        step_run = await _running_step_run(db_session, run, step_id=None)

        await graph_executor.on_step_complete(
            db_session, step_run.id, _completed_job()
        )

        await db_session.refresh(run)
        await db_session.refresh(step_run)
        assert run.status == RunStatus.FAILED.value, (
            "the array fallback used to read `pipeline.steps` here and "
            "dispatch v1's `on_success` action"
        )
        assert step_run.status == RunStatus.FAILED.value
        assert "no graph definition" in (step_run.error or "")

    async def test_a_callback_for_a_step_run_with_no_step_id_fails_the_run(
        self, db_session, graph_executor, quiet_manager
    ):
        """The second half of the old condition. A graph is present, so a
        fallback here would look even more reasonable - and would be
        continuing an array the pipeline does not have."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _running_step_run(db_session, run, step_id=None)

        await graph_executor.on_step_complete(
            db_session, step_run.id, _completed_job()
        )

        await db_session.refresh(run)
        await db_session.refresh(step_run)
        assert run.status == RunStatus.FAILED.value
        assert "no graph definition" in (step_run.error or "")

    async def test_the_refusal_does_not_swallow_the_ordinary_callback(
        self, db_session, graph_executor, quiet_manager
    ):
        """The guard must reject only what it genuinely cannot continue: a
        real graph step still completes and still reaches the verdict."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _running_step_run(db_session, run, step_id="only")

        await graph_executor.on_step_complete(
            db_session, step_run.id, _completed_job()
        )

        await db_session.refresh(run)
        await db_session.refresh(step_run)
        assert step_run.status == RunStatus.PASSED.value
        assert run.status == RunStatus.PASSED.value
        assert json.loads(run.completed_step_ids) == ["only"]

    async def test_an_existing_step_error_is_kept_when_the_callback_refuses(
        self, db_session, graph_executor, quiet_manager
    ):
        """The job's own error is the real cause; the refusal is APPENDED to
        it, never substituted for it."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _running_step_run(db_session, run, step_id=None)

        await graph_executor.on_step_complete(
            db_session,
            step_run.id,
            _completed_job(status="failed", error="container exited 137"),
        )

        await db_session.refresh(step_run)
        assert "container exited 137" in step_run.error
        assert "no graph definition" in step_run.error


# =============================================================================
# TWO CLAIMS THE DELETED v1 CLASSES OWNED, ON THEIR SURVIVING SURFACE
#
# `TestParseSteps` tested `parse_steps`, deleted with the array. Its twin on
# the graph column, `parse_steps_graph`, had NO unit coverage of its own -
# and it now feeds `start_pipeline`'s refusal, so exactly what it swallows and
# what it returns decides whether a definition-less run is loud or green.
#
# `test_the_whole_vocabulary_still_dispatches` tested that the v1 gate
# rejected only what `_handle_action` genuinely could not run. The node-action
# dispatcher has the same two-sided contract and the same drift hazard: a
# vocabulary member with no handler reaches `_run_terminal_action`'s
# `raise ValueError(... have drifted)`, which is marked `pragma: no cover`
# precisely because nothing is supposed to reach it.
# =============================================================================


class TestParseStepsGraph:
    def test_a_graph_json_string_parses_to_the_dict(self):
        raw = json.dumps(
            {"steps": {"a": {"id": "a"}}, "edges": [], "entry_points": ["a"]}
        )
        assert parse_steps_graph(raw)["entry_points"] == ["a"]

    @pytest.mark.parametrize("raw", [None, "", "   ", "not valid json", "{"])
    def test_absent_or_unparseable_yields_none_not_an_empty_definition(self, raw):
        """None, never `{}`. The difference is the whole refusal: `{}` is
        falsy too, but a caller that treated it as "a graph with no steps"
        would take the EMPTY-GRAPH branch and pass vacuously. `None` has no
        such reading."""
        assert parse_steps_graph(raw) is None

    def test_the_swallow_is_a_known_dark_channel_with_an_owner(self):
        """`parse_steps_graph` cannot tell "no definition" from "a definition
        that will not parse", exactly as `parse_steps` could not. That is
        pinned rather than fixed because the looking belongs upstream:
        migration 0014 refuses to convert a row whose `steps` will not parse
        and names the pipeline id, and `PipelineRead.parse_steps_graph` pins
        the same swallow on the wire side (plan 4.7).

        What P5 adds is that the swallow can no longer end in a PASS - see
        `TestPipelineExecutorStartPipeline`.
        """
        assert parse_steps_graph("{not json") is parse_steps_graph(None)


class TestEveryNodeActionReachesAHandler:
    """The converted `test_the_whole_vocabulary_still_dispatches`.

    Driven off `TERMINAL_ACTION_PREFIXES` rather than a literal list, so a
    prefix added to the vocabulary without a handler fails HERE instead of
    reaching the drift `ValueError` at run time.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize("prefix", list(TERMINAL_ACTION_PREFIXES))
    async def test_every_accepted_prefix_dispatches(
        self, db_session, graph_executor, quiet_manager, prefix
    ):
        action = f"{prefix}target"
        assert describe_terminal_action(action) is None, (
            "the fixture must be a form the vocabulary accepts"
        )

        g = _graph([_node("a")], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        with patch.object(
            graph_executor, "_merge_step_branch", new=AsyncMock(return_value=None)
        ), patch.object(
            graph_executor, "_spawn_fix_card", new=AsyncMock(return_value=None)
        ), patch.object(
            graph_executor,
            "_fail_run_on_terminal_action",
            new=AsyncMock(),
        ) as refused:
            performed = await graph_executor._run_terminal_action(
                db_session, run, repo, "a", step_run, action
            )

        assert performed is True
        refused.assert_not_called()

    async def test_an_action_outside_the_vocabulary_never_reaches_a_handler(
        self, db_session, graph_executor, quiet_manager
    ):
        """The other side of the same gate: the dispatcher must refuse, not
        fall through to a handler that would half-perform something."""
        g = _graph([_node("a")], [], ["a"])
        repo, pipeline, run = await _graph_rows(db_session, g)
        step_run = await _finished_step_run(db_session, run, g, "a", passed=True)

        with patch.object(
            graph_executor, "_merge_step_branch", new=AsyncMock(return_value=None)
        ) as merged, patch.object(
            graph_executor, "_spawn_fix_card", new=AsyncMock(return_value=None)
        ) as spawned:
            performed = await graph_executor._run_terminal_action(
                db_session, run, repo, "a", step_run, "deploy"
            )

        assert performed is False
        merged.assert_not_called()
        spawned.assert_not_called()
        assert run.status == RunStatus.FAILED.value


# =============================================================================
# THE LOCAL STEP TASK'S OWN CONTEXT LOAD
#
# `_load_local_step_context` runs in the per-step asyncio task, on its own
# session, and it used to end in the array: `is_graph = bool(graph and
# step_run.step_id)`, and anything falsy fell through to
# `steps[step_run.step_index]`. With the array gone that fall-through is a
# missing graph, and a missing graph has to RAISE - the class it raises exists
# precisely because "no early return may leave a RUNNING StepRun with no
# owner", and an unguarded `graph.get(...)` on None is an AttributeError that
# escapes the task and does exactly that.
# =============================================================================


class TestALocalStepWithNoGraphIsNotStranded:
    pytestmark = pytest.mark.asyncio

    SOLO = _graph([_node("only")], [], ["only"])

    async def _graphless_rows(self, db):
        repo = Repo(
            id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True
        )
        db.add(repo)
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="graphless",
            steps=json.dumps([{"name": "Step 1", "type": "script"}]),
            steps_graph=None,
        )
        db.add(pipeline)
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            steps_total=1,
            completed_step_ids=json.dumps([]),
            active_step_ids=json.dumps([]),
        )
        db.add(run)
        await db.commit()
        step_run = await _running_step_run(db, run, step_id="only")
        return repo, pipeline, run, step_run

    async def test_a_missing_graph_raises_rather_than_reading_an_array(
        self, db_session, graph_executor
    ):
        _, pipeline, run, step_run = await self._graphless_rows(db_session)

        with pytest.raises(LocalStepContextError) as caught:
            await graph_executor._load_local_step_context(
                db_session, run.id, step_run.id
            )

        assert "no steps_graph" in str(caught.value)
        assert caught.value.can_continue is False, (
            "there is no graph to fan out over, so the only honest "
            "continuation is failing the run"
        )
        assert caught.value.step_run is step_run, (
            "the error must carry the row, or the handler cannot drive it "
            "out of RUNNING"
        )

    async def test_the_wedged_step_and_its_run_both_reach_terminal(
        self, db_session, graph_executor, quiet_manager
    ):
        """The half that matters operationally: a RUNNING StepRun nobody owns
        is invisible until someone wonders why a run never finished."""
        _, pipeline, run, step_run = await self._graphless_rows(db_session)

        try:
            await graph_executor._load_local_step_context(
                db_session, run.id, step_run.id
            )
        except LocalStepContextError as err:
            await graph_executor._fail_wedged_local_step(db_session, run.id, err)
        else:
            raise AssertionError("the context load must have refused")

        await db_session.refresh(run)
        await db_session.refresh(step_run)
        assert step_run.status == RunStatus.FAILED.value
        assert "local step context error" in (step_run.error or "")
        assert run.status == RunStatus.FAILED.value

    async def test_a_step_id_the_graph_does_not_define_still_continues(
        self, db_session, graph_executor, quiet_manager
    ):
        """The neighbouring branch, which must NOT be swept up: the graph is
        intact and only this node is missing, so the run keeps its normal
        continuation (`can_continue=True`) instead of being failed outright.
        Without this the refusal above would be indistinguishable from a
        blanket "any load problem kills the run"."""
        repo, pipeline, run = await _graph_rows(db_session, self.SOLO)
        step_run = await _running_step_run(db_session, run, step_id="ghost")

        with pytest.raises(LocalStepContextError) as caught:
            await graph_executor._load_local_step_context(
                db_session, run.id, step_run.id
            )

        assert caught.value.can_continue is True
        assert caught.value.graph is not None
