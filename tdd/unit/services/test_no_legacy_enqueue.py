"""
Card start, card retry, the playground and agent pipeline steps all run as
AD-HOC CONTROL-LAYER RUNS (Phase 12.5, kept honest through 12.6).

12.5 moved every one of those paths off the polling runner queue and this
module asserted the move by spying on the real ``job_queue.enqueue``: a
silent fallback to the queue is indistinguishable from success everywhere
else (R1) - the job is enqueued, a runner picks it up, the work happens, the
card goes green, and the phase quietly did not land.

12.6 DELETED that queue, so there is nothing left to spy on and "nothing
enqueues" became structurally true. `tdd/unit/services/test_no_legacy_code.py`
asserts it once, unconditionally, at the module level where it now belongs.

What this module keeps is the half a deletion cannot prove: that each of
those paths produces a REAL AD-HOC RUN on the control layer - one
PipelineRun with the right trigger, backed by a hidden single-step agent
pipeline, recording ``executor='local'`` on its StepRun. "Nothing went to the
old place" and "the work went to the right new place" are different claims,
and only the second one survives its subject being deleted.
"""
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineRun, StepRun
from app.services import agent_run

pytestmark = pytest.mark.asyncio


async def _settle():
    """Let dispatched step tasks run far enough to reach a dispatch decision."""
    for _ in range(20):
        await asyncio.sleep(0.01)


async def create_agent_card(client, repo_id, *, runner_type="mock", title="Add a thing"):
    response = await client.post(
        f"/api/repos/{repo_id}/cards",
        json={
            "title": title,
            "description": "Do the thing described in the title.",
            "runner_type": runner_type,
            "step_type": "agent",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def adhoc_runs(db_session, trigger_type: str) -> list[PipelineRun]:
    result = await db_session.execute(
        select(PipelineRun).where(PipelineRun.trigger_type == trigger_type)
    )
    return list(result.scalars().all())


class TestCardsRunOnTheControlLayer:
    async def test_card_start_creates_an_adhoc_run(
        self, client, ingested_repo, db_session
    ):
        card = await create_agent_card(client, ingested_repo["id"])
        await client.post(f"/api/cards/{card['id']}/start")
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert len(runs) == 1, "card start must produce exactly one ad-hoc run"
        assert runs[0].trigger_ref == card["id"]

        # ... backed by a hidden ephemeral pipeline with ONE agent step.
        # Read off `steps_graph`: 12.8 P3 moved every ad-hoc writer onto the
        # graph (`agent_run.adhoc_steps_graph`) and P5 deleted the array the
        # executor could have run instead, so the array is no longer where
        # this claim can be checked - `pipeline.steps` is the column default
        # here and asserting against it would pass on an empty list.
        pipeline = await db_session.get(Pipeline, runs[0].pipeline_id)
        assert agent_run.is_adhoc_pipeline_name(pipeline.name)
        graph = json.loads(pipeline.steps_graph)
        nodes = list(graph["steps"].values())
        assert [n["type"] for n in nodes] == ["agent"]
        assert nodes[0]["config"]["agent"] == "mock"
        assert graph["entry_points"] == [nodes[0]["id"]]

    async def test_card_retry_creates_a_second_adhoc_run(self, client, ingested_repo, db_session):
        card = await create_agent_card(client, ingested_repo["id"])
        await client.post(f"/api/cards/{card['id']}/start")
        await _settle()

        # Retry is only legal from failed/in_review. `in_progress -> failed`
        # is the RUN's outcome to write, not a field update - PATCH refuses it
        # since 12.7 (MANUAL_STATUSES in app/routers/cards.py, QA finding T2),
        # so the precondition is staged through the ORM rather than through
        # the guard. What this test asserts - that retry takes the same ad-hoc
        # control-layer path start does - is unchanged.
        from tdd.integration.api.test_cards_api import stage_card

        await stage_card(db_session, card["id"], status="failed")

        response = await client.post(f"/api/cards/{card['id']}/retry")
        assert response.status_code == 200, response.text
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert len(runs) == 2, (
            "retry must take the same ad-hoc control-layer path start does, "
            f"producing a second run; saw {len(runs)}"
        )
        assert {r.trigger_ref for r in runs} == {card["id"]}


class TestPlaygroundRunsOnTheControlLayer:
    async def test_playground_start_creates_an_adhoc_run(
        self, client, ingested_repo, db_session
    ):
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={"runner_type": "mock", "branch": ingested_repo["default_branch"]},
        )
        assert response.status_code == 200, response.text
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_PLAYGROUND)
        assert len(runs) == 1
        assert runs[0].trigger_ref == response.json()["session_id"]


class TestAgentPipelineStepRunsLocal:
    async def _agent_pipeline(self, client, repo_id, *, config_extra=None):
        config = {"agent": "mock", "task": "do the thing"}
        config.update(config_extra or {})
        response = await client.post(
            f"/api/repos/{repo_id}/pipelines",
            json={
                "name": "agent-pipeline",
                "steps": [
                    {
                        "id": "work",
                        "name": "Agent work",
                        "type": "agent",
                        "config": config,
                        "on_success": "next",
                        "on_failure": "stop",
                    }
                ],
                "triggers": [],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_agent_step_records_executor_local(
        self, client, ingested_repo, db_session
    ):
        """R1: the routing decision is observable on the StepRun row."""
        pipeline = await self._agent_pipeline(client, ingested_repo["id"])
        response = await client.post(f"/api/pipelines/{pipeline['id']}/run", json={})
        run_id = response.json()["id"]
        await _settle()

        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run_id)
        )
        step_runs = list(result.scalars().all())
        assert step_runs, "the agent step never produced a StepRun"
        assert [sr.executor for sr in step_runs] == ["local"]

    async def test_the_legacy_escape_hatch_fails_the_step_loudly(
        self, client, ingested_repo, db_session
    ):
        """R2's close-out.

        Until 12.6, `executor: legacy` on an agent step was the ONE remaining
        caller of the polling queue and was kept callable on purpose - a phase
        that moves work off a path must leave the old path usable until the
        path itself is deleted. The deletion commit removed the path, so the
        override now FAILS THE STEP with a message naming what happened,
        rather than being silently downgraded to a local run: a user who asked
        for a specific executor and got a different one has been lied to.
        """
        pipeline = await self._agent_pipeline(
            client, ingested_repo["id"], config_extra={"executor": "legacy"}
        )

        response = await client.post(f"/api/pipelines/{pipeline['id']}/run", json={})
        run_id = response.json()["id"]
        await _settle()

        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run_id)
        )
        step_runs = list(result.scalars().all())
        assert step_runs, "the step never produced a StepRun"
        assert step_runs[0].status == "failed"
        assert "legacy" in (step_runs[0].error or "")


class TestFixCardActionRunsOnTheControlLayer:
    """`trigger:{card_id}` - the pipeline action that clones a card to fix a
    failed step - was the last live caller of the polling queue on the card
    path.

    Card start and card retry moved to the ad-hoc agent run in 12.5; this one
    did not, so "nothing enqueues any more" was true of the paths people look
    at and false of this one. A queue with a single live caller is a queue
    nobody notices has stopped being polled - exactly the silent-fallback
    failure mode R1 exists to catch - so the claim is asserted here rather
    than assumed.

    12.8 P5 CONVERTED these two off `_trigger_card`. The action is now a
    NODE action fired by `_handle_step_complete` -> `_run_terminal_action` ->
    `_spawn_fix_card`, and `_trigger_card` (the v1 flow wrapper that spawned
    the card and then ran `current_step + 1`) is deleted. The FIXTURE is
    unchanged on purpose: the array is still the authoring dialect at the API
    boundary, so the same POST body still produces this pipeline - it just
    arrives as `actions.failure = ["trigger:{id}"]` plus a FAILURE edge,
    which is what `array_to_graph` makes of a non-final effect. Both claims
    below survive the move verbatim; only the driver changed.
    """

    async def _fixture_rows(self, client, db_session, repo_id):
        """A template card + a two-step pipeline with a started run."""
        template = await create_agent_card(
            client, repo_id, title="Fix the build"
        )
        response = await client.post(
            f"/api/repos/{repo_id}/pipelines",
            json={
                "name": "fix-action-pipeline",
                "steps": [
                    {
                        "id": "a",
                        "name": "A",
                        "type": "script",
                        "config": {"command": "false"},
                        "on_success": "next",
                        "on_failure": f"trigger:{template['id']}",
                    },
                    {
                        "id": "b",
                        "name": "B",
                        "type": "script",
                        "config": {"command": "true"},
                        "on_success": "stop",
                        "on_failure": "stop",
                    },
                ],
                "triggers": [],
            },
        )
        assert response.status_code == 201, response.text
        pipeline_id = response.json()["id"]

        from app.models import Pipeline, Repo

        repo = await db_session.get(Repo, repo_id)
        pipeline = await db_session.get(Pipeline, pipeline_id)
        return template, repo, pipeline

    async def _failed_run_at_step_a(self, db_session, pipeline):
        """A run of that pipeline whose step `a` has just FAILED.

        Returns (run, step_run). The row is real and terminal, because
        `_run_terminal_action` attributes the action to it and
        `_fail_run_on_terminal_action` writes the reason onto it.
        """
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running",
            trigger_type="manual",
            steps_total=2,
            active_step_ids=json.dumps(["a"]),
            completed_step_ids=json.dumps([]),
        )
        db_session.add(run)
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=0,
            step_id="a",
            step_name="A",
            status="failed",
            executor="local",
        )
        db_session.add(step_run)
        await db_session.commit()
        return run, step_run

    async def _complete_step_a(
        self, db_session, run, pipeline, repo, step_run, template_id
    ):
        from app.services.pipeline_executor import (
            parse_steps_graph,
            pipeline_executor,
        )

        graph = parse_steps_graph(pipeline.steps_graph)
        assert graph is not None, "the API boundary must have written a graph"
        assert graph["steps"]["a"]["actions"]["failure"] == [
            f"trigger:{template_id}"
        ], (
            "the fix action must survive conversion as a NODE action - "
            "dropping it silently is the defect 12.8 P2 closed"
        )
        assert any(
            e["from_step"] == "a"
            and e["to_step"] == "b"
            and e["condition"] == "failure"
            for e in graph["edges"]
        ), "a non-final effect converts to the action AND the edge v1 took"

        await pipeline_executor._handle_step_complete(
            db_session, run, pipeline, repo, graph, "a", False, None,
            step_run=step_run,
        )

    async def test_fix_card_action_starts_an_adhoc_card_work_run(
        self, client, ingested_repo, db_session
    ):
        from app.models import Card

        template, repo, pipeline = await self._fixture_rows(
            client, db_session, ingested_repo["id"]
        )
        run, step_run = await self._failed_run_at_step_a(db_session, pipeline)
        await self._complete_step_a(
            db_session, run, pipeline, repo, step_run, template["id"]
        )
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert len(runs) == 1, "the fix card must produce one ad-hoc run"

        result = await db_session.execute(
            select(Card).where(Card.title.like("[Pipeline Fix]%"))
        )
        cloned = result.scalars().one()
        assert runs[0].trigger_ref == cloned.id
        assert cloned.branch_name and cloned.branch_name.startswith("lazyaf/")

    async def test_the_parent_run_is_not_left_waiting(
        self, client, ingested_repo, db_session
    ):
        """The legacy shape blocked the parent on a runner callback that no
        longer comes. The action fires as an EFFECT and the parent then
        continues down its own FAILURE edge - and the marker StepRun it
        leaves behind is terminal, never a RUNNING row with no owner."""
        template, repo, pipeline = await self._fixture_rows(
            client, db_session, ingested_repo["id"]
        )
        run, step_run = await self._failed_run_at_step_a(db_session, pipeline)
        await self._complete_step_a(
            db_session, run, pipeline, repo, step_run, template["id"]
        )
        await _settle()

        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run.id)
        )
        step_runs = list(result.scalars().all())
        markers = [sr for sr in step_runs if sr.step_name.startswith("[Fix]")]
        assert len(markers) == 1
        assert markers[0].status == "passed", (
            "the marker StepRun must be terminal - nothing will ever "
            f"complete it (error: {markers[0].error})"
        )
        assert markers[0].error is None
        assert markers[0].step_id is None, (
            "the marker must not claim the step's graph id: "
            "`_latest_step_run_for` selects by step_id, and a marker "
            "carrying one becomes a second candidate row for the step that "
            "spawned it"
        )
        assert markers[0].job_id is None, (
            "a second row at this index claiming the step's job would poison "
            "merge-source branch resolution"
        )
        # ... and step `b` was dispatched down the failure edge.
        assert any(sr.step_id == "b" for sr in step_runs), (
            "the parent run must continue past the fix action"
        )


class TestSynchronousStartFailureDoesNotFallBack:
    """The FAILURE path must not reach the queue either.

    ``start_pipeline`` completes a run synchronously when image preflight
    fails - the one 12.5 code path where the ad-hoc run never dispatches a
    container at all. That is exactly the shape a "well, fall back to a
    runner then" rescue would be added to, and a rescue there is
    indistinguishable from success (R1): the card would go green off the
    legacy stack while the phase quietly did not land.
    """

    class _MissingImageExecutor:
        async def image_supports_control_layer(self, image):
            return False

        async def find_missing_images(self, images):
            return sorted(images)

        async def execute_step(self, step_config, execution_context):
            raise AssertionError("preflight failed - nothing should dispatch")
            yield  # pragma: no cover - keeps this an async generator

        async def cancel_step(self, execution_key):
            return False

        async def cancel_all(self):
            return None

        def reset(self):
            return None

    @pytest.fixture
    def missing_image_executor(self):
        from app.services.pipeline_executor import pipeline_executor

        previous = pipeline_executor._local_executor
        pipeline_executor._local_executor = self._MissingImageExecutor()
        try:
            yield
        finally:
            pipeline_executor._local_executor = previous

    async def test_a_preflight_failure_fails_the_card_instead_of_falling_back(
        self, client, ingested_repo, db_session, missing_image_executor
    ):
        card = await create_agent_card(client, ingested_repo["id"])

        response = await client.post(f"/api/cards/{card['id']}/start")
        assert response.status_code == 200, response.text
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert [r.status for r in runs] == ["failed"]

        current = await client.get(f"/api/cards/{card['id']}")
        assert current.json()["status"] == "failed", (
            "the card was left in in_progress with no run behind it - the "
            "'running' write after start_pipeline clobbered a terminal status"
        )
