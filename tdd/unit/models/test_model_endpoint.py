"""Unit tests for the ModelEndpoint model and its wire shapes (M14.1).

Structure, vocabularies, defaults, indexes and DERIVED state - no I/O,
matching the unit-tier convention (table metadata + direct construction).

The two properties the milestone turns on are pinned here rather than merely
commented:

1. **The DB stores only `auth_secret_ref`** - the NAME of a backend env var,
   prefix-allowlisted so a row can never reference `ANTHROPIC_API_KEY` or
   `LAZYAF_STEP_AUTH_SECRET`. A stored config must not be an exfiltration
   route.
2. **`supports_tools` is THREE-STATE**, where `None` means never probed and
   REFUSES dispatch rather than silently routing the fallback protocol.
"""
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.model_endpoint import (  # noqa: E402
    AUTH_STYLES,
    ENDPOINT_FAILURE_THRESHOLD,
    HEALTH_STATES,
    IN_FLIGHT_STEP_STATUSES,
    NODE_ID_PREFIX,
    PROBE_STATUSES,
    REACH_MODES,
    SERVER_KINDS,
    ModelEndpoint,
    default_gpu_node_id,
    default_runner_label,
)
from app.models.pipeline import StepExecution  # noqa: E402
from app.schemas.model_endpoint import (  # noqa: E402
    CAPABILITY_INVALIDATING_FIELDS,
    ModelEndpointCreate,
    ModelEndpointUpdate,
    base_url_warning,
    capabilities_of,
    endpoint_read,
    validate_auth_fields,
)
from app.services.model_endpoints.probe import PROBE_TTL_SECONDS  # noqa: E402
from app.services.model_endpoints.resolve import (  # noqa: E402
    ENDPOINT_MODEL_PREFIX,
    endpoint_dispatch_refusal,
    parse_endpoint_reference,
)


def make_endpoint(**overrides) -> ModelEndpoint:
    """A registered, probed, no-auth LAN ollama endpoint - the FIRST-CLASS
    case, so it is what the fixture defaults to."""
    values = dict(
        id="e1",
        name="local-4090",
        base_url="http://192.168.1.50:11434/v1",
        model="qwen2.5-coder:32b",
        server_kind="ollama",
        auth_style="none",
        auth_secret_ref=None,
        auth_header_name=None,
        reach="direct",
        runner_label=None,
        rate_usd_hour=None,
        gpu_node_id="endpoint:local-4090",
        max_concurrency=1,
        request_timeout_seconds=300,
        context_window=None,
        max_output_tokens=None,
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        probe_status="ok",
        probe_detail="{}",
        probed_at=datetime.utcnow(),
        probed_from="backend",
        consecutive_failures=0,
        last_success_at=datetime.utcnow(),
        last_error=None,
        enabled=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    values.update(overrides)
    return ModelEndpoint(**values)


# -----------------------------------------------------------------------------
# Table shape
# -----------------------------------------------------------------------------

class TestTableShape:
    def test_table_name_and_identity(self):
        assert ModelEndpoint.__tablename__ == "model_endpoints"
        assert ModelEndpoint.__table__.c.id.primary_key is True

    def test_indexes_are_exactly_the_access_paths(self):
        """Every index earns its write cost: the unique handle every other
        surface uses, the usage-rollup join key, and the "who can take work"
        scan. Nothing else."""
        indexes = {
            index.name: (tuple(c.name for c in index.columns), index.unique)
            for index in ModelEndpoint.__table__.indexes
        }
        assert indexes == {
            "ix_model_endpoints_name": (("name",), True),
            "ix_model_endpoints_gpu_node_id": (("gpu_node_id",), False),
            "ix_model_endpoints_enabled_reach": (("enabled", "reach"), False),
        }

    def test_no_column_could_hold_a_secret_value(self):
        """The security decision of the phase, at the schema level: the row
        holds a REFERENCE and there is nowhere to put a key."""
        columns = set(ModelEndpoint.__table__.c.keys())
        assert "auth_secret_ref" in columns
        for forbidden in ("api_key", "auth_secret", "secret", "token", "auth_value"):
            assert forbidden not in columns

    def test_gpu_node_id_is_not_null_because_it_is_the_usage_join(self):
        """`step_usages` gained nothing in this milestone; the endpoint's cost
        history is reachable ONLY through this column."""
        assert ModelEndpoint.__table__.c.gpu_node_id.nullable is False

    def test_capability_booleans_are_nullable(self):
        for name in ("supports_tools", "supports_streaming", "reports_usage"):
            assert ModelEndpoint.__table__.c[name].nullable is True, name

    def test_rate_is_numeric_not_float(self):
        assert "NUMERIC" in str(ModelEndpoint.__table__.c.rate_usd_hour.type).upper()
        assert ModelEndpoint.__table__.c.rate_usd_hour.nullable is True

    def test_step_executions_carries_the_admission_gate_column(self):
        """Cross-agent contract #9: the in-flight count is read from the DB."""
        assert "model_endpoint_id" in StepExecution.__table__.c
        indexes = {
            index.name: tuple(c.name for c in index.columns)
            for index in StepExecution.__table__.indexes
        }
        assert indexes["ix_step_executions_endpoint_status"] == (
            "model_endpoint_id",
            "status",
        )

    def test_vocabularies(self):
        assert SERVER_KINDS == ("ollama", "vllm", "llamacpp", "lmstudio", "other")
        assert AUTH_STYLES == ("none", "bearer", "header")
        assert REACH_MODES == ("direct", "runner-local", "proxy")
        assert PROBE_STATUSES == ("unprobed", "ok", "degraded", "unreachable")
        assert set(HEALTH_STATES) == {
            "healthy",
            "stale",
            "degraded",
            "unhealthy",
            "unprobed",
        }
        assert IN_FLIGHT_STEP_STATUSES == (
            "assigned",
            "preparing",
            "running",
            "completing",
        )


# -----------------------------------------------------------------------------
# Derived state
# -----------------------------------------------------------------------------

class TestDerivedState:
    def test_node_and_label_defaults_share_one_spelling(self):
        assert default_gpu_node_id("local-4090") == "endpoint:local-4090"
        assert default_runner_label("local-4090") == "endpoint:local-4090"
        assert ENDPOINT_MODEL_PREFIX == NODE_ID_PREFIX == "endpoint:"

    def test_node_id_fits_the_usage_column(self):
        """`name` is capped at 40 precisely so this always holds."""
        longest = "a" * 39
        assert len(default_gpu_node_id(longest)) <= 64

    def test_probe_age_is_none_when_never_probed(self):
        endpoint = make_endpoint(probed_at=None, probe_status="unprobed")
        assert endpoint.probe_age_seconds is None
        assert endpoint.probe_stale is False

    def test_probe_stale_flips_at_the_ttl(self):
        fresh = make_endpoint(probed_at=datetime.utcnow() - timedelta(hours=1))
        stale = make_endpoint(
            probed_at=datetime.utcnow() - timedelta(seconds=PROBE_TTL_SECONDS + 60)
        )
        assert fresh.probe_stale is False
        assert stale.probe_stale is True

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({"probe_status": "unprobed", "probed_at": None}, "unprobed"),
            ({"probe_status": "ok"}, "healthy"),
            (
                {
                    "probe_status": "ok",
                    "probed_at": datetime.utcnow()
                    - timedelta(seconds=PROBE_TTL_SECONDS + 60),
                },
                "stale",
            ),
            ({"probe_status": "degraded"}, "degraded"),
            ({"probe_status": "unreachable", "consecutive_failures": 1}, "unhealthy"),
            (
                {
                    "probe_status": "ok",
                    "consecutive_failures": ENDPOINT_FAILURE_THRESHOLD,
                },
                "unhealthy",
            ),
        ],
    )
    def test_health_derivation_table(self, overrides, expected):
        """One stored health column would be a second writer that drifts from
        probe_status; this is the ONE derivation."""
        assert make_endpoint(**overrides).health == expected

    def test_degraded_outranks_stale_because_it_says_why(self):
        endpoint = make_endpoint(
            probe_status="degraded",
            probed_at=datetime.utcnow() - timedelta(seconds=PROBE_TTL_SECONDS + 60),
        )
        assert endpoint.health == "degraded"

    def test_gpu_fraction_is_one_over_max_concurrency(self):
        assert make_endpoint(max_concurrency=1).gpu_fraction == 1.0
        assert make_endpoint(max_concurrency=2).gpu_fraction == 0.5
        assert make_endpoint(max_concurrency=4).gpu_fraction == 0.25
        # A zero/None cap must not divide by zero on a hand-written row.
        assert make_endpoint(max_concurrency=0).gpu_fraction == 1.0

    def test_zero_rate_is_priced_and_null_rate_is_not(self):
        """The distinction decision 4 exists to preserve: `$0.00` is the
        honest claim "this cost no cash"; `null` is "we do not know"."""
        assert make_endpoint(rate_usd_hour=Decimal("0.000000")).priced is True
        assert make_endpoint(rate_usd_hour=Decimal("1.89")).priced is True
        assert make_endpoint(rate_usd_hour=None).priced is False

    def test_probe_detail_survives_corruption(self):
        endpoint = make_endpoint(probe_detail="not json{{{")
        assert endpoint.get_probe_detail() == {}

    def test_probe_detail_is_capped(self):
        endpoint = make_endpoint()
        endpoint.set_probe_detail({"body": "x" * 20_000, "probe_status": "degraded"})
        assert len(endpoint.probe_detail.encode("utf-8")) < 4096
        assert endpoint.get_probe_detail()["truncated"] is True


class TestContextWindowPrecedence:
    """wave8 s2.2: operator override, then the probe's discovery, then None -
    and the column is the OVERRIDE so a re-probe can never clobber it."""

    def test_override_wins_and_is_labelled(self):
        endpoint = make_endpoint(context_window=32768)
        endpoint.set_probe_detail({"context_window": 8192, "context_window_source": "ollama"})
        assert endpoint.effective_context_window == 32768
        assert endpoint.context_window_source == "override"

    def test_probe_discovery_is_used_when_there_is_no_override(self):
        endpoint = make_endpoint(context_window=None)
        endpoint.set_probe_detail(
            {"context_window": 32768, "context_window_source": "ollama"}
        )
        assert endpoint.effective_context_window == 32768
        assert endpoint.context_window_source == "ollama"

    def test_unknown_window_is_none_not_a_guess(self):
        endpoint = make_endpoint(context_window=None, probe_detail="{}")
        assert endpoint.effective_context_window is None
        assert endpoint.context_window_source is None


# -----------------------------------------------------------------------------
# The three-state capability and the dispatch refusals
# -----------------------------------------------------------------------------

class TestThreeStateSupportsTools:
    def test_none_refuses_dispatch(self):
        endpoint = make_endpoint(supports_tools=None, probe_status="ok")
        refusal = endpoint_dispatch_refusal(endpoint)
        assert refusal is not None
        assert "supports_tools is null" in refusal

    def test_false_is_usable_and_routes_the_fallback(self):
        """`False` is an OBSERVATION and the endpoint stays dispatchable -
        that is what makes `None` mean something different."""
        endpoint = make_endpoint(supports_tools=False, probe_status="degraded")
        assert endpoint_dispatch_refusal(endpoint) is None

    def test_unprobed_refuses_and_names_the_fix(self):
        endpoint = make_endpoint(
            probe_status="unprobed", probed_at=None, supports_tools=None
        )
        refusal = endpoint_dispatch_refusal(endpoint)
        assert "never been probed" in refusal
        assert "/probe" in refusal

    def test_disabled_refuses(self):
        assert "disabled" in endpoint_dispatch_refusal(make_endpoint(enabled=False))

    def test_three_consecutive_failures_refuse(self):
        endpoint = make_endpoint(
            probe_status="unreachable",
            consecutive_failures=ENDPOINT_FAILURE_THRESHOLD,
            last_error="Connection refused",
        )
        refusal = endpoint_dispatch_refusal(endpoint)
        assert "consecutive" in refusal
        assert "Connection refused" in refusal

    def test_two_failures_do_not_refuse(self):
        endpoint = make_endpoint(
            probe_status="degraded", consecutive_failures=ENDPOINT_FAILURE_THRESHOLD - 1
        )
        assert endpoint_dispatch_refusal(endpoint) is None

    def test_stale_does_not_refuse(self):
        """Blocking on staleness would make a working endpoint stop working
        overnight. Stale is amber, not red."""
        endpoint = make_endpoint(
            probed_at=datetime.utcnow() - timedelta(seconds=PROBE_TTL_SECONDS * 3)
        )
        assert endpoint.probe_stale is True
        assert endpoint_dispatch_refusal(endpoint) is None


class TestEndpointReferenceParsing:
    @pytest.mark.parametrize(
        "config,expected",
        [
            ({"endpoint": "local-4090"}, "local-4090"),
            ({"model": "endpoint:local-4090"}, "local-4090"),
            ({"endpoint": "local-4090", "model": "endpoint:other"}, "local-4090"),
            ({"model": "claude-haiku-4-5"}, None),
            ({}, None),
            (None, None),
            ({"model": "endpoint:"}, None),
        ],
    )
    def test_precedence_table(self, config, expected):
        assert parse_endpoint_reference(config) == expected


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class TestSchemas:
    def test_no_auth_is_the_default_path(self):
        """LAN ollama has no key, and a schema that makes "no auth" the
        exceptional branch is a schema that will grow a fake key."""
        created = ModelEndpointCreate(
            name="local-4090", base_url="http://x:11434/v1", model="qwen"
        )
        assert created.auth_style == "none"
        assert created.auth_secret_ref is None
        assert created.reach == "direct"
        assert created.max_concurrency == 1
        assert created.enabled is True

    @pytest.mark.parametrize(
        "ref",
        ["ANTHROPIC_API_KEY", "LAZYAF_STEP_AUTH_SECRET", "LAZYAF_RUNNER_AUTH_SECRET",
         "PATH", "lazyaf_endpoint_lower", "LAZYAF_ENDPOINTX", ""],
    )
    def test_forbidden_secret_refs_are_refused(self, ref):
        with pytest.raises(ValueError):
            validate_auth_fields("bearer", ref, None)

    def test_allowed_secret_ref(self):
        validate_auth_fields("bearer", "LAZYAF_ENDPOINT_LOCAL_4090", None)

    def test_header_style_needs_a_header_name(self):
        with pytest.raises(ValueError):
            validate_auth_fields("header", "LAZYAF_ENDPOINT_X", None)
        validate_auth_fields("header", "LAZYAF_ENDPOINT_X", "x-api-key")

    @pytest.mark.parametrize("name", ["Local-4090", "-local", "a" * 41, "local_4090", ""])
    def test_bad_names_are_refused(self, name):
        with pytest.raises(Exception):
            ModelEndpointCreate(name=name, base_url="http://x/v1", model="m")

    def test_base_url_is_normalized_not_rewritten(self):
        created = ModelEndpointCreate(
            name="e", base_url="http://x:11434/v1/", model="m"
        )
        assert created.base_url == "http://x:11434/v1"
        assert base_url_warning("http://x:11434/v1") is None
        warning = base_url_warning("http://x:11434")
        assert warning is not None and "never rewrites" in warning

    def test_capability_invalidating_fields(self):
        assert CAPABILITY_INVALIDATING_FIELDS == frozenset(
            {
                "base_url",
                "model",
                "server_kind",
                "auth_style",
                "auth_secret_ref",
                "auth_header_name",
            }
        )

    def test_patch_rejects_an_explicit_null_on_a_not_null_column(self):
        with pytest.raises(Exception):
            ModelEndpointUpdate(base_url=None, **{})  # explicit null
        # ...but absent is fine and means "leave it".
        assert ModelEndpointUpdate().model_dump(exclude_unset=True) == {}

    def test_capabilities_snapshot_shape(self):
        """The 10 keys the agent-config `capabilities` block carries
        (cross-agent contract #2's CAPABILITY_KEYS)."""
        caps = capabilities_of(make_endpoint(context_window=32768))
        assert set(caps.model_dump()) == {
            "supports_tools",
            "supports_streaming",
            "reports_usage",
            "context_window",
            "max_output_tokens",
            "probe_status",
            "probed_at",
            "probed_from",
            "probe_age_seconds",
            "stale",
        }
        assert caps.context_window == 32768

    def test_read_projection_carries_the_ref_and_never_a_value(self):
        endpoint = make_endpoint(
            auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_DEMO"
        )
        read = endpoint_read(endpoint, in_flight=2)
        payload = json.dumps(read.model_dump(mode="json"))
        assert "LAZYAF_ENDPOINT_DEMO" in payload
        assert read.in_flight == 2
        assert read.pricing.gpu_node_id == "endpoint:local-4090"
        assert read.pricing.gpu_fraction == 1.0
        assert read.health == "healthy"

    def test_pricing_block_matches_the_wire_contract_keys(self):
        read = endpoint_read(make_endpoint())
        assert set(read.pricing.model_dump()) == {
            "gpu_node_id",
            "gpu_fraction",
            "priced",
        }


class TestDispatchWarnings:
    """R1: warn plus refresh is the only honest response to a stale record,
    and every degraded-but-usable condition has to SAY SO in the step log
    rather than quietly changing what the step does."""

    def test_a_healthy_endpoint_warns_about_nothing(self):
        from app.services.model_endpoints.resolve import endpoint_dispatch_warning

        endpoint = make_endpoint(context_window=32768, reports_usage=True)
        assert endpoint_dispatch_warning(endpoint) is None

    def test_stale_warns_and_names_the_age(self):
        from app.services.model_endpoints.resolve import endpoint_dispatch_warning

        endpoint = make_endpoint(
            context_window=32768,
            probed_at=datetime.utcnow() - timedelta(seconds=PROBE_TTL_SECONDS + 3600),
        )
        warning = endpoint_dispatch_warning(endpoint)
        assert "re-probing in the background" in warning
        assert "25h old" in warning

    def test_unknown_context_window_warns_about_the_assumption(self):
        from app.services.model_endpoints.resolve import endpoint_dispatch_warning

        warning = endpoint_dispatch_warning(make_endpoint(context_window=None))
        assert "assume 8192" in warning

    def test_no_usage_reporting_warns_that_tokens_will_be_null(self):
        from app.services.model_endpoints.resolve import endpoint_dispatch_warning

        warning = endpoint_dispatch_warning(
            make_endpoint(context_window=32768, reports_usage=False)
        )
        assert "token counts will be null" in warning

    def test_proxy_reach_warns_on_every_use_not_only_in_the_docs(self):
        from app.services.model_endpoints.resolve import endpoint_dispatch_warning

        warning = endpoint_dispatch_warning(
            make_endpoint(context_window=32768, reach="proxy")
        )
        assert "bottleneck" in warning
