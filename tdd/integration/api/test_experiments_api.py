"""
Integration tests for the experiment REST surface (Phase 12.6.5).

Covers the whole surface including the 409/422 refusal paths, and drives the
REAL dispatch path (`pipeline_executor.start_pipeline` via the ad-hoc run
builders) rather than a stub — the T1 conftest makes the local executor
Docker-free, so the routing decision under test is the production one.

The WS manager is the REAL one (R6). Frames are captured by attaching a fake
connection to the real `ConnectionManager`, not by replacing it with an
AsyncMock, so a broadcast that stops being emitted is a failing assertion
rather than an unnoticed call count.

ONE THING IS NOT WIRED HERE, AND IT IS DELIBERATE (see the report):
`agent_run.on_run_complete` needs the four-line dispatch registration
(design section 12.4) before a finished cell run lands its cell
automatically. `agent_run.py` is not this lane's file, so the tests below
call `experiment_service.on_cell_complete` at exactly the point that
registration would — the same function, the same arguments, from the same
persisted `trigger_type`/`trigger_ref` the hook routes on. What is asserted
either way is that those two columns carry the durable link.
"""
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (
    AcceptanceCriterion,
    Card,
    Feature,
    Pipeline,
    PipelineRun,
    Repo,
    RunStatus,
    UserStory,
)
from app.models.experiment import Experiment, ExperimentRun, ExperimentRunStatus
from app.models.spec import PromptTemplate
from app.services import experiment_service as svc
from app.services.websocket import manager


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _register_router():
    """Include the experiments router on the app under test.

    Idempotent, and exactly the line the integrator adds to main.py
    (`app.include_router(experiments.router)`), so once that registration
    lands this fixture becomes a no-op and the tests keep exercising the real
    application object rather than a bespoke one.
    """
    from app.main import app
    from app.routers import experiments

    if not any(
        getattr(route, "path", "") == "/api/experiments" for route in app.routes
    ):
        app.include_router(experiments.router)
    yield


@pytest.fixture(autouse=True)
def _clean_pump_state():
    svc._pump_locks.clear()
    svc._repump.clear()
    yield
    svc._pump_locks.clear()
    svc._repump.clear()


class _CapturingSocket:
    """A real ConnectionManager connection that records the frames it gets."""

    def __init__(self):
        self.frames: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.frames.append(json.loads(message))

    def of_type(self, message_type: str) -> list[dict]:
        return [f["payload"] for f in self.frames if f["type"] == message_type]


@pytest.fixture
def ws_frames():
    socket = _CapturingSocket()
    manager.active_connections.append(socket)
    yield socket
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)


@pytest.fixture
async def repo(db_session) -> Repo:
    row = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True,
               default_branch="main")
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.fixture
async def card(db_session, repo) -> Card:
    row = Card(id=str(uuid4()), repo_id=repo.id, title="Add the widget",
               description="It should widget, twice.")
    db_session.add(row)
    await db_session.commit()
    return row


def payload(card, **overrides):
    body = {
        "name": "opus vs haiku",
        "target_type": "card",
        "target_id": card.id,
        "matrix": {
            "models": [
                {"agent": "mock", "model": "mock-a", "label": "a"},
                {"agent": "mock", "model": "mock-b", "label": "b"},
            ],
            "prompts": [{"prompt_template_id": None, "label": "default"}],
            "repeat": 1,
        },
        "budget_usd": "5.00",
        "max_concurrency": 2,
    }
    body.update(overrides)
    return body


async def create(client, card, **overrides):
    response = await client.post("/api/experiments", json=payload(card, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


async def settle(db_session, run_id: str, timeout: float = 8.0) -> PipelineRun:
    """Wait for a real pipeline run to reach a terminal status."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = (
            await db_session.execute(
                select(PipelineRun.status).where(PipelineRun.id == run_id)
            )
        ).scalar_one_or_none()
        if status in (RunStatus.PASSED.value, RunStatus.FAILED.value,
                      RunStatus.CANCELLED.value):
            return await db_session.get(PipelineRun, run_id)
        await asyncio.sleep(0.05)
    raise AssertionError(f"pipeline run {run_id} never reached a terminal status")


# -----------------------------------------------------------------------------
# Create / dry run
# -----------------------------------------------------------------------------

class TestCreate:
    async def test_create_returns_201_and_a_draft(self, client, card):
        body = await create(client, card)
        assert body["status"] == "draft"
        assert body["cells_total"] == 0, "cells are created at LAUNCH, not create"
        assert body["budget_usd"] == "5.000000"
        assert body["push_branches"] is False

    async def test_dry_run_returns_200_and_creates_nothing(
        self, client, card, db_session
    ):
        response = await client.post(
            "/api/experiments", json=payload(card, dry_run=True)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["cells"] == 2
        assert body["estimate_basis"] == "no-history"
        assert body["budget_enforced_at_dispatch"] is True

        rows = list((await db_session.execute(select(Experiment))).scalars())
        assert rows == []

    async def test_dry_run_estimate_never_renders_a_bare_zero(self, client, card):
        response = await client.post(
            "/api/experiments", json=payload(card, dry_run=True)
        )
        body = response.json()
        assert body["estimated_cost_usd"] == "0.000000"
        # ...but it is accompanied by the basis and by a warning saying the
        # number is a LOWER BOUND, which is what stops it reading as "free".
        assert body["estimate_basis"] == "no-history"
        assert any("LOWER BOUND" in w for w in body["warnings"])

    async def test_unknown_card_is_422_naming_the_id(self, client, card):
        response = await client.post(
            "/api/experiments", json=payload(card, target_id="ghost-42")
        )
        assert response.status_code == 422
        assert "ghost-42" in response.text

    async def test_feature_target_is_refused_and_names_the_phase(self, client, card):
        response = await client.post(
            "/api/experiments",
            json=payload(card, target_type="feature", target_id="f1"),
        )
        assert response.status_code == 422
        assert "13.2" in response.text or "feature" in response.text

    async def test_user_story_without_repo_id_is_refused(self, client, db_session):
        feature = Feature(id=str(uuid4()), title="F", repo_ids="[]")
        db_session.add(feature)
        await db_session.commit()
        story = UserStory(id=str(uuid4()), feature_id=feature.id, title="S")
        db_session.add(story)
        await db_session.commit()

        response = await client.post(
            "/api/experiments",
            json=payload(story, target_type="user_story", target_id=story.id),
        )
        assert response.status_code == 422
        assert "repo_id" in response.text

    async def test_user_story_repo_must_belong_to_the_feature(
        self, client, db_session, repo
    ):
        other = Repo(id=str(uuid4()), name="other", is_ingested=True)
        db_session.add(other)
        await db_session.commit()
        feature = Feature(id=str(uuid4()), title="F", repo_ids=json.dumps([other.id]))
        db_session.add(feature)
        await db_session.commit()
        story = UserStory(id=str(uuid4()), feature_id=feature.id, title="S")
        db_session.add(story)
        await db_session.commit()

        response = await client.post(
            "/api/experiments",
            json=payload(
                story, target_type="user_story", target_id=story.id, repo_id=repo.id
            ),
        )
        assert response.status_code == 422
        assert "repo_ids" in response.text

    async def test_user_story_target_folds_criteria_into_the_task(
        self, client, db_session, repo
    ):
        feature = Feature(id=str(uuid4()), title="F", repo_ids=json.dumps([repo.id]))
        db_session.add(feature)
        await db_session.commit()
        story = UserStory(id=str(uuid4()), feature_id=feature.id, title="S",
                          narrative="As a user I want widgets")
        db_session.add(story)
        await db_session.commit()
        db_session.add(
            AcceptanceCriterion(
                id=str(uuid4()), user_story_id=story.id, text="widgets are blue"
            )
        )
        await db_session.commit()

        target = await svc.resolve_target(db_session, "user_story", story.id, repo.id)
        assert "widgets are blue" in target.description
        assert target.repo_id == repo.id

    async def test_card_repo_mismatch_is_refused_not_silently_honoured(
        self, client, card, db_session
    ):
        """Cloning a different repo for a card's task would run the agent
        against code the card does not describe."""
        other = Repo(id=str(uuid4()), name="elsewhere", is_ingested=True)
        db_session.add(other)
        await db_session.commit()

        response = await client.post(
            "/api/experiments", json=payload(card, repo_id=other.id)
        )
        assert response.status_code == 422
        assert "does not match card" in response.text

    async def test_unknown_prompt_template_is_refused_by_id(self, client, card):
        body = payload(card)
        body["matrix"]["prompts"] = [{"prompt_template_id": "tpl-ghost"}]
        response = await client.post("/api/experiments", json=body)
        assert response.status_code == 422
        assert "tpl-ghost" in response.text

    async def test_matrix_validation_is_a_422(self, client, card):
        body = payload(card)
        body["matrix"]["models"] = []
        response = await client.post("/api/experiments", json=body)
        assert response.status_code == 422

    async def test_estimate_over_budget_is_refused_when_history_is_real(
        self, client, card, db_session, repo
    ):
        from tdd.unit.services.experiment_rows import add_usage, make_run

        run = await make_run(db_session, repo)
        await add_usage(db_session, run.id, cost="4.00", model="mock-a")
        await add_usage(db_session, run.id, cost="4.00", model="mock-b")

        response = await client.post(
            "/api/experiments", json=payload(card, budget_usd="1.00")
        )
        assert response.status_code == 422
        assert "exceeds" in response.text
        assert "8.000000" in response.text

    async def test_estimate_over_budget_is_allowed_when_history_is_absent(
        self, client, card
    ):
        """We cannot prove it, so we do not pretend to. Enforcement still runs
        off observed spend at dispatch."""
        response = await client.post(
            "/api/experiments", json=payload(card, budget_usd="0.01")
        )
        assert response.status_code == 201


class TestListAndRead:
    async def test_list_is_newest_first(self, client, card):
        first = await create(client, card, name="first")
        second = await create(client, card, name="second")
        body = (await client.get("/api/experiments")).json()
        assert [e["id"] for e in body][:2] == [second["id"], first["id"]]

    async def test_filters(self, client, card, repo):
        await create(client, card)
        assert len((await client.get("/api/experiments?status=draft")).json()) == 1
        assert len((await client.get("/api/experiments?status=complete")).json()) == 0
        assert len(
            (await client.get(f"/api/experiments?repo_id={repo.id}")).json()
        ) == 1
        assert len(
            (await client.get(f"/api/experiments?target_id={card.id}")).json()
        ) == 1

    async def test_unknown_status_filter_is_a_400_naming_the_vocabulary(self, client):
        response = await client.get("/api/experiments?status=banana")
        assert response.status_code == 400
        assert "budget_exhausted" in response.text

    async def test_detail_404(self, client):
        assert (await client.get("/api/experiments/nope")).status_code == 404

    async def test_detail_carries_cells_and_progress(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        detail = (await client.get(f"/api/experiments/{body['id']}")).json()
        assert detail["cells_total"] == 2
        assert len(detail["cells"]) == 2
        assert sum(detail["by_status"].values()) == 2
        assert detail["spend_usd"] == "0.000000"


class TestUpdateAndDelete:
    async def test_patch_edits_a_draft_matrix(self, client, card):
        body = await create(client, card)
        new_matrix = payload(card)["matrix"]
        new_matrix["repeat"] = 2
        response = await client.patch(
            f"/api/experiments/{body['id']}", json={"matrix": new_matrix}
        )
        assert response.status_code == 200
        assert response.json()["matrix"]["repeat"] == 2

    async def test_patch_refuses_a_launched_matrix(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        response = await client.patch(
            f"/api/experiments/{body['id']}", json={"matrix": payload(card)["matrix"]}
        )
        assert response.status_code == 422
        assert "frozen once launched" in response.text

    async def test_patch_allows_budget_while_running(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        response = await client.patch(
            f"/api/experiments/{body['id']}", json={"budget_usd": "9.00"}
        )
        assert response.status_code == 200
        assert response.json()["budget_usd"] == "9.000000"

    async def test_delete_a_draft(self, client, card):
        body = await create(client, card)
        assert (
            await client.delete(f"/api/experiments/{body['id']}")
        ).status_code == 204
        assert (await client.get(f"/api/experiments/{body['id']}")).status_code == 404

    async def test_delete_refuses_a_running_experiment(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        response = await client.delete(f"/api/experiments/{body['id']}")
        assert response.status_code == 422
        assert "abort it first" in response.text


class TestEstimateEndpoint:
    async def test_estimate_for_a_saved_draft(self, client, card):
        body = await create(client, card)
        estimate = (
            await client.get(f"/api/experiments/{body['id']}/estimate")
        ).json()
        assert estimate["cells"] == 2
        assert estimate["budget_usd"] == "5.000000"
        assert len(estimate["per_variant"]) == 2

    async def test_estimate_404(self, client):
        assert (await client.get("/api/experiments/nope/estimate")).status_code == 404


# -----------------------------------------------------------------------------
# Launch — the real dispatch path
# -----------------------------------------------------------------------------

class TestLaunch:
    async def test_launch_creates_a_pipeline_run_per_cell(
        self, client, card, db_session
    ):
        body = await create(client, card)
        response = await client.post(f"/api/experiments/{body['id']}/launch")
        assert response.status_code == 202
        assert response.json()["cells_created"] == 2
        assert response.json()["dispatched"] == 2

        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == body["id"]
                    )
                )
            ).scalars()
        )
        assert len(cells) == 2
        assert all(c.pipeline_run_id for c in cells)

    async def test_cell_run_carries_trigger_ref(self, client, card, db_session):
        """The DURABLE cell -> run link, replacing PLAN's
        pipeline_runs.experiment_id: trigger_type/trigger_ref are written at
        run CREATION, so they are already true when start_pipeline completes a
        run synchronously."""
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")

        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == body["id"]
                    )
                )
            ).scalars()
        )
        for cell in cells:
            run = await db_session.get(PipelineRun, cell.pipeline_run_id)
            assert run.trigger_type == "experiment"
            assert run.trigger_ref == cell.id
            context = json.loads(run.trigger_context)
            assert context["experiment_id"] == body["id"]
            assert context["experiment_run_id"] == cell.id
            assert context["cell_index"] == cell.cell_index

    async def test_persisted_step_config_carries_the_cells_model(
        self, client, card, db_session
    ):
        """Contract #5 (R3): experiment_runs.model IS the model in the
        persisted step config of that cell's ephemeral pipeline. One
        assertion across the dispatch boundary."""
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")

        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == body["id"]
                    )
                )
            ).scalars()
        )
        for cell in cells:
            run = await db_session.get(PipelineRun, cell.pipeline_run_id)
            pipeline = await db_session.get(Pipeline, run.pipeline_id)
            steps = json.loads(pipeline.steps)
            assert steps[0]["type"] == "agent"
            assert steps[0]["config"]["model"] == cell.model
            assert steps[0]["config"]["agent"] == cell.agent

    async def test_cell_pipelines_are_hidden_ad_hoc_pipelines(
        self, client, card, db_session
    ):
        """They reuse agent_run's naming, so GET /api/pipelines keeps hiding
        them for free - the RUNS stay visible, which is the point."""
        from app.services.agent_run import is_adhoc_pipeline_name

        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(ExperimentRun.experiment_id == body["id"])
            )
        ).scalars().first()
        run = await db_session.get(PipelineRun, cell.pipeline_run_id)
        pipeline = await db_session.get(Pipeline, run.pipeline_id)
        assert is_adhoc_pipeline_name(pipeline.name)

        listed = (await client.get("/api/pipelines")).json()
        assert not any(p["id"] == pipeline.id for p in listed)

    async def test_cells_get_their_own_branch(self, client, card, db_session):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == body["id"]
                    )
                )
            ).scalars()
        )
        branches = set()
        for cell in cells:
            run = await db_session.get(PipelineRun, cell.pipeline_run_id)
            pipeline = await db_session.get(Pipeline, run.pipeline_id)
            branches.add(json.loads(pipeline.steps)[0]["config"]["branch"])
        assert len(branches) == 2
        assert all(b.startswith("lazyaf/exp/") for b in branches)

    async def test_push_is_off_by_default(self, client, card, db_session):
        """`commit: {enabled, push: False}` is the spelling agent_run uses for
        commit-locally-do-not-push."""
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(ExperimentRun.experiment_id == body["id"])
            )
        ).scalars().first()
        run = await db_session.get(PipelineRun, cell.pipeline_run_id)
        pipeline = await db_session.get(Pipeline, run.pipeline_id)
        commit = json.loads(pipeline.steps)[0]["config"]["commit"]
        assert commit == {"enabled": True, "push": False}

    async def test_verify_appends_a_script_step(self, client, card, db_session):
        body = await create(
            client,
            card,
            verify={"image": "python:3.11", "command": "pytest -q", "timeout": 120},
        )
        await client.post(f"/api/experiments/{body['id']}/launch")
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(ExperimentRun.experiment_id == body["id"])
            )
        ).scalars().first()
        run = await db_session.get(PipelineRun, cell.pipeline_run_id)
        pipeline = await db_session.get(Pipeline, run.pipeline_id)
        steps = json.loads(pipeline.steps)
        assert [s["type"] for s in steps] == ["agent", "script"]
        assert steps[1]["config"]["command"] == "pytest -q"
        assert steps[1]["timeout"] == 120
        # A crashed agent must not run verify and paper a 0% over an error.
        assert steps[0]["on_failure"] == "stop"

    async def test_launch_twice_is_a_409(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        second = await client.post(f"/api/experiments/{body['id']}/launch")
        assert second.status_code == 409
        assert "already" in second.text

    async def test_launch_404(self, client):
        assert (await client.post("/api/experiments/nope/launch")).status_code == 404

    async def test_launch_broadcasts_experiment_and_cell_frames(
        self, client, card, ws_frames
    ):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")

        statuses = ws_frames.of_type("experiment_status")
        cells = ws_frames.of_type("experiment_cell_status")
        assert statuses, "no experiment_status frame"
        assert len(cells) == 2
        assert {"id", "name", "status", "cells_total", "by_status", "spend_usd",
                "budget_usd", "cost_coverage", "stalled"} <= set(statuses[0])
        assert {"id", "experiment_id", "cell_index", "variant_index", "status",
                "pipeline_run_id", "label", "agent", "model",
                "prompt_template_id", "prompt_version"} <= set(cells[0])

    async def test_prompt_template_body_reaches_the_step_config(
        self, client, card, db_session
    ):
        template = PromptTemplate(
            id=str(uuid4()), name=f"tpl-{uuid4().hex[:6]}",
            content="FROZEN PROMPT BODY",
        )
        db_session.add(template)
        await db_session.commit()

        body = payload(card)
        body["matrix"]["prompts"] = [
            {"prompt_template_id": template.id, "label": "custom"}
        ]
        created = (await client.post("/api/experiments", json=body)).json()
        await client.post(f"/api/experiments/{created['id']}/launch")

        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == created["id"]
                )
            )
        ).scalars().first()
        run = await db_session.get(PipelineRun, cell.pipeline_run_id)
        pipeline = await db_session.get(Pipeline, run.pipeline_id)
        config = json.loads(pipeline.steps)[0]["config"]
        assert config["prompt_template"] == "FROZEN PROMPT BODY"
        assert cell.prompt_version == 1


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------

class TestLifecycle:
    async def test_completed_run_lands_its_cell(self, client, card, db_session):
        """The dispatch in agent_run.on_run_complete is the integrator's four
        lines (design 12.4); this drives the same function it calls, off the
        same persisted trigger_ref."""
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")

        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == body["id"]
                    )
                )
            ).scalars()
        )
        for cell in cells:
            run = await settle(db_session, cell.pipeline_run_id)
            await svc.on_cell_complete(
                db_session, run, run.status == RunStatus.PASSED.value
            )

        detail = (await client.get(f"/api/experiments/{body['id']}")).json()
        assert detail["status"] in ("complete", "budget_exhausted")
        assert detail["completed_at"] is not None
        assert set(detail["by_status"]) <= {"passed", "failed", "error"}

    async def test_abort_cancels_pending_and_reports_running(
        self, client, card, db_session
    ):
        body = await create(client, card, max_concurrency=1)
        await client.post(f"/api/experiments/{body['id']}/launch")

        response = await client.post(f"/api/experiments/{body['id']}/abort")
        assert response.status_code == 200
        assert response.json()["cancelled"] == 1
        assert response.json()["status"] == "aborted"

    async def test_abort_twice_is_a_409(self, client, card):
        body = await create(client, card, max_concurrency=1)
        await client.post(f"/api/experiments/{body['id']}/launch")
        await client.post(f"/api/experiments/{body['id']}/abort")
        # The remaining live cell keeps the experiment open; force the second
        # abort against a definitely-terminal experiment.
        second = await client.post(f"/api/experiments/{body['id']}/abort")
        assert second.status_code == 409

    async def test_resume_reports_what_it_did(self, client, card, db_session):
        body = await create(client, card, max_concurrency=1)
        await client.post(f"/api/experiments/{body['id']}/launch")
        # Simulate the restart: the live cell's process is gone.
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == body["id"],
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalars().first()
        cell.status = ExperimentRunStatus.PASSED.value
        await db_session.commit()
        svc._pump_locks.clear()

        response = await client.post(f"/api/experiments/{body['id']}/resume")
        assert response.status_code == 200
        assert response.json()["dispatched"] == 1

    async def test_stalled_is_reported_on_the_detail_endpoint(
        self, client, card, db_session
    ):
        body = await create(client, card, max_concurrency=1)
        await client.post(f"/api/experiments/{body['id']}/launch")
        for cell in (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == body["id"],
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalars():
            cell.status = ExperimentRunStatus.PASSED.value
        await db_session.commit()

        detail = (await client.get(f"/api/experiments/{body['id']}")).json()
        assert detail["stalled"] is True

    async def test_resume_on_a_terminal_experiment_is_a_409(
        self, client, card, db_session
    ):
        body = await create(client, card)
        experiment = await db_session.get(Experiment, body["id"])
        experiment.status = "complete"
        await db_session.commit()
        response = await client.post(f"/api/experiments/{body['id']}/resume")
        assert response.status_code == 409


# -----------------------------------------------------------------------------
# Results + leaderboard shapes
# -----------------------------------------------------------------------------

class TestResults:
    async def test_results_are_one_row_per_cell_with_coordinates(
        self, client, card
    ):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        rows = (await client.get(f"/api/experiments/{body['id']}/results")).json()

        assert len(rows) == 2
        assert [r["cell_index"] for r in rows] == [0, 1]
        assert {r["model"] for r in rows} == {"mock-a", "mock-b"}
        assert all(r["tests_passed"] == 0 for r in rows)
        assert all(r["cost_usd"] is None for r in rows), "no usage yet"

    async def test_results_404(self, client):
        assert (
            await client.get("/api/experiments/nope/results")
        ).status_code == 404

    async def test_leaderboard_always_reports_not_ranked(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        board = (await client.get(f"/api/experiments/{body['id']}/leaderboard")).json()

        from app.schemas.experiment import NOT_RANKED_NOTE

        assert board["ranked"] is False
        assert board["note"] == NOT_RANKED_NOTE

    async def test_leaderboard_has_one_row_per_variant(self, client, card):
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        board = (await client.get(f"/api/experiments/{body['id']}/leaderboard")).json()
        assert len(board["variants"]) == 2
        assert [v["variant_index"] for v in board["variants"]] == [0, 1]

    async def test_leaderboard_handles_zero_runs(self, client, card):
        """A variant with no test evidence is null with a reason, never 0%."""
        body = await create(client, card)
        await client.post(f"/api/experiments/{body['id']}/launch")
        board = (await client.get(f"/api/experiments/{body['id']}/leaderboard")).json()
        for variant in board["variants"]:
            assert variant["pass_rate"] is None
            assert variant["reason"]
            assert variant["cost_usd_total"] == "0.000000"

    async def test_leaderboard_404(self, client):
        assert (
            await client.get("/api/experiments/nope/leaderboard")
        ).status_code == 404

    async def test_feature_leaderboard_404_without_criteria(self, client, db_session):
        feature = Feature(id=str(uuid4()), title="F", repo_ids="[]")
        db_session.add(feature)
        await db_session.commit()
        response = await client.get(f"/api/leaderboards/feature/{feature.id}")
        assert response.status_code == 404
        assert "no acceptance criteria" in response.text
