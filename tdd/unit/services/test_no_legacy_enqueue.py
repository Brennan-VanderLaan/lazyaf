"""
Phase 12.5: nothing enqueues to the legacy runner queue any more.

After 12.5 the polling runners keep their compose services and their replica
counts - setting them to 0 would be deletion-by-config and would make 12.6's
acceptance untestable - but no DEFAULT path feeds them. Card start, card
retry, the playground and agent pipeline steps all run on the control layer
as local, control-mode containers.

Idleness is ASSERTED here rather than assumed, because a silent fallback to
the legacy queue is indistinguishable from success everywhere else (R1): the
job is enqueued, a runner picks it up, the work happens, the card goes green
- and the phase quietly did not land. The spy wraps the REAL
``job_queue.enqueue`` on the REAL singleton (R6), so a caller that reaches
the queue through any import path is caught.

The one deliberately-kept exception is the ``executor: legacy`` escape hatch
on an agent step (R2, deleted in 12.6). It is exercised at the bottom of this
module: the escape hatch must keep working, and it must be the ONLY thing
that still enqueues.
"""
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineRun, StepRun
from app.services import agent_run

pytestmark = pytest.mark.asyncio


@pytest.fixture
def enqueue_spy(monkeypatch):
    """Record every job that reaches the real queue, then enqueue it.

    Wrapping rather than replacing keeps the legacy escape-hatch test honest:
    the job really does land in the real queue, so that test asserts the path
    still WORKS instead of only that it was called.
    """
    from app.services.job_queue import job_queue

    calls: list = []
    original = job_queue.enqueue

    async def spy(job):
        calls.append(job)
        return await original(job)

    monkeypatch.setattr(job_queue, "enqueue", spy)
    return calls


@pytest.fixture(autouse=True)
async def _drain_queue():
    """The queue is a process-wide singleton; leave it as we found it."""
    yield
    from app.services.job_queue import job_queue

    await job_queue.clear()


async def _settle():
    """Let dispatched step tasks run far enough to enqueue, if they would."""
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


class TestCardsDoNotEnqueue:
    async def test_card_start_enqueues_nothing(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        card = await create_agent_card(client, ingested_repo["id"])

        response = await client.post(f"/api/cards/{card['id']}/start")
        assert response.status_code == 200, response.text
        await _settle()

        assert enqueue_spy == [], (
            "starting a card enqueued a legacy job - card work runs as an "
            "ad-hoc agent run on the control layer since 12.5"
        )

    async def test_card_start_creates_an_adhoc_run(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        card = await create_agent_card(client, ingested_repo["id"])
        await client.post(f"/api/cards/{card['id']}/start")
        await _settle()

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert len(runs) == 1, "card start must produce exactly one ad-hoc run"
        assert runs[0].trigger_ref == card["id"]

        # ... backed by a hidden ephemeral pipeline with ONE agent step.
        pipeline = await db_session.get(Pipeline, runs[0].pipeline_id)
        assert agent_run.is_adhoc_pipeline_name(pipeline.name)
        steps = json.loads(pipeline.steps)
        assert [s["type"] for s in steps] == ["agent"]
        assert steps[0]["config"]["agent"] == "mock"

    async def test_card_retry_enqueues_nothing(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        card = await create_agent_card(client, ingested_repo["id"])
        await client.post(f"/api/cards/{card['id']}/start")
        await _settle()
        enqueue_spy.clear()

        # Retry is only legal from failed/in_review.
        await client.patch(f"/api/cards/{card['id']}", json={"status": "failed"})

        response = await client.post(f"/api/cards/{card['id']}/retry")
        assert response.status_code == 200, response.text
        await _settle()

        assert enqueue_spy == [], "retrying a card enqueued a legacy job"


class TestPlaygroundDoesNotEnqueue:
    async def test_playground_start_enqueues_nothing(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={"runner_type": "mock", "branch": ingested_repo["default_branch"]},
        )
        assert response.status_code == 200, response.text
        await _settle()

        assert enqueue_spy == [], (
            "the playground enqueued a legacy job - it runs as an ad-hoc "
            "agent run on the control layer since 12.5"
        )

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_PLAYGROUND)
        assert len(runs) == 1
        assert runs[0].trigger_ref == response.json()["session_id"]


class TestAgentPipelineStepDoesNotEnqueue:
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

    async def test_agent_step_enqueues_nothing(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        pipeline = await self._agent_pipeline(client, ingested_repo["id"])

        response = await client.post(f"/api/pipelines/{pipeline['id']}/run", json={})
        assert response.status_code in (200, 201), response.text
        await _settle()

        assert enqueue_spy == [], (
            "an agent pipeline step enqueued a legacy job - agent steps route "
            "local since 12.5 (ExecutionRouter reason 'agent-default-local')"
        )

    async def test_agent_step_records_executor_local(
        self, client, ingested_repo, db_session, enqueue_spy
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

    async def test_explicit_legacy_override_still_enqueues(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        """R2: the ONE remaining escape hatch must not rot.

        `executor: legacy` on an agent step is the last caller of the polling
        queue, kept callable until the 12.6 deletion commit. If this ever
        stops enqueueing, 12.6's "delete the legacy stack" commit loses the
        thing it is supposed to be deleting.
        """
        pipeline = await self._agent_pipeline(
            client, ingested_repo["id"], config_extra={"executor": "legacy"}
        )

        await client.post(f"/api/pipelines/{pipeline['id']}/run", json={})
        await _settle()

        assert len(enqueue_spy) == 1, (
            "executor: legacy on an agent step must still reach the runner "
            "queue - it is the R2 escape hatch"
        )
        assert enqueue_spy[0].step_type == "agent"


class TestFixCardActionDoesNotEnqueue:
    """`trigger:{card_id}` - the pipeline action that clones a card to fix a
    failed step - was the last live caller of job_queue.enqueue on the card
    path.

    Card start and card retry moved to the ad-hoc agent run in 12.5; this one
    did not, so "nothing enqueues any more" was true of the paths people look
    at and false of this one. A queue with a single live caller is a queue
    nobody notices has stopped being polled - exactly the silent-fallback
    failure mode R1 exists to catch - so the claim is asserted here rather
    than assumed.
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

    async def _run_row(self, db_session, pipeline_id):
        result = await db_session.execute(
            select(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id)
        )
        return result.scalars().first()

    async def test_fix_card_action_enqueues_nothing(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        from app.services.pipeline_executor import pipeline_executor

        template, repo, pipeline = await self._fixture_rows(
            client, db_session, ingested_repo["id"]
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running",
            trigger_type="manual",
            steps_total=2,
        )
        db_session.add(run)
        await db_session.commit()
        enqueue_spy.clear()

        steps = json.loads(pipeline.steps)
        await pipeline_executor._trigger_card(
            db_session, run, repo, steps, 0, template["id"]
        )
        await _settle()

        assert enqueue_spy == [], (
            "the trigger:{card_id} fix action enqueued a legacy job - it "
            "takes the ad-hoc agent run path like card start since 12.5"
        )

    async def test_fix_card_action_starts_an_adhoc_card_work_run(
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        from app.models import Card
        from app.services.pipeline_executor import pipeline_executor

        template, repo, pipeline = await self._fixture_rows(
            client, db_session, ingested_repo["id"]
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running",
            trigger_type="manual",
            steps_total=2,
        )
        db_session.add(run)
        await db_session.commit()

        steps = json.loads(pipeline.steps)
        await pipeline_executor._trigger_card(
            db_session, run, repo, steps, 0, template["id"]
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
        self, client, ingested_repo, db_session, enqueue_spy
    ):
        """The legacy shape blocked the parent on a runner callback that no
        longer comes. The action now continues the parent immediately, like
        its `trigger:pipeline:` sibling - and the marker StepRun it leaves
        behind is terminal, never a RUNNING row with no owner."""
        from app.services.pipeline_executor import pipeline_executor

        template, repo, pipeline = await self._fixture_rows(
            client, db_session, ingested_repo["id"]
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running",
            trigger_type="manual",
            steps_total=2,
        )
        db_session.add(run)
        await db_session.commit()

        steps = json.loads(pipeline.steps)
        await pipeline_executor._trigger_card(
            db_session, run, repo, steps, 0, template["id"]
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
        assert markers[0].job_id is None, (
            "a second row at this index claiming the step's job would poison "
            "merge-source branch resolution"
        )
        # ... and step 1 was dispatched.
        assert any(sr.step_index == 1 for sr in step_runs), (
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

    async def test_preflight_failure_enqueues_nothing_and_fails_the_card(
        self, client, ingested_repo, db_session, enqueue_spy, missing_image_executor
    ):
        card = await create_agent_card(client, ingested_repo["id"])

        response = await client.post(f"/api/cards/{card['id']}/start")
        assert response.status_code == 200, response.text
        await _settle()

        assert enqueue_spy == [], (
            "a card whose ad-hoc run failed preflight fell back to the legacy "
            "runner queue"
        )

        runs = await adhoc_runs(db_session, agent_run.TRIGGER_CARD_WORK)
        assert [r.status for r in runs] == ["failed"]

        current = await client.get(f"/api/cards/{card['id']}")
        assert current.json()["status"] == "failed", (
            "the card was left in in_progress with no run behind it - the "
            "'running' write after start_pipeline clobbered a terminal status"
        )
