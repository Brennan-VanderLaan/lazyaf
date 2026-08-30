"""
Integration tests for experiment coordinates on TestRun, and for the
leaderboard built from them (Phase 12.6.5).

CONTRACT #4, BOTH DIRECTIONS. Coordinates come from the CELL ROW, server
side, resolved through the run's PERSISTED `trigger_type` / `trigger_ref`:

  positive: `test_ingest_stamps_experiment_coordinates_from_the_cell`
  negative: `test_ingest_ignores_wire_supplied_model`

This deviates deliberately from PLAN's sketch (a manifest forwarding
model/prompt from the container). The container is untrusted, the backend
already knows the answer from the row it created, and `usage_ingestion`'s own
docstring establishes the precedent: "step_run_id, pipeline_run_id | HERE —
never trusted from the wire". A step cannot mislabel which variant it was,
and `TestResultsManifest` is unchanged — the frozen control-layer protocol
stays frozen.

The leaderboard tests then read those rows back through the real endpoint, so
the aggregation is exercised over data that arrived the way production data
arrives, not over hand-inserted TestRuns.
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

from app.models import (
    AcceptanceCriterion,
    Feature,
    Pipeline,
    PipelineRun,
    Repo,
    StepExecution,
    StepRun,
    TestRef,
    TestRun,
    UserStory,
)
from app.models.experiment import (
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.services import experiment_service as svc
from app.services.control_layer.auth import generate_step_token

COMMIT = "cafebabe0001"
BRANCH = "lazyaf/exp/abc12345/000"


@pytest.fixture(autouse=True)
def _register_router():
    from app.main import app
    from app.routers import experiments

    if not any(
        getattr(route, "path", "") == "/api/experiments" for route in app.routes
    ):
        app.include_router(experiments.router)
    yield


async def make_world(db_session):
    repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True)
    db_session.add(repo)
    feature = Feature(id=str(uuid4()), title="Widgets",
                      repo_ids=json.dumps([str(uuid4())]))
    db_session.add(feature)
    await db_session.commit()
    story = UserStory(id=str(uuid4()), feature_id=feature.id, title="Story")
    db_session.add(story)
    await db_session.commit()
    criteria = [
        AcceptanceCriterion(id=str(uuid4()), user_story_id=story.id, text=f"c{i}")
        for i in range(3)
    ]
    for criterion in criteria:
        db_session.add(criterion)
    await db_session.commit()
    refs = []
    for i, criterion in enumerate(criteria):
        ref = TestRef(
            id=str(uuid4()), lazyaf_test_id=f"exp.c{i}", repo_id=repo.id,
            criterion_id=criterion.id, status="active",
        )
        db_session.add(ref)
        refs.append(ref)
    # A fourth criterion with NO test at all - it must report null, not 0%.
    untested = AcceptanceCriterion(
        id=str(uuid4()), user_story_id=story.id, text="never measured"
    )
    db_session.add(untested)
    await db_session.commit()
    return repo, feature, criteria, refs, untested


async def make_experiment(db_session, repo, *, models=2, status=None):
    experiment = Experiment(
        id=str(uuid4()),
        name="stamping",
        description="",
        target_type="card",
        target_id=str(uuid4()),
        repo_id=repo.id,
        matrix=json.dumps(
            {
                "models": [
                    {"agent": "mock", "model": f"mock-{i}", "label": f"v{i}"}
                    for i in range(models)
                ],
                "prompts": [{"prompt_template_id": None, "label": "default"}],
                "repeat": 1,
            }
        ),
        budget_usd=Decimal("5"),
        status=status or ExperimentStatus.RUNNING.value,
    )
    db_session.add(experiment)
    await db_session.commit()
    return experiment


async def make_cell(db_session, experiment, *, cell_index=0, model="mock-0",
                    prompt_template_id=None, prompt_version=None,
                    status=ExperimentRunStatus.RUNNING.value):
    cell = ExperimentRun(
        id=str(uuid4()),
        experiment_id=experiment.id,
        cell_index=cell_index,
        variant_index=cell_index,
        agent="mock",
        model=model,
        prompt_template_id=prompt_template_id,
        prompt_version=prompt_version,
        label=f"v{cell_index}",
        status=status,
    )
    db_session.add(cell)
    await db_session.commit()
    return cell


async def make_step_ctx(db_session, repo, *, trigger_type, trigger_ref):
    """Full StepExecution chain for a run with the given persisted trigger."""
    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci", steps="[]")
    db_session.add(pipeline)
    await db_session.commit()
    run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status="running",
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        trigger_context=json.dumps({"branch": BRANCH, "commit_sha": COMMIT}),
    )
    db_session.add(run)
    await db_session.commit()
    step_run = StepRun(
        id=str(uuid4()), pipeline_run_id=run.id, step_index=0,
        step_name="verify", status="running", logs="",
    )
    db_session.add(step_run)
    await db_session.commit()
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run.id}:0:1",
        step_run_id=step_run.id,
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()
    token = generate_step_token(
        step_id=execution.id, execution_key=execution.execution_key
    )
    return {
        "run": run,
        "execution_id": execution.id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def post_manifest(client, ctx, results):
    return await client.post(
        f"/api/steps/{ctx['execution_id']}/test-results",
        json={"version": 1, "results": results},
        headers=ctx["headers"],
    )


def entry(test_id, status="passed", **extra):
    body = {"lazyaf_test_id": test_id, "status": status, "duration_ms": 5}
    body.update(extra)
    return body


# -----------------------------------------------------------------------------
# Contract #4
# -----------------------------------------------------------------------------

class TestStamping:
    async def test_ingest_stamps_experiment_coordinates_from_the_cell(
        self, client, db_session
    ):
        repo, _, _, refs, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo)
        cell = await make_cell(
            db_session, experiment, model="mock-0",
            prompt_template_id=None, prompt_version=7,
        )
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )

        response = await post_manifest(client, ctx, [entry("exp.c0")])
        assert response.status_code == 200

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == refs[0].id)
            )
        ).scalar_one()
        assert run.experiment_run_id == cell.id
        assert run.model == "mock-0"
        assert run.prompt_version == 7

    async def test_ingest_ignores_wire_supplied_model(self, client, db_session):
        """The negative pin: a container that claims a different variant is
        ignored, because the manifest is not the source of that fact."""
        repo, _, _, refs, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo)
        cell = await make_cell(db_session, experiment, model="mock-0")
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )

        response = await post_manifest(
            client,
            ctx,
            [
                entry(
                    "exp.c0",
                    model="claude-opus-5-LIE",
                    prompt_template_id="tpl-LIE",
                    experiment_run_id="cell-LIE",
                )
            ],
        )
        assert response.status_code == 200

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == refs[0].id)
            )
        ).scalar_one()
        assert run.model == "mock-0"
        assert run.prompt_template_id is None
        assert run.experiment_run_id == cell.id

    async def test_trigger_type_is_single_sourced(self):
        """R3: the cell -> run link is one string, and every side reads it
        from `models.experiment` rather than re-spelling it."""
        from app.models import experiment as model_module
        from app.services import experiment_service, test_ingestion

        assert model_module.TRIGGER_EXPERIMENT == "experiment"
        assert experiment_service.TRIGGER_EXPERIMENT is (
            model_module.TRIGGER_EXPERIMENT
        )
        assert test_ingestion.TRIGGER_EXPERIMENT is (
            model_module.TRIGGER_EXPERIMENT
        )

    async def test_manifest_schema_is_unchanged(self):
        """The frozen control-layer protocol stays frozen: nothing about
        experiments appears on the wire."""
        from app.schemas.testref import TestResultEntry, TestResultsManifest

        assert set(TestResultEntry.model_fields) == {
            "lazyaf_test_id", "status", "duration_ms", "file_path",
        }
        assert set(TestResultsManifest.model_fields) == {"version", "results"}

    async def test_non_experiment_runs_stamp_null(self, client, db_session):
        """NULL is the TRUE value on an ordinary CI run: it measured the repo,
        not a variant."""
        repo, _, _, refs, _ = await make_world(db_session)
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="push", trigger_ref="main:abc123"
        )

        await post_manifest(client, ctx, [entry("exp.c0")])

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == refs[0].id)
            )
        ).scalar_one()
        assert run.experiment_run_id is None
        assert run.model is None
        assert run.prompt_version is None

    async def test_a_vanished_cell_still_ingests_the_results(
        self, client, db_session
    ):
        """Dropping measurements over a missing label would be the worse
        failure; the gap is logged, not swallowed."""
        repo, _, _, refs, _ = await make_world(db_session)
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=str(uuid4())
        )

        response = await post_manifest(client, ctx, [entry("exp.c0")])
        assert response.status_code == 200

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == refs[0].id)
            )
        ).scalar_one()
        assert run.experiment_run_id is None
        assert run.status == "passed"

    async def test_reposting_updates_the_coordinates_too(self, client, db_session):
        """Idempotency is per (step_run, test_ref); the update branch must
        stamp the same way the create branch does."""
        repo, _, _, refs, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo)
        cell = await make_cell(db_session, experiment, model="mock-0")
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )

        await post_manifest(client, ctx, [entry("exp.c0", status="failed")])
        await post_manifest(client, ctx, [entry("exp.c0", status="passed")])

        rows = list(
            (
                await db_session.execute(
                    select(TestRun).where(TestRun.test_ref_id == refs[0].id)
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].status == "passed"
        assert rows[0].model == "mock-0"
        assert rows[0].experiment_run_id == cell.id

    async def test_criterion_history_surfaces_the_coordinates(
        self, client, db_session
    ):
        """GET /api/criteria/{id}/history already returned model and
        prompt_template_id; 12.6.5 is what makes them non-NULL."""
        repo, _, criteria, _, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo)
        cell = await make_cell(db_session, experiment, model="mock-0")
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )
        await post_manifest(client, ctx, [entry("exp.c0")])

        history = (
            await client.get(f"/api/criteria/{criteria[0].id}/history")
        ).json()
        assert history[0]["model"] == "mock-0"


# -----------------------------------------------------------------------------
# The leaderboard over real ingested rows
# -----------------------------------------------------------------------------

class TestLeaderboardOverIngestedRows:
    async def _two_variants(self, client, db_session):
        """Variant 0 passes c0+c1, variant 1 passes c0 and fails c1; c2 is
        skipped by both; a fourth criterion has no test at all."""
        repo, feature, criteria, refs, untested = await make_world(db_session)
        experiment = await make_experiment(db_session, repo, models=2)
        cells = []
        for index, outcomes in enumerate(
            ([("exp.c0", "passed"), ("exp.c1", "passed"), ("exp.c2", "skipped")],
             [("exp.c0", "passed"), ("exp.c1", "failed"), ("exp.c2", "skipped")])
        ):
            cell = await make_cell(
                db_session, experiment, cell_index=index, model=f"mock-{index}",
                status=(
                    ExperimentRunStatus.PASSED.value
                    if index == 0
                    else ExperimentRunStatus.FAILED.value
                ),
            )
            ctx = await make_step_ctx(
                db_session, repo, trigger_type="experiment", trigger_ref=cell.id
            )
            cell.pipeline_run_id = ctx["run"].id
            await db_session.commit()
            await post_manifest(
                client, ctx, [entry(test_id, status) for test_id, status in outcomes]
            )
            cells.append(cell)
        return repo, feature, experiment, criteria, cells, untested

    async def test_per_variant_aggregation_is_real(self, client, db_session):
        _, _, experiment, criteria, _, _ = await self._two_variants(
            client, db_session
        )
        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()

        assert len(board["variants"]) == 2
        by_index = {v["variant_index"]: v for v in board["variants"]}

        c0, c1 = criteria[0].id, criteria[1].id
        rates_0 = {c["criterion_id"]: c["pass_rate"] for c in by_index[0]["criteria"]}
        rates_1 = {c["criterion_id"]: c["pass_rate"] for c in by_index[1]["criteria"]}
        assert rates_0[c0] == 1.0
        assert rates_1[c0] == 1.0
        assert rates_0[c1] == 1.0
        assert rates_1[c1] == 0.0

    async def test_skips_are_excluded_from_every_denominator(
        self, client, db_session
    ):
        _, _, experiment, criteria, _, _ = await self._two_variants(
            client, db_session
        )
        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()
        c2 = criteria[2].id
        for variant in board["variants"]:
            entry_c2 = next(c for c in variant["criteria"] if c["criterion_id"] == c2)
            assert entry_c2["skipped"] == 1
            assert entry_c2["passed"] == 0 and entry_c2["failed"] == 0
            assert entry_c2["pass_rate"] is None
            assert entry_c2["reason"]

    async def test_macro_headline_ignores_the_all_skipped_criterion(
        self, client, db_session
    ):
        _, _, experiment, _, _, _ = await self._two_variants(client, db_session)
        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()
        by_index = {v["variant_index"]: v for v in board["variants"]}
        assert by_index[0]["pass_rate"] == 1.0     # macro over {c0: 1.0, c1: 1.0}
        assert by_index[1]["pass_rate"] == 0.5     # macro over {c0: 1.0, c1: 0.0}

    async def test_untested_criterion_never_appears_as_zero_percent(
        self, client, db_session
    ):
        _, _, experiment, _, _, untested = await self._two_variants(
            client, db_session
        )
        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()
        for variant in board["variants"]:
            assert untested.id not in {c["criterion_id"] for c in variant["criteria"]}

    async def test_results_endpoint_reports_per_cell_test_counts(
        self, client, db_session
    ):
        _, _, experiment, _, _, _ = await self._two_variants(client, db_session)
        rows = (
            await client.get(f"/api/experiments/{experiment.id}/results")
        ).json()
        by_index = {r["cell_index"]: r for r in rows}
        assert by_index[0]["tests_passed"] == 2
        assert by_index[0]["tests_skipped"] == 1
        assert by_index[1]["tests_failed"] == 1

    async def test_errored_cells_contribute_no_outcomes(self, client, db_session):
        """An error measured NOTHING; its rows must not enter a denominator."""
        repo, _, criteria, _, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo, models=1)
        cell = await make_cell(
            db_session, experiment, status=ExperimentRunStatus.ERROR.value
        )
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )
        cell.pipeline_run_id = ctx["run"].id
        await db_session.commit()
        await post_manifest(client, ctx, [entry("exp.c0", "failed")])

        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()
        variant = board["variants"][0]
        assert variant["criteria"] == []
        assert variant["pass_rate"] is None
        assert variant["cells_errored"] == 1
        assert variant["cells_measured"] == 0

    async def test_unlinked_tests_are_bucketed_not_dropped(self, client, db_session):
        repo, _, _, _, _ = await make_world(db_session)
        experiment = await make_experiment(db_session, repo, models=1)
        cell = await make_cell(
            db_session, experiment, status=ExperimentRunStatus.PASSED.value
        )
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="experiment", trigger_ref=cell.id
        )
        cell.pipeline_run_id = ctx["run"].id
        await db_session.commit()
        # An unregistered id: ingestion auto-creates an ORPHAN ref with no
        # criterion link.
        await post_manifest(client, ctx, [entry("not.registered", "passed")])

        board = (
            await client.get(f"/api/experiments/{experiment.id}/leaderboard")
        ).json()
        variant = board["variants"][0]
        assert variant["unlinked_tests"]["passed"] == 1
        assert variant["criteria"] == []

    async def test_feature_leaderboard_spans_experiments_and_labels_the_baseline(
        self, client, db_session
    ):
        repo, feature, experiment, criteria, cells, _ = await self._two_variants(
            client, db_session
        )
        # An ordinary CI run over the same criteria.
        ctx = await make_step_ctx(
            db_session, repo, trigger_type="push", trigger_ref="main:abc"
        )
        await post_manifest(client, ctx, [entry("exp.c0", "passed")])

        board = (
            await client.get(f"/api/leaderboards/feature/{feature.id}")
        ).json()
        labels = [v["label"] for v in board["variants"]]
        assert "non-experiment runs" in labels
        assert board["ranked"] is False
        baseline = next(
            v for v in board["variants"] if v["label"] == "non-experiment runs"
        )
        assert baseline["variant_index"] == -1
        assert baseline["criteria"][0]["pass_rate"] == 1.0

    async def test_feature_leaderboard_can_be_scoped_to_one_experiment(
        self, client, db_session
    ):
        _, feature, experiment, _, _, _ = await self._two_variants(
            client, db_session
        )
        board = (
            await client.get(
                f"/api/leaderboards/feature/{feature.id}"
                f"?experiment_id={experiment.id}"
            )
        ).json()
        assert len([v for v in board["variants"] if v["variant_index"] >= 0]) == 2
