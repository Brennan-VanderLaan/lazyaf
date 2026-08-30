"""
Unit tests for the test tie-back models (Phase 12.2.6, pinned contract #4).

TestRef / TestRun structure, enums, defaults, FKs and indexes — no I/O,
matching the unit-tier convention (table metadata + direct construction
only).
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import TestRef, TestRefStatus, TestRun, TestRunStatus


def _indexes(model) -> dict[str, tuple[tuple[str, ...], bool]]:
    """name -> (column names, unique) for the model's table."""
    return {
        index.name: (tuple(c.name for c in index.columns), bool(index.unique))
        for index in model.__table__.indexes
    }


class TestStatusEnums:
    def test_test_ref_status_values(self):
        """Contract #4: TestRef.status is 'active' | 'orphan'."""
        assert {s.value for s in TestRefStatus} == {"active", "orphan"}

    def test_test_run_status_values(self):
        """Contract #1: manifest statuses are passed|failed|skipped."""
        assert {s.value for s in TestRunStatus} == {"passed", "failed", "skipped"}

    def test_enums_are_string_enums(self):
        assert issubclass(TestRefStatus, str)
        assert issubclass(TestRunStatus, str)
        assert TestRefStatus.ORPHAN == "orphan"
        assert TestRunStatus.PASSED == "passed"


class TestTestRefModel:
    def test_table_name(self):
        assert TestRef.__tablename__ == "test_refs"

    def test_lazyaf_test_id_is_required_but_not_globally_unique(self):
        """Contract #1: identity is (repo_id, lazyaf_test_id). The id alone
        is NOT unique — two repos may declare the same marker string."""
        col = TestRef.__table__.c.lazyaf_test_id
        assert col.nullable is False
        assert not col.unique

    def test_identity_is_the_repo_id_pair(self):
        """The composite unique IS the identity, and its leading repo_id
        column doubles as the repo-scoped lookup index."""
        assert _indexes(TestRef)["ix_test_refs_repo_id_lazyaf_test_id"] == (
            ("repo_id", "lazyaf_test_id"),
            True,
        )

    def test_repo_id_is_required_fk(self):
        col = TestRef.__table__.c.repo_id
        assert col.nullable is False
        assert {fk.target_fullname for fk in col.foreign_keys} == {"repos.id"}

    def test_file_path_is_nullable(self):
        assert TestRef.__table__.c.file_path.nullable is True

    def test_criterion_id_is_nullable_indexed_fk(self):
        col = TestRef.__table__.c.criterion_id
        assert col.nullable is True
        assert col.index is True
        assert {fk.target_fullname for fk in col.foreign_keys} == {
            "acceptance_criteria.id"
        }

    def test_status_defaults_to_active(self):
        col = TestRef.__table__.c.status
        assert col.type.python_type is str
        assert col.default.arg == "active"

    def test_has_timestamps_with_onupdate(self):
        assert "created_at" in TestRef.__table__.c
        updated = TestRef.__table__.c.updated_at
        assert updated.onupdate is not None

    def test_construction(self):
        ref = TestRef(
            lazyaf_test_id="us1.push_triggers_pipeline",
            repo_id="r1",
            file_path="tdd/integration/api/test_pipeline_execution_api.py",
            criterion_id="ac1",
            status=TestRefStatus.ACTIVE.value,
        )
        assert ref.lazyaf_test_id == "us1.push_triggers_pipeline"
        assert ref.status == "active"

    def test_runs_relationship_cascades(self):
        rel = TestRef.runs.property
        assert rel.cascade.delete
        assert rel.cascade.delete_orphan


class TestTestRunModel:
    def test_table_name(self):
        assert TestRun.__tablename__ == "test_runs"

    def test_test_ref_id_is_required_fk(self):
        col = TestRun.__table__.c.test_ref_id
        assert col.nullable is False
        assert {fk.target_fullname for fk in col.foreign_keys} == {"test_refs.id"}

    def test_pipeline_run_id_is_required_plain_column(self):
        """Provenance survives pipeline-run pruning: NOT an FK. Nothing
        queries runs by it, so it carries no index either."""
        col = TestRun.__table__.c.pipeline_run_id
        assert col.nullable is False
        assert col.index is not True
        assert not col.foreign_keys

    def test_step_run_id_is_nullable(self):
        assert TestRun.__table__.c.step_run_id.nullable is True

    def test_indexes_serve_exactly_the_declared_access_paths(self):
        """(test_ref_id, created_at) for criterion history and the
        blocks-done freshness walk; step_run_id for the ingestion idempotency
        lookup; and since 12.6.5 (experiment_run_id, test_ref_id) for the
        leaderboard's per-criterion aggregation. No index on pipeline_run_id
        or created_at alone: nothing queries by them, and every extra index
        is write cost on the hot ingestion path."""
        assert _indexes(TestRun) == {
            "ix_test_runs_test_ref_id_created_at": (
                ("test_ref_id", "created_at"),
                False,
            ),
            "ix_test_runs_step_run_id": (("step_run_id",), False),
            "ix_test_runs_experiment_run_id_test_ref_id": (
                ("experiment_run_id", "test_ref_id"),
                False,
            ),
        }

    def test_commit_sha_required_branch_nullable(self):
        assert TestRun.__table__.c.commit_sha.nullable is False
        assert TestRun.__table__.c.branch.nullable is True

    def test_duration_and_experiment_context_nullable(self):
        """The experiment coordinates are nullable: NULL is the TRUE value on
        an ordinary CI run, which measured the repo rather than a variant."""
        assert TestRun.__table__.c.duration_ms.nullable is True
        assert TestRun.__table__.c.model.nullable is True
        col = TestRun.__table__.c.prompt_template_id
        assert col.nullable is True
        assert {fk.target_fullname for fk in col.foreign_keys} == {
            "prompt_templates.id"
        }

    def test_created_at_is_the_ordering_column_not_a_standalone_index(self):
        assert TestRun.__table__.c.created_at.index is not True

    def test_construction(self):
        run = TestRun(
            test_ref_id="tr1",
            pipeline_run_id="pr1",
            step_run_id="sr1",
            commit_sha="abc123",
            branch="main",
            status=TestRunStatus.PASSED.value,
            duration_ms=142,
        )
        assert run.status == "passed"
        assert run.duration_ms == 142
        assert run.model is None
