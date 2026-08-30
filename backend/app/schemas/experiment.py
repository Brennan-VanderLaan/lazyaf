"""
Pydantic schemas for the experiment layer (Phase 12.6.5).

THIS FILE IS THE SOURCE OF TRUTH for the experiment wire shapes (R3);
``frontend/src/lib/api/types.ts`` mirrors it field-for-field.

Money follows the 12.5 convention (``docs/milestone-13/api-surface.md`` s0):
``Decimal`` in Python and in the DB, STRING on the wire. ``money()`` from
``app/schemas/usage.py`` is the single formatter, reused here rather than
re-spelled, so a dollar renders identically in the usage rollup and on the
leaderboard.

Validation lives here (models store plain strings, the Card/spec idiom).
Every refusal names the offending value: a 422 that says "invalid matrix" is
a 422 nobody can act on.
"""
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.experiment import (
    EXPERIMENT_MAX_CELLS,
    EXPERIMENT_MAX_CONCURRENCY,
    DEFAULT_CELL_TIMEOUT,
    DEFAULT_MAX_CONCURRENCY,
    EstimateBasis,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null
from app.schemas._strings import Body, Name
from app.schemas.usage import money

__all__ = [
    "AGENT_VOCABULARY",
    "RESERVED_STEP_CONFIG_KEYS",
    "NOT_RANKED_NOTE",
    "MatrixModelEntry",
    "MatrixPromptEntry",
    "MatrixSpec",
    "VerifySpec",
    "ExperimentCreate",
    "ExperimentUpdate",
    "ExperimentRead",
    "ExperimentCellRead",
    "ExperimentDetail",
    "VariantEstimate",
    "EstimateResponse",
    "LaunchResponse",
    "AbortResponse",
    "ResumeResponse",
    "CriterionRate",
    "VariantRow",
    "LeaderboardResponse",
    "money",
]


# The agent vocabulary is agent_run.AGENT_BY_RUNNER_TYPE's VALUES — the CLIs
# a step can actually name. Imported lazily-by-value rather than re-spelled so
# the vocabularies cannot fork (R3).
def _agent_vocabulary() -> tuple[str, ...]:
    from app.services.agent_run import AGENT_BY_RUNNER_TYPE

    return tuple(sorted(set(AGENT_BY_RUNNER_TYPE.values())))


AGENT_VOCABULARY = _agent_vocabulary()

# A per-axis step_config overlay may not rewrite the axis it is varying, nor
# the branch/commit machinery the cell depends on. Dropping such a key
# silently is the definition of dark (R1); naming it in a 422 is not.
#
# This is a SUPERSET of the design's list, on purpose. `build_agent_step_config`
# already drops every key in its own `_RESERVED_STEP_CONFIG_KEYS` from an
# `extra` overlay, so anything in that set which is NOT refused here would
# vanish silently between the matrix and the container - the exact failure the
# refusal exists to prevent. `mock_config` is the one member of that set which
# is legal in an overlay: the experiment service pops it and passes it through
# `build_agent_step_config`'s named parameter, so it genuinely arrives.
# `test_reserved_set_covers_every_key_the_builder_would_drop` pins the
# relationship rather than trusting this comment.
RESERVED_STEP_CONFIG_KEYS = frozenset(
    {
        # the axes the matrix itself varies
        "agent",
        "model",
        "prompt_template",
        # the branch/commit machinery a cell depends on
        "base_branch",
        "branch",
        "commit",
        "card_id",
        # the task text, which comes from the experiment's TARGET - varying it
        # per-cell would make two cells solve different problems under one label
        "task",
        "title",
        "description",
        "agent_file_ids",
    }
)

# Part of the API contract, asserted verbatim by the metrics tests and by the
# Playwright spec. 12.6.5 REPORTS; it does not rank.
NOT_RANKED_NOTE = (
    "Reported, not ranked. Ranking requires the paired cluster bootstrap and "
    "the separability rule (Milestone 13.4, "
    "docs/milestone-13/phase-specs-and-metrics.md Part 2). Sort the table if "
    "you like; the platform makes no claim that one variant beats another."
)


# -----------------------------------------------------------------------------
# Matrix
# -----------------------------------------------------------------------------

class MatrixModelEntry(BaseModel):
    """One model-axis point.

    ``agent`` is REQUIRED and there is no string sugar: inferring a CLI from a
    model name would be a silent fallback (R1), and a guessed agent is
    unfalsifiable once the run is over. ``model: null`` is legal and means
    "the CLI's own default" — a real control variant.
    """

    agent: str
    model: str | None = None
    label: str | None = None
    step_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent")
    @classmethod
    def _known_agent(cls, value: str) -> str:
        if value not in AGENT_VOCABULARY:
            raise ValueError(
                f"unknown agent {value!r}: valid agents are "
                + ", ".join(AGENT_VOCABULARY)
            )
        return value

    @field_validator("step_config")
    @classmethod
    def _no_reserved_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_reserved(value)


class MatrixPromptEntry(BaseModel):
    """One prompt-axis point.

    ``prompt_template_id: null`` is legal and means the platform default
    prompt (``agent_prompt.DEFAULT_PROMPT_TEMPLATE``) — the other control
    variant.
    """

    prompt_template_id: str | None = None
    label: str | None = None
    step_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("step_config")
    @classmethod
    def _no_reserved_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_reserved(value)


def _reject_reserved(value: dict[str, Any]) -> dict[str, Any]:
    offending = sorted(set(value) & RESERVED_STEP_CONFIG_KEYS)
    if offending:
        raise ValueError(
            "step_config may not set "
            + ", ".join(repr(key) for key in offending)
            + " — those are the axes the matrix itself varies (and the branch "
            "machinery a cell depends on). Vary them through the matrix axes."
        )
    return value


class MatrixSpec(BaseModel):
    models: list[MatrixModelEntry]
    prompts: list[MatrixPromptEntry]
    repeat: int = 1

    @model_validator(mode="after")
    def _check(self) -> "MatrixSpec":
        if not self.models:
            raise ValueError("matrix.models must contain at least one entry")
        if not self.prompts:
            raise ValueError("matrix.prompts must contain at least one entry")
        if self.repeat < 1:
            raise ValueError(f"matrix.repeat must be >= 1, got {self.repeat}")
        cells = len(self.models) * len(self.prompts) * self.repeat
        if cells > EXPERIMENT_MAX_CELLS:
            raise ValueError(
                f"matrix would create {cells} cells "
                f"({len(self.models)} models x {len(self.prompts)} prompts x "
                f"{self.repeat} repeats); the maximum is {EXPERIMENT_MAX_CELLS}"
            )
        return self

    @property
    def cells(self) -> int:
        return len(self.models) * len(self.prompts) * self.repeat


class VerifySpec(BaseModel):
    """The optional post-agent script step that runs the target's suite.

    ``verify: null`` is legal and means the only test evidence is whatever the
    agent itself shipped. A variant with no ``TestRun`` rows renders
    ``pass_rate: null`` with a reason — never ``0%``.
    """

    image: str
    command: str
    timeout: int = 900

    @field_validator("image", "command")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("verify.image and verify.command must be non-empty")
        return value

    @field_validator("timeout")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"verify.timeout must be > 0, got {value}")
        return value


# -----------------------------------------------------------------------------
# Experiment CRUD
# -----------------------------------------------------------------------------

class ExperimentCreate(BaseModel):
    name: Name
    description: Body = ""
    target_type: Literal["card", "user_story"]
    target_id: str
    # Required for a user_story target (a story spans repos; guessing one is
    # dark). Derived from the card for a card target.
    repo_id: str | None = None
    matrix: MatrixSpec
    verify: VerifySpec | None = None
    # HARD cap, required. A cap that can be omitted is not a cap.
    budget_usd: Decimal
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    cell_timeout: int = DEFAULT_CELL_TIMEOUT
    push_branches: bool = False
    created_by: str | None = None
    # True -> price the matrix and return the estimate; create NOTHING.
    dry_run: bool = False

    @field_validator("name")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @field_validator("budget_usd")
    @classmethod
    def _positive_budget(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError(
                f"budget_usd must be > 0, got {value} — an experiment without a "
                "cap is an unbounded bill"
            )
        return value

    @field_validator("max_concurrency")
    @classmethod
    def _concurrency(cls, value: int) -> int:
        if not 1 <= value <= EXPERIMENT_MAX_CONCURRENCY:
            raise ValueError(
                f"max_concurrency must be between 1 and "
                f"{EXPERIMENT_MAX_CONCURRENCY}, got {value}"
            )
        return value

    @field_validator("cell_timeout")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"cell_timeout must be > 0, got {value}")
        return value


class ExperimentUpdate(BaseModel):
    name: Name | None = None
    description: Body | None = None
    budget_usd: Decimal | None = None
    max_concurrency: int | None = None
    cell_timeout: int | None = None
    push_branches: bool | None = None
    # Only editable while the experiment is a draft: a launched matrix is the
    # frozen record of what ran.
    matrix: MatrixSpec | None = None
    verify: VerifySpec | None = None

    @field_validator("budget_usd")
    @classmethod
    def _positive_budget(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError(f"budget_usd must be > 0, got {value}")
        return value

    @field_validator("max_concurrency")
    @classmethod
    def _concurrency(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= EXPERIMENT_MAX_CONCURRENCY:
            raise ValueError(
                f"max_concurrency must be between 1 and "
                f"{EXPERIMENT_MAX_CONCURRENCY}, got {value}"
            )
        return value

    # experiments.verify is nullable (null clears the verify spec); every
    # other column patched here is NOT NULL, so null is a 422, not a 500.
    _reject_nulls = not_null(
        "name",
        "description",
        "budget_usd",
        "max_concurrency",
        "cell_timeout",
        "push_branches",
        "matrix",
    )


class ExperimentRead(BaseModel):
    id: str
    name: str
    description: str
    target_type: str
    target_id: str
    repo_id: str
    matrix: MatrixSpec | None = None
    verify: VerifySpec | None = None
    budget_usd: str
    max_concurrency: int
    cell_timeout: int
    push_branches: bool
    status: ExperimentStatus
    estimated_cost_usd: str | None = None
    estimate_basis: EstimateBasis | None = None
    budget_overrun_usd: str
    created_by: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    launched_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None

    # Progress, computed from the cells (never a materialized column - R3).
    cells_total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    spend_usd: str = "0.000000"
    cost_coverage: float | None = None
    # True when the pump is gone but work remains: status is running, no cell
    # is live, and cells are still pending. REPORTED, never hidden — a
    # backend restart must not leave a matrix silently frozen.
    stalled: bool = False


class ExperimentCellRead(BaseModel):
    id: str
    experiment_id: str
    cell_index: int
    variant_index: int
    agent: str
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
    label: str | None = None
    repeat_index: int
    pipeline_run_id: str | None = None
    status: ExperimentRunStatus
    error: str | None = None
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    created_at: UTCDateTime
    # From StepUsage / TestRun — the sources of truth, joined per read.
    cost_usd: str | None = None
    cost_coverage: float | None = None
    wall_clock_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0


class ExperimentDetail(ExperimentRead):
    cells: list[ExperimentCellRead] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Estimate / dry run
# -----------------------------------------------------------------------------

class VariantEstimate(BaseModel):
    variant_index: int
    label: str
    agent: str
    model: str | None = None
    prompt_template_id: str | None = None
    runs: int
    # A string, always. An unpriced variant carries "0.000000" ONLY alongside
    # basis "no-history", and the warnings name it — a missing estimate must
    # never read as a real $0.00.
    estimate_usd: str
    basis: EstimateBasis
    samples: int


class EstimateResponse(BaseModel):
    cells: int
    models: int
    prompts: int
    repeat: int
    runs: int
    estimated_cost_usd: str
    estimate_basis: EstimateBasis
    per_variant: list[VariantEstimate]
    budget_usd: str
    within_budget: bool
    # Enforcement runs off OBSERVED StepUsage, so an unpriceable model is
    # still stopped once real dollars land. Always true; echoed so a client
    # cannot read "no estimate" as "no cap".
    budget_enforced_at_dispatch: bool = True
    warnings: list[str] = Field(default_factory=list)


class LaunchResponse(BaseModel):
    id: str
    status: ExperimentStatus
    cells_created: int
    dispatched: int
    estimated_cost_usd: str
    estimate_basis: EstimateBasis
    warnings: list[str] = Field(default_factory=list)


class AbortResponse(BaseModel):
    id: str
    status: ExperimentStatus
    cancelled: int
    still_running: int


class ResumeResponse(BaseModel):
    id: str
    status: ExperimentStatus
    dispatched: int
    reset_dispatching: int


# -----------------------------------------------------------------------------
# Leaderboard
# -----------------------------------------------------------------------------

class CriterionRate(BaseModel):
    criterion_id: str | None = None
    criterion_text: str | None = None
    passed: int
    failed: int
    skipped: int
    # None, never 0.0, when the denominator is zero. `reason` says why.
    pass_rate: float | None = None
    reason: str | None = None


class VariantRow(BaseModel):
    variant_index: int
    label: str
    agent: str
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None

    cells_total: int
    cells_measured: int
    cells_errored: int
    cells_skipped_budget: int
    error_rate: float

    # MACRO average over criteria (equal weight per criterion) — the
    # headline. A micro (pooled) rate rides alongside as a footnote so one
    # criterion with 40 tests cannot own the number.
    pass_rate: float | None = None
    pass_rate_micro: float | None = None
    reason: str | None = None
    criteria: list[CriterionRate] = Field(default_factory=list)
    # TestRefs with no criterion link (including ingestion's auto-created
    # orphans). Bucketed, never dropped: tests that ran and nobody counted is
    # exactly the quiet hole R1 exists to prevent.
    unlinked_tests: CriterionRate | None = None

    cost_usd_total: str
    cost_usd_per_run_median: str | None = None
    cost_coverage: float | None = None
    wall_clock_ms_median: int | None = None
    input_tokens_total: int = 0
    output_tokens_total: int = 0

    insufficient_repeats: bool = False
    warnings: list[str] = Field(default_factory=list)


class LeaderboardResponse(BaseModel):
    experiment_id: str | None = None
    feature_id: str | None = None
    variants: list[VariantRow] = Field(default_factory=list)
    # 12.6.5 reports; 13.4 ranks. Both fields are part of the contract and
    # are asserted verbatim.
    ranked: Literal[False] = False
    note: str = NOT_RANKED_NOTE
    cost_coverage: float | None = None
    warnings: list[str] = Field(default_factory=list)
