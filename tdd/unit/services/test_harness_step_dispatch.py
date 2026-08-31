"""Dispatching an `openai-harness` step (M14 s5.3 / s6.1 / s6.2).

The four things this pins, in the order the design argues them:

1. **The resolver.** `model: "endpoint:<name>"` is the ONE sugar spelling and
   `resolve_step_endpoint` is its ONE parser (contract #4). That is what makes
   14.3 cheap: `step_config["model"]` is the field the card picker, the
   playground, the pipeline step form and `MatrixModelEntry.model` ALL already
   populate, so a self-hosted model reaches the dispatcher from every one of
   them with zero schema changes.
2. **The routing sugar.** A `runner-local` endpoint injects one `has:` label
   before `ExecutionRouter.decide` runs, and NOTHING else in 12.6 changes
   (contract #8). A `direct` endpoint with no operator `requires:` stays LOCAL:
   a global accidental flip to remote is as much a regression as the reverse.
3. **The three lines wave 5 named.** `gpu_node_id` and `gpu_fraction` reach
   `execution_context`, which is what `local_executor` and `runner_protocol`
   already stamp into container env and what `run.py` already copies onto the
   usage manifest.
4. **The cost invariant.** A harness `StepUsage` row may NEVER carry
   `cost_source == "cli-reported"`. That value is what the board reads as "the
   provider billed us this amount", and no self-hosted endpoint can make that
   claim.
"""
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.models.model_endpoint import (  # noqa: E402
    ModelEndpoint,
    default_gpu_node_id,
    default_runner_label,
)
from app.models.pipeline import (  # noqa: E402
    Pipeline,
    PipelineRun,
    StepExecution,
    StepRun,
)
from app.models.repo import Repo  # noqa: E402
from app.models.usage import StepUsage, UsageCostSource  # noqa: E402
from app.schemas.usage import UsageManifest  # noqa: E402
from app.services.model_endpoints.resolve import (  # noqa: E402
    ENDPOINT_MODEL_PREFIX,
    parse_endpoint_reference,
    resolve_step_endpoint,
)
from app.services.pipeline_executor import (  # noqa: E402
    AGENT_USAGE_PROVIDER,
    DEFAULT_AGENT_IMAGE,
    HARNESS_AGENT,
    PipelineExecutor,
    endpoint_wire_block,
    harness_wire_block,
    inject_endpoint_requirements,
)
from app.services.usage_ingestion import ingest_usage  # noqa: E402
from app.services.usage_pricing import resolve_node_rate  # noqa: E402
from app.services.workspace.execution_router import ExecutionRouter  # noqa: E402


async def _make_endpoint(db, name="local-4090", **overrides):
    fields = dict(
        id=str(uuid4()),
        name=name,
        base_url="http://172.17.0.1:11434/v1",
        model="qwen2.5-coder:32b",
        server_kind="ollama",
        auth_style="none",
        reach="direct",
        runner_label=None,
        rate_usd_hour=None,
        gpu_node_id=default_gpu_node_id(name),
        max_concurrency=1,
        request_timeout_seconds=300,
        probe_status="ok",
        probe_detail="{}",
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        consecutive_failures=0,
        enabled=True,
    )
    fields.update(overrides)
    if fields["reach"] == "runner-local" and fields["runner_label"] is None:
        fields["runner_label"] = default_runner_label(fields["name"])
    endpoint = ModelEndpoint(**fields)
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


# --------------------------------------------------------------------------
# 1. The resolver
# --------------------------------------------------------------------------

class TestResolverPrecedence:
    def test_the_explicit_endpoint_key_wins(self):
        assert parse_endpoint_reference(
            {"endpoint": "local-4090", "model": "endpoint:other"}
        ) == "local-4090"

    def test_the_model_sugar_is_read_when_there_is_no_explicit_key(self):
        assert parse_endpoint_reference(
            {"model": f"{ENDPOINT_MODEL_PREFIX}local-4090"}
        ) == "local-4090"

    def test_an_ordinary_model_string_names_no_endpoint(self):
        assert parse_endpoint_reference({"model": "claude-haiku-4-5"}) is None

    def test_neither_key_names_no_endpoint(self):
        assert parse_endpoint_reference({"agent": "openai-harness"}) is None

    async def test_it_resolves_by_name_and_by_id(self, db_session):
        endpoint = await _make_endpoint(db_session)
        by_name = await resolve_step_endpoint(
            db_session, {"model": "endpoint:local-4090"}, "implement"
        )
        by_id = await resolve_step_endpoint(
            db_session, {"endpoint": endpoint.id}, "implement"
        )
        assert by_name.id == by_id.id == endpoint.id


class TestResolverRefusals:
    async def test_no_endpoint_named_at_all(self, db_session):
        with pytest.raises(ValueError, match="names no endpoint"):
            await resolve_step_endpoint(db_session, {"agent": HARNESS_AGENT}, "s")

    async def test_an_unknown_name_lists_the_ones_that_exist(self, db_session):
        await _make_endpoint(db_session, name="local-4090")
        await _make_endpoint(db_session, name="runpod-a100")
        with pytest.raises(ValueError) as excinfo:
            await resolve_step_endpoint(db_session, {"endpoint": "typo"}, "s")
        message = str(excinfo.value)
        assert "local-4090" in message and "runpod-a100" in message

    async def test_a_disabled_endpoint_is_refused(self, db_session):
        await _make_endpoint(db_session, enabled=False)
        with pytest.raises(ValueError, match="is disabled"):
            await resolve_step_endpoint(db_session, {"endpoint": "local-4090"}, "s")

    async def test_an_unprobed_endpoint_is_refused_with_the_fix_in_the_message(
        self, db_session
    ):
        """NOT a probe-on-first-use: a 30-minute agent step is not the place
        to discover the model cannot tool-call."""
        await _make_endpoint(
            db_session, probe_status="unprobed", supports_tools=None
        )
        with pytest.raises(ValueError) as excinfo:
            await resolve_step_endpoint(db_session, {"endpoint": "local-4090"}, "s")
        assert "never been probed" in str(excinfo.value)
        assert "/probe" in str(excinfo.value)

    async def test_three_consecutive_failures_is_refused(self, db_session):
        await _make_endpoint(
            db_session,
            probe_status="unreachable",
            consecutive_failures=3,
            last_error="connection refused",
        )
        with pytest.raises(ValueError) as excinfo:
            await resolve_step_endpoint(db_session, {"endpoint": "local-4090"}, "s")
        assert "3 consecutive times" in str(excinfo.value)
        assert "connection refused" in str(excinfo.value)

    async def test_two_failures_still_dispatches(self, db_session):
        """Three in a row is not a blip; two is."""
        await _make_endpoint(
            db_session, probe_status="unreachable", consecutive_failures=2
        )
        endpoint = await resolve_step_endpoint(
            db_session, {"endpoint": "local-4090"}, "s"
        )
        assert endpoint.name == "local-4090"


# --------------------------------------------------------------------------
# 2. The routing sugar (contract #8)
# --------------------------------------------------------------------------

class TestRunnerLocalInjection:
    def test_the_label_is_appended_and_the_route_becomes_remote(self):
        endpoint = SimpleNamespace(
            reach="runner-local", runner_label="endpoint:local-4090",
            name="local-4090",
        )
        routed = inject_endpoint_requirements({"agent": HARNESS_AGENT}, endpoint)
        decision = ExecutionRouter().decide("agent", routed)

        assert routed["requires"] == {"has": ["endpoint:local-4090"]}
        assert (decision.mode, decision.reason) == ("remote", "runner-pin")
        assert decision.requirements["has"] == ["endpoint:local-4090"]

    def test_an_operators_existing_requires_is_merged_not_replaced(self):
        endpoint = SimpleNamespace(
            reach="runner-local", runner_label="endpoint:local-4090",
            name="local-4090",
        )
        routed = inject_endpoint_requirements(
            {"requires": {"has": ["docker"], "arch": "arm64"}}, endpoint
        )
        assert routed["requires"]["has"] == ["docker", "endpoint:local-4090"]
        assert routed["requires"]["arch"] == "arm64"

    def test_a_string_has_value_is_coerced_to_a_list(self):
        endpoint = SimpleNamespace(
            reach="runner-local", runner_label="endpoint:x", name="x"
        )
        routed = inject_endpoint_requirements({"requires": {"has": "docker"}}, endpoint)
        assert routed["requires"]["has"] == ["docker", "endpoint:x"]

    def test_the_label_is_never_added_twice(self):
        endpoint = SimpleNamespace(
            reach="runner-local", runner_label="endpoint:x", name="x"
        )
        once = inject_endpoint_requirements({}, endpoint)
        twice = inject_endpoint_requirements(once, endpoint)
        assert twice["requires"]["has"] == ["endpoint:x"]

    def test_a_missing_runner_label_defaults_to_the_endpoint_coordinate(self):
        endpoint = SimpleNamespace(
            reach="runner-local", runner_label=None, name="local-4090"
        )
        routed = inject_endpoint_requirements({}, endpoint)
        assert routed["requires"]["has"] == ["endpoint:local-4090"]

    @pytest.mark.parametrize("reach", ["direct", "proxy"])
    def test_a_non_runner_local_endpoint_stays_LOCAL(self, reach):
        """A global accidental flip to remote is as much a regression as the
        reverse."""
        endpoint = SimpleNamespace(reach=reach, runner_label=None, name="x")
        routed = inject_endpoint_requirements({"agent": HARNESS_AGENT}, endpoint)
        decision = ExecutionRouter().decide("agent", routed)

        assert "requires" not in routed
        assert decision.mode == "local"
        assert decision.reason == "agent-default-local"

    def test_no_endpoint_at_all_leaves_the_config_untouched(self):
        config = {"agent": "claude-code"}
        assert inject_endpoint_requirements(config, None) is config

    async def test_dispatch_persists_the_injected_requirement(self, db_session):
        """The end-to-end shape: the resolver + the injection + the router,
        driven through the executor's own routing decision."""
        endpoint = await _make_endpoint(db_session, reach="runner-local")
        executor = PipelineExecutor()
        step_config = {"agent": HARNESS_AGENT, "model": "endpoint:local-4090"}

        resolved = await executor._resolve_step_endpoint(
            db_session, "agent", step_config, "implement"
        )
        routed = inject_endpoint_requirements(step_config, resolved)
        mode, reason, requirements = executor._decide_route(
            "agent", routed, "implement"
        )

        assert resolved.id == endpoint.id
        assert mode.value == "remote" and reason == "runner-pin"
        assert requirements == {"has": ["endpoint:local-4090"]}


# --------------------------------------------------------------------------
# 3. The three lines wave 5 named
# --------------------------------------------------------------------------

class TestCostCoordinatesReachTheContainer:
    async def test_gpu_node_id_and_fraction_land_in_the_execution_context(
        self, db_session
    ):
        endpoint = await _make_endpoint(db_session, max_concurrency=2)
        executor = PipelineExecutor()
        pipeline_run = SimpleNamespace(id=str(uuid4()))
        step_run = SimpleNamespace(id=str(uuid4()), step_index=0, step_name="implement")

        exec_config, exec_context = executor._build_local_execution_config(
            pipeline_run, step_run, "agent",
            {"agent": HARNESS_AGENT, "model": "endpoint:local-4090"},
            1800, None, endpoint=endpoint,
        )

        assert exec_context["gpu_node_id"] == "endpoint:local-4090"
        # 1/max_concurrency: the node bills by the hour regardless of how many
        # steps share it, so charging each of K concurrent steps 1.0 would
        # multiply the node's real cost by K.
        assert exec_context["gpu_fraction"] == 0.5
        assert exec_context["model_endpoint_id"] == endpoint.id
        assert exec_config["usage_provider"] == "openai-compatible"
        assert exec_config["image"] == DEFAULT_AGENT_IMAGE[HARNESS_AGENT]
        assert exec_config["secret_environment"] == {}

    async def test_a_non_harness_step_gains_no_node_coordinates(self, db_session):
        executor = PipelineExecutor()
        pipeline_run = SimpleNamespace(id=str(uuid4()))
        step_run = SimpleNamespace(id=str(uuid4()), step_index=0, step_name="s")
        _config, context = executor._build_local_execution_config(
            pipeline_run, step_run, "script", {"command": "true"}, 300, None,
        )
        assert "gpu_node_id" not in context and "gpu_fraction" not in context

    def test_the_provider_is_the_one_run_pys_fallback_record_will_use(self):
        assert AGENT_USAGE_PROVIDER[HARNESS_AGENT] == "openai-compatible"


class TestNodeRateResolution:
    async def test_the_endpoint_row_beats_the_env_table(self, db_session, monkeypatch):
        from app.config import get_settings

        await _make_endpoint(db_session, rate_usd_hour=Decimal("1.500000"))
        settings = get_settings()
        monkeypatch.setattr(
            settings, "gpu_node_rates",
            {"endpoint:local-4090": {"rate_usd_hour": "99.00"}}, raising=False,
        )

        rate = await resolve_node_rate(db_session, "endpoint:local-4090")
        assert rate == Decimal("1.500000")

    async def test_it_falls_back_to_the_env_table_for_a_non_endpoint_node(
        self, db_session, monkeypatch
    ):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "gpu_node_rates",
            {"runpod-a100-80g": {"rate_usd_hour": "1.89"}}, raising=False,
        )
        assert await resolve_node_rate(db_session, "runpod-a100-80g") == Decimal("1.89")

    async def test_an_endpoint_with_no_rate_falls_through_to_the_env_table(
        self, db_session, monkeypatch
    ):
        from app.config import get_settings

        await _make_endpoint(db_session, rate_usd_hour=None)
        monkeypatch.setattr(
            get_settings(), "gpu_node_rates",
            {"endpoint:local-4090": {"rate_usd_hour": "2.00"}}, raising=False,
        )
        assert await resolve_node_rate(db_session, "endpoint:local-4090") == Decimal("2.00")

    async def test_a_null_rate_everywhere_is_None_not_zero(
        self, db_session, monkeypatch
    ):
        """`null` is "we do not know"; `0.00` is "owned hardware, marginal
        cash cost". Keeping the two distinguishable is the whole point."""
        from app.config import get_settings

        await _make_endpoint(db_session, rate_usd_hour=None)
        monkeypatch.setattr(get_settings(), "gpu_node_rates", {}, raising=False)
        assert await resolve_node_rate(db_session, "endpoint:local-4090") is None

    async def test_a_zero_rate_on_the_row_is_a_REAL_answer(self, db_session):
        await _make_endpoint(db_session, rate_usd_hour=Decimal("0"))
        rate = await resolve_node_rate(db_session, "endpoint:local-4090")
        assert rate is not None and rate == Decimal("0.000000")

    async def test_an_unknown_node_is_None(self, db_session):
        assert await resolve_node_rate(db_session, "never-heard-of-it") is None
        assert await resolve_node_rate(db_session, None) is None


# --------------------------------------------------------------------------
# 4. The cost-source invariant, asserted THROUGH ingest_usage
# --------------------------------------------------------------------------

async def _make_execution(db, endpoint_id=None):
    repo = Repo(id=str(uuid4()), name="r", default_branch="main")
    db.add(repo)
    await db.commit()
    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="p", steps="[]",
                        triggers="[]")
    db.add(pipeline)
    await db.commit()
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    db.add(run)
    await db.commit()
    step_run = StepRun(id=str(uuid4()), pipeline_run_id=run.id, step_index=0,
                       step_name="implement", status="running")
    db.add(step_run)
    await db.commit()
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run.id}:0:{step_run.id}",
        step_run_id=step_run.id,
        status="running",
        model_endpoint_id=endpoint_id,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution


def _manifest(**overrides):
    payload = dict(
        version=1,
        provider="openai-compatible",
        cost_source="unknown",
        model="qwen2.5-coder:32b",
        input_tokens=1200,
        output_tokens=300,
        wall_clock_ms=41000,
        container_seconds=45.0,
        gpu_node_id="endpoint:local-4090",
        gpu_fraction=1.0,
        determinism={"temperature": 0, "seed": 7, "top_p": None},
        raw={"harness": {"stop_reason": "finish", "endpoint_http_errors": 0}},
    )
    payload.update(overrides)
    return UsageManifest(**payload)


class TestHarnessCostSource:
    async def test_a_priced_endpoint_produces_a_gpu_node_row(self, db_session):
        endpoint = await _make_endpoint(db_session, rate_usd_hour=Decimal("1.200000"))
        execution = await _make_execution(db_session, endpoint.id)

        usage = await ingest_usage(db_session, execution, _manifest())

        assert usage.cost_source == UsageCostSource.GPU_NODE.value
        assert usage.cost_usd is not None and usage.cost_usd > 0
        assert usage.provider == "openai-compatible"

    async def test_a_zero_rate_produces_a_REAL_zero_not_an_absence(self, db_session):
        endpoint = await _make_endpoint(db_session, rate_usd_hour=Decimal("0"))
        execution = await _make_execution(db_session, endpoint.id)

        usage = await ingest_usage(db_session, execution, _manifest())

        assert usage.cost_source == UsageCostSource.GPU_NODE.value
        assert usage.cost_usd == Decimal("0.000000")

    async def test_an_unpriced_endpoint_produces_unknown_and_a_null_cost(
        self, db_session, monkeypatch
    ):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "gpu_node_rates", {}, raising=False)
        endpoint = await _make_endpoint(db_session, rate_usd_hour=None)
        execution = await _make_execution(db_session, endpoint.id)

        usage = await ingest_usage(db_session, execution, _manifest())

        assert usage.cost_source == UsageCostSource.UNKNOWN.value
        assert usage.cost_usd is None

    async def test_a_manifest_that_CLAIMS_a_cost_cannot_make_it_cli_reported(
        self, db_session
    ):
        """A harness StepUsage row may NEVER carry `cli-reported`: that value
        is what the board reads as "the provider billed us this amount", and
        no self-hosted endpoint can make that claim.

        The harness itself always sends `cost_usd = null` and
        `cost_source = "unknown"`; that half is pinned container-side against
        `runner_common.harness.constants` in
        `tdd/unit/control_runtime/test_harness_runtime_contract.py` (this
        package cannot import runner_common). This asserts the SERVER-side
        half: what happens when a manifest tries anyway.
        """
        endpoint = await _make_endpoint(db_session, rate_usd_hour=Decimal("1.000000"))
        execution = await _make_execution(db_session, endpoint.id)
        liar = _manifest(cost_usd=Decimal("9.99"), cost_source="cli-reported")

        usage = await ingest_usage(db_session, execution, liar)

        # The claim is DROPPED, loudly, and the row falls through to the
        # honest basis. This is the invariant `verify_executor`'s assertion 18
        # asserts on the dogfood lane: no row may carry `cli-reported`
        # together with `openai-compatible`.
        assert usage.provider == "openai-compatible"
        assert usage.cost_source != UsageCostSource.CLI_REPORTED.value
        assert usage.cost_source == UsageCostSource.GPU_NODE.value
        assert usage.cost_usd != Decimal("9.990000")

    async def test_an_unpriceable_liar_lands_as_unknown_not_as_a_bill(
        self, db_session, monkeypatch
    ):
        """Dropping the claim must not accidentally invent a number either."""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "gpu_node_rates", {}, raising=False)
        endpoint = await _make_endpoint(db_session, rate_usd_hour=None)
        execution = await _make_execution(db_session, endpoint.id)

        usage = await ingest_usage(
            db_session, execution,
            _manifest(cost_usd=Decimal("9.99"), cost_source="cli-reported"),
        )

        assert usage.cost_source == UsageCostSource.UNKNOWN.value
        assert usage.cost_usd is None

    async def test_a_commercial_provider_may_still_report_its_own_bill(
        self, db_session
    ):
        """The rule is narrow on purpose: claude-code and gemini DO receive an
        invoice, and `cli-reported` is the honest source for them."""
        execution = await _make_execution(db_session, None)
        usage = await ingest_usage(
            db_session, execution,
            _manifest(provider="anthropic", model="claude-haiku-4-5",
                      cost_usd=Decimal("0.184100"), cost_source="cli-reported",
                      gpu_node_id=None, raw=None),
        )
        assert usage.cost_source == UsageCostSource.CLI_REPORTED.value
        assert usage.cost_usd == Decimal("0.184100")

    async def test_the_harness_never_sends_a_cost_so_the_row_is_node_priced(
        self, db_session
    ):
        """The realistic path, end to end: what the harness ACTUALLY writes.

        `"unknown"` is `runner_common.harness.constants.HARNESS_COST_SOURCE`,
        pinned to this literal in the control_runtime contract test.
        """
        endpoint = await _make_endpoint(db_session, rate_usd_hour=Decimal("1.000000"))
        execution = await _make_execution(db_session, endpoint.id)
        manifest = _manifest(cost_usd=None, cost_source="unknown")

        usage = await ingest_usage(db_session, execution, manifest)

        assert usage.cost_source == UsageCostSource.GPU_NODE.value
        assert usage.cost_source != UsageCostSource.CLI_REPORTED.value


class TestEndpointHealthFromRealWork:
    async def test_a_clean_step_zeroes_the_failure_counter(self, db_session):
        endpoint = await _make_endpoint(db_session, consecutive_failures=2)
        execution = await _make_execution(db_session, endpoint.id)

        await ingest_usage(db_session, execution, _manifest())

        await db_session.refresh(endpoint)
        assert endpoint.consecutive_failures == 0
        assert endpoint.last_success_at is not None

    async def test_an_endpoint_stop_reason_bumps_the_failure_counter(self, db_session):
        endpoint = await _make_endpoint(db_session)
        execution = await _make_execution(db_session, endpoint.id)
        manifest = _manifest(
            raw={"harness": {"stop_reason": "endpoint", "endpoint_http_errors": 3,
                             "stop_error": "HTTP 503 from the model server"}}
        )

        await ingest_usage(db_session, execution, manifest)

        await db_session.refresh(endpoint)
        assert endpoint.consecutive_failures == 1
        assert "503" in (endpoint.last_error or "")

    async def test_a_model_capability_failure_is_not_an_endpoint_failure(
        self, db_session
    ):
        """Conflating them would make a perfectly working endpoint look down."""
        endpoint = await _make_endpoint(db_session)
        execution = await _make_execution(db_session, endpoint.id)
        manifest = _manifest(
            raw={"harness": {"stop_reason": "unparseable",
                             "endpoint_http_errors": 0}}
        )

        await ingest_usage(db_session, execution, manifest)

        await db_session.refresh(endpoint)
        assert endpoint.consecutive_failures == 0

    async def test_two_drifting_steps_demote_supports_tools(self, db_session):
        """The teeth behind "a probe that lies": the capability record is
        corrected BY THE WORK, visibly, within two steps."""
        endpoint = await _make_endpoint(db_session)
        manifest = _manifest(
            raw={"harness": {"stop_reason": "finish", "endpoint_http_errors": 0,
                             "probe_drift": True}}
        )

        for index in range(2):
            execution = await _make_execution(db_session, endpoint.id)
            await ingest_usage(db_session, execution, manifest)

        await db_session.refresh(endpoint)
        assert endpoint.supports_tools is False
        assert endpoint.probe_status == "degraded"
        assert endpoint.get_probe_detail()["demoted_reason"]

    async def test_a_usage_row_with_no_endpoint_is_untouched(self, db_session):
        """Every non-harness step in the platform takes this path."""
        execution = await _make_execution(db_session, None)
        usage = await ingest_usage(db_session, execution, _manifest())
        assert usage is not None


# --------------------------------------------------------------------------
# The wire blocks the dispatcher builds
# --------------------------------------------------------------------------

class TestWireBlocks:
    async def test_the_endpoint_block_snapshots_the_capability_record(self, db_session):
        endpoint = await _make_endpoint(db_session, context_window=16384)
        block = endpoint_wire_block(endpoint)

        assert block["name"] == "local-4090"
        assert block["capabilities"]["context_window"] == 16384
        assert block["capabilities"]["supports_tools"] is True
        assert block["pricing"]["gpu_node_id"] == "endpoint:local-4090"
        assert block["pricing"]["priced"] is False

    async def test_the_harness_block_defaults_are_the_named_budgets(self, db_session):
        endpoint = await _make_endpoint(db_session)
        block = harness_wire_block({}, endpoint, 1800)

        assert block["mode"] == "auto"
        assert block["max_iterations"] == 40
        assert block["max_total_tokens"] == 400_000
        assert block["time_budget_seconds"] == 1740
        assert block["temperature"] == 0

    async def test_the_operator_can_override_every_budget(self, db_session):
        endpoint = await _make_endpoint(db_session)
        block = harness_wire_block(
            {"harness": {"mode": "text", "max_iterations": 8, "seed": 42,
                         "time_budget_seconds": 120}},
            endpoint, 1800,
        )
        assert block["mode"] == "text"
        assert block["max_iterations"] == 8
        assert block["seed"] == 42
        assert block["time_budget_seconds"] == 120

    async def test_require_changes_follows_whether_the_step_commits(self, db_session):
        """An analysis-only step (`commit: false`, "review this and report")
        legitimately changes nothing."""
        endpoint = await _make_endpoint(db_session)
        assert harness_wire_block({}, endpoint, 1800)["require_changes"] is True
        assert harness_wire_block(
            {"commit": False}, endpoint, 1800
        )["require_changes"] is False
        assert harness_wire_block(
            {"commit": False, "harness": {"require_changes": True}}, endpoint, 1800
        )["require_changes"] is True

    async def test_a_malformed_harness_block_is_refused_loudly(self, db_session):
        endpoint = await _make_endpoint(db_session)
        with pytest.raises(ValueError, match="must be a mapping"):
            harness_wire_block({"harness": ["mode=text"]}, endpoint, 1800)


# --------------------------------------------------------------------------
# The whole dispatch sequence, through the REAL producers (R6)
# --------------------------------------------------------------------------

class TestFullAgentPayload:
    """Drives `_build_local_execution_config` -> `_attach_agent_payload` ->
    `generate_agent_config` on real rows, which is the only way to see what a
    harness step's agent config file ACTUALLY looks like."""

    async def _dispatch(self, db, endpoint, step_config, monkeypatch=None):
        repo = Repo(id=str(uuid4()), name="r", default_branch="main",
                    is_ingested=True)
        db.add(repo)
        await db.commit()
        pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci",
                            steps="[]", triggers="[]")
        db.add(pipeline)
        await db.commit()
        run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id,
                          status="running", trigger_context="{}")
        db.add(run)
        await db.commit()
        step_run = StepRun(id=str(uuid4()), pipeline_run_id=run.id, step_index=0,
                           step_name="implement", status="running", logs="")
        db.add(step_run)
        await db.commit()

        executor = PipelineExecutor()
        exec_config, exec_context = executor._build_local_execution_config(
            run, step_run, "agent", step_config, 1800, None, endpoint=endpoint,
        )
        await executor._attach_agent_payload(
            db, run, pipeline, repo, step_run, step_config, exec_config,
            endpoint=endpoint,
        )
        return exec_config, exec_context

    async def test_the_agent_config_file_carries_both_new_blocks(self, db_session):
        from app.services.control_layer.workspace import generate_agent_config

        endpoint = await _make_endpoint(db_session)
        exec_config, _ctx = await self._dispatch(
            db_session, endpoint,
            {"agent": HARNESS_AGENT, "model": "endpoint:local-4090",
             "task": "Add rate limiting", "commit": False},
        )

        rendered = generate_agent_config(**exec_config["agent"])

        assert rendered["agent"] == HARNESS_AGENT
        assert rendered["endpoint"]["name"] == "local-4090"
        assert rendered["endpoint"]["base_url"] == "http://172.17.0.1:11434/v1"
        assert rendered["harness"]["mode"] == "auto"
        # `endpoint:<name>` is the COORDINATE; `endpoint.model` is what is
        # actually driven and what StepUsage.model will record.
        assert rendered["model"] == "qwen2.5-coder:32b"
        assert rendered["endpoint"]["model"] == rendered["model"]
        # `commit: false` -> the success-with-no-change refusal is off.
        assert rendered["harness"]["require_changes"] is False

    async def test_the_secret_is_in_secret_environment_and_nowhere_else(
        self, db_session, monkeypatch
    ):
        import json as _json

        from app.services.control_layer.workspace import generate_agent_config
        from app.services.model_endpoints.secrets import HARNESS_API_KEY_ENV

        sentinel = "sk-planted-dispatch-key-000000"
        monkeypatch.setenv("LAZYAF_ENDPOINT_LOCAL_4090", sentinel)
        endpoint = await _make_endpoint(
            db_session, auth_style="bearer",
            auth_secret_ref="LAZYAF_ENDPOINT_LOCAL_4090",
        )
        exec_config, _ctx = await self._dispatch(
            db_session, endpoint,
            {"agent": HARNESS_AGENT, "model": "endpoint:local-4090", "task": "x"},
        )

        assert exec_config["secret_environment"] == {HARNESS_API_KEY_ENV: sentinel}
        rendered = generate_agent_config(**exec_config["agent"])
        assert sentinel not in _json.dumps(rendered)
        assert rendered["endpoint"]["auth_env"] == HARNESS_API_KEY_ENV

    async def test_a_no_auth_endpoint_dispatches_with_no_secret_at_all(
        self, db_session
    ):
        """The FIRST-CLASS case, end to end: the step still dispatches."""
        endpoint = await _make_endpoint(db_session, auth_style="none")
        exec_config, _ctx = await self._dispatch(
            db_session, endpoint,
            {"agent": HARNESS_AGENT, "model": "endpoint:local-4090", "task": "x"},
        )
        assert exec_config["secret_environment"] == {}

    async def test_a_missing_backend_variable_fails_dispatch_naming_the_ref(
        self, db_session, monkeypatch
    ):
        monkeypatch.delenv("LAZYAF_ENDPOINT_LOCAL_4090", raising=False)
        monkeypatch.delenv("LAZYAF_ENDPOINT_LOCAL_4090_FILE", raising=False)
        endpoint = await _make_endpoint(
            db_session, auth_style="bearer",
            auth_secret_ref="LAZYAF_ENDPOINT_LOCAL_4090",
        )
        with pytest.raises(ValueError) as excinfo:
            await self._dispatch(
                db_session, endpoint,
                {"agent": HARNESS_AGENT, "model": "endpoint:local-4090"},
            )
        assert "LAZYAF_ENDPOINT_LOCAL_4090" in str(excinfo.value)

    async def test_a_harness_step_always_ships_a_null_agents_json(self, db_session):
        """Seam left open on purpose (wave8 s12): the harness runs ONE loop.
        Multi-agent shapes belong in the graph, where they are visible and
        costed per role, not inside one step's loop where they are not.

        The producer's refusal of a NON-null value is pinned in
        `tdd/unit/control_runtime/test_endpoint_config_contract.py`; here the
        dispatcher's own normalization is pinned, so an unresolvable
        `agent_file_ids` cannot quietly become a subagent bundle later.
        """
        endpoint = await _make_endpoint(db_session)
        exec_config, _ctx = await self._dispatch(
            db_session, endpoint,
            {"agent": HARNESS_AGENT, "model": "endpoint:local-4090",
             "agent_file_ids": ["nonexistent-but-never-resolved"]},
        )
        assert exec_config["agent"]["agents_json"] is None


# --------------------------------------------------------------------------
# The runner-local probe run (M14 s2.3)
# --------------------------------------------------------------------------

class TestRunnerLocalProbeRun:
    """A `runner-local` endpoint is unreachable from the backend BY
    DEFINITION, so probing it uses the machinery that already reaches that
    host. The step definition is what this pins; `start_pipeline` itself is
    12.6's and is stubbed so no container is spawned."""

    async def _schedule(self, db, endpoint, monkeypatch):
        from app.services import agent_run, pipeline_executor as pe_module

        captured = {}

        async def _fake_start(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="run-123")

        monkeypatch.setattr(
            pe_module.pipeline_executor, "start_pipeline", _fake_start
        )
        run = await agent_run.start_endpoint_probe_run(db, endpoint)
        return run, captured

    async def test_the_probe_run_is_pinned_to_the_endpoints_label(
        self, db_session, monkeypatch
    ):
        import json as _json

        db_session.add(Repo(id=str(uuid4()), name="host", default_branch="main"))
        await db_session.commit()
        endpoint = await _make_endpoint(db_session, reach="runner-local")

        run, captured = await self._schedule(db_session, endpoint, monkeypatch)

        assert run.id == "run-123"
        assert captured["trigger_type"] == "endpoint_probe"
        assert captured["trigger_ref"] == endpoint.id
        # 12.8: the ad-hoc probe pipeline is authored as a GRAPH. Its
        # single node is keyed `probe` by `start_endpoint_probe_run`.
        graph = _json.loads(captured["pipeline"].steps_graph)
        assert list(graph["steps"]) == ["probe"]
        assert graph["entry_points"] == ["probe"]
        step = graph["steps"]["probe"]
        assert step["type"] == "script"
        assert step["config"]["requires"] == {"has": ["endpoint:local-4090"]}
        assert step["config"]["command"] == "python3 -m runner_common.endpoint_probe"
        assert step["config"]["environment"]["LAZYAF_PROBE_ENDPOINT_ID"] == endpoint.id
        # The marker `_prepare_control_mode` reads to place the step JWT in the
        # SECRET channel and to stamp model_endpoint_id for /probe-result's
        # split-brain fence.
        assert step["config"]["endpoint_probe"] == endpoint.id

    async def test_the_pinned_step_routes_REMOTE(self, db_session, monkeypatch):
        import json as _json

        db_session.add(Repo(id=str(uuid4()), name="host", default_branch="main"))
        await db_session.commit()
        endpoint = await _make_endpoint(db_session, reach="runner-local")

        _run, captured = await self._schedule(db_session, endpoint, monkeypatch)
        step = _json.loads(captured["pipeline"].steps_graph)["steps"]["probe"]
        decision = ExecutionRouter().decide("script", step["config"])

        assert (decision.mode, decision.reason) == ("remote", "runner-pin")
        assert decision.requirements["has"] == ["endpoint:local-4090"]

    async def test_the_probe_pipeline_is_hidden_from_the_pipeline_list(
        self, db_session, monkeypatch
    ):
        from app.services.agent_run import is_adhoc_pipeline_name

        db_session.add(Repo(id=str(uuid4()), name="host", default_branch="main"))
        await db_session.commit()
        endpoint = await _make_endpoint(db_session, reach="runner-local")

        _run, captured = await self._schedule(db_session, endpoint, monkeypatch)
        assert is_adhoc_pipeline_name(captured["pipeline"].name)

    async def test_a_non_runner_local_endpoint_is_refused(self, db_session):
        from app.services.agent_run import start_endpoint_probe_run

        endpoint = await _make_endpoint(db_session, reach="direct")
        with pytest.raises(ValueError, match="only a runner-local endpoint"):
            await start_endpoint_probe_run(db_session, endpoint)

    async def test_no_repo_is_a_named_refusal_not_a_crash(self, db_session):
        from app.services.agent_run import start_endpoint_probe_run

        endpoint = await _make_endpoint(db_session, reach="runner-local")
        with pytest.raises(ValueError, match="needs a repo"):
            await start_endpoint_probe_run(db_session, endpoint)


# --------------------------------------------------------------------------
# R1: the step's own log says which endpoint it drives and how it will behave
# --------------------------------------------------------------------------

class TestEndpointIsAnnouncedInTheStepLog:
    async def _announce(self, db, endpoint):
        repo = Repo(id=str(uuid4()), name="r", default_branch="main")
        db.add(repo)
        await db.commit()
        pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci",
                            steps="[]", triggers="[]")
        db.add(pipeline)
        await db.commit()
        run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id,
                          status="running")
        db.add(run)
        await db.commit()
        step_run = StepRun(id=str(uuid4()), pipeline_run_id=run.id, step_index=0,
                           step_name="implement", status="running", logs="")
        db.add(step_run)
        await db.commit()

        await PipelineExecutor()._announce_endpoint(db, run, step_run, endpoint)
        await db.refresh(step_run)
        return step_run.logs or ""

    async def test_the_resolved_url_is_named_before_any_request(self, db_session):
        """The biggest new exposure is a genuinely new hop: step container ->
        model endpoint. One grep must answer "why can't the step reach it"."""
        endpoint = await _make_endpoint(db_session)
        logs = await self._announce(db_session, endpoint)

        assert "[executor] endpoint local-4090" in logs
        assert "http://172.17.0.1:11434/v1" in logs
        assert "reach=direct" in logs
        assert "node=endpoint:local-4090" in logs

    async def test_proxy_reach_warns_that_the_backend_is_the_bottleneck(
        self, db_session
    ):
        endpoint = await _make_endpoint(db_session, reach="proxy")
        logs = await self._announce(db_session, endpoint)
        assert "WARNING" in logs and "reach=proxy" in logs

    async def test_an_unknown_context_window_says_what_it_will_assume(
        self, db_session
    ):
        """Assuming 128k silently is how a step dies at turn 12 with an
        opaque 400."""
        endpoint = await _make_endpoint(db_session, context_window=None)
        logs = await self._announce(db_session, endpoint)
        assert "no context window" in logs and "8192" in logs

    async def test_an_endpoint_that_reports_no_usage_says_tokens_will_be_null(
        self, db_session
    ):
        endpoint = await _make_endpoint(db_session, reports_usage=False)
        logs = await self._announce(db_session, endpoint)
        assert "token counts will be null" in logs

    async def test_a_healthy_endpoint_with_a_window_warns_about_nothing(
        self, db_session
    ):
        endpoint = await _make_endpoint(db_session, context_window=32768)
        logs = await self._announce(db_session, endpoint)
        assert "WARNING" not in logs
