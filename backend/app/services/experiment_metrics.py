"""
Experiment aggregation — PURE FUNCTIONS OVER FETCHED ROWS (Phase 12.6.5).

No metric is computed inline in an endpoint. That rule is lifted verbatim
from ``docs/milestone-13/phase-specs-and-metrics.md`` Part 1 so 13.4's
``bench_metrics.py`` can absorb these without an archaeology pass, and so
every rule below is unit-testable against a fixture instead of against a
database.

THE BOUNDARY WITH MILESTONE 13 (do not cross it)
------------------------------------------------
12.6.5 REPORTS. It does not RANK. There is no ``separable`` field, no
bootstrap CI, no "winner", no Holm correction, no KM curve — those arrive
with 13.4. Everything emitted here must remain TRUE after 13.4 lands, which
is why the response carries ``ranked: false`` and a note saying so.

Four hard behaviours are borrowed from 13.4 anyway, because they are cheap
now and the board must not teach the wrong habit:

- ``n < MIN_REPEATS`` -> ``insufficient_repeats``: point values only.
- ``error_rate > MAX_ERROR_RATE`` -> a warning that comparison is disabled.
- ``cost_coverage < MIN_COST_COVERAGE`` -> a warning naming the variants.
- a zero denominator is ``None`` with a reason, NEVER ``0.0``.

Rules that are easy to get subtly wrong, stated once
----------------------------------------------------
- **Skipped is excluded from the denominator.** ``pass_rate =
  passed / (passed + failed)``. A suite that skipped everything has no
  pass rate; it does not have a pass rate of 0.
- **The headline is the MACRO average over criteria** (equal weight per
  criterion). The pooled MICRO rate rides alongside as a footnote. A micro
  headline lets one criterion with 40 tests own the number.
- **``criterion_id IS NULL`` is bucketed, not dropped.** Tests that ran and
  nobody counted is exactly the quiet hole R1 exists to prevent. They land
  in ``unlinked_tests`` and are excluded from the macro average (they are not
  a criterion).
- **Only MEASURED cells** (``passed`` / ``failed``) enter denominators.
  ``error`` means nothing was measured; ``cancelled`` / ``skipped_budget``
  never ran.
- **Dollars are summed in Python over ``Decimal``, never with SQL ``SUM()``**:
  SQLite returns a float for ``SUM(NUMERIC)``, and this codebase's money rule
  is Decimal in Python and in the DB, string on the wire.
- **The cost centre is the MEDIAN, not the mean** (M2's heavy-tail argument
  applies at any n).
- **``cost_source="unknown"`` counts as ZERO dollars** and lowers
  ``cost_coverage``. That is the only defensible arithmetic, and the coverage
  number is what stops it from reading as "cheap".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from app.models.experiment import (
    MEASURED_CELL_STATUSES,
    ExperimentRunStatus,
)
from app.models.usage import UsageCostSource
from app.schemas.experiment import CriterionRate, VariantRow
from app.schemas.usage import money

logger = logging.getLogger(__name__)

#: Below this many measured repeats a variant reports point values only.
MIN_REPEATS = 3
#: Above this error rate a variant's numbers are shown but comparison is off.
MAX_ERROR_RATE = 0.10
#: Below this pooled coverage the cap is largely unenforced and must say so.
MIN_COST_COVERAGE = 0.9

_ZERO = Decimal("0")

NO_TEST_EVIDENCE = (
    "no test evidence: no test result was tied back to any measured cell of "
    "this variant"
)
NO_MEASURED_CELLS = (
    "no measured cells: every cell of this variant errored, was cancelled, or "
    "was skipped for budget"
)
ALL_SKIPPED = "every test for this criterion was skipped — skips are not failures"


# -----------------------------------------------------------------------------
# Inputs — plain rows, so every rule below is testable without a database
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class CellRow:
    """One matrix cell, with its frozen coordinates."""

    id: str
    variant_index: int
    status: str
    agent: str
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
    label: str | None = None


@dataclass(frozen=True)
class OutcomeRow:
    """One TestRun of a measured cell, joined to its criterion."""

    variant_index: int
    criterion_id: str | None
    status: str
    criterion_text: str | None = None


@dataclass(frozen=True)
class UsageRow:
    """One StepUsage row of a cell's pipeline run."""

    cell_id: str
    variant_index: int
    cost_usd: Decimal | None = None
    cost_source: str = UsageCostSource.UNKNOWN.value
    wall_clock_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class _Counts:
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    def add(self, status: str) -> None:
        if status == "passed":
            self.passed += 1
        elif status == "failed":
            self.failed += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            # Not in today's manifest vocabulary; count it as a failure
            # rather than dropping it — an unknown outcome is at least as bad
            # as a known bad one (test_ingestion's own ranking rule).
            logger.warning("Unknown TestRun status %r counted as failed", status)
            self.failed += 1


# -----------------------------------------------------------------------------
# Primitives
# -----------------------------------------------------------------------------

def pass_rate(passed: int, failed: int) -> tuple[float | None, str | None]:
    """``passed / (passed + failed)``, or ``(None, reason)``.

    Skips never reach here — they are excluded by the caller. A zero
    denominator is ``None`` with a reason and is NEVER ``0.0``: "no test ran"
    and "every test failed" are different facts.
    """
    denominator = passed + failed
    if denominator == 0:
        return None, ALL_SKIPPED
    return passed / denominator, None


def median_decimal(values: list[Decimal]) -> Decimal | None:
    """Median over ``Decimal``. Even counts average the two middle values."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def median_int(values: list[int]) -> int | None:
    """Median over ints, rounded down on an even count."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def observed_spend(usages: list[UsageRow]) -> Decimal:
    """Total observed dollars, summed in Python over ``Decimal``.

    A row with ``cost_source="unknown"`` (or a NULL ``cost_usd``) contributes
    ZERO. That is the only defensible arithmetic — and it is exactly why
    ``cost_coverage`` is surfaced on every cell and variant: coverage 0.4
    means the cap is largely unenforced, and the UI has to show that rather
    than let a quietly-too-cheap total imply headroom.
    """
    total = _ZERO
    for row in usages:
        if row.cost_usd is None:
            continue
        if row.cost_source == UsageCostSource.UNKNOWN.value:
            continue
        total += Decimal(str(row.cost_usd))
    return total


def cost_coverage(usages: list[UsageRow]) -> float | None:
    """Fraction of usage rows whose cost is actually known.

    ``None`` (not ``0.0``) when there are no usage rows at all: "nothing has
    reported yet" is not "nothing is priced".
    """
    if not usages:
        return None
    known = sum(
        1 for row in usages if row.cost_source != UsageCostSource.UNKNOWN.value
    )
    return known / len(usages)


# -----------------------------------------------------------------------------
# The leaderboard
# -----------------------------------------------------------------------------

@dataclass
class _VariantAccumulator:
    cells: list[CellRow] = field(default_factory=list)
    per_criterion: dict[str | None, _Counts] = field(default_factory=dict)
    usages: list[UsageRow] = field(default_factory=list)
    criterion_text: dict[str | None, str | None] = field(default_factory=dict)


def build_leaderboard(
    cells: list[CellRow],
    outcomes: list[OutcomeRow],
    usages: list[UsageRow],
    *,
    min_repeats: int = MIN_REPEATS,
) -> list[VariantRow]:
    """One row per variant. Deterministic, ordered by ``variant_index``.

    ``outcomes`` must already be restricted to MEASURED cells (the SQL does
    it with ``experiment_runs.status IN ('passed','failed')``); this function
    does not re-filter them because it cannot see which cell a row came from
    without a join it should not need.
    """
    by_variant: dict[int, _VariantAccumulator] = {}

    for cell in cells:
        by_variant.setdefault(cell.variant_index, _VariantAccumulator()).cells.append(
            cell
        )
    for outcome in outcomes:
        acc = by_variant.setdefault(outcome.variant_index, _VariantAccumulator())
        acc.per_criterion.setdefault(outcome.criterion_id, _Counts()).add(
            outcome.status
        )
        if outcome.criterion_text is not None:
            acc.criterion_text[outcome.criterion_id] = outcome.criterion_text
    for usage in usages:
        by_variant.setdefault(usage.variant_index, _VariantAccumulator()).usages.append(
            usage
        )

    return [
        _build_row(variant_index, by_variant[variant_index], min_repeats)
        for variant_index in sorted(by_variant)
    ]


def _build_row(
    variant_index: int, acc: _VariantAccumulator, min_repeats: int
) -> VariantRow:
    coordinates = acc.cells[0] if acc.cells else None

    cells_total = len(acc.cells)
    cells_measured = sum(1 for c in acc.cells if c.status in MEASURED_CELL_STATUSES)
    cells_errored = sum(
        1 for c in acc.cells if c.status == ExperimentRunStatus.ERROR.value
    )
    cells_skipped_budget = sum(
        1 for c in acc.cells if c.status == ExperimentRunStatus.SKIPPED_BUDGET.value
    )
    # n is the cells that RAN. A cell cancelled or skipped for budget never
    # got the chance to error, so including it would dilute the rate.
    ran = cells_measured + cells_errored
    error_rate = (cells_errored / ran) if ran else 0.0

    criteria: list[CriterionRate] = []
    unlinked: CriterionRate | None = None
    macro_rates: list[float] = []
    micro_passed = micro_failed = 0

    for criterion_id in sorted(acc.per_criterion, key=lambda k: (k is None, k or "")):
        counts = acc.per_criterion[criterion_id]
        rate, reason = pass_rate(counts.passed, counts.failed)
        entry = CriterionRate(
            criterion_id=criterion_id,
            criterion_text=acc.criterion_text.get(criterion_id),
            passed=counts.passed,
            failed=counts.failed,
            skipped=counts.skipped,
            pass_rate=rate,
            reason=reason,
        )
        if criterion_id is None:
            # Not a criterion: bucketed and surfaced, but never allowed to
            # move the headline.
            unlinked = entry
            continue
        criteria.append(entry)
        micro_passed += counts.passed
        micro_failed += counts.failed
        if rate is not None:
            macro_rates.append(rate)

    if macro_rates:
        headline: float | None = sum(macro_rates) / len(macro_rates)
        reason = None
    else:
        headline = None
        reason = NO_MEASURED_CELLS if cells_measured == 0 else NO_TEST_EVIDENCE

    micro, _ = pass_rate(micro_passed, micro_failed)

    # Cost. Per-CELL totals first, so the median is over runs and not over
    # step rows (a cell with an agent step and a verify step must not weigh
    # twice as much as a single-step cell).
    per_cell: dict[str, Decimal] = {}
    per_cell_wall: dict[str, int] = {}
    input_tokens_total = output_tokens_total = 0
    for usage in acc.usages:
        if usage.cost_usd is not None and usage.cost_source != UsageCostSource.UNKNOWN.value:
            per_cell[usage.cell_id] = per_cell.get(usage.cell_id, _ZERO) + Decimal(
                str(usage.cost_usd)
            )
        else:
            per_cell.setdefault(usage.cell_id, _ZERO)
        if usage.wall_clock_ms is not None:
            per_cell_wall[usage.cell_id] = (
                per_cell_wall.get(usage.cell_id, 0) + usage.wall_clock_ms
            )
        input_tokens_total += usage.input_tokens or 0
        output_tokens_total += usage.output_tokens or 0

    total_cost = sum(per_cell.values(), _ZERO)
    median_cost = median_decimal(list(per_cell.values()))
    coverage = cost_coverage(acc.usages)

    warnings: list[str] = []
    label = _variant_label(coordinates)
    if error_rate > MAX_ERROR_RATE:
        warnings.append(
            f"{label}: {cells_errored}/{ran} cells errored "
            f"({error_rate * 100:.0f}%) - numbers shown, comparison disabled"
        )
    if cells_skipped_budget:
        warnings.append(
            f"{label}: {cells_skipped_budget}/{cells_total} cells were never "
            "dispatched (budget cap reached) - this variant is measured on "
            "fewer runs than the matrix asked for"
        )
    insufficient = cells_measured < min_repeats
    if insufficient:
        warnings.append(
            f"{label}: only {cells_measured} measured run(s) - below the "
            f"{min_repeats}-repeat floor; point values only, no comparison"
        )
    if coverage is not None and coverage < MIN_COST_COVERAGE:
        warnings.append(
            f"{label}: cost coverage {coverage * 100:.0f}% - some steps "
            "reported no price, so this variant's dollars are a LOWER BOUND"
        )

    return VariantRow(
        variant_index=variant_index,
        label=label,
        agent=coordinates.agent if coordinates else "",
        model=coordinates.model if coordinates else None,
        prompt_template_id=coordinates.prompt_template_id if coordinates else None,
        prompt_version=coordinates.prompt_version if coordinates else None,
        cells_total=cells_total,
        cells_measured=cells_measured,
        cells_errored=cells_errored,
        cells_skipped_budget=cells_skipped_budget,
        error_rate=error_rate,
        pass_rate=headline,
        pass_rate_micro=micro,
        reason=reason,
        criteria=criteria,
        unlinked_tests=unlinked,
        cost_usd_total=money(total_cost) or "0.000000",
        cost_usd_per_run_median=money(median_cost),
        cost_coverage=coverage,
        wall_clock_ms_median=median_int(list(per_cell_wall.values())),
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        insufficient_repeats=insufficient,
        warnings=warnings,
    )


def _variant_label(cell: CellRow | None) -> str:
    if cell is None:
        return "unknown variant"
    if cell.label:
        return cell.label
    return f"{cell.agent} / {cell.model or 'default model'}"


def board_warnings(variants: list[VariantRow]) -> list[str]:
    """Board-level warnings: the pooled coverage rule (4.5)."""
    warnings: list[str] = []
    unpriced = [
        v.label
        for v in variants
        if v.cost_coverage is not None and v.cost_coverage < MIN_COST_COVERAGE
    ]
    if unpriced:
        warnings.append(
            "cost coverage below "
            f"{int(MIN_COST_COVERAGE * 100)}% for: "
            + ", ".join(sorted(unpriced))
            + " - the budget cap is only partly enforced for those variants"
        )
    return warnings


def board_coverage(variants: list[VariantRow]) -> float | None:
    """Board headline coverage: the MEAN of the variants that reported usage.

    Variant-weighted, not row-weighted, deliberately: the number exists to
    answer "how enforced is this experiment's cap across its variants?", and a
    variant with many steps should not be able to hide an entirely unpriced
    one. Variants with no usage rows at all are excluded rather than counted
    as zero — "nothing has reported yet" is not "nothing is priced".
    """
    known = [v.cost_coverage for v in variants if v.cost_coverage is not None]
    if not known:
        return None
    return sum(known) / len(known)
