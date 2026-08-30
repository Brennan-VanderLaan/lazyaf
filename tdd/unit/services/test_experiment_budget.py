"""
Unit tests for the budget cap and the dry-run estimate (Phase 12.6.5).

The cap is the phase's headline guardrail, so its three stated properties get
one test each:

1. The cap bounds DISPATCH, not in-flight spend; overshoot is RECORDED in
   `budget_overrun_usd`, never absorbed.
2. `cost_source="unknown"` rows count as ZERO against the cap — which is why
   `cost_coverage` is surfaced everywhere.
3. No pricing history does NOT disable the cap: the estimate is advisory,
   enforcement runs off observed `StepUsage`.

And the estimate's own rule: a variant with no priced history contributes
NOTHING, the basis degrades, the warnings name it, and the number is
explicitly a LOWER BOUND — it never silently reads as $0.00.
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
    Card,
    Pipeline,
    PipelineRun,
    Repo,
    RunStatus,
    StepExecution,
    StepRun,
    StepUsage,
)
from app.models.experiment import (
    EstimateBasis,
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.schemas.experiment import MatrixSpec
from app.services import experiment_service as svc

from tdd.unit.services.experiment_rows import (  # noqa: E402
    add_usage,
    cells_by_status,
    clean_pump_state,  # noqa: F401  (autouse fixture)
    fake_dispatch,  # noqa: F401  (fixture)
    make_card,
    make_experiment,
    make_repo,
    make_run,
)


def spec(models=1, prompts=1, repeat=1, model_names=None) -> MatrixSpec:
    names = model_names or [f"m{i}" for i in range(models)]
    return MatrixSpec.model_validate(
        {
            "models": [{"agent": "mock", "model": name, "label": name}
                       for name in names],
            "prompts": [{"prompt_template_id": None, "label": f"p{i}"}
                        for i in range(prompts)],
            "repeat": repeat,
        }
    )


# -----------------------------------------------------------------------------
# The estimate
# -----------------------------------------------------------------------------

class TestEstimate:
    async def test_no_history_never_reads_as_zero_dollars(self, db_session):
        estimate = await svc.estimate_matrix(db_session, spec(), Decimal("5"))

        assert estimate.estimate_basis == EstimateBasis.NO_HISTORY
        assert estimate.per_variant[0].basis == EstimateBasis.NO_HISTORY
        assert estimate.per_variant[0].samples == 0
        assert any("no priced history" in w for w in estimate.warnings)
        assert any("LOWER BOUND" in w for w in estimate.warnings)

    async def test_history_prices_the_variant_by_median(self, db_session):
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo)
        for cost in ("0.10", "0.20", "5.00"):
            await add_usage(db_session, run.id, cost=cost, model="m0")

        estimate = await svc.estimate_matrix(db_session, spec(repeat=4), Decimal("50"))

        # median 0.20 * 4 runs; NOT the mean (which the outlier would own).
        assert estimate.estimated_cost_usd == "0.800000"
        assert estimate.estimate_basis == EstimateBasis.HISTORICAL_MEDIAN
        assert estimate.per_variant[0].samples == 3

    async def test_unknown_cost_source_rows_are_not_history(self, db_session):
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo)
        await add_usage(db_session, run.id, cost="9.00", source="unknown",
                        model="m0")

        estimate = await svc.estimate_matrix(db_session, spec(), Decimal("5"))
        assert estimate.estimate_basis == EstimateBasis.NO_HISTORY

    async def test_partial_basis_when_only_some_variants_are_priced(self, db_session):
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo)
        await add_usage(db_session, run.id, cost="0.50", model="priced")

        estimate = await svc.estimate_matrix(
            db_session, spec(model_names=["priced", "unpriced"]), Decimal("5")
        )

        assert estimate.estimate_basis == EstimateBasis.PARTIAL
        assert estimate.estimated_cost_usd == "0.500000"
        assert any("unpriced" in w for w in estimate.warnings)

    async def test_null_model_is_honestly_unpriced_not_approximated(self, db_session):
        """'the CLI's own default' has nothing to key history on. Borrowing an
        unrelated model's rows would be an invented number."""
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo)
        await add_usage(db_session, run.id, cost="0.50", model="m0")

        estimate = await svc.estimate_matrix(
            db_session,
            MatrixSpec.model_validate(
                {
                    "models": [{"agent": "claude-code", "model": None}],
                    "prompts": [{"prompt_template_id": None}],
                }
            ),
            Decimal("5"),
        )
        assert estimate.estimate_basis == EstimateBasis.NO_HISTORY
        assert any("CLI default" in w for w in estimate.warnings)

    async def test_within_budget_flag(self, db_session):
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo)
        await add_usage(db_session, run.id, cost="1.00", model="m0")

        under = await svc.estimate_matrix(db_session, spec(repeat=2), Decimal("5"))
        over = await svc.estimate_matrix(db_session, spec(repeat=2), Decimal("1"))

        assert under.within_budget is True
        assert over.within_budget is False

    async def test_estimate_reports_the_matrix_shape(self, db_session):
        estimate = await svc.estimate_matrix(
            db_session, spec(models=2, prompts=3, repeat=2), Decimal("5")
        )
        assert (estimate.cells, estimate.models, estimate.prompts, estimate.repeat) == (
            12,
            2,
            3,
            2,
        )
        assert estimate.runs == 12
        assert len(estimate.per_variant) == 6

    async def test_budget_enforced_at_dispatch_is_always_echoed(self, db_session):
        """A client must not be able to read 'no estimate' as 'no cap'."""
        estimate = await svc.estimate_matrix(db_session, spec(), Decimal("5"))
        assert estimate.budget_enforced_at_dispatch is True

    async def test_money_is_a_string_on_the_wire(self, db_session):
        estimate = await svc.estimate_matrix(db_session, spec(), Decimal("5"))
        assert isinstance(estimate.estimated_cost_usd, str)
        assert isinstance(estimate.budget_usd, str)


class TestPushTriggerWarning:
    async def test_push_branches_names_the_pipeline_that_would_fire(self, db_session):
        """A push trigger with no branches: pattern matches EVERY branch."""
        repo = await make_repo(db_session)
        db_session.add(
            Pipeline(
                id=str(uuid4()), repo_id=repo.id, name="Test Suite", steps="[]",
                triggers=json.dumps([{"type": "push", "config": {}}]),
            )
        )
        await db_session.commit()

        estimate = await svc.estimate_matrix(
            db_session, spec(repeat=12), Decimal("50"),
            repo_id=repo.id, push_branches=True,
        )

        warning = next(w for w in estimate.warnings if "push_branches=true" in w)
        assert "Test Suite" in warning
        assert "every branch" in warning
        assert "12 additional runs" in warning

    async def test_no_warning_when_push_branches_is_off(self, db_session):
        repo = await make_repo(db_session)
        db_session.add(
            Pipeline(
                id=str(uuid4()), repo_id=repo.id, name="Test Suite", steps="[]",
                triggers=json.dumps([{"type": "push", "config": {}}]),
            )
        )
        await db_session.commit()

        estimate = await svc.estimate_matrix(
            db_session, spec(), Decimal("5"), repo_id=repo.id, push_branches=False
        )
        assert not any("push_branches" in w for w in estimate.warnings)

    async def test_patterned_trigger_names_its_patterns(self, db_session):
        repo = await make_repo(db_session)
        db_session.add(
            Pipeline(
                id=str(uuid4()), repo_id=repo.id, name="Release", steps="[]",
                triggers=json.dumps(
                    [{"type": "push", "config": {"branches": ["main", "release/*"]}}]
                ),
            )
        )
        await db_session.commit()

        estimate = await svc.estimate_matrix(
            db_session, spec(), Decimal("5"), repo_id=repo.id, push_branches=True
        )
        warning = next(w for w in estimate.warnings if "push_branches=true" in w)
        assert "release/*" in warning

    async def test_disabled_trigger_is_not_warned_about(self, db_session):
        repo = await make_repo(db_session)
        db_session.add(
            Pipeline(
                id=str(uuid4()), repo_id=repo.id, name="Off", steps="[]",
                triggers=json.dumps([{"type": "push", "enabled": False, "config": {}}]),
            )
        )
        await db_session.commit()

        estimate = await svc.estimate_matrix(
            db_session, spec(), Decimal("5"), repo_id=repo.id, push_branches=True
        )
        assert not any("push_branches" in w for w in estimate.warnings)


# -----------------------------------------------------------------------------
# The cap
# -----------------------------------------------------------------------------

class TestCap:
    async def test_cap_stops_dispatch_and_refuses_the_rest_in_one_pass(
        self, db_session, monkeypatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="1.00", concurrency=1, models=5
        )

        async def _spendy(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="0.60")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _spendy)
        await svc.launch(db_session, experiment)

        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.PASSED.value] == 2      # 0.60 + 0.60
        assert counts[ExperimentRunStatus.SKIPPED_BUDGET.value] == 3
        assert ExperimentRunStatus.PENDING.value not in counts

        await db_session.refresh(experiment)
        assert experiment.status == ExperimentStatus.BUDGET_EXHAUSTED.value

    async def test_refused_cells_say_why(self, db_session, monkeypatch):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="0.50", concurrency=1, models=3
        )

        async def _spendy(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="0.90")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _spendy)
        await svc.launch(db_session, experiment)

        skipped = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status == ExperimentRunStatus.SKIPPED_BUDGET.value,
                )
            )
        ).scalars().first()
        assert "budget cap reached" in skipped.error
        assert "0.900000" in skipped.error
        assert "0.500000" in skipped.error

    async def test_overshoot_is_recorded_not_absorbed(self, db_session, monkeypatch):
        """The cap bounds DISPATCH. Whatever was in flight when it tripped is
        written to budget_overrun_usd and rendered next to the cap."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="1.00", concurrency=1, models=2
        )

        async def _spendy(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="2.50")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _spendy)
        await svc.launch(db_session, experiment)

        await db_session.refresh(experiment)
        assert experiment.budget_overrun_usd == Decimal("1.500000")

    async def test_no_overrun_when_the_matrix_stayed_under(
        self, db_session, monkeypatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="10.00", concurrency=1, models=2
        )

        async def _cheap(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="0.01")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _cheap)
        await svc.launch(db_session, experiment)

        await db_session.refresh(experiment)
        assert experiment.budget_overrun_usd == Decimal("0")
        assert experiment.status == ExperimentStatus.COMPLETE.value

    async def test_unknown_cost_source_counts_as_zero_against_the_cap(
        self, db_session, monkeypatch
    ):
        """Unenforceable spend does not stop dispatch - and that is exactly
        why cost_coverage is reported on every cell."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="1.00", concurrency=1, models=3
        )

        async def _unpriced(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="99.00", source="unknown")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _unpriced)
        await svc.launch(db_session, experiment)

        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.PASSED.value] == 3
        assert ExperimentRunStatus.SKIPPED_BUDGET.value not in counts

        _, spend, coverage = await svc.experiment_progress(db_session, experiment.id)
        assert spend == Decimal("0")
        assert coverage == 0.0, "coverage 0 is how the UI learns the cap is blind"

    async def test_no_history_does_not_disable_the_cap(self, db_session, monkeypatch):
        """The estimate is advisory; enforcement runs off observed spend, so
        an unpriceable model is still stopped once real dollars land."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="0.10", concurrency=1, models=4
        )
        estimate = await svc.estimate_matrix(
            db_session, spec(models=4), Decimal("0.10")
        )
        assert estimate.estimate_basis == EstimateBasis.NO_HISTORY

        async def _spendy(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await add_usage(db, run.id, cost="0.50")
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _spendy)
        await svc.launch(db_session, experiment)

        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.SKIPPED_BUDGET.value] == 3

    async def test_progress_reports_spend_and_coverage(self, db_session, monkeypatch):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, budget="10.00", concurrency=2, models=2
        )

        async def _mixed(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            source = "cli-reported" if cell.cell_index == 0 else "unknown"
            await add_usage(db, run.id, cost="0.25", source=source)
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _mixed)
        await svc.launch(db_session, experiment)

        by_status, spend, coverage = await svc.experiment_progress(
            db_session, experiment.id
        )
        assert by_status == {ExperimentRunStatus.PASSED.value: 2}
        assert spend == Decimal("0.25")
        assert coverage == 0.5
