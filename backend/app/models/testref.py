"""
Test tie-back models (Phase 12.2.6).

TestRef: a stable, repo-scoped registration of a test by its declared
`lazyaf_test_id` (the `@pytest.mark.lazyaf_test_id("...")` marker string).
Identity is the PAIR (repo_id, lazyaf_test_id) — the same marker string in
two repos is two independent refs, so one repo's green can never satisfy
another repo's criterion.
Optionally joined to an AcceptanceCriterion — that join is what turns a spec
criterion into something measurable.

TestRun: one observed execution of a TestRef, joined to the pipeline run,
step run, commit and branch it ran under. Since Phase 12.6.5 a run that
belongs to an experiment cell also carries that cell's coordinates
(experiment_run_id / model / prompt_template_id / prompt_version), stamped
server-side from the cell row — NULL on every ordinary CI run, which is the
true value rather than a hole.

Statuses are plain string columns (Card/spec idiom); validation happens in
the pydantic schemas (app/schemas/testref.py).
"""
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TestRefStatus(str, Enum):
    __test__ = False  # names start with "Test": keep pytest collection away

    ACTIVE = "active"
    ORPHAN = "orphan"


class TestRunStatus(str, Enum):
    """Manifest result vocabulary (pinned contract #1)."""
    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TestRef(Base):
    __test__ = False  # not a pytest class (contract #4 table name)
    __tablename__ = "test_refs"
    __table_args__ = (
        # Identity is (repo_id, lazyaf_test_id), NOT the id alone: two repos
        # may declare the same marker string and must stay independent.
        # Leading repo_id also serves every repo-scoped lookup (ingestion,
        # reconcile, seeding), so no separate repo_id index is needed.
        Index(
            "ix_test_refs_repo_id_lazyaf_test_id",
            "repo_id",
            "lazyaf_test_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # Declared identifier (arbitrary string per PLAN 12.2.6 open question #1
    # — users adopt their own convention). Unique per repo, not globally.
    lazyaf_test_id: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("repos.id"), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    criterion_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("acceptance_criteria.id"), nullable=True, index=True
    )
    # 'active' | 'orphan' — orphan means auto-created by ingestion before
    # registration, or reconciled away (absent from the repo's declared set).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TestRefStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    runs: Mapped[list["TestRun"]] = relationship(
        "TestRun",
        back_populates="test_ref",
        cascade="all, delete-orphan",
    )


class TestRun(Base):
    __test__ = False  # not a pytest class (contract #4 table name)
    __tablename__ = "test_runs"
    __table_args__ = (
        # The two access paths, and nothing else: criterion history / the
        # blocks-done freshness check walk (test_ref_id, created_at); the
        # ingestion idempotency lookup is by step_run_id.
        Index("ix_test_runs_test_ref_id_created_at", "test_ref_id", "created_at"),
        Index("ix_test_runs_step_run_id", "step_run_id"),
        # Phase 12.6.5: the leaderboard's per-criterion aggregation walks
        # (experiment_run_id, test_ref_id). Mirrors M13's
        # test_runs (trial_iteration_id, test_ref_id).
        Index("ix_test_runs_experiment_run_id_test_ref_id", "experiment_run_id", "test_ref_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    test_ref_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("test_refs.id"), nullable=False
    )
    # Deliberately NOT an FK: runs are provenance records that must survive
    # pipeline-run pruning. Not indexed — nothing queries runs by it.
    pipeline_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Indexed: the ingestion idempotency key is (step_run_id, test_ref_id).
    step_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Empty string when the triggering context carried no sha (manual runs).
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Experiment context (Phase 12.6.5). NULL on ordinary CI runs — that is
    # the honest value, not a gap: those runs measured the repo, not a
    # variant. All four are DERIVED SERVER-SIDE from the experiment cell that
    # owns the run (app/services/test_ingestion.py), never trusted from the
    # wire: a container cannot mislabel which variant it was.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_template_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("prompt_templates.id"), nullable=True
    )
    # Deliberately NOT an FK, for the same reason pipeline_run_id is not:
    # runs are provenance records that must survive pruning. One link, not
    # two — every experiment-scoped read joins experiment_runs, which is
    # already indexed on (experiment_id, status) and (experiment_id, cell_index).
    experiment_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ordering column of the (test_ref_id, created_at) composite index.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    test_ref: Mapped["TestRef"] = relationship("TestRef", back_populates="runs")
