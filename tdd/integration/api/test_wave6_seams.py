"""
Integrator seam tests for wave 6 (12.6.5 / 12.6.6 / 12.7).

Each lane proved its own half. These prove the JOINS between halves — the
places where two lanes' code meets and where a green suite on either side
would not have noticed a break.

WHY THIS FILE EXISTS AT ALL. `test_experiments_api.py` launches a real
experiment and asserts the cell -> run link exists. `test_experiment_stamping.py`
asserts ingestion reads that link and stamps the coordinates. Neither test
touches the other's half: the stamping suite builds its `PipelineRun` by hand
with `trigger_type="experiment"` already written on it, so a launch that
stopped writing the link, or wrote a different one, would leave both suites
green and the leaderboard silently empty. The tests below drive ONE object
through BOTH halves: `POST /api/experiments` -> `launch` -> the real
`/api/steps/{id}/test-results` and `/api/steps/{id}/usage` endpoints (real
step-auth token, real ingestion services) -> the real leaderboard endpoint.

The `StepExecution` row is created here rather than by the executor because
the T1 tier has no Docker: the cell's agent step fails at image selection, so
no execution row is ever minted. Everything that the seam is ABOUT — the
experiment, its cells, the ephemeral pipeline, the `PipelineRun` and its
persisted `trigger_type`/`trigger_ref` — comes from the real launch, and the
execution row only supplies the authenticated address the container would
have posted to.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (  # noqa: E402
    AcceptanceCriterion,
    Card,
    Feature,
    PipelineRun,
    Repo,
    StepExecution,
    StepRun,
    StepUsage,
    TestRef,
    TestRun,
    UserStory,
)
from app.models.experiment import ExperimentRun  # noqa: E402
from app.services import experiment_service as svc  # noqa: E402
from app.services.control_layer.auth import generate_step_token  # noqa: E402


# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------

@pytest.fixture
async def repo(db_session) -> Repo:
    row = Repo(id=str(uuid4()), name=f"seam-{uuid4().hex[:6]}", is_ingested=True,
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


@pytest.fixture
async def criterion_and_ref(db_session, repo):
    """One criterion with one TestRef, so the leaderboard has a denominator."""
    feature = Feature(id=str(uuid4()), title="Widgets",
                      repo_ids=json.dumps([repo.id]))
    db_session.add(feature)
    await db_session.commit()
    story = UserStory(id=str(uuid4()), feature_id=feature.id, title="Story")
    db_session.add(story)
    await db_session.commit()
    criterion = AcceptanceCriterion(
        id=str(uuid4()), user_story_id=story.id, text="it widgets"
    )
    db_session.add(criterion)
    await db_session.commit()
    ref = TestRef(
        id=str(uuid4()), lazyaf_test_id="seam.widget", repo_id=repo.id,
        criterion_id=criterion.id, status="active",
    )
    db_session.add(ref)
    await db_session.commit()
    return criterion, ref


def experiment_payload(card):
    return {
        "name": "seam matrix",
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


async def launch(client, card):
    """Create + launch a real experiment. Returns the experiment body."""
    created = await client.post("/api/experiments", json=experiment_payload(card))
    assert created.status_code == 201, created.text
    body = created.json()
    launched = await client.post(f"/api/experiments/{body['id']}/launch")
    assert launched.status_code == 202, launched.text
    return body


async def cells_of(db_session, experiment_id) -> list[ExperimentRun]:
    rows = list(
        (
            await db_session.execute(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == experiment_id)
                .order_by(ExperimentRun.cell_index)
            )
        ).scalars()
    )
    assert rows, "launch created no cells"
    return rows


async def address_of(db_session, cell: ExperimentRun) -> dict:
    """The authenticated /api/steps address for a launched cell's step.

    The run, the pipeline and the StepRun all come from the REAL launch; only
    the StepExecution is minted here (T1 has no Docker, so the agent step
    never reaches the executor that would create one).
    """
    assert cell.pipeline_run_id, "the cell never got a pipeline run"
    step_run = (
        await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == cell.pipeline_run_id)
        )
    ).scalars().first()
    assert step_run is not None, "the launched cell's run has no StepRun"

    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{cell.pipeline_run_id}:{step_run.step_index}:1",
        step_run_id=step_run.id,
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()
    token = generate_step_token(
        step_id=execution.id, execution_key=execution.execution_key
    )
    return {
        "execution_id": execution.id,
        "step_run_id": step_run.id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


# -----------------------------------------------------------------------------
# 12.6.5: a launched cell's coordinates survive the whole round trip
# -----------------------------------------------------------------------------

class TestLaunchedCellStampsItsTestRuns:
    async def test_a_real_launch_feeds_the_real_ingestion(
        self, client, card, db_session, criterion_and_ref
    ):
        """THE JOIN. One cell, launched for real, ingesting for real.

        Every coordinate asserted here is read back from the TestRun the
        ingestion service wrote, and compared against the CELL ROW the
        launch created — not against a literal this test chose. A launch
        that wrote a different trigger_ref, or an ingestion that resolved
        the cell differently, fails here and nowhere else.
        """
        body = await launch(client, card)
        cells = await cells_of(db_session, body["id"])
        cell = cells[0]
        addr = await address_of(db_session, cell)

        response = await client.post(
            f"/api/steps/{addr['execution_id']}/test-results",
            json={
                "version": 1,
                "results": [
                    {"lazyaf_test_id": "seam.widget", "status": "passed",
                     "duration_ms": 7}
                ],
            },
            headers=addr["headers"],
        )
        assert response.status_code == 200, response.text

        _criterion, ref = criterion_and_ref
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == ref.id)
            )
        ).scalar_one()

        assert run.experiment_run_id == cell.id
        assert run.model == cell.model
        assert run.model is not None, (
            "the cell carries a model; a NULL here means the coordinates were "
            "never resolved from the cell row"
        )
        assert run.prompt_template_id == cell.prompt_template_id
        assert run.prompt_version == cell.prompt_version
        assert run.step_run_id == addr["step_run_id"]

    async def test_the_two_cells_stamp_two_different_models(
        self, client, card, db_session, criterion_and_ref
    ):
        """The matrix is only worth running if the variants are DISTINGUISHABLE
        downstream. Both cells ingest the same test id; the TestRuns must
        differ by exactly the coordinates that differ on the cells."""
        body = await launch(client, card)
        cells = await cells_of(db_session, body["id"])
        assert len({c.model for c in cells}) == 2, "the matrix collapsed"

        for cell in cells:
            addr = await address_of(db_session, cell)
            response = await client.post(
                f"/api/steps/{addr['execution_id']}/test-results",
                json={
                    "version": 1,
                    "results": [
                        {"lazyaf_test_id": "seam.widget", "status": "passed",
                         "duration_ms": 7}
                    ],
                },
                headers=addr["headers"],
            )
            assert response.status_code == 200, response.text

        _criterion, ref = criterion_and_ref
        runs = list(
            (
                await db_session.execute(
                    select(TestRun).where(TestRun.test_ref_id == ref.id)
                )
            ).scalars()
        )
        assert len(runs) == 2
        by_cell = {r.experiment_run_id: r.model for r in runs}
        assert by_cell == {c.id: c.model for c in cells}


# -----------------------------------------------------------------------------
# 12.6.5: the same launch's usage rows are attributable to the cell
# -----------------------------------------------------------------------------

class TestLaunchedCellOwnsItsUsageRows:
    async def test_usage_posted_by_a_cells_step_joins_back_to_that_cell(
        self, client, card, db_session
    ):
        """StepUsage carries no experiment column by design: the join is
        `step_usages.pipeline_run_id == experiment_runs.pipeline_run_id`.
        That join is load-bearing for every cost number the leaderboard
        shows, and nothing else asserts it over a REAL launch."""
        body = await launch(client, card)
        cells = await cells_of(db_session, body["id"])
        cell = cells[0]
        addr = await address_of(db_session, cell)

        response = await client.post(
            f"/api/steps/{addr['execution_id']}/usage",
            json={
                "version": 1,
                "provider": "anthropic",
                "model": cell.model,
                "input_tokens": 1200,
                "output_tokens": 340,
                "cost_usd": "0.250000",
                "cost_source": "cli-reported",
                "wall_clock_ms": 4321,
            },
            headers=addr["headers"],
        )
        assert response.status_code == 200, response.text

        usage = (
            await db_session.execute(
                select(StepUsage).where(
                    StepUsage.step_execution_id == addr["execution_id"]
                )
            )
        ).scalar_one()
        assert usage.pipeline_run_id == cell.pipeline_run_id, (
            "usage ingestion did not resolve the run; the experiment join is "
            "on pipeline_run_id and would silently return nothing"
        )
        assert usage.model == cell.model

        rows = await svc.fetch_usage_rows(db_session, body["id"])
        assert [r.cell_id for r in rows] == [cell.id]
        assert rows[0].cost_usd == Decimal("0.250000")
        assert rows[0].input_tokens == 1200
        assert rows[0].wall_clock_ms == 4321

    async def test_the_leaderboard_shows_that_cost_against_the_right_variant(
        self, client, card, db_session, criterion_and_ref
    ):
        """End of the wire: a real launch, a real usage post, a real
        leaderboard read. The dollars must land on the variant that spent
        them and on no other."""
        body = await launch(client, card)
        cells = await cells_of(db_session, body["id"])
        spender, other = cells[0], cells[1]
        addr = await address_of(db_session, spender)

        assert (
            await client.post(
                f"/api/steps/{addr['execution_id']}/usage",
                json={
                    "version": 1,
                    "provider": "anthropic",
                    "model": spender.model,
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cost_usd": "1.500000",
                    "cost_source": "cli-reported",
                    "wall_clock_ms": 900,
                },
                headers=addr["headers"],
            )
        ).status_code == 200

        response = await client.get(f"/api/experiments/{body['id']}/leaderboard")
        assert response.status_code == 200, response.text
        board = response.json()

        by_index = {v["variant_index"]: v for v in board["variants"]}
        assert Decimal(by_index[spender.variant_index]["cost_usd_total"]) == Decimal("1.5")
        assert Decimal(by_index[other.variant_index]["cost_usd_total"]) == Decimal("0")
        # R1: an unranked board must say so rather than implying a winner.
        assert board["ranked"] is False


# -----------------------------------------------------------------------------
# 12.6.5 x 12.7: the two trigger vocabularies stay disjoint and both closed
# -----------------------------------------------------------------------------

class TestTriggerVocabularyAfterRegistration:
    """Both lanes widened the same closed vocabulary through the integrator.

    `experiment` and `debug_rerun` are stampable only from inside; the public
    run endpoint must refuse BOTH, and the schema must accept both as valid
    persisted values. A registration that added one and dropped the other
    passes every lane suite and fails here.
    """

    def test_both_reserved_families_are_known_and_disjoint(self):
        from app.schemas.pipeline import (
            ADHOC_TRIGGER_TYPES,
            DEBUG_TRIGGER_TYPES,
            KNOWN_TRIGGER_TYPES,
            PUBLIC_TRIGGER_TYPES,
        )
        from app.models.experiment import TRIGGER_EXPERIMENT
        from app.services.agent_run import (
            ADHOC_TRIGGER_TYPES as SERVICE_ADHOC,
            TRIGGER_EXPERIMENT as SERVICE_TRIGGER,
        )

        assert TRIGGER_EXPERIMENT in ADHOC_TRIGGER_TYPES
        assert "debug_rerun" in DEBUG_TRIGGER_TYPES
        assert set(ADHOC_TRIGGER_TYPES) & set(DEBUG_TRIGGER_TYPES) == set()
        assert set(ADHOC_TRIGGER_TYPES) & set(PUBLIC_TRIGGER_TYPES) == set()
        assert set(DEBUG_TRIGGER_TYPES) & set(PUBLIC_TRIGGER_TYPES) == set()
        assert set(ADHOC_TRIGGER_TYPES) <= set(KNOWN_TRIGGER_TYPES)
        assert set(DEBUG_TRIGGER_TYPES) <= set(KNOWN_TRIGGER_TYPES)
        # R3: the service's tuple and the schema's tuple are the same list,
        # spelled once each. Drift between them is what lets a run be stamped
        # with a type the public endpoint forgot to refuse.
        assert set(SERVICE_ADHOC) == set(ADHOC_TRIGGER_TYPES)
        assert SERVICE_TRIGGER == TRIGGER_EXPERIMENT

    @pytest.mark.parametrize("reserved", ["card_work", "playground",
                                          "experiment", "debug_rerun"])
    async def test_the_public_run_endpoint_refuses_every_reserved_type(
        self, client, db_session, repo, reserved
    ):
        from app.models import Pipeline

        pipeline = Pipeline(
            id=str(uuid4()), repo_id=repo.id, name="public", steps="[]"
        )
        db_session.add(pipeline)
        await db_session.commit()

        response = await client.post(
            f"/api/pipelines/{pipeline.id}/run",
            json={"trigger_type": reserved},
        )
        assert response.status_code == 400, response.text
        assert reserved in response.json()["detail"]

        # And nothing was started: the guard runs before any lookup or write.
        runs = list(
            (
                await db_session.execute(
                    select(PipelineRun).where(
                        PipelineRun.pipeline_id == pipeline.id
                    )
                )
            ).scalars()
        )
        assert runs == []


# -----------------------------------------------------------------------------
# 12.7: the debug router and its resettables are actually registered
# -----------------------------------------------------------------------------

class TestDebugSurfaceIsRegistered:
    def test_the_debug_routes_and_terminal_socket_are_mounted(self):
        from app.main import app

        paths = {getattr(route, "path", "") for route in app.routes}
        for path in (
            "/api/pipeline-runs/{run_id}/debug-rerun",
            "/api/debug/{session_id}",
            "/api/debug/{session_id}/resume",
            "/api/debug/{session_id}/abort",
            "/api/debug/{session_id}/terminal",
        ):
            assert path in paths, f"{path} is not mounted"

    def test_the_experiments_routes_are_mounted(self):
        from app.main import app

        paths = {getattr(route, "path", "") for route in app.routes}
        for path in (
            "/api/experiments",
            "/api/experiments/{experiment_id}/launch",
            "/api/experiments/{experiment_id}/leaderboard",
            "/api/leaderboards/feature/{feature_id}",
        ):
            assert path in paths, f"{path} is not mounted"

    def test_both_debug_resettables_are_registered(self):
        """R6/e2e hygiene: a paused gate and a live sidecar outlive the DB
        reset unless the test-mode reset knows about them."""
        from app.routers.test_api import _RESETTABLES

        assert "debug_sessions" in _RESETTABLES
        assert "debug_terminals" in _RESETTABLES

    def test_every_model_module_this_wave_added_is_exported(self):
        """`import app.models` is what puts a table on Base.metadata before
        `init_db`. A model module that is never imported produces a schema
        that create_all builds and the migration chain does not — the parity
        test's failure mode, one step earlier."""
        import app.models as models

        for name in (
            "DebugSession",
            "Experiment",
            "ExperimentRun",
            "PromptVersion",
        ):
            assert name in models.__all__
            assert getattr(models, name, None) is not None
