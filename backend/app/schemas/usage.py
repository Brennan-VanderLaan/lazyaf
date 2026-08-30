"""
Pydantic schemas for the usage channel (Phase 12.5, cross-agent contract #3).

This module is THE source of truth for the `POST /api/steps/{id}/usage` wire
shape (R3: one writer per datum, one definition per contract). The agent
wrapper writes it, `images/base/control/run.py` ships it verbatim, and the
endpoint here validates it — and both sides' tests import the shared pin at
`tdd/unit/control_runtime/usage_contract.py`, which is checked against these
models in one process so neither side can drift alone.

Version pinning mirrors `TestResultsManifest`: `version: Literal[1]` makes an
unknown version a 422 rather than a silent partial parse. A half-understood
accounting record is worse than a rejected one — the runtime's fallback for a
422 is a WARN in the step logs, and the step's exit code is untouched either
way (the never-fail-a-step rule, api-surface 2.4).

Money convention (api-surface 0): `Decimal` in Python and in the DB, STRING on
the wire out. `cost_usd` comes IN as a Decimal (pydantic parses the JSON
string or number losslessly) and goes OUT as a quantized 6dp string. No floats
for dollars, ever.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Money resolution on the wire out (NUMERIC(18,6)).
_MONEY = Decimal("0.000001")

#: Every cost_source, always present in a rollup's `by_source` — a zero is a
#: fact ("no gpu-node steps"), an absent key is an ambiguity.
COST_SOURCES: tuple[str, ...] = ("cli-reported", "gpu-node", "estimated", "unknown")

#: Bucket for a StepUsage whose role could not be resolved (api-surface 2.6):
#: never silently dropped from a total, always visible in the warning.
UNATTRIBUTED = "unattributed"


# -----------------------------------------------------------------------------
# Vocabularies (pinned; an out-of-vocabulary value is a 422)
# -----------------------------------------------------------------------------

Provider = Literal["anthropic", "google", "openai-compatible", "self-hosted"]
CostSource = Literal["cli-reported", "gpu-node", "estimated", "unknown"]


# -----------------------------------------------------------------------------
# Manifest (cross-agent contract #3) — api-surface 2.2, verbatim
# -----------------------------------------------------------------------------

class UsageManifest(BaseModel):
    version: Literal[1]
    provider: Provider
    model: str | None = None
    model_version: str | None = None      # provider's exact version string
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: Decimal | None = None       # CLI-reported dollars, if any
    cost_source: CostSource
    wall_clock_ms: int
    container_seconds: float | None = None
    gpu_node_id: str | None = None        # set on self-hosted nodes
    gpu_fraction: float | None = None     # 1.0 = exclusive
    determinism: dict = {}                # {temperature, seed, top_p} as exposed
    role: str | None = None               # M13: which strategy role this step was
    raw: dict | None = None               # the CLI's own usage blob, verbatim

    # NOTE: `trial_iteration_id` is deliberately absent (design section 3.6).
    # Nothing writes it and there is no table to reference in 12.5; it lands
    # with M13's trials table. `role` is here NOW because it is unrecoverable
    # after the fact — that is exactly the retrofit line api-surface 2.6 draws.


class UsageIngestResponse(BaseModel):
    usage_id: str
    cost_usd: str | None = None
    cost_source: CostSource


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------

def _decode_json_object(text: str | None, field: str) -> dict | None:
    """JSON-text column -> dict. A corrupt blob is reported, never a 500.

    The usage channel's whole premise is that accounting must not break the
    thing it accounts for; that holds on the read side too.
    """
    if not text:
        return None
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Unparseable %s blob on a StepUsage row", field)
        return {"_unparseable": True}
    return value if isinstance(value, dict) else {"_unparseable": True}


def money(value: Decimal | float | int | None) -> str | None:
    """Dollars as a 6dp STRING (api-surface 0: no floats for money)."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_MONEY))


class StepUsageRead(BaseModel):
    """One step's usage row (operator/UI: GET /api/steps/{id}/usage)."""

    id: str
    step_execution_id: str
    step_run_id: str | None = None
    pipeline_run_id: str | None = None
    provider: str
    model: str | None = None
    model_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # Dollars leave as a string (never a float) — see module docstring.
    cost_usd: str | None = None
    cost_source: str
    wall_clock_ms: int
    container_seconds: float | None = None
    gpu_node_id: str | None = None
    gpu_fraction: float | None = None
    role: str | None = None
    determinism: dict = {}
    raw: dict | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, usage: Any) -> "StepUsageRead":
        return cls(
            id=usage.id,
            step_execution_id=usage.step_execution_id,
            step_run_id=usage.step_run_id,
            pipeline_run_id=usage.pipeline_run_id,
            provider=usage.provider,
            model=usage.model,
            model_version=usage.model_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=money(usage.cost_usd),
            cost_source=usage.cost_source,
            wall_clock_ms=usage.wall_clock_ms,
            container_seconds=usage.container_seconds,
            gpu_node_id=usage.gpu_node_id,
            gpu_fraction=usage.gpu_fraction,
            role=usage.role,
            determinism=_decode_json_object(usage.determinism, "determinism") or {},
            raw=_decode_json_object(usage.raw, "raw"),
            created_at=usage.created_at,
            updated_at=usage.updated_at,
        )


class UsageRoleBucket(BaseModel):
    """One `by_role` cell of a rollup (api-surface 2.7)."""

    cost_usd: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    wall_clock_ms: int
    steps: int


class RunUsageStep(BaseModel):
    """One row in a run rollup's per-step listing.

    The dogfood gate (`scripts/verify_executor.py`) compares this list
    against the run's StepRuns: a silently dropped usage channel must fail
    the push, and it can only do that if the rollup names the steps it has.
    """

    usage_id: str
    step_execution_id: str
    step_run_id: str | None = None
    step_index: int | None = None
    step_name: str | None = None
    provider: str
    model: str | None = None
    role: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: str | None = None
    cost_source: str
    wall_clock_ms: int
    container_seconds: float | None = None

    @classmethod
    def from_model(
        cls,
        usage: Any,
        step_index: int | None = None,
        step_name: str | None = None,
    ) -> "RunUsageStep":
        return cls(
            usage_id=usage.id,
            step_execution_id=usage.step_execution_id,
            step_run_id=usage.step_run_id,
            step_index=step_index,
            step_name=step_name,
            provider=usage.provider,
            model=usage.model,
            role=usage.role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=money(usage.cost_usd),
            cost_source=usage.cost_source,
            wall_clock_ms=usage.wall_clock_ms,
            container_seconds=usage.container_seconds,
        )


class RunUsageRollup(BaseModel):
    """GET /api/pipeline-runs/{run_id}/usage — read-heavy (api-surface s6,
    served by ix_step_usages_pipeline_run_id_role)."""

    pipeline_run_id: str
    total_cost_usd: str
    # priced rows / total rows. 0.0 when the run has NO usage rows at all:
    # "we recorded nothing" is not full coverage, and a run with no
    # accounting deserves the same warning a partially-priced one gets.
    cost_coverage: float
    step_count: int
    by_role: dict[str, UsageRoleBucket]
    by_source: dict[str, int]
    steps: list[RunUsageStep]

    @classmethod
    def build(
        cls,
        pipeline_run_id: str,
        rows: "list[tuple[Any, int | None, str | None]]",
    ) -> "RunUsageRollup":
        """Aggregate `(StepUsage, step_index, step_name)` rows into the rollup.

        Pure computation over an indexed scan (api-surface s6:
        ix_step_usages_pipeline_run_id_role serves the fetch; percentiles and
        anything the index cannot answer in one pass are computed here).

        Two rules that the numbers depend on:
        - A NULL role lands in the "unattributed" bucket, never nowhere.
        - `cost_coverage` counts rows whose cost_source is anything but
          "unknown". A run with no usage rows at all reports 0.0: "we
          recorded nothing" is not full coverage.
        """
        totals: dict[str, dict[str, int | Decimal]] = {}
        by_source = {source: 0 for source in COST_SOURCES}
        total_cost = Decimal("0")
        priced = 0
        steps: list[RunUsageStep] = []

        for usage, step_index, step_name in rows:
            steps.append(RunUsageStep.from_model(usage, step_index, step_name))

            by_source[usage.cost_source] = by_source.get(usage.cost_source, 0) + 1
            if usage.cost_source != "unknown":
                priced += 1
            if usage.cost_usd is not None:
                total_cost += Decimal(str(usage.cost_usd))

            bucket = totals.setdefault(
                usage.role or UNATTRIBUTED,
                {
                    "cost_usd": Decimal("0"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "wall_clock_ms": 0,
                    "steps": 0,
                },
            )
            bucket["steps"] += 1
            bucket["wall_clock_ms"] += usage.wall_clock_ms or 0
            if usage.cost_usd is not None:
                bucket["cost_usd"] += Decimal(str(usage.cost_usd))
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                bucket[field] += getattr(usage, field) or 0

        return cls(
            pipeline_run_id=pipeline_run_id,
            total_cost_usd=money(total_cost) or "0.000000",
            cost_coverage=round(priced / len(steps), 4) if steps else 0.0,
            step_count=len(steps),
            by_role={
                role: UsageRoleBucket(
                    cost_usd=money(bucket["cost_usd"]) or "0.000000",
                    input_tokens=bucket["input_tokens"],
                    output_tokens=bucket["output_tokens"],
                    cache_read_tokens=bucket["cache_read_tokens"],
                    cache_write_tokens=bucket["cache_write_tokens"],
                    wall_clock_ms=bucket["wall_clock_ms"],
                    steps=bucket["steps"],
                )
                for role, bucket in sorted(totals.items())
            },
            by_source=by_source,
            steps=steps,
        )
