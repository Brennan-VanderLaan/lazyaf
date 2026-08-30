"""
Unit tests for the experiment models (Phase 12.6.5).

Table names, index names/shapes, cascades, status vocabularies and the
cell-index arithmetic — no I/O, matching the unit-tier convention (table
metadata + direct construction only).

The index NAMES are pinned here because they are half of the migration
parity contract (tdd/integration/test_migrations.py snapshots columns AND
indexes): a rename on either side has to be a deliberate two-file change.
"""
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.experiment import (
    DEFAULT_CELL_TIMEOUT,
    DEFAULT_MAX_CONCURRENCY,
    EXPERIMENT_MAX_CELLS,
    EXPERIMENT_MAX_CONCURRENCY,
    LIVE_CELL_STATUSES,
    MEASURED_CELL_STATUSES,
    TERMINAL_CELL_STATUSES,
    TERMINAL_EXPERIMENT_STATUSES,
    EstimateBasis,
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
    PromptVersion,
)
from app.models.testref import TestRun


def _indexes(model) -> dict[str, tuple[tuple[str, ...], bool]]:
    return {
        index.name: (tuple(c.name for c in index.columns), bool(index.unique))
        for index in model.__table__.indexes
    }


def _column(model, name):
    return model.__table__.columns[name]


class TestVocabularies:
    def test_experiment_status_values(self):
        assert {s.value for s in ExperimentStatus} == {
            "draft",
            "running",
            "complete",
            "aborted",
            "budget_exhausted",
        }

    def test_cell_status_values(self):
        assert {s.value for s in ExperimentRunStatus} == {
            "pending",
            "dispatching",
            "running",
            "passed",
            "failed",
            "error",
            "cancelled",
            "skipped_budget",
        }

    def test_failed_and_error_are_distinct_members(self):
        """'the suite was red' and 'nothing was measured' are different facts.

        Collapsing them is what turns an infrastructure crash into a 0% score,
        which is the single thing this vocabulary exists to prevent."""
        assert ExperimentRunStatus.FAILED.value != ExperimentRunStatus.ERROR.value
        assert ExperimentRunStatus.FAILED.value in MEASURED_CELL_STATUSES
        assert ExperimentRunStatus.ERROR.value not in MEASURED_CELL_STATUSES

    def test_status_sets_are_consistent(self):
        every = {s.value for s in ExperimentRunStatus}
        assert LIVE_CELL_STATUSES < every
        assert TERMINAL_CELL_STATUSES < every
        # A cell is never both live and terminal.
        assert not (LIVE_CELL_STATUSES & TERMINAL_CELL_STATUSES)
        # Pending is neither: it has not started and it is not finished.
        assert "pending" not in LIVE_CELL_STATUSES
        assert "pending" not in TERMINAL_CELL_STATUSES
        assert MEASURED_CELL_STATUSES < TERMINAL_CELL_STATUSES

    def test_terminal_experiment_statuses(self):
        assert TERMINAL_EXPERIMENT_STATUSES == {
            "complete",
            "aborted",
            "budget_exhausted",
        }
        assert ExperimentStatus.RUNNING.value not in TERMINAL_EXPERIMENT_STATUSES
        assert ExperimentStatus.DRAFT.value not in TERMINAL_EXPERIMENT_STATUSES

    def test_estimate_basis_values(self):
        """The basis is part of the estimate: a bare number that might be a
        lower bound is worse than no number."""
        assert {b.value for b in EstimateBasis} == {
            "historical-median",
            "partial",
            "no-history",
        }

    def test_enums_are_string_enums(self):
        assert issubclass(ExperimentStatus, str)
        assert issubclass(ExperimentRunStatus, str)
        assert issubclass(EstimateBasis, str)
        assert ExperimentRunStatus.SKIPPED_BUDGET == "skipped_budget"


class TestExperimentModel:
    def test_table_name(self):
        assert Experiment.__tablename__ == "experiments"

    def test_indexes(self):
        assert _indexes(Experiment) == {
            "ix_experiments_status_created_at": (("status", "created_at"), False),
            "ix_experiments_target_type_target_id": (
                ("target_type", "target_id"),
                False,
            ),
        }

    def test_target_id_is_not_a_foreign_key(self):
        """Provenance must survive its target being deleted: what an
        experiment measured and what it cost outlive the card."""
        assert _column(Experiment, "target_id").foreign_keys == set()

    def test_repo_id_is_a_foreign_key(self):
        """Cells need a repo to clone; a dangling one would fail at dispatch."""
        fks = {fk.target_fullname for fk in _column(Experiment, "repo_id").foreign_keys}
        assert fks == {"repos.id"}

    def test_budget_is_required_numeric_not_float(self):
        column = _column(Experiment, "budget_usd")
        assert column.nullable is False, "a cap that can be omitted is not a cap"
        assert str(column.type) == "NUMERIC(18, 6)"

    def test_money_columns_are_numeric(self):
        for name in ("budget_usd", "estimated_cost_usd", "budget_overrun_usd"):
            assert str(_column(Experiment, name).type) == "NUMERIC(18, 6)", name

    def test_budget_overrun_is_not_nullable(self):
        """Overshoot is RECORDED, never absent: the cap bounds dispatch, and
        whatever was in flight when it tripped has to be visible."""
        assert _column(Experiment, "budget_overrun_usd").nullable is False

    def test_push_branches_defaults_false(self):
        """A push-triggered pipeline with no branches: pattern matches every
        branch, so pushing a 20-cell matrix would start 20 uncosted runs."""
        assert _column(Experiment, "push_branches").default.arg is False

    def test_no_materialized_cost_or_test_columns(self):
        """R3: StepUsage and TestRun are the only sources of truth for money
        and outcomes. A copy here would be a second writer."""
        names = set(Experiment.__table__.columns.keys())
        assert not (names & {"cost_usd", "spend_usd", "tests_passed", "tests_failed"})

    def test_defaults(self):
        assert _column(Experiment, "max_concurrency").default.arg == (
            DEFAULT_MAX_CONCURRENCY
        )
        assert _column(Experiment, "cell_timeout").default.arg == DEFAULT_CELL_TIMEOUT
        assert _column(Experiment, "status").default.arg == "draft"

    def test_limits_are_named_constants(self):
        assert EXPERIMENT_MAX_CELLS == 200
        assert EXPERIMENT_MAX_CONCURRENCY == 8


class TestExperimentRunModel:
    def test_table_name(self):
        assert ExperimentRun.__tablename__ == "experiment_runs"

    def test_indexes(self):
        assert _indexes(ExperimentRun) == {
            "ix_experiment_runs_experiment_id_cell_index": (
                ("experiment_id", "cell_index"),
                True,
            ),
            "ix_experiment_runs_experiment_id_status": (
                ("experiment_id", "status"),
                False,
            ),
            "ix_experiment_runs_pipeline_run_id": (("pipeline_run_id",), False),
        }

    def test_cell_index_is_unique_per_experiment(self):
        """cell_index IS the identity of a cell within its matrix."""
        assert _indexes(ExperimentRun)[
            "ix_experiment_runs_experiment_id_cell_index"
        ][1] is True

    def test_pipeline_run_index_is_not_unique(self):
        """A retry lane would add a second run per cell; a unique constraint
        here would break that and buys nothing today."""
        assert _indexes(ExperimentRun)["ix_experiment_runs_pipeline_run_id"][1] is False

    def test_cascade_from_experiment(self):
        fk = next(iter(_column(ExperimentRun, "experiment_id").foreign_keys))
        assert fk.target_fullname == "experiments.id"
        assert fk.ondelete == "CASCADE"

    def test_pipeline_run_id_is_not_a_foreign_key(self):
        """It is a convenience MIRROR. The durable link is
        PipelineRun.trigger_ref, written at run creation."""
        assert _column(ExperimentRun, "pipeline_run_id").foreign_keys == set()

    def test_model_and_prompt_template_are_nullable(self):
        """NULL model = the CLI's own default; NULL template = the platform
        default prompt. Both are real CONTROL variants, not gaps."""
        assert _column(ExperimentRun, "model").nullable is True
        assert _column(ExperimentRun, "prompt_template_id").nullable is True

    def test_no_materialized_cost_or_test_columns(self):
        names = set(ExperimentRun.__table__.columns.keys())
        assert not (
            names
            & {"cost_usd", "tests_passed", "tests_failed", "tests_skipped"}
        )

    def test_construction_defaults(self):
        cell = ExperimentRun(experiment_id="e1", cell_index=0, variant_index=0,
                             agent="mock")
        assert cell.model is None
        assert cell.pipeline_run_id is None
        # Column defaults are applied at flush, not construction.
        assert len(_column(ExperimentRun, "id").default.arg(None)) == 36
        assert _column(ExperimentRun, "status").default.arg == "pending"
        assert _column(ExperimentRun, "repeat_index").default.arg == 0


class TestPromptVersionModel:
    def test_table_name(self):
        assert PromptVersion.__tablename__ == "prompt_versions"

    def test_indexes(self):
        assert _indexes(PromptVersion) == {
            "ix_prompt_versions_template_id_version": (
                ("template_id", "version"),
                True,
            ),
            "ix_prompt_versions_template_id_content_hash": (
                ("template_id", "content_hash"),
                True,
            ),
        }

    def test_both_indexes_are_unique(self):
        """(template, version) is the numbering constraint; (template, hash)
        is what makes get-or-create a constraint rather than a hope."""
        for shape in _indexes(PromptVersion).values():
            assert shape[1] is True

    def test_cascade_from_template(self):
        fk = next(iter(_column(PromptVersion, "template_id").foreign_keys))
        assert fk.target_fullname == "prompt_templates.id"
        assert fk.ondelete == "CASCADE"

    def test_body_is_required(self):
        """The frozen text is the whole point of the row."""
        assert _column(PromptVersion, "body").nullable is False
        assert _column(PromptVersion, "content_hash").nullable is False


class TestTestRunExperimentColumns:
    """1.4: the two columns and one index 12.6.5 adds to test_runs."""

    def test_columns_exist_and_are_nullable(self):
        """NULL on ordinary CI runs is the TRUE value: those runs measured the
        repo, not a variant."""
        assert _column(TestRun, "experiment_run_id").nullable is True
        assert _column(TestRun, "prompt_version").nullable is True

    def test_experiment_run_id_is_not_a_foreign_key(self):
        """Same reason pipeline_run_id is not: runs are provenance records
        that must survive pruning."""
        assert _column(TestRun, "experiment_run_id").foreign_keys == set()

    def test_no_experiment_id_column(self):
        """One link, not two. Every experiment-scoped read joins
        experiment_runs, which is already indexed for it."""
        assert "experiment_id" not in TestRun.__table__.columns

    def test_aggregation_index(self):
        assert _indexes(TestRun)["ix_test_runs_experiment_run_id_test_ref_id"] == (
            ("experiment_run_id", "test_ref_id"),
            False,
        )


class TestCellIndexArithmetic:
    """cell_index / variant_index are part of the API contract: the grid
    renders straight off them and the leaderboard groups on variant_index."""

    @pytest.mark.parametrize(
        "models,prompts,repeat,expected",
        [(1, 1, 1, 1), (2, 2, 3, 12), (3, 4, 1, 12), (2, 1, 5, 10)],
    )
    def test_cell_count(self, models, prompts, repeat, expected):
        assert models * prompts * repeat == expected

    def test_variant_index_is_cell_index_floordiv_repeat(self):
        repeat = 3
        for model_i in range(2):
            for prompt_i in range(2):
                variant = model_i * 2 + prompt_i
                for repeat_i in range(repeat):
                    cell_index = variant * repeat + repeat_i
                    assert cell_index // repeat == variant
