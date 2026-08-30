"""
StepUsage — the control-layer protocol's fourth channel (Phase 12.5).

One row per StepExecution, recording what that step COST: tokens, dollars,
wall clock, container occupancy, and (from Milestone 13) which strategy role
the step was playing. Written by `POST /api/steps/{id}/usage`, which the
control runtime calls after the command exits — the same Bearer step token,
the same terminal-write rejection, as /logs and /test-results.

Why it ships in 12.5 and not in M13 (docs/milestone-13/api-surface.md
section 2, BINDING): the control-layer protocol freezes with the agent-step
migration. Adding a channel afterwards is a retrofit against a frozen
protocol — exactly what 12.2.6 documents the cost of.

Two deliberate scope decisions, both stated in the design:

- `role` IS here, NULL everywhere in 12.5. It is on the frozen wire NOW
  because `cost_by_role` — the number that tests the "expensive planner,
  cheap workers" hypothesis — is unrecoverable after the fact.
- `trial_iteration_id` is deliberately NOT here. Nothing writes it and there
  is no table to reference; an orphan column buys nothing. It lands with
  M13's trials table.

Vocabularies are plain string columns (Card/spec/TestRef idiom); validation
happens in the pydantic schemas (app/schemas/usage.py) where `Literal` pins
them so an unknown value is a 422 rather than a silent partial parse.

Money is `Numeric(18, 6)`: SQLite stores it as REAL, and the ingestion
service quantizes to 6dp on write, so a value round-trips exactly (float64
carries 15-16 significant digits — summing thousands of sub-dollar rows at
6dp is exact). Dollars are NEVER floats in Python and NEVER floats on the
wire; they are `Decimal` here and strings in the schemas.
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UsageProvider(str, Enum):
    """Who served the tokens (api-surface 2.2)."""

    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENAI_COMPATIBLE = "openai-compatible"
    SELF_HOSTED = "self-hosted"


class UsageCostSource(str, Enum):
    """How `cost_usd` was arrived at (api-surface 2.2 / 2.5).

    `unknown` is a RECORDED FACT — "the provider told us nothing" — not a
    gap: the board counts those rows as `cost_coverage < 1.0` and warns,
    rather than reporting a quietly-too-cheap median.

    `estimated` stays in the vocabulary for a future price-table backfill
    and is written by nothing today (owner decision 2026-08-29: while the
    CLIs report cost, a second pricing table is a second source of truth
    that will drift).
    """

    CLI_REPORTED = "cli-reported"
    GPU_NODE = "gpu-node"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class StepUsage(Base):
    __tablename__ = "step_usages"
    __table_args__ = (
        # Idempotency key: a retrying runtime UPDATES, never double-bills.
        Index("ix_step_usages_step_execution_id", "step_execution_id", unique=True),
        # The run rollup is read-heavy and groups by role (api-surface s6).
        Index("ix_step_usages_pipeline_run_id_role", "pipeline_run_id", "role"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    # Identity: one usage row per execution attempt. UNIQUE (see __table_args__)
    # — that constraint IS the "a retry must not double-bill" rule.
    step_execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("step_executions.id"), nullable=False
    )
    # Derived server-side from the StepExecution -> StepRun -> PipelineRun
    # chain; never trusted from the wire.
    step_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("step_runs.id"), nullable=True
    )
    # DENORMALIZED on purpose: the per-run rollup is read-heavy and must not
    # join to reach the run. Not an FK, for the same reason TestRun's is not
    # — usage rows are accounting provenance that outlive run pruning.
    pipeline_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Provenance: the exact version string, not the family.
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Dollars. NULL is legal and means "not priced" (see cost_source).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(16), nullable=False)

    # Timing is owned by the control runtime (run.py), the one component
    # present for script steps too, so there is exactly one timing writer.
    wall_clock_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # LOWER BOUND: measured from run.py process start to the usage POST, so
    # it excludes image pull and the entrypoint chown.
    container_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Self-hosted occupancy pricing inputs. Nothing sets these in 12.5 —
    # 12.6 does, when steps land on real nodes.
    gpu_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gpu_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)

    # M13 attribution. NULL in 12.5; a NULL role is aggregated under
    # "unattributed" in rollups and never silently dropped.
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # JSON text (the codebase's dict-column idiom): {temperature, seed, top_p}
    # as the provider exposed it.
    determinism: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # The CLI's own usage blob, verbatim, capped at 8 KiB by the ingestion
    # service. It exists so a disputed number can be re-derived later — never
    # as a second source of truth.
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
