"""
Unit tests for the leaderboard's pure aggregation (Phase 12.6.5).

Every rule in the design's section 4 gets a named test here, against
fixtures rather than a database — which is the whole point of the metrics
module being pure functions over fetched rows.

The rules that are easy to get subtly wrong, and that this file exists to
stop from regressing:

- skipped is EXCLUDED from the denominator
- a zero denominator is `None` with a reason, never `0.0`
- the headline is the MACRO average over criteria, micro is a footnote
- `criterion_id IS NULL` is bucketed, never dropped
- only measured cells count; errors are counted separately and printed
- the cost centre is the MEDIAN, not the mean
- `cost_source="unknown"` is zero dollars AND lowers coverage
- `ranked` is always false
"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.experiment import ExperimentRunStatus
from app.schemas.experiment import NOT_RANKED_NOTE, LeaderboardResponse
from app.services.experiment_metrics import (
    MAX_ERROR_RATE,
    MIN_COST_COVERAGE,
    MIN_REPEATS,
    CellRow,
    OutcomeRow,
    UsageRow,
    board_coverage,
    board_warnings,
    build_leaderboard,
    cost_coverage,
    median_decimal,
    median_int,
    observed_spend,
    pass_rate,
)

PASSED = ExperimentRunStatus.PASSED.value
FAILED = ExperimentRunStatus.FAILED.value
ERROR = ExperimentRunStatus.ERROR.value


def cell(cell_id, variant=0, status=PASSED, label="v0", model="m1"):
    return CellRow(
        id=cell_id,
        variant_index=variant,
        status=status,
        agent="mock",
        model=model,
        prompt_template_id="t1",
        prompt_version=1,
        label=label,
    )


def usage(cell_id, variant=0, cost="0.10", source="cli-reported", wall=1000,
          tin=100, tout=50):
    return UsageRow(
        cell_id=cell_id,
        variant_index=variant,
        cost_usd=Decimal(cost) if cost is not None else None,
        cost_source=source,
        wall_clock_ms=wall,
        input_tokens=tin,
        output_tokens=tout,
    )


class TestPassRatePrimitive:
    def test_basic(self):
        assert pass_rate(3, 1) == (0.75, None)

    def test_zero_denominator_is_none_not_zero(self):
        rate, reason = pass_rate(0, 0)
        assert rate is None
        assert reason and "skip" in reason.lower()

    def test_all_failed_is_zero_not_none(self):
        """'every test failed' and 'no test ran' must not collapse."""
        assert pass_rate(0, 4) == (0.0, None)


class TestMedians:
    def test_odd_count(self):
        assert median_decimal([Decimal("1"), Decimal("5"), Decimal("2")]) == Decimal("2")

    def test_even_count_averages_the_middle_two(self):
        assert median_decimal(
            [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("10")]
        ) == Decimal("2.5")

    def test_empty_is_none(self):
        assert median_decimal([]) is None
        assert median_int([]) is None

    def test_median_resists_the_heavy_tail_a_mean_would_not(self):
        """M2's argument: one runaway run must not become the centre."""
        values = [Decimal("0.10"), Decimal("0.12"), Decimal("9.00")]
        assert median_decimal(values) == Decimal("0.12")
        mean = sum(values) / 3
        assert mean > Decimal("3")

    def test_median_int(self):
        assert median_int([10, 30, 20]) == 20
        assert median_int([10, 20, 30, 40]) == 25


class TestObservedSpend:
    def test_sums_decimals(self):
        total = observed_spend([usage("c1", cost="0.10"), usage("c2", cost="0.25")])
        assert total == Decimal("0.35")
        assert isinstance(total, Decimal)

    def test_unknown_cost_source_counts_as_zero(self):
        """api-surface 4.2: nothing else is defensible. The coverage number is
        what stops that from reading as 'cheap'."""
        rows = [usage("c1", cost="0.10"), usage("c2", cost="99.00", source="unknown")]
        assert observed_spend(rows) == Decimal("0.10")

    def test_null_cost_counts_as_zero(self):
        assert observed_spend([usage("c1", cost=None, source="unknown")]) == Decimal("0")

    def test_no_rows_is_zero(self):
        assert observed_spend([]) == Decimal("0")

    def test_sum_stays_exact_at_six_decimal_places(self):
        """Dollars are summed in Python over Decimal, never SQL SUM() -
        SQLite returns a float for SUM(NUMERIC)."""
        rows = [usage(f"c{i}", cost="0.000001") for i in range(10)]
        assert observed_spend(rows) == Decimal("0.000010")


class TestCostCoverage:
    def test_all_known(self):
        assert cost_coverage([usage("c1"), usage("c2")]) == 1.0

    def test_partial(self):
        assert cost_coverage([usage("c1"), usage("c2", source="unknown")]) == 0.5

    def test_no_rows_is_none_not_zero(self):
        """'nothing has reported yet' is not 'nothing is priced'."""
        assert cost_coverage([]) is None


class TestLeaderboardPassRates:
    def test_skipped_is_excluded_from_the_denominator(self):
        rows = build_leaderboard(
            [cell("c1")],
            [
                OutcomeRow(0, "crit-1", PASSED),
                OutcomeRow(0, "crit-1", "skipped"),
                OutcomeRow(0, "crit-1", "skipped"),
            ],
            [],
        )
        criterion = rows[0].criteria[0]
        assert criterion.passed == 1
        assert criterion.skipped == 2
        assert criterion.pass_rate == 1.0

    def test_zero_denominator_is_null_with_a_reason_never_zero_percent(self):
        rows = build_leaderboard(
            [cell("c1")],
            [OutcomeRow(0, "crit-1", "skipped")],
            [],
        )
        criterion = rows[0].criteria[0]
        assert criterion.pass_rate is None
        assert criterion.reason
        assert rows[0].pass_rate is None
        assert rows[0].reason

    def test_variant_with_no_test_evidence_reports_null_and_says_why(self):
        rows = build_leaderboard([cell("c1")], [], [])
        assert rows[0].pass_rate is None
        assert "no test evidence" in rows[0].reason

    def test_variant_with_no_measured_cells_says_that_instead(self):
        rows = build_leaderboard([cell("c1", status=ERROR)], [], [])
        assert rows[0].pass_rate is None
        assert "no measured cells" in rows[0].reason

    def test_macro_average_over_criteria_is_the_headline(self):
        """One criterion with many tests must not own the number."""
        outcomes = [OutcomeRow(0, "big", PASSED) for _ in range(40)]
        outcomes += [OutcomeRow(0, "small", FAILED)]
        rows = build_leaderboard([cell("c1")], outcomes, [])
        # macro: (1.0 + 0.0) / 2
        assert rows[0].pass_rate == 0.5
        # micro (pooled) would have been 40/41
        assert rows[0].pass_rate_micro == pytest.approx(40 / 41)

    def test_micro_is_carried_as_a_footnote_not_the_headline(self):
        rows = build_leaderboard(
            [cell("c1")],
            [
                OutcomeRow(0, "a", PASSED),
                OutcomeRow(0, "a", PASSED),
                OutcomeRow(0, "b", FAILED),
            ],
            [],
        )
        assert rows[0].pass_rate == 0.5           # macro over {a: 1.0, b: 0.0}
        assert rows[0].pass_rate_micro == pytest.approx(2 / 3)

    def test_unlinked_tests_are_bucketed_not_dropped(self):
        """Tests that ran and nobody counted is the quiet hole R1 exists to
        prevent."""
        rows = build_leaderboard(
            [cell("c1")],
            [OutcomeRow(0, "crit-1", PASSED), OutcomeRow(0, None, FAILED)],
            [],
        )
        row = rows[0]
        assert row.unlinked_tests is not None
        assert row.unlinked_tests.failed == 1
        assert [c.criterion_id for c in row.criteria] == ["crit-1"]
        # ...and they do NOT move the headline, because they are not criteria.
        assert row.pass_rate == 1.0

    def test_criterion_text_is_carried_through(self):
        rows = build_leaderboard(
            [cell("c1")],
            [OutcomeRow(0, "crit-1", PASSED, criterion_text="It works")],
            [],
        )
        assert rows[0].criteria[0].criterion_text == "It works"

    def test_unknown_status_counts_as_failed_not_dropped(self):
        rows = build_leaderboard(
            [cell("c1")], [OutcomeRow(0, "crit-1", "exploded")], []
        )
        assert rows[0].criteria[0].failed == 1
        assert rows[0].criteria[0].pass_rate == 0.0


class TestLeaderboardCells:
    def test_one_row_per_variant_ordered_by_variant_index(self):
        rows = build_leaderboard(
            [cell("c1", variant=2), cell("c2", variant=0), cell("c3", variant=1)],
            [],
            [],
        )
        assert [r.variant_index for r in rows] == [0, 1, 2]

    def test_error_cells_are_counted_and_excluded_from_measured(self):
        cells = [cell("c1"), cell("c2", status=FAILED), cell("c3", status=ERROR)]
        row = build_leaderboard(cells, [], [])[0]
        assert row.cells_total == 3
        assert row.cells_measured == 2
        assert row.cells_errored == 1
        assert row.error_rate == pytest.approx(1 / 3)

    def test_error_rate_denominator_excludes_never_dispatched_cells(self):
        """A cell skipped for budget never got the chance to error."""
        cells = [
            cell("c1"),
            cell("c2", status=ERROR),
            cell("c3", status=ExperimentRunStatus.SKIPPED_BUDGET.value),
            cell("c4", status=ExperimentRunStatus.CANCELLED.value),
        ]
        row = build_leaderboard(cells, [], [])[0]
        assert row.error_rate == 0.5
        assert row.cells_skipped_budget == 1

    def test_high_error_rate_warns_that_comparison_is_disabled(self):
        cells = [cell(f"c{i}") for i in range(8)] + [cell("bad", status=ERROR)]
        row = build_leaderboard(cells, [], [])[0]
        assert row.error_rate > MAX_ERROR_RATE
        assert any("comparison disabled" in w for w in row.warnings)
        assert any("1/9" in w for w in row.warnings)

    def test_error_rate_exactly_at_the_threshold_does_not_warn(self):
        """The rule is `> 10%`, so 1-in-10 is inside it. Pinned because an
        off-by-one here silently disables comparison for a clean matrix."""
        cells = [cell(f"c{i}") for i in range(9)] + [cell("bad", status=ERROR)]
        row = build_leaderboard(cells, [], [])[0]
        assert row.error_rate == MAX_ERROR_RATE
        assert not any("comparison disabled" in w for w in row.warnings)

    def test_insufficient_repeats_below_the_floor(self):
        row = build_leaderboard([cell("c1"), cell("c2")], [], [])[0]
        assert row.insufficient_repeats is True
        assert any(f"{MIN_REPEATS}-repeat floor" in w for w in row.warnings)

    def test_enough_repeats_clears_the_flag(self):
        cells = [cell(f"c{i}") for i in range(MIN_REPEATS)]
        assert build_leaderboard(cells, [], [])[0].insufficient_repeats is False

    def test_skipped_budget_cells_are_reported_on_the_row(self):
        cells = [cell("c1"), cell("c2", status=ExperimentRunStatus.SKIPPED_BUDGET.value)]
        row = build_leaderboard(cells, [], [])[0]
        assert row.cells_skipped_budget == 1
        assert any("budget cap reached" in w or "never dispatched" in w
                   for w in row.warnings)


class TestLeaderboardCost:
    def test_cost_is_summed_per_cell_then_medianed_over_runs(self):
        """A two-step cell must not weigh twice as much as a one-step cell."""
        cells = [cell("c1"), cell("c2"), cell("c3")]
        usages = [
            usage("c1", cost="0.10"),
            usage("c1", cost="0.10"),   # verify step of the same cell
            usage("c2", cost="0.50"),
            usage("c3", cost="0.30"),
        ]
        row = build_leaderboard(cells, [], usages)[0]
        assert row.cost_usd_total == "1.000000"
        assert row.cost_usd_per_run_median == "0.300000"

    def test_unknown_source_is_zero_dollars_and_lowers_coverage(self):
        cells = [cell("c1"), cell("c2")]
        usages = [usage("c1", cost="0.10"), usage("c2", cost="9.99", source="unknown")]
        row = build_leaderboard(cells, [], usages)[0]
        assert row.cost_usd_total == "0.100000"
        assert row.cost_coverage == 0.5

    def test_low_coverage_warns_that_dollars_are_a_lower_bound(self):
        cells = [cell("c1"), cell("c2")]
        usages = [usage("c1"), usage("c2", source="unknown")]
        row = build_leaderboard(cells, [], usages)[0]
        assert row.cost_coverage < MIN_COST_COVERAGE
        assert any("LOWER BOUND" in w for w in row.warnings)

    def test_no_usage_rows_leaves_coverage_null(self):
        row = build_leaderboard([cell("c1")], [], [])[0]
        assert row.cost_coverage is None
        assert row.cost_usd_total == "0.000000"
        assert row.cost_usd_per_run_median is None

    def test_wall_clock_median_and_token_totals(self):
        cells = [cell("c1"), cell("c2"), cell("c3")]
        usages = [
            usage("c1", wall=100, tin=10, tout=1),
            usage("c2", wall=300, tin=20, tout=2),
            usage("c3", wall=200, tin=30, tout=3),
        ]
        row = build_leaderboard(cells, [], usages)[0]
        assert row.wall_clock_ms_median == 200
        assert row.input_tokens_total == 60
        assert row.output_tokens_total == 6

    def test_money_is_a_string_on_the_wire(self):
        row = build_leaderboard([cell("c1")], [], [usage("c1")])[0]
        assert isinstance(row.cost_usd_total, str)
        assert row.cost_usd_total.count(".") == 1


class TestBoardLevel:
    def test_ranked_is_always_false(self):
        board = LeaderboardResponse(experiment_id="e1", variants=[])
        assert board.ranked is False

    def test_note_points_at_milestone_13_4_verbatim(self):
        board = LeaderboardResponse(experiment_id="e1", variants=[])
        assert board.note == NOT_RANKED_NOTE
        assert "13.4" in board.note
        assert "makes no claim that one variant beats another" in board.note

    def test_no_ranking_fields_exist_at_all(self):
        """13.4 owns separability, CIs and winners. Anything emitted here must
        stay TRUE after 13.4 lands."""
        fields = set(LeaderboardResponse.model_fields)
        assert not (fields & {"separable", "winner", "ci", "confidence_interval",
                              "rank", "holm"})

    def test_board_warnings_name_the_unpriced_variants(self):
        rows = build_leaderboard(
            [cell("c1", label="cheap"), cell("c2", variant=1, label="pricey")],
            [],
            [usage("c1"), usage("c2", variant=1, source="unknown")],
        )
        warnings = board_warnings(rows)
        assert any("pricey" in w for w in warnings)
        assert not any(w.startswith("cheap") for w in warnings)

    def test_board_coverage_pools_variants_that_reported(self):
        rows = build_leaderboard(
            [cell("c1"), cell("c2", variant=1)],
            [],
            [usage("c1"), usage("c2", variant=1, source="unknown")],
        )
        assert board_coverage(rows) == 0.5

    def test_board_coverage_is_none_when_nothing_reported(self):
        assert board_coverage(build_leaderboard([cell("c1")], [], [])) is None
