"""
Experiments: the matrix, its cells, and the prompt bodies that actually ran
(Phase 12.6.5).

An EXPERIMENT is a question ("does opus beat haiku on this story, and is the
new prompt worth it?") expressed as a MATRIX of models x prompts x repeats.
Each CELL of that matrix is one ad-hoc agent run — a real, visible
``PipelineRun`` created through ``app/services/agent_run.py``'s builders — so
a cell gets the workspace volume, StepRun, StepExecution + step JWT, control
mode, streamed logs, ``/test-results`` tie-back, ``/usage`` accounting, the
watchdog and cancellation with no second implementation of any of it.

Three deliberate scope decisions, restated here because a model file is where
they become load-bearing:

- **No ``cost_usd`` / ``tests_passed`` columns on ``experiment_runs``** (R3).
  ``StepUsage`` and ``TestRun`` are the ONLY sources of truth for money and
  for outcomes. A materialized copy is a second writer, and a second writer
  drifts. A matrix is capped at ``EXPERIMENT_MAX_CELLS`` so live aggregation
  is a few hundred indexed rows, not a scan.

- **``trigger_ref = ExperimentRun.id`` is the durable cell -> run link**, not
  a ``pipeline_runs.experiment_id`` column. ``pipeline_executor.start_pipeline``
  can complete a run SYNCHRONOUSLY (image-preflight failure, empty step list),
  so the link has to exist before the run starts; ``trigger_type`` /
  ``trigger_ref`` are written at run CREATION and a column set afterwards
  could not beat that race. ``experiment_runs.pipeline_run_id`` is a
  convenience mirror for the UI's "open this run" link and is load-bearing
  for nothing.

- **``PromptVersion`` exists because ``PromptTemplate`` has no versions.**
  A leaderboard that groups by (template, version, model) over a body the
  user can edit mid-experiment silently merges two different prompts into one
  row. ``PromptTemplate.content`` is the EDITABLE DRAFT (owned by
  ``routers/spec.py``); ``PromptVersion`` is the IMMUTABLE RECORD OF WHAT RAN,
  get-or-create by content hash, never updated. That is one source of truth
  for each of two different data, not two for one.

Vocabularies are plain string columns (the Card/spec/TestRef idiom);
validation happens in the pydantic schemas (``app/schemas/experiment.py``)
where ``Literal``/enum pins them so an unknown value is a 422 rather than a
silent partial parse.

Money is ``Numeric(18, 6)`` — ``Decimal`` in Python and in the DB, STRING on
the wire (the 12.5 convention, ``models/usage.py``). Dollars are never
floats, and totals are summed in Python rather than with SQL ``SUM()``,
because SQLite returns a float for ``SUM(NUMERIC)``.
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


#: ``PipelineRun.trigger_type`` for an experiment cell — the durable half of
#: the cell -> run link. It lives HERE, on the leaf module that the service,
#: the ingestion path and the completion hook all already import, so the
#: several places that must agree on this string cannot fork (R3).
TRIGGER_EXPERIMENT = "experiment"

# A matrix is bounded. 200 cells at, say, $0.50 each is $100 of agent time
# from one button; the number exists so the refusal is a 422 naming the count
# rather than a bill naming it.
EXPERIMENT_MAX_CELLS = 200

# Concurrency ceiling. The cap bounds DISPATCH, so the maximum budget
# overshoot is whatever this many in-flight cells cost.
EXPERIMENT_MAX_CONCURRENCY = 8

DEFAULT_CELL_TIMEOUT = 1800
DEFAULT_MAX_CONCURRENCY = 2


class ExperimentStatus(str, Enum):
    DRAFT = "draft"                        # created; matrix editable, nothing dispatched
    RUNNING = "running"
    COMPLETE = "complete"                  # every cell terminal
    ABORTED = "aborted"
    BUDGET_EXHAUSTED = "budget_exhausted"  # the cap stopped dispatch with cells left


#: Statuses from which no further dispatch happens.
TERMINAL_EXPERIMENT_STATUSES = frozenset(
    {
        ExperimentStatus.COMPLETE.value,
        ExperimentStatus.ABORTED.value,
        ExperimentStatus.BUDGET_EXHAUSTED.value,
    }
)


class ExperimentRunStatus(str, Enum):
    """Cell outcome vocabulary.

    ``FAILED`` and ``ERROR`` are DIFFERENT FACTS and the schema keeps them
    different: "the suite came back red" is a measurement (an unsolved
    trial), "nothing was ever measured" is an infrastructure event. Only the
    first belongs in a pass-rate denominator; the second is counted in
    ``error_rate`` and printed. That is
    ``docs/milestone-13/phase-specs-and-metrics.md``'s error rule implemented
    at the one point where it is cheap to get right.
    """

    PENDING = "pending"
    DISPATCHING = "dispatching"      # CAS-claimed, run not created yet
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"                # ran, measured, did not satisfy the suite
    ERROR = "error"                  # ran, measured NOTHING
    CANCELLED = "cancelled"          # aborted before dispatch
    SKIPPED_BUDGET = "skipped_budget"


#: A cell in one of these is still occupying concurrency.
LIVE_CELL_STATUSES = frozenset(
    {ExperimentRunStatus.DISPATCHING.value, ExperimentRunStatus.RUNNING.value}
)

#: A cell in one of these will never change again.
TERMINAL_CELL_STATUSES = frozenset(
    {
        ExperimentRunStatus.PASSED.value,
        ExperimentRunStatus.FAILED.value,
        ExperimentRunStatus.ERROR.value,
        ExperimentRunStatus.CANCELLED.value,
        ExperimentRunStatus.SKIPPED_BUDGET.value,
    }
)

#: Cells that produced a MEASUREMENT. Only these enter pass-rate denominators.
MEASURED_CELL_STATUSES = frozenset(
    {ExperimentRunStatus.PASSED.value, ExperimentRunStatus.FAILED.value}
)


class EstimateBasis(str, Enum):
    """How an estimate was arrived at — never hidden behind a bare number.

    There is no price table (owner decision 2026-08-29: while the CLIs report
    cost, a second pricing table is a second source of truth that will
    drift). Estimates come from the MEDIAN of real, priced ``StepUsage``
    history for the same model. With no history the variant contributes
    NOTHING and the basis degrades — the number is then explicitly a LOWER
    BOUND and says so, rather than reading as ``$0.00``.
    """

    HISTORICAL_MEDIAN = "historical-median"  # every variant priced from history
    PARTIAL = "partial"                      # some variants had no history
    NO_HISTORY = "no-history"                # none did; the estimate is 0 and means nothing


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        # The list endpoint's scan.
        Index("ix_experiments_status_created_at", "status", "created_at"),
        # "which experiments ran against this card / story?"
        Index("ix_experiments_target_type_target_id", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # "card" | "user_story". "feature" is refused at the API (a feature spans
    # repos and has no single task text) — see schemas/experiment.py.
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Deliberately NOT an FK: an experiment's provenance — what it measured,
    # what it cost — must survive its target being deleted. Same reasoning as
    # TestRun.pipeline_run_id.
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Resolved at CREATE: every cell needs a repo to clone.
    repo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("repos.id"), nullable=False
    )

    # JSON, FROZEN at launch. {"models": [...], "prompts": [...], "repeat": N}
    matrix: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON {image, command, timeout} or NULL. NULL means the only test
    # evidence is whatever the agent itself shipped.
    verify: Mapped[str | None] = mapped_column(Text, nullable=True)

    # HARD cap, required, > 0. A cap that can be omitted is not a cap.
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_CONCURRENCY
    )
    cell_timeout: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CELL_TIMEOUT
    )
    # False by default: a push-triggered pipeline that declares no `branches:`
    # pattern matches EVERY branch (trigger_service.on_push), so a 20-cell
    # matrix pushing 20 branches would start 20 CI runs this experiment's cap
    # neither covers nor estimated.
    push_branches: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExperimentStatus.DRAFT.value
    )
    # What the dry run said AT LAUNCH, and on what basis. Kept so the estimate
    # can be compared with what actually happened.
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    estimate_basis: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # The cap bounds DISPATCH; spend already in flight when it trips is
    # RECORDED here rather than quietly absorbed (M13's Trial.budget_overrun_usd
    # contract, one phase early).
    budget_overrun_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    launched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cells: Mapped[list["ExperimentRun"]] = relationship(
        "ExperimentRun",
        back_populates="experiment",
        cascade="all, delete-orphan",
    )


class ExperimentRun(Base):
    """One cell of the matrix: one set of coordinates, one agent run."""

    __tablename__ = "experiment_runs"
    __table_args__ = (
        # The board's primary scan AND the pump's "next pending cell" read.
        # UNIQUE because cell_index is the deterministic identity of a cell
        # within its experiment.
        Index(
            "ix_experiment_runs_experiment_id_cell_index",
            "experiment_id",
            "cell_index",
            unique=True,
        ),
        # The pump's live-cell count and the budget scan.
        Index("ix_experiment_runs_experiment_id_status", "experiment_id", "status"),
        # Reverse lookup from a run-detail view, and the StepUsage join.
        # NOT unique: a future retry lane would break a unique constraint here
        # and gains nothing today.
        Index("ix_experiment_runs_pipeline_run_id", "pipeline_run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    experiment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Deterministic: ((model_i * n_prompts) + prompt_i) * repeat + repeat_i.
    cell_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # cell_index // repeat. Repeats of one variant share it, which turns the
    # leaderboard's grouping into an integer comparison instead of a
    # four-column tuple match.
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Frozen coordinates. These are what the leaderboard groups by, and what
    # the ingestion stamp copies onto every TestRun of the cell's run.
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    # NULL = the CLI's own default model. A real control variant, not a gap.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # NULL = the platform default prompt (agent_prompt.DEFAULT_PROMPT_TEMPLATE).
    prompt_template_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("prompt_templates.id"), nullable=True
    )
    prompt_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("prompt_versions.id"), nullable=True
    )
    # Denormalized int, frozen at launch: the leaderboard groups on it and
    # the TestRun stamp carries it, neither of which should need a join.
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repeat_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Convenience mirror only — the LINK is PipelineRun.trigger_ref == this
    # row's id, written at run creation (see the module docstring).
    pipeline_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExperimentRunStatus.PENDING.value
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    experiment: Mapped["Experiment"] = relationship(
        "Experiment", back_populates="cells"
    )


class PromptVersion(Base):
    """An immutable snapshot of a PromptTemplate body that actually ran.

    Get-or-create by ``(template_id, content_hash)``: relaunching an
    unchanged template reuses version N; editing it and relaunching yields
    N+1. Every version for a matrix is resolved BEFORE any cell dispatches,
    so a template edited mid-experiment cannot split one variant across two
    prompt bodies.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index(
            "ix_prompt_versions_template_id_version",
            "template_id",
            "version",
            unique=True,
        ),
        # The get-or-create lookup, and the constraint that makes "same body
        # twice" impossible rather than merely unlikely.
        Index(
            "ix_prompt_versions_template_id_content_hash",
            "template_id",
            "content_hash",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    template_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("prompt_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The FROZEN text that ran. Never updated.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
