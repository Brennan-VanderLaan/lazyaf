"""
Unit tests for the StepUsage model and the usage wire schema (Phase 12.5).

Structure, vocabularies, defaults, FKs and indexes — no I/O, matching the
unit-tier convention (table metadata + direct construction only), plus the
pure-computation halves of the channel: the gpu-node cost model
(api-surface 2.5) and the rollup aggregation (api-surface 2.7).

The two scope decisions this phase makes irreversible are pinned here, not
just commented: `role` EXISTS (it is unrecoverable after the fact) and
`trial_iteration_id` DOES NOT (nothing writes it, no table to reference).
"""
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# The shared wire pin lives beside the control-runtime tests and is imported
# by BOTH sides (cross-agent contract #3), so the repo root goes on the path.
repo_root = Path(__file__).parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app.models import StepUsage, UsageCostSource, UsageProvider
from app.schemas.usage import (
    COST_SOURCES,
    UNATTRIBUTED,
    RunUsageRollup,
    StepUsageRead,
    UsageManifest,
    money,
)
from app.services.usage_pricing import gpu_node_cost_usd

from tdd.unit.control_runtime.usage_contract import (
    CANONICAL_MANIFEST,
    FALLBACK_MANIFEST,
    FORBIDDEN_KEYS,
    INVALID_MANIFESTS,
    REQUIRED_KEYS,
    TOP_LEVEL_KEYS,
    assert_manifest_conforms,
    manifest_violations,
)


def _indexes(model) -> dict[str, tuple[tuple[str, ...], bool]]:
    """name -> (column names, unique) for the model's table."""
    return {
        index.name: (tuple(c.name for c in index.columns), bool(index.unique))
        for index in model.__table__.indexes
    }


class _FakeUsage:
    """A StepUsage-shaped row for the pure aggregation tests (no DB)."""

    def __init__(self, **kwargs):
        defaults = dict(
            id="usg-1",
            step_execution_id="exec-1",
            step_run_id="sr-1",
            pipeline_run_id="pr-1",
            provider="anthropic",
            model="claude-haiku-4-5",
            model_version=None,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=Decimal("0.100000"),
            cost_source="cli-reported",
            wall_clock_ms=1000,
            container_seconds=1.5,
            gpu_node_id=None,
            gpu_fraction=None,
            role=None,
            determinism="{}",
            raw=None,
        )
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


class _FakeUsageRow(_FakeUsage):
    """_FakeUsage plus the timestamps StepUsageRead needs."""

    def __init__(self, **kwargs):
        kwargs.setdefault("created_at", datetime(2026, 8, 29, 12, 0, 0))
        kwargs.setdefault("updated_at", datetime(2026, 8, 29, 12, 0, 1))
        super().__init__(**kwargs)


class TestUsageVocabularies:
    def test_provider_values(self):
        """api-surface 2.2: exactly these four providers."""
        assert {p.value for p in UsageProvider} == {
            "anthropic",
            "google",
            "openai-compatible",
            "self-hosted",
        }

    def test_cost_source_values(self):
        """`estimated` stays in the vocabulary and is written by nothing."""
        assert {s.value for s in UsageCostSource} == {
            "cli-reported",
            "gpu-node",
            "estimated",
            "unknown",
        }

    def test_enums_are_string_enums(self):
        assert issubclass(UsageProvider, str)
        assert issubclass(UsageCostSource, str)
        assert UsageProvider.ANTHROPIC == "anthropic"
        assert UsageCostSource.UNKNOWN == "unknown"

    def test_schema_vocabularies_match_the_model_enums(self):
        """One vocabulary, two spellings — they must not drift."""
        assert set(COST_SOURCES) == {s.value for s in UsageCostSource}


class TestStepUsageModel:
    def test_table_name(self):
        assert StepUsage.__tablename__ == "step_usages"

    def test_step_execution_id_is_the_required_identity_fk(self):
        col = StepUsage.__table__.c.step_execution_id
        assert col.nullable is False
        assert {fk.target_fullname for fk in col.foreign_keys} == {
            "step_executions.id"
        }

    def test_idempotency_key_is_a_unique_index_on_step_execution_id(self):
        """A retrying runtime UPDATES; it must be structurally unable to
        double-bill."""
        assert _indexes(StepUsage)["ix_step_usages_step_execution_id"] == (
            ("step_execution_id",),
            True,
        )

    def test_rollup_index_is_pipeline_run_id_then_role(self):
        """The read-heavy path (api-surface s6) groups by role inside a run."""
        assert _indexes(StepUsage)["ix_step_usages_pipeline_run_id_role"] == (
            ("pipeline_run_id", "role"),
            False,
        )

    def test_indexes_are_exactly_the_access_paths(self):
        """Every index earns its write cost: the idempotency key and the
        rollup scan — nothing else."""
        assert set(_indexes(StepUsage)) == {
            "ix_step_usages_step_execution_id",
            "ix_step_usages_pipeline_run_id_role",
        }

    def test_pipeline_run_id_is_denormalized_and_not_an_fk(self):
        """The rollup must not join to reach the run, and accounting rows
        outlive run pruning (the TestRun.pipeline_run_id precedent)."""
        col = StepUsage.__table__.c.pipeline_run_id
        assert col.nullable is True
        assert not col.foreign_keys

    def test_step_run_id_is_a_nullable_fk(self):
        col = StepUsage.__table__.c.step_run_id
        assert col.nullable is True
        assert {fk.target_fullname for fk in col.foreign_keys} == {"step_runs.id"}

    def test_role_column_exists_and_is_nullable(self):
        """M13 attribution on the frozen wire NOW: without it, cost_by_role
        is unrecoverable after the fact (api-surface 2.6)."""
        col = StepUsage.__table__.c.role
        assert col.nullable is True
        assert col.type.length == 64

    def test_trial_iteration_id_is_deliberately_absent(self):
        """Design 3.6: nothing writes it and there is no table to reference;
        an orphan column buys nothing. It lands with M13's trials table."""
        assert "trial_iteration_id" not in StepUsage.__table__.c

    def test_cost_usd_is_numeric_not_float(self):
        """Money is NUMERIC(18,6) in the DB — no floats for dollars, ever."""
        col = StepUsage.__table__.c.cost_usd
        assert col.type.precision == 18
        assert col.type.scale == 6
        assert col.nullable is True

    def test_cost_source_and_provider_are_required(self):
        """A row always states WHO served it and HOW it was priced — even
        when the answer is self-hosted/unknown."""
        assert StepUsage.__table__.c.cost_source.nullable is False
        assert StepUsage.__table__.c.provider.nullable is False

    def test_wall_clock_ms_is_required(self):
        """run.py always knows the wall clock, even for a step whose CLI
        reported nothing."""
        assert StepUsage.__table__.c.wall_clock_ms.nullable is False

    def test_token_and_dollar_fields_are_nullable(self):
        """Null tokens are the never-fail-a-step record, not a broken row."""
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "container_seconds",
        ):
            assert StepUsage.__table__.c[name].nullable is True, name

    def test_determinism_defaults_to_empty_json_object(self):
        col = StepUsage.__table__.c.determinism
        assert col.nullable is False
        assert col.default.arg == "{}"

    def test_row_constructs_from_the_minimal_required_set(self):
        """The never-fail-a-step record is constructible: provider,
        cost_source and wall clock, nothing else."""
        usage = StepUsage(
            step_execution_id="exec-1",
            provider="self-hosted",
            cost_source="unknown",
            wall_clock_ms=1,
        )
        assert usage.step_execution_id == "exec-1"
        assert usage.cost_usd is None
        assert usage.role is None


class TestUsageManifestSchema:
    def test_version_is_pinned_to_one(self):
        with pytest.raises(Exception):
            UsageManifest(version=2, provider="anthropic", cost_source="unknown", wall_clock_ms=1)

    def test_minimal_manifest_is_the_never_fail_record(self):
        """A step whose CLI reported nothing must still produce a VALID
        manifest: unknown cost, null tokens, real wall clock."""
        manifest = UsageManifest(
            version=1,
            provider="self-hosted",
            cost_source="unknown",
            wall_clock_ms=1204,
        )
        assert manifest.cost_usd is None
        assert manifest.input_tokens is None
        assert manifest.determinism == {}
        assert manifest.role is None

    def test_cost_usd_parses_a_string_into_a_decimal(self):
        """Dollars travel as strings and land as Decimal — never float."""
        manifest = UsageManifest(
            version=1,
            provider="anthropic",
            cost_usd="0.1841",
            cost_source="cli-reported",
            wall_clock_ms=1,
        )
        assert manifest.cost_usd == Decimal("0.1841")
        assert isinstance(manifest.cost_usd, Decimal)

    def test_unknown_provider_is_rejected(self):
        with pytest.raises(Exception):
            UsageManifest(
                version=1, provider="acme-ai", cost_source="unknown", wall_clock_ms=1
            )

    def test_unknown_cost_source_is_rejected(self):
        with pytest.raises(Exception):
            UsageManifest(
                version=1, provider="anthropic", cost_source="guessed", wall_clock_ms=1
            )

    def test_manifest_has_no_trial_iteration_id_field(self):
        assert "trial_iteration_id" not in UsageManifest.model_fields

    def test_manifest_has_a_role_field(self):
        assert "role" in UsageManifest.model_fields


class TestMoneyOnTheWire:
    def test_money_is_a_six_dp_string(self):
        assert money(Decimal("0.1841")) == "0.184100"

    def test_money_of_none_is_none(self):
        assert money(None) is None

    def test_money_of_zero_is_explicit(self):
        """A genuinely free step reports 0.000000, not null: 'free' and
        'unpriced' are different facts."""
        assert money(Decimal("0")) == "0.000000"


class TestGpuNodeCostModel:
    """api-surface 2.5: you rent the node, not the tokens."""

    def test_one_hour_exclusive_is_the_hourly_rate(self):
        assert gpu_node_cost_usd(Decimal("1.89"), 3600.0) == Decimal("1.890000")

    def test_half_an_hour_is_half_the_rate(self):
        assert gpu_node_cost_usd(Decimal("1.89"), 1800.0) == Decimal("0.945000")

    def test_gpu_fraction_scales_the_cost(self):
        """gpu_fraction < 1.0 only when the node is deliberately shared."""
        assert gpu_node_cost_usd(Decimal("1.89"), 3600.0, 0.5) == Decimal("0.945000")

    def test_owned_hardware_at_zero_is_honest_not_a_bug(self):
        assert gpu_node_cost_usd(Decimal("0.00"), 7200.0) == Decimal("0.000000")

    def test_result_is_quantized_to_six_dp(self):
        cost = gpu_node_cost_usd(Decimal("1.89"), 1.0)
        assert cost.as_tuple().exponent == -6

    def test_no_float_arithmetic_leaks_in(self):
        """container_seconds arrives as a float and must be converted via
        str(): binary float multiplication would drift the cents."""
        assert gpu_node_cost_usd(Decimal("3.60"), 0.1) == Decimal("0.000100")


class TestRollupAggregation:
    """api-surface 2.7: grouped by role, with coverage stated."""

    def test_empty_run_reports_zero_coverage_not_full(self):
        """A run with NO usage rows recorded nothing; that is not full
        coverage and it deserves the same warning a partial run gets."""
        rollup = RunUsageRollup.build("pr-1", [])
        assert rollup.step_count == 0
        assert rollup.cost_coverage == 0.0
        assert rollup.total_cost_usd == "0.000000"
        assert rollup.by_role == {}

    def test_by_source_always_carries_every_vocabulary_key(self):
        """A zero is a fact ('no gpu-node steps'); an absent key is an
        ambiguity."""
        rollup = RunUsageRollup.build("pr-1", [(_FakeUsage(), 0, "build")])
        assert set(rollup.by_source) == set(COST_SOURCES)
        assert rollup.by_source["gpu-node"] == 0
        assert rollup.by_source["cli-reported"] == 1

    def test_null_role_lands_in_unattributed(self):
        """Never silently dropped from the total (api-surface 2.6)."""
        rollup = RunUsageRollup.build("pr-1", [(_FakeUsage(role=None), 0, "build")])
        assert set(rollup.by_role) == {UNATTRIBUTED}
        assert rollup.by_role[UNATTRIBUTED].steps == 1

    def test_roles_are_bucketed_and_summed(self):
        rows = [
            (_FakeUsage(id="u1", role="planner", cost_usd=Decimal("0.400000")), 0, "plan"),
            (_FakeUsage(id="u2", role="worker", cost_usd=Decimal("1.000000")), 1, "work"),
            (_FakeUsage(id="u3", role="worker", cost_usd=Decimal("0.500000")), 2, "work"),
        ]
        rollup = RunUsageRollup.build("pr-1", rows)
        assert rollup.by_role["planner"].cost_usd == "0.400000"
        assert rollup.by_role["worker"].cost_usd == "1.500000"
        assert rollup.by_role["worker"].steps == 2
        assert rollup.by_role["worker"].input_tokens == 200
        assert rollup.total_cost_usd == "1.900000"

    def test_coverage_counts_unknown_rows_against_the_run(self):
        rows = [
            (_FakeUsage(id="u1"), 0, "agent"),
            (
                _FakeUsage(
                    id="u2", cost_usd=None, cost_source="unknown", input_tokens=None
                ),
                1,
                "script",
            ),
        ]
        rollup = RunUsageRollup.build("pr-1", rows)
        assert rollup.cost_coverage == 0.5
        assert rollup.by_source["unknown"] == 1
        assert rollup.step_count == 2

    def test_unpriced_rows_do_not_poison_the_total(self):
        """A null cost contributes nothing rather than making the sum null:
        'we could not price this' must not erase what we could price."""
        rows = [
            (_FakeUsage(id="u1", cost_usd=Decimal("0.250000")), 0, "agent"),
            (_FakeUsage(id="u2", cost_usd=None, cost_source="unknown"), 1, "script"),
        ]
        rollup = RunUsageRollup.build("pr-1", rows)
        assert rollup.total_cost_usd == "0.250000"

    def test_step_listing_carries_the_step_identity(self):
        """The dogfood gate compares this list against the run's StepRuns."""
        rollup = RunUsageRollup.build("pr-1", [(_FakeUsage(), 3, "implement")])
        assert rollup.steps[0].step_index == 3
        assert rollup.steps[0].step_name == "implement"
        assert rollup.steps[0].step_execution_id == "exec-1"
        assert rollup.steps[0].cost_usd == "0.100000"


class TestStepUsageRead:
    def test_json_columns_are_decoded(self):
        read = StepUsageRead.from_model(
            _FakeUsageRow(determinism='{"temperature": 0.0}', raw='{"a": 1}')
        )
        assert read.determinism == {"temperature": 0.0}
        assert read.raw == {"a": 1}

    def test_corrupt_json_is_reported_not_a_500(self):
        """Accounting must not break the thing it accounts for — on the read
        side too."""
        read = StepUsageRead.from_model(_FakeUsageRow(determinism="{not json", raw="[["))
        assert read.determinism == {"_unparseable": True}
        assert read.raw == {"_unparseable": True}

    def test_cost_is_a_string_on_the_way_out(self):
        read = StepUsageRead.from_model(_FakeUsageRow(cost_usd=Decimal("0.1841")))
        assert read.cost_usd == "0.184100"

    def test_absent_raw_stays_none(self):
        read = StepUsageRead.from_model(_FakeUsageRow(raw=None))
        assert read.raw is None


class TestUsageWireContract:
    """The shared pin (tdd/unit/control_runtime/usage_contract.py) checked
    against the SERVER schema in one process.

    The wrapper writes the manifest, run.py ships it, this endpoint parses
    it. Three components, one shape — and a drift on any one of them has to
    fail a test that names the side that drifted, not surface as a silently
    missing cost three phases later.
    """

    def test_pinned_keys_match_the_server_schema_field_for_field(self):
        assert set(TOP_LEVEL_KEYS) == set(UsageManifest.model_fields)

    def test_required_keys_are_exactly_the_ones_with_no_default(self):
        """Everything else is nullable BY DESIGN: a step whose CLI reported
        nothing must still be able to produce a valid manifest."""
        no_default = {
            name
            for name, field in UsageManifest.model_fields.items()
            if field.is_required()
        }
        assert no_default == set(REQUIRED_KEYS)

    def test_canonical_manifest_conforms_and_parses(self):
        assert_manifest_conforms(CANONICAL_MANIFEST, "SERVER (usage schema)")
        assert UsageManifest(**CANONICAL_MANIFEST).cost_usd == Decimal("0.1841")

    def test_fallback_manifest_conforms_and_parses(self):
        """The never-fail-a-step record is a first-class manifest, not a
        degenerate one that only some side accepts."""
        assert_manifest_conforms(FALLBACK_MANIFEST, "SERVER (usage schema)")
        parsed = UsageManifest(**FALLBACK_MANIFEST)
        assert parsed.cost_source == "unknown"
        assert parsed.cost_usd is None

    @pytest.mark.parametrize(
        "label,value", INVALID_MANIFESTS, ids=[label for label, _ in INVALID_MANIFESTS]
    )
    def test_no_producer_may_emit_these(self, label, value):
        assert manifest_violations(value), label

    def test_trial_iteration_id_is_forbidden_on_the_12_5_wire(self):
        """It lands with M13's trials table. A producer that ships it early
        is writing to a column that does not exist."""
        assert "trial_iteration_id" in FORBIDDEN_KEYS
        assert manifest_violations({**CANONICAL_MANIFEST, "trial_iteration_id": "ti1"})

    def test_float_dollars_are_a_contract_violation(self):
        """No floats for money, ever — including in the file the wrapper
        writes."""
        problems = manifest_violations({**CANONICAL_MANIFEST, "cost_usd": 0.1841})
        assert any("float" in p for p in problems)

    def test_assert_helper_names_the_side_that_drifted(self):
        with pytest.raises(AssertionError, match="PRODUCER"):
            assert_manifest_conforms({"version": 2}, "PRODUCER (agent wrapper)")


class TestGpuNodeRateConfig:
    """LAZYAF_GPU_NODE_RATES parsing: a pricing typo must never stop the
    backend from booting or 500 a telemetry POST."""

    def test_absent_env_is_an_empty_table(self):
        from app.config import _parse_gpu_node_rates

        assert _parse_gpu_node_rates(None) == {}
        assert _parse_gpu_node_rates("") == {}
        assert _parse_gpu_node_rates("   ") == {}

    def test_valid_json_object_is_parsed(self):
        from app.config import _parse_gpu_node_rates

        table = _parse_gpu_node_rates('{"n1": {"rate_usd_hour": "1.89"}}')
        assert table["n1"]["rate_usd_hour"] == "1.89"

    def test_malformed_json_degrades_to_empty(self):
        from app.config import _parse_gpu_node_rates

        assert _parse_gpu_node_rates("{not json") == {}

    def test_non_object_json_degrades_to_empty(self):
        from app.config import _parse_gpu_node_rates

        assert _parse_gpu_node_rates("[1, 2, 3]") == {}
