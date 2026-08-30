"""
Unit tests for the completion invariant: "no more steps I can reach" is NOT
success (QA finding T4, a BLOCKER).

Three shapes used to finish GREEN having run a fraction of the pipeline:

    cycle       a->b->c->b, entry [a]      -> passed  1/3
    unreachable {a, orphan}, no edges      -> passed  1/2
    typo        on_success: "nextt"        -> passed  1/3   (legacy path)

For a CI product a false green is the worst defect class there is: the run
finishes FAST and GREEN, nothing on screen suggests anything is wrong, and
every downstream gate - merge-on-pass, card completion, the ratchet itself -
trusts it. This project spends a great deal of effort making its own tests
incapable of lying; a runner that reports PASSED for work it did not do is the
same lie one layer down.

Three seams, three altitudes:

- `graph_definition_errors` / `unreached_graph_steps` / `describe_step_action`
  are pure functions over plain dicts - no db, no container, no run.
- `_verify_graph_coverage` is exercised against a REAL session with REAL rows
  (R6): it writes StepRuns and drives `_complete_pipeline`, and a mock would
  prove nothing about either.
- The legacy `on_success` typo is covered next door in
  `test_pipeline_executor.py::test_an_undispatchable_action_fails_the_run`.

The live-stack reproductions are `tdd/qa/test_graph_execution_qa4.py` (QA
sandbox, :8790). These are the fast tier's version of the same assertions.
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.models import Pipeline, PipelineRun, Repo, RunStatus, StepRun  # noqa: E402
from app.services.pipeline_executor import (  # noqa: E402
    STEP_ACTIONS,
    PipelineExecutor,
    describe_step_action,
    graph_definition_errors,
    unreached_graph_steps,
)


# ---------------------------------------------------------------------------
# graph builders (the same shapes qa4_support.py posts over HTTP)
# ---------------------------------------------------------------------------

def step(step_id, **overrides):
    node = {
        "id": step_id,
        "name": step_id,
        "type": "script",
        "config": {"command": "echo x"},
        "timeout": 300,
    }
    node.update(overrides)
    return node


def edge(edge_id, from_step, to_step, condition="success"):
    return {
        "id": edge_id,
        "from_step": from_step,
        "to_step": to_step,
        "condition": condition,
    }


def graph(steps, edges, entry_points, version=2):
    return {
        "steps": {s["id"]: s for s in steps},
        "edges": edges,
        "entry_points": entry_points,
        "version": version,
    }


CYCLE = graph(
    [step("a"), step("b"), step("c")],
    [edge("e1", "a", "b"), edge("e2", "b", "c"), edge("e3", "c", "b")],
    ["a"],
)
ORPHAN = graph([step("a"), step("orphan")], [], ["a"])
DIAMOND = graph(
    [step("a"), step("b"), step("c"), step("d")],
    [
        edge("e1", "a", "b", "always"),
        edge("e2", "a", "c", "always"),
        edge("e3", "b", "d", "always"),
        edge("e4", "c", "d", "always"),
    ],
    ["a"],
)
BRANCHING = graph(
    [step("a"), step("ok"), step("recover")],
    [
        edge("e1", "a", "ok", "success"),
        edge("e2", "a", "recover", "failure"),
    ],
    ["a"],
)


# ---------------------------------------------------------------------------
# graph_definition_errors - the DEFINITION-time check
# ---------------------------------------------------------------------------

class TestGraphDefinitionErrors:
    def test_a_correct_graph_has_no_defects(self):
        assert graph_definition_errors(DIAMOND) == []
        assert graph_definition_errors(BRANCHING) == []

    def test_a_cycle_is_reported_as_the_path_that_closes_it(self):
        defects = graph_definition_errors(CYCLE)
        assert len(defects) == 1
        assert "cycle" in defects[0]
        assert "b -> c -> b" in defects[0]

    def test_a_long_cycle_is_found(self):
        long_cycle = graph(
            [step(f"s{i}") for i in range(6)],
            [edge(f"e{i}", f"s{i}", f"s{i + 1}") for i in range(5)]
            + [edge("back", "s5", "s2")],
            ["s0"],
        )
        (defect,) = graph_definition_errors(long_cycle)
        assert "s2 -> s3 -> s4 -> s5 -> s2" in defect

    def test_a_self_edge_names_the_edge_and_the_step(self):
        (defect,) = graph_definition_errors(
            graph([step("a")], [edge("e1", "a", "a")], ["a"])
        )
        assert "'e1'" in defect and "'a'" in defect and "self-edge" in defect

    def test_a_self_edge_does_not_count_as_reaching_its_own_step(self):
        """Otherwise `{a, b}` with only `b -> b` would look reachable."""
        defects = graph_definition_errors(
            graph([step("a"), step("b")], [edge("e1", "b", "b")], ["a"])
        )
        assert any("self-edge" in d for d in defects)
        assert any("'b' is unreachable" in d for d in defects)

    def test_an_edge_to_a_step_that_does_not_exist_names_it(self):
        (defect,) = graph_definition_errors(
            graph([step("a")], [edge("e1", "a", "ghost")], ["a"])
        )
        assert "'e1'" in defect and "ghost" in defect

    def test_an_edge_from_a_step_that_does_not_exist_names_it(self):
        defects = graph_definition_errors(
            graph([step("a")], [edge("e1", "ghost", "a")], ["a"])
        )
        assert any("'e1'" in d and "ghost" in d for d in defects)

    def test_an_entry_point_that_does_not_exist_names_it(self):
        defects = graph_definition_errors(graph([step("a")], [], ["ghost"]))
        assert any("entry point 'ghost'" in d for d in defects)

    def test_steps_with_no_entry_point_at_all(self):
        defects = graph_definition_errors(graph([step("a")], [], []))
        assert any("no entry" in d for d in defects)
        # ...and 'a' is then unreachable too, which is the same fact said the
        # other way round. Both are reported; neither is guessed at.
        assert any("'a' is unreachable" in d for d in defects)

    def test_an_unreachable_step_names_it(self):
        (defect,) = graph_definition_errors(ORPHAN)
        assert "'orphan' is unreachable" in defect

    def test_an_empty_graph_is_not_a_defect(self):
        assert graph_definition_errors(None) == []
        assert graph_definition_errors({}) == []
        assert (
            graph_definition_errors(
                {"steps": {}, "edges": [], "entry_points": [], "version": 2}
            )
            == []
        )

    def test_a_five_hundred_step_chain_does_not_blow_the_stack(self):
        """A pipeline graph is user input. Trading a false-green bug for a
        RecursionError inside a request handler is not a fix, so the cycle
        walk is iterative."""
        chain = graph(
            [step(f"s{i}") for i in range(500)],
            [edge(f"e{i}", f"s{i}", f"s{i + 1}", "always") for i in range(499)],
            ["s0"],
        )
        assert graph_definition_errors(chain) == []

    def test_defects_are_reported_together_not_one_at_a_time(self):
        """An operator should not have to fix-and-resubmit five times."""
        defects = graph_definition_errors(
            graph(
                [step("a"), step("b"), step("orphan")],
                [edge("e1", "a", "a"), edge("e2", "a", "ghost")],
                ["a", "nobody"],
            )
        )
        assert len(defects) >= 4


# ---------------------------------------------------------------------------
# unreached_graph_steps - the COMPLETION invariant
# ---------------------------------------------------------------------------

class TestUnreachedGraphSteps:
    def test_a_fully_covered_run_has_nothing_unreached(self):
        assert (
            unreached_graph_steps(
                DIAMOND,
                completed_ids={"a", "b", "c", "d"},
                active_ids=set(),
                outcomes={"a": True, "b": True, "c": True, "d": True},
            )
            == {}
        )

    def test_the_cycle_names_the_step_that_was_selected_and_never_ran(self):
        """QA4-03. `a` passes, edge `e1` selects `b`, and `b`'s fan-in waits
        for `c` - which can only run after `b`. One third of the pipeline ran
        and the run used to be stamped PASSED."""
        verdicts = unreached_graph_steps(
            CYCLE,
            completed_ids={"a"},
            active_ids=set(),
            outcomes={"a": True},
        )
        assert set(verdicts) == {"b"}
        assert "'e1'" in verdicts["b"]
        assert "'a'" in verdicts["b"]
        assert "'c'" in verdicts["b"]

    def test_the_orphan_is_named_as_never_runnable(self):
        """QA4-04."""
        verdicts = unreached_graph_steps(
            ORPHAN,
            completed_ids={"a"},
            active_ids=set(),
            outcomes={"a": True},
        )
        assert set(verdicts) == {"orphan"}
        assert "no entry point names it" in verdicts["orphan"]

    def test_a_conditional_branch_that_did_not_fire_is_not_a_defect(self):
        """THE false-red guard, and the reason this is not just
        `completed == total`. `a` passes, so the `failure` edge to `recover`
        was never taken. That is what a conditional edge IS."""
        assert (
            unreached_graph_steps(
                BRANCHING,
                completed_ids={"a", "ok"},
                active_ids=set(),
                outcomes={"a": True, "ok": True},
            )
            == {}
        )

    def test_the_other_side_of_the_branch_is_not_a_defect_either(self):
        assert (
            unreached_graph_steps(
                BRANCHING,
                completed_ids={"a", "recover"},
                active_ids=set(),
                outcomes={"a": False, "recover": True},
            )
            == {}
        )

    def test_a_step_downstream_of_a_failure_is_not_a_defect(self):
        """`a` failed and only a `success` edge leaves it. `b` and `d` not
        running is the correct behaviour, not a coverage hole."""
        assert (
            unreached_graph_steps(
                graph(
                    [step("a"), step("b")],
                    [edge("e1", "a", "b", "success")],
                    ["a"],
                ),
                completed_ids={"a"},
                active_ids=set(),
                outcomes={"a": False},
            )
            == {}
        )

    def test_an_always_edge_demands_its_target_whatever_happened(self):
        verdicts = unreached_graph_steps(
            graph(
                [step("a"), step("b")],
                [edge("e1", "a", "b", "always")],
                ["a"],
            ),
            completed_ids={"a"},
            active_ids=set(),
            outcomes={"a": False},
        )
        assert set(verdicts) == {"b"}

    def test_still_active_steps_are_not_unreached(self):
        assert (
            unreached_graph_steps(
                DIAMOND,
                completed_ids={"a"},
                active_ids={"b", "c"},
                outcomes={"a": True},
            )
            == {}
        )

    def test_an_entry_point_that_never_dispatched_is_a_defect(self):
        verdicts = unreached_graph_steps(
            graph([step("a"), step("b")], [], ["a", "b"]),
            completed_ids={"a"},
            active_ids=set(),
            outcomes={"a": True},
        )
        assert "entry point" in verdicts["b"]


# ---------------------------------------------------------------------------
# describe_step_action - the CLOSED legacy vocabulary
# ---------------------------------------------------------------------------

class TestStepActionVocabulary:
    @pytest.mark.parametrize(
        "action",
        ["next", "stop", "trigger:card-1", "trigger:pipeline:p-1", "merge:main"],
    )
    def test_the_vocabulary_is_accepted(self, action):
        assert describe_step_action(action) is None

    @pytest.mark.parametrize(
        "action", ["nextt", "Next", "next ", "b", "[b, c]", "", "merge", None, 7]
    )
    def test_everything_else_is_refused(self, action):
        problem = describe_step_action(action)
        assert problem is not None
        assert "'next', 'stop'" in problem

    @pytest.mark.parametrize("action", ["trigger:", "merge:", "trigger:pipeline:"])
    def test_a_prefix_with_no_target_is_refused(self, action):
        problem = describe_step_action(action)
        assert problem is not None
        assert "empty target" in problem

    def test_the_message_names_the_offender(self):
        assert "'nextt'" in describe_step_action("nextt")

    def test_bare_actions_are_exactly_next_and_stop(self):
        assert STEP_ACTIONS == ("next", "stop")


# ---------------------------------------------------------------------------
# _verify_graph_coverage - against a REAL session and REAL rows (R6)
# ---------------------------------------------------------------------------

async def _make_run(db, graph_dict, *, completed=(), active=(), failed=()):
    """A repo + graph pipeline + a run that has finished `completed`."""
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
        steps_completed=len(completed),
        steps_total=len(graph_dict.get("steps", {})),
        completed_step_ids=json.dumps(list(completed)),
        active_step_ids=json.dumps(list(active)),
    )
    db.add(run)
    for index, step_id in enumerate(completed):
        db.add(
            StepRun(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                step_index=index,
                step_id=step_id,
                step_name=step_id,
                status=(
                    RunStatus.FAILED.value
                    if step_id in failed
                    else RunStatus.PASSED.value
                ),
            )
        )
    await db.commit()
    return run


@pytest.fixture
def executor():
    return PipelineExecutor()


@pytest.fixture
def quiet_manager():
    """The websocket manager is not the subject here, but the broadcasts are
    real calls and must not be swallowed by a MagicMock returning a coroutine
    nobody awaits."""
    with patch(
        "app.services.pipeline_executor.manager", new_callable=MagicMock
    ) as mock:
        mock.send_pipeline_run_status = AsyncMock()
        mock.send_step_run_status = AsyncMock()
        mock.publish_step_update = AsyncMock()
        yield mock


class TestVerifyGraphCoverage:
    async def test_a_covered_run_is_left_alone(
        self, db_session, executor, quiet_manager
    ):
        run = await _make_run(
            db_session, DIAMOND, completed=["a", "b", "c", "d"]
        )
        assert (
            await executor._verify_graph_coverage(db_session, run, DIAMOND)
            is False
        )
        assert run.status == RunStatus.RUNNING.value

    async def test_an_untaken_conditional_branch_is_left_alone(
        self, db_session, executor, quiet_manager
    ):
        run = await _make_run(db_session, BRANCHING, completed=["a", "ok"])
        assert (
            await executor._verify_graph_coverage(db_session, run, BRANCHING)
            is False
        )
        assert run.status == RunStatus.RUNNING.value

    async def test_the_cycle_run_is_failed_not_passed(
        self, db_session, executor, quiet_manager
    ):
        run = await _make_run(db_session, CYCLE, completed=["a"])

        assert (
            await executor._verify_graph_coverage(db_session, run, CYCLE)
            is True
        )
        assert run.status == RunStatus.FAILED.value
        assert run.completed_at is not None

    async def test_the_cycle_run_says_which_step_never_ran_and_why(
        self, db_session, executor, quiet_manager
    ):
        """R1: the explanation goes where a user looks, not only into a log.

        `PipelineRun` has no error column, so the unreached steps become FAILED
        StepRuns - the row the graph view marks red, the run detail lists and
        the websocket streams."""
        run = await _make_run(db_session, CYCLE, completed=["a"])
        await executor._verify_graph_coverage(db_session, run, CYCLE)

        rows = {
            row.step_id: row
            for row in (
                await db_session.execute(
                    StepRun.__table__.select().where(
                        StepRun.__table__.c.pipeline_run_id == run.id
                    )
                )
            ).fetchall()
        }
        assert rows["b"].status == RunStatus.FAILED.value
        assert "never ran" in rows["b"].error
        assert "'c'" in rows["b"].error

    async def test_the_orphan_run_is_failed_and_names_the_orphan(
        self, db_session, executor, quiet_manager
    ):
        run = await _make_run(db_session, ORPHAN, completed=["a"])

        assert (
            await executor._verify_graph_coverage(db_session, run, ORPHAN)
            is True
        )
        assert run.status == RunStatus.FAILED.value

        rows = (
            await db_session.execute(
                StepRun.__table__.select().where(
                    StepRun.__table__.c.pipeline_run_id == run.id
                )
            )
        ).fetchall()
        orphan = [row for row in rows if row.step_id == "orphan"]
        assert len(orphan) == 1
        assert orphan[0].status == RunStatus.FAILED.value
        assert "no entry point names it" in orphan[0].error

    async def test_a_structural_defect_with_nothing_unrun_still_fails(
        self, db_session, executor, quiet_manager
    ):
        """A self-edge is silently discarded by the traversal, so every step
        runs and the old code reported PASSED for a graph the engine did not
        honour. There is no step to blame, so the run gets one row that says
        what is wrong with the graph."""
        self_edged = graph([step("a")], [edge("e1", "a", "a")], ["a"])
        run = await _make_run(db_session, self_edged, completed=["a"])

        assert (
            await executor._verify_graph_coverage(db_session, run, self_edged)
            is True
        )
        assert run.status == RunStatus.FAILED.value

        rows = (
            await db_session.execute(
                StepRun.__table__.select().where(
                    StepRun.__table__.c.pipeline_run_id == run.id
                )
            )
        ).fetchall()
        synthetic = [row for row in rows if row.step_name == "pipeline graph"]
        assert len(synthetic) == 1
        assert "self-edge" in synthetic[0].error

    async def test_an_empty_graph_is_still_a_vacuous_pass(
        self, db_session, executor, quiet_manager
    ):
        empty = {"steps": {}, "edges": [], "entry_points": [], "version": 2}
        run = await _make_run(db_session, empty)
        assert (
            await executor._verify_graph_coverage(db_session, run, empty)
            is False
        )


# ---------------------------------------------------------------------------
# Re-entrancy: a step that fails to ROUTE completes inside the caller's stack
#
# This is the shape that made the completion invariant dangerous to add. A
# routing failure (the realistic `executor: legacy` stale-config mistake)
# finishes a step SYNCHRONOUSLY inside `_execute_graph_step`, which re-enters
# `_handle_graph_step_complete` in the caller's own frame - so a fan-out
# sibling that has not been dispatched yet looks, from the inner frame, exactly
# like a step that will never run.
#
# It was already a bug before the invariant existed: the inner frame saw an
# empty active set and stamped the whole run terminal. Claiming the batch
# before dispatching any of it is what makes "nothing is active" mean it.
# ---------------------------------------------------------------------------

async def _make_graph_run(db, graph_dict):
    """(repo, pipeline, run) for a graph that has not started yet."""
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
        steps_completed=0,
        steps_total=len(graph_dict.get("steps", {})),
        completed_step_ids=json.dumps([]),
        active_step_ids=json.dumps([]),
    )
    db.add(run)
    await db.commit()
    return repo, pipeline, run


def _synchronously_failing_dispatch(db, executor, run, pipeline, repo, graph_dict, ran):
    """A stand-in for `_execute_graph_step` on the routing-failure path.

    Writes the FAILED StepRun the real dispatch writes, then re-enters
    `_handle_graph_step_complete` in the caller's stack - which is precisely
    what `_dispatch_step_run` -> `_fail_step_run` -> the `route_error` branch
    does today.
    """

    async def dispatch(_db, _run, _pipeline, _repo, _graph, step_id, *args, **kwargs):
        ran.append(step_id)
        db.add(
            StepRun(
                id=str(uuid4()),
                pipeline_run_id=run.id,
                step_index=list(graph_dict["steps"]).index(step_id),
                step_id=step_id,
                step_name=step_id,
                status=RunStatus.FAILED.value,
                error="execution routing failed",
            )
        )
        await db.commit()
        await executor._handle_graph_step_complete(
            db, run, pipeline, repo, graph_dict, step_id, False, None
        )

    return dispatch


class TestSynchronousDispatchDoesNotStrandSiblings:
    async def test_a_fan_out_sibling_is_never_reported_as_unreached(
        self, db_session, executor, quiet_manager
    ):
        """a -> {b, c} -> d, every step failing to route.

        Dispatching `b` completes it inside this frame, before `c` has been
        dispatched at all. `c` must not be mistaken for a step that never ran.
        """
        repo, pipeline, run = await _make_graph_run(db_session, DIAMOND)
        ran = []
        with patch.object(
            executor,
            "_execute_graph_step",
            new=_synchronously_failing_dispatch(
                db_session, executor, run, pipeline, repo, DIAMOND, ran
            ),
        ):
            executor._reserve_active_steps(run, ["a"])
            await db_session.commit()
            await executor._execute_graph_step(
                db_session, run, pipeline, repo, DIAMOND, "a"
            )

        assert ran == ["a", "b", "c", "d"], ran
        assert sorted(json.loads(run.completed_step_ids)) == ["a", "b", "c", "d"]
        assert run.status == RunStatus.FAILED.value  # every step failed to route

        rows = (
            await db_session.execute(
                StepRun.__table__.select().where(
                    StepRun.__table__.c.pipeline_run_id == run.id
                )
            )
        ).fetchall()
        assert sorted(row.step_id for row in rows) == ["a", "b", "c", "d"], (
            "the coverage check invented StepRuns for steps that were about "
            "to be dispatched"
        )

    async def test_a_second_entry_point_is_never_reported_as_unreached(
        self, db_session, executor, quiet_manager
    ):
        """Two entry points converging on one step, all failing to route.

        `a` finishing before `b` is dispatched used to stamp the whole run
        terminal; it must not now fail it for `b` "never running" either.
        """
        two_entries = graph(
            [step("a"), step("b"), step("z")],
            [edge("e1", "a", "z", "always"), edge("e2", "b", "z", "always")],
            ["a", "b"],
        )
        repo, pipeline, run = await _make_graph_run(db_session, two_entries)
        ran = []
        with patch.object(
            executor,
            "_execute_graph_step",
            new=_synchronously_failing_dispatch(
                db_session, executor, run, pipeline, repo, two_entries, ran
            ),
        ):
            executor._reserve_active_steps(run, ["a", "b"])
            await db_session.commit()
            for entry in ("a", "b"):
                await executor._execute_graph_step(
                    db_session, run, pipeline, repo, two_entries, entry
                )

        assert ran == ["a", "b", "z"], ran
        assert run.status == RunStatus.FAILED.value
        assert sorted(json.loads(run.completed_step_ids)) == ["a", "b", "z"]

    async def test_the_run_is_only_completed_once(
        self, db_session, executor, quiet_manager
    ):
        """Each unwinding frame re-checks completion. Without the terminal
        guard that is one `_complete_pipeline` per frame - double workspace
        cleanup, double trigger action, double card notification."""
        repo, pipeline, run = await _make_graph_run(db_session, DIAMOND)
        ran = []
        completions = []
        real_complete = executor._complete_pipeline

        async def counting_complete(db, pipeline_run, success):
            # Wraps the REAL method rather than replacing it: the terminal
            # guard reads `pipeline_run.status`, so a stub that never stamps
            # the run would disarm the very thing under test.
            completions.append(success)
            return await real_complete(db, pipeline_run, success=success)

        with patch.object(
            executor,
            "_execute_graph_step",
            new=_synchronously_failing_dispatch(
                db_session, executor, run, pipeline, repo, DIAMOND, ran
            ),
        ), patch.object(executor, "_complete_pipeline", new=counting_complete):
            executor._reserve_active_steps(run, ["a"])
            await db_session.commit()
            await executor._execute_graph_step(
                db_session, run, pipeline, repo, DIAMOND, "a"
            )

        assert len(completions) == 1, (
            f"_complete_pipeline was called {len(completions)} times"
        )


class TestReserveActiveSteps:
    def test_it_is_idempotent_and_order_preserving(self):
        run = PipelineRun(
            id="r", pipeline_id="p", active_step_ids=json.dumps(["a"])
        )
        PipelineExecutor._reserve_active_steps(run, ["b", "a", "c"])
        assert json.loads(run.active_step_ids) == ["a", "b", "c"]

    def test_it_copes_with_a_null_column(self):
        run = PipelineRun(id="r", pipeline_id="p", active_step_ids=None)
        PipelineExecutor._reserve_active_steps(run, ["a"])
        assert json.loads(run.active_step_ids) == ["a"]
