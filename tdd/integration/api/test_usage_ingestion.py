"""
Integration tests for the usage channel (Phase 12.5).

Endpoints under test:
- POST /api/steps/{step_id}/usage        (control runtime -> backend)
- GET  /api/steps/{step_id}/usage        (operator/UI)
- GET  /api/pipeline-runs/{run_id}/usage (rollup, read-heavy)

Pinned contracts exercised here (docs/milestone-13/api-surface.md section 2,
BINDING — the usage channel ships in 12.5 or it becomes a retrofit against a
frozen protocol):

- 2.1 auth parity with /logs and /test-results: Bearer step token, missing
  header 401, wrong token 401/403, terminal StepExecution 409
- 2.1 idempotency keyed on step_execution_id: a re-POST UPDATES, so a
  retrying runtime can never double-bill
- 2.2 wire shape is version-pinned: an unknown version is a 422, never a
  silent partial parse; `raw` over 8 KiB is TRUNCATED, never rejected
- 2.5 server-side cost precedence: manifest cost -> gpu-node occupancy ->
  unknown, and the SERVER (not the runtime) states which one applied
- 2.6 role resolution, and a null role landing in "unattributed"
- 2.4 THE never-fail-a-step rule, from this endpoint's side: a step whose
  CLI reported nothing still gets a 200 and a real row with
  cost_source="unknown". Telemetry must never be able to fail work.

The shared wire pin (tdd/unit/control_runtime/usage_contract.py) is checked
against the SERVER here, in the same process, so the producer and the
endpoint cannot drift apart one side at a time.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

repo_root = Path(__file__).parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app.config import get_settings
from app.models import Pipeline, PipelineRun, Repo, StepExecution, StepRun, StepUsage
from app.schemas.usage import UsageManifest
from app.services.control_layer.auth import generate_step_token

from tdd.unit.control_runtime.usage_contract import (
    CANONICAL_MANIFEST,
    FALLBACK_MANIFEST,
    assert_manifest_conforms,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

async def _make_run(db_session) -> dict:
    """repo -> pipeline -> pipeline run (no steps yet)."""
    repo = Repo(id=str(uuid4()), name=f"usage-repo-{uuid4().hex[:8]}", is_ingested=True)
    db_session.add(repo)

    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci", steps="[]")
    db_session.add(pipeline)

    pipeline_run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status="running",
        trigger_type="push",
        trigger_context=json.dumps({"branch": "main", "commit_sha": "abc123"}),
    )
    db_session.add(pipeline_run)
    await db_session.commit()
    return {"repo_id": repo.id, "pipeline_run_id": pipeline_run.id}


async def _add_step(db_session, run: dict, step_index: int, step_name: str) -> dict:
    """One StepRun + StepExecution on an existing run, plus a valid token."""
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run["pipeline_run_id"],
        step_index=step_index,
        step_name=step_name,
        status="running",
        logs="",
    )
    db_session.add(step_run)

    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run['pipeline_run_id']}:{step_index}:1",
        step_run_id=step_run.id,
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    token = generate_step_token(
        step_id=execution.id, execution_key=execution.execution_key
    )
    return {
        **run,
        "step_run_id": step_run.id,
        "step_run": step_run,
        "step_index": step_index,
        "execution_id": execution.id,
        "execution": execution,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
async def step_ctx(db_session):
    run = await _make_run(db_session)
    return await _add_step(db_session, run, 0, "agent")


@pytest.fixture
async def other_step_ctx(db_session):
    """A second run's step — a token from here must not write over there."""
    run = await _make_run(db_session)
    return await _add_step(db_session, run, 0, "agent")


@pytest.fixture
def gpu_rates(monkeypatch):
    """Install a node rate table (LAZYAF_GPU_NODE_RATES) for one test.

    get_settings is lru_cached, so the cache is cleared on the way in AND on
    the way out — a leaked rate table would silently re-price other tests.
    """

    def _install(table: dict):
        monkeypatch.setenv("LAZYAF_GPU_NODE_RATES", json.dumps(table))
        get_settings.cache_clear()
        return table

    yield _install
    get_settings.cache_clear()


def manifest(**overrides) -> dict:
    """The canonical claude-code manifest, with per-test overrides."""
    body = dict(CANONICAL_MANIFEST)
    body.update(overrides)
    return body


async def _post(client, ctx, body):
    return await client.post(
        f"/api/steps/{ctx['execution_id']}/usage",
        json=body,
        headers=ctx["headers"],
    )


async def _row(db_session, ctx) -> StepUsage | None:
    return (
        await db_session.execute(
            select(StepUsage).where(StepUsage.step_execution_id == ctx["execution_id"])
        )
    ).scalar_one_or_none()


# -----------------------------------------------------------------------------
# 2.1 Auth parity with /logs and /test-results
# -----------------------------------------------------------------------------

class TestAuth:
    async def test_requires_auth_header(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/usage", json=manifest()
        )
        assert response.status_code == 401

    async def test_rejects_non_bearer_format(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/usage",
            json=manifest(),
            headers={"Authorization": step_ctx["token"]},
        )
        assert response.status_code == 401

    async def test_rejects_garbage_token(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/usage",
            json=manifest(),
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert response.status_code in (401, 403)

    async def test_rejects_another_steps_token(
        self, client, step_ctx, other_step_ctx, db_session
    ):
        """A token minted for one step may not write another step's row —
        the same cross-step hardening /logs has."""
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/usage",
            json=manifest(),
            headers=other_step_ctx["headers"],
        )
        assert response.status_code in (401, 403)
        assert await _row(db_session, step_ctx) is None

    async def test_unknown_step_is_404(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{uuid4()}/usage",
            json=manifest(),
            headers=step_ctx["headers"],
        )
        assert response.status_code == 404


class TestTerminalRejection:
    @pytest.mark.parametrize(
        "status", ["completed", "failed", "cancelled", "timeout"]
    )
    async def test_terminal_execution_answers_409(
        self, client, step_ctx, db_session, status
    ):
        """Zombie-token hardening: usage arriving after the terminal /status
        is dropped with a 409, exactly as api-surface 2.1 specifies. The
        runtime WARNs and continues; the step's exit code is untouched."""
        step_ctx["execution"].status = status
        await db_session.commit()

        response = await _post(client, step_ctx, manifest())

        assert response.status_code == 409
        assert await _row(db_session, step_ctx) is None

    async def test_409_does_not_disturb_an_already_ingested_row(
        self, client, step_ctx, db_session
    ):
        """A usage row written while running survives the step going
        terminal — the 409 drops the late write, it does not delete."""
        assert (await _post(client, step_ctx, manifest())).status_code == 200
        step_ctx["execution"].status = "completed"
        await db_session.commit()

        assert (await _post(client, step_ctx, manifest())).status_code == 409

        row = await _row(db_session, step_ctx)
        assert row is not None
        assert row.cost_source == "cli-reported"


# -----------------------------------------------------------------------------
# 2.2 Wire shape
# -----------------------------------------------------------------------------

class TestWireShape:
    async def test_canonical_manifest_is_accepted(self, client, step_ctx):
        response = await _post(client, step_ctx, manifest())
        assert response.status_code == 200
        body = response.json()
        assert body["cost_source"] == "cli-reported"
        assert body["cost_usd"] == "0.184100"
        assert body["usage_id"]

    async def test_server_accepts_the_shared_contract_fixtures(self, client, step_ctx):
        """The SERVER side of cross-agent contract #3: both pinned manifests
        (the happy path and the never-fail fallback) validate here, in the
        same process that pins the producer."""
        assert_manifest_conforms(CANONICAL_MANIFEST, "SERVER (usage router)")
        assert_manifest_conforms(FALLBACK_MANIFEST, "SERVER (usage router)")
        assert UsageManifest(**CANONICAL_MANIFEST).version == 1
        assert UsageManifest(**FALLBACK_MANIFEST).cost_source == "unknown"

    @pytest.mark.parametrize("version", [0, 2, 99])
    async def test_unknown_version_is_422(self, client, step_ctx, version):
        """A half-understood accounting record is worse than a rejected
        one: Literal[1] makes this a 422, never a partial parse."""
        assert (await _post(client, step_ctx, manifest(version=version))).status_code == 422

    async def test_missing_version_is_422(self, client, step_ctx):
        body = manifest()
        del body["version"]
        assert (await _post(client, step_ctx, body)).status_code == 422

    @pytest.mark.parametrize("provider", ["acme-ai", "", "ANTHROPIC"])
    async def test_out_of_vocabulary_provider_is_422(self, client, step_ctx, provider):
        assert (await _post(client, step_ctx, manifest(provider=provider))).status_code == 422

    async def test_out_of_vocabulary_cost_source_is_422(self, client, step_ctx):
        assert (await _post(client, step_ctx, manifest(cost_source="guessed"))).status_code == 422

    async def test_missing_wall_clock_is_422(self, client, step_ctx):
        body = manifest()
        del body["wall_clock_ms"]
        assert (await _post(client, step_ctx, body)).status_code == 422

    async def test_determinism_is_persisted_verbatim(
        self, client, step_ctx, db_session
    ):
        await _post(
            client,
            step_ctx,
            manifest(determinism={"temperature": 0.0, "seed": 7, "top_p": None}),
        )
        row = await _row(db_session, step_ctx)
        assert json.loads(row.determinism) == {
            "temperature": 0.0,
            "seed": 7,
            "top_p": None,
        }

    async def test_absent_determinism_stores_an_empty_object(
        self, client, step_ctx, db_session
    ):
        body = manifest()
        del body["determinism"]
        await _post(client, step_ctx, body)
        assert json.loads((await _row(db_session, step_ctx)).determinism) == {}


class TestRawCapping:
    async def test_raw_under_the_cap_is_stored_verbatim(
        self, client, step_ctx, db_session
    ):
        blob = {"total_cost_usd": 0.1841, "usage": {"input_tokens": 18422}}
        await _post(client, step_ctx, manifest(raw=blob))
        assert json.loads((await _row(db_session, step_ctx)).raw) == blob

    async def test_oversized_raw_is_truncated_not_rejected(
        self, client, step_ctx, db_session
    ):
        """api-surface 2.2: `raw` exists so a disputed number can be
        re-derived later, not as a second source of truth. An 80 KiB blob
        costs a truncation marker, never a failed accounting record."""
        blob = {"transcript": "x" * 80_000}

        response = await _post(client, step_ctx, manifest(raw=blob))

        assert response.status_code == 200
        row = await _row(db_session, step_ctx)
        stored = json.loads(row.raw)
        assert stored["_truncated"] is True
        assert stored["_original_bytes"] > 8 * 1024
        assert len(row.raw.encode("utf-8")) <= 8 * 1024

    async def test_absent_raw_stays_null(self, client, step_ctx, db_session):
        body = manifest()
        del body["raw"]
        await _post(client, step_ctx, body)
        assert (await _row(db_session, step_ctx)).raw is None


# -----------------------------------------------------------------------------
# Server-side derivation (2.1 "derived server-side", 2.5 precedence)
# -----------------------------------------------------------------------------

class TestServerSideDerivation:
    async def test_run_ids_come_from_the_execution_chain_not_the_wire(
        self, client, step_ctx, db_session
    ):
        """step_run_id / pipeline_run_id are walked from StepExecution ->
        StepRun -> PipelineRun; the container never states them."""
        await _post(client, step_ctx, manifest())
        row = await _row(db_session, step_ctx)
        assert row.step_run_id == step_ctx["step_run_id"]
        assert row.pipeline_run_id == step_ctx["pipeline_run_id"]

    async def test_manifest_cost_wins_and_is_labelled_cli_reported(
        self, client, step_ctx, db_session
    ):
        response = await _post(
            client, step_ctx, manifest(cost_usd="0.1841", cost_source="unknown")
        )
        assert response.status_code == 200
        row = await _row(db_session, step_ctx)
        # The runtime's own cost_source is ADVISORY: the server states how
        # the number it stored was actually arrived at.
        assert row.cost_source == "cli-reported"
        assert row.cost_usd == Decimal("0.184100")

    async def test_gpu_node_with_a_configured_rate_is_priced_server_side(
        self, client, step_ctx, db_session, gpu_rates
    ):
        """api-surface 2.5: you rent the node, not the tokens — and the
        SERVER prices it so history can be re-priced when a rate changes."""
        gpu_rates({"runpod-a100-80g": {"rate_usd_hour": "1.89"}})

        response = await _post(
            client,
            step_ctx,
            manifest(
                cost_usd=None,
                cost_source="unknown",
                gpu_node_id="runpod-a100-80g",
                gpu_fraction=1.0,
                container_seconds=3600.0,
            ),
        )

        assert response.status_code == 200
        assert response.json()["cost_source"] == "gpu-node"
        row = await _row(db_session, step_ctx)
        assert row.cost_usd == Decimal("1.890000")
        assert row.gpu_node_id == "runpod-a100-80g"

    async def test_gpu_fraction_scales_the_priced_cost(
        self, client, step_ctx, db_session, gpu_rates
    ):
        gpu_rates({"shared-mig": {"rate_usd_hour": "1.89"}})

        await _post(
            client,
            step_ctx,
            manifest(
                cost_usd=None,
                gpu_node_id="shared-mig",
                gpu_fraction=0.25,
                container_seconds=3600.0,
            ),
        )

        assert (await _row(db_session, step_ctx)).cost_usd == Decimal("0.472500")

    async def test_owned_hardware_priced_at_zero_is_a_real_number(
        self, client, step_ctx, db_session, gpu_rates
    ):
        """A rate of 0.00 on owned hardware is honest, not a bug: the row
        says gpu-node/0.000000, which is a different fact from unknown."""
        gpu_rates({"local-4090": {"rate_usd_hour": "0.00"}})

        await _post(
            client,
            step_ctx,
            manifest(
                cost_usd=None, gpu_node_id="local-4090", container_seconds=120.0
            ),
        )

        row = await _row(db_session, step_ctx)
        assert row.cost_source == "gpu-node"
        assert row.cost_usd == Decimal("0.000000")

    async def test_unconfigured_gpu_node_falls_back_to_unknown(
        self, client, step_ctx, db_session, gpu_rates
    ):
        """No rate means we do not know what the node costs — never a
        guessed one."""
        gpu_rates({"some-other-node": {"rate_usd_hour": "1.00"}})

        response = await _post(
            client,
            step_ctx,
            manifest(
                cost_usd=None, gpu_node_id="unpriced-node", container_seconds=60.0
            ),
        )

        assert response.status_code == 200
        assert response.json()["cost_source"] == "unknown"
        assert response.json()["cost_usd"] is None
        assert (await _row(db_session, step_ctx)).cost_usd is None

    async def test_neither_cost_nor_node_is_unknown_with_null_cost(
        self, client, step_ctx, db_session
    ):
        response = await _post(
            client, step_ctx, manifest(cost_usd=None, cost_source="cli-reported")
        )
        assert response.status_code == 200
        row = await _row(db_session, step_ctx)
        assert row.cost_source == "unknown"
        assert row.cost_usd is None

    async def test_estimated_is_never_written(self, client, step_ctx, db_session):
        """`estimated` stays in the vocabulary for a future price-table
        backfill and is written by nothing today — including by a runtime
        that claims it."""
        await _post(client, step_ctx, manifest(cost_source="estimated"))
        assert (await _row(db_session, step_ctx)).cost_source != "estimated"


class TestMoneyRoundTrip:
    async def test_decimal_round_trips_at_six_dp_through_sqlite(
        self, client, step_ctx, db_session
    ):
        """NUMERIC(18,6) is REAL on SQLite. Quantizing on write is what
        makes the read exact — float64 carries 15-16 significant digits, so
        summing thousands of sub-dollar rows at 6dp is exact."""
        await _post(client, step_ctx, manifest(cost_usd="0.1841"))

        db_session.expire_all()
        row = await _row(db_session, step_ctx)

        assert row.cost_usd == Decimal("0.184100")
        assert str(row.cost_usd) == "0.184100"

    async def test_large_and_tiny_amounts_survive(
        self, client, step_ctx, db_session
    ):
        await _post(client, step_ctx, manifest(cost_usd="1234.567891"))
        db_session.expire_all()
        assert (await _row(db_session, step_ctx)).cost_usd == Decimal("1234.567891")

    async def test_cost_leaves_the_endpoint_as_a_string(self, client, step_ctx):
        """No floats for dollars, ever — not in the DB and not on the wire."""
        body = (await _post(client, step_ctx, manifest(cost_usd="0.1841"))).json()
        assert isinstance(body["cost_usd"], str)


# -----------------------------------------------------------------------------
# 2.1 Idempotency
# -----------------------------------------------------------------------------

class TestIdempotency:
    async def test_two_posts_leave_exactly_one_row(
        self, client, step_ctx, db_session
    ):
        """A retrying runtime must not double-bill."""
        first = await _post(client, step_ctx, manifest())
        second = await _post(client, step_ctx, manifest())

        assert first.status_code == second.status_code == 200
        assert first.json()["usage_id"] == second.json()["usage_id"]

        rows = (
            await db_session.execute(
                select(StepUsage).where(
                    StepUsage.step_execution_id == step_ctx["execution_id"]
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_re_post_updates_every_field(self, client, step_ctx, db_session):
        """An idempotent write REPLACES the record; it does not merge, so a
        corrected re-POST cannot leave a stale number behind."""
        await _post(client, step_ctx, manifest(input_tokens=100, cost_usd="0.100000"))
        await _post(
            client,
            step_ctx,
            manifest(input_tokens=999, cost_usd=None, cost_source="unknown", raw=None),
        )

        db_session.expire_all()
        row = await _row(db_session, step_ctx)
        assert row.input_tokens == 999
        assert row.cost_usd is None
        assert row.cost_source == "unknown"
        assert row.raw is None

    async def test_sibling_steps_get_independent_rows(
        self, client, step_ctx, db_session
    ):
        sibling = await _add_step(
            db_session,
            {
                "repo_id": step_ctx["repo_id"],
                "pipeline_run_id": step_ctx["pipeline_run_id"],
            },
            1,
            "tests",
        )

        await _post(client, step_ctx, manifest())
        await _post(client, sibling, manifest(input_tokens=7))

        rows = (
            await db_session.execute(
                select(StepUsage).where(
                    StepUsage.pipeline_run_id == step_ctx["pipeline_run_id"]
                )
            )
        ).scalars().all()
        assert len(rows) == 2


# -----------------------------------------------------------------------------
# 2.4 The never-fail-a-step rule, from the endpoint's side
# -----------------------------------------------------------------------------

class TestTelemetryNeverFailsAStep:
    async def test_a_step_that_reported_nothing_still_succeeds(
        self, client, step_ctx, db_session
    ):
        """THE property: a step whose CLI reported nothing gets a 200 and a
        real row with cost_source="unknown". That row is the recorded fact
        that the provider told us nothing — not a gap, and not a red step."""
        response = await _post(client, step_ctx, FALLBACK_MANIFEST)

        assert response.status_code == 200
        assert response.json()["cost_source"] == "unknown"
        assert response.json()["cost_usd"] is None

        row = await _row(db_session, step_ctx)
        assert row is not None
        assert row.provider == "self-hosted"
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.wall_clock_ms == 1204

    async def test_a_script_steps_fallback_record_is_a_first_class_row(
        self, client, step_ctx, db_session
    ):
        """Every control-mode step produces a row, script steps included —
        that is what makes a DROPPED usage channel visible as a missing row
        rather than as a quietly-too-cheap median."""
        response = await _post(
            client,
            step_ctx,
            {
                "version": 1,
                "provider": "self-hosted",
                "cost_source": "unknown",
                "wall_clock_ms": 480,
                "container_seconds": 1.9,
            },
        )
        assert response.status_code == 200
        assert (await _row(db_session, step_ctx)).container_seconds == 1.9

    async def test_zero_wall_clock_is_accepted(self, client, step_ctx):
        """An instantly-failing step still reports; a 422 here would turn a
        telemetry edge case into a lost record."""
        assert (await _post(client, step_ctx, manifest(wall_clock_ms=0))).status_code == 200

    async def test_malformed_rate_table_does_not_break_ingestion(
        self, client, step_ctx, db_session, gpu_rates, monkeypatch
    ):
        """A pricing typo prices the step as unknown; it never 500s a
        telemetry POST."""
        monkeypatch.setenv("LAZYAF_GPU_NODE_RATES", "{not json")
        get_settings.cache_clear()

        response = await _post(
            client,
            step_ctx,
            manifest(cost_usd=None, gpu_node_id="runpod-a100-80g", container_seconds=60.0),
        )

        assert response.status_code == 200
        assert (await _row(db_session, step_ctx)).cost_source == "unknown"

    async def test_unparseable_node_rate_prices_as_unknown(
        self, client, step_ctx, db_session, gpu_rates
    ):
        gpu_rates({"typo-node": {"rate_usd_hour": "one dollar eighty nine"}})

        response = await _post(
            client,
            step_ctx,
            manifest(cost_usd=None, gpu_node_id="typo-node", container_seconds=60.0),
        )

        assert response.status_code == 200
        assert (await _row(db_session, step_ctx)).cost_source == "unknown"


# -----------------------------------------------------------------------------
# 2.6 Role resolution
# -----------------------------------------------------------------------------

class TestRoleResolution:
    async def test_manifest_role_is_recorded(self, client, step_ctx, db_session):
        await _post(client, step_ctx, manifest(role="planner"))
        assert (await _row(db_session, step_ctx)).role == "planner"

    async def test_null_role_stays_null_in_12_5(
        self, client, step_ctx, db_session
    ):
        """Nothing assigns roles until M13's strategy fan-out; the column is
        here NOW because cost_by_role is unrecoverable after the fact."""
        await _post(client, step_ctx, manifest(role=None))
        assert (await _row(db_session, step_ctx)).role is None

    async def test_blank_role_is_normalized_to_null(
        self, client, step_ctx, db_session
    ):
        """An empty LAZYAF_ROLE env var must not create an empty-string
        bucket beside "unattributed"."""
        await _post(client, step_ctx, manifest(role="   "))
        assert (await _row(db_session, step_ctx)).role is None


# -----------------------------------------------------------------------------
# 2.7 Reads
# -----------------------------------------------------------------------------

class TestStepUsageRead:
    async def test_get_returns_the_ingested_row(self, client, step_ctx):
        await _post(client, step_ctx, manifest())

        response = await client.get(f"/api/steps/{step_ctx['execution_id']}/usage")

        assert response.status_code == 200
        body = response.json()
        assert body["cost_usd"] == "0.184100"
        assert body["cost_source"] == "cli-reported"
        assert body["input_tokens"] == 18422
        assert body["determinism"] == {"temperature": 0.0, "seed": None, "top_p": None}
        assert body["raw"]["total_cost_usd"] == 0.1841

    async def test_get_needs_no_step_token(self, client, step_ctx):
        """Operator read: the step token gates WRITES from the container,
        not reads of what was written."""
        await _post(client, step_ctx, manifest())
        assert (await client.get(f"/api/steps/{step_ctx['execution_id']}/usage")).status_code == 200

    async def test_unknown_execution_is_404(self, client):
        assert (await client.get(f"/api/steps/{uuid4()}/usage")).status_code == 404

    async def test_execution_without_usage_is_404_naming_the_step(
        self, client, step_ctx
    ):
        """"No rows" and "no such thing" are different facts (api-surface 0)."""
        response = await client.get(f"/api/steps/{step_ctx['execution_id']}/usage")
        assert response.status_code == 404
        assert step_ctx["execution_id"] in response.json()["detail"]


class TestRunUsageRollup:
    async def test_unknown_run_is_404_not_an_empty_rollup(self, client):
        assert (await client.get(f"/api/pipeline-runs/{uuid4()}/usage")).status_code == 404

    async def test_run_with_no_usage_reports_zero_coverage(self, client, step_ctx):
        response = await client.get(
            f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["step_count"] == 0
        assert body["cost_coverage"] == 0.0
        assert body["total_cost_usd"] == "0.000000"

    async def test_rollup_sums_the_run_and_names_every_step(
        self, client, step_ctx, db_session
    ):
        """The dogfood gate compares this listing against the run's steps:
        a silently dropped usage channel must fail the push."""
        script_step = await _add_step(
            db_session,
            {
                "repo_id": step_ctx["repo_id"],
                "pipeline_run_id": step_ctx["pipeline_run_id"],
            },
            1,
            "tier1",
        )

        await _post(client, step_ctx, manifest(cost_usd="1.000000"))
        await _post(client, script_step, FALLBACK_MANIFEST)

        body = (
            await client.get(f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage")
        ).json()

        assert body["step_count"] == 2
        assert body["total_cost_usd"] == "1.000000"
        assert body["cost_coverage"] == 0.5
        assert body["by_source"]["cli-reported"] == 1
        assert body["by_source"]["unknown"] == 1
        assert body["by_source"]["gpu-node"] == 0
        assert [s["step_name"] for s in body["steps"]] == ["agent", "tier1"]
        assert [s["step_index"] for s in body["steps"]] == [0, 1]

    async def test_null_roles_aggregate_under_unattributed(
        self, client, step_ctx
    ):
        """api-surface 2.6: never silently dropped from the run total."""
        await _post(client, step_ctx, manifest(role=None, cost_usd="0.500000"))

        body = (
            await client.get(f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage")
        ).json()

        assert set(body["by_role"]) == {"unattributed"}
        assert body["by_role"]["unattributed"]["cost_usd"] == "0.500000"
        assert body["by_role"]["unattributed"]["steps"] == 1

    async def test_roles_are_bucketed_separately(
        self, client, step_ctx, db_session
    ):
        worker = await _add_step(
            db_session,
            {
                "repo_id": step_ctx["repo_id"],
                "pipeline_run_id": step_ctx["pipeline_run_id"],
            },
            1,
            "work",
        )

        await _post(client, step_ctx, manifest(role="planner", cost_usd="0.400000"))
        await _post(client, worker, manifest(role="worker", cost_usd="1.500000"))

        body = (
            await client.get(f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage")
        ).json()

        assert body["by_role"]["planner"]["cost_usd"] == "0.400000"
        assert body["by_role"]["worker"]["cost_usd"] == "1.500000"
        assert body["total_cost_usd"] == "1.900000"

    async def test_rollup_excludes_other_runs(
        self, client, step_ctx, other_step_ctx
    ):
        await _post(client, step_ctx, manifest())
        await _post(client, other_step_ctx, manifest())

        body = (
            await client.get(f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage")
        ).json()

        assert body["step_count"] == 1
        assert body["steps"][0]["step_execution_id"] == step_ctx["execution_id"]


# -----------------------------------------------------------------------------
# 2.4 again, from the RACE side (12.5 review finding F3.2)
# -----------------------------------------------------------------------------

class TestConcurrentIngestion:
    """Two runtimes POSTing usage for the same step execution at once.

    `test_test_ingestion.py::TestConcurrentIngestion` has covered this shape
    for the test-results channel since 12.2.6; the usage channel shipped with
    the same rollback/re-select idiom and none of the coverage, which is how
    the unsafe half of it went unnoticed. `db.rollback()` expires every live
    ORM object in the session, and touching one afterwards under asyncio
    raises `MissingGreenlet` - a 500 that loses the whole accounting record
    in exactly the case the recovery path exists to survive.

    The race here is REAL, not a fake exception: the row genuinely exists and
    the UNIQUE index on step_execution_id genuinely rejects the second
    INSERT. Only the SELECT that would have found it is blinded - which is
    precisely what a concurrent POST does to it.
    """

    @staticmethod
    def _blind_the_first_select(monkeypatch, state):
        from app.services import usage_ingestion

        real_select = usage_ingestion._select_existing

        async def blind_once(db, step_execution_id):
            """The loser's SELECT ran before the winner's INSERT landed."""
            if not state["blinded"]:
                state["blinded"] = True
                return None
            return await real_select(db, step_execution_id)

        monkeypatch.setattr(usage_ingestion, "_select_existing", blind_once)

    async def test_racing_insert_recovers_instead_of_losing_the_record(
        self, client, step_ctx, db_session, monkeypatch
    ):
        winner = await _post(client, step_ctx, manifest())
        assert winner.status_code == 200

        state = {"blinded": False}
        self._blind_the_first_select(monkeypatch, state)

        loser = await _post(
            client, step_ctx, manifest(input_tokens=999, wall_clock_ms=77)
        )

        assert state["blinded"] is True, "the race was never simulated"
        assert loser.status_code == 200
        monkeypatch.undo()

        rows = (
            await db_session.execute(
                select(StepUsage).where(
                    StepUsage.step_execution_id == step_ctx["execution_id"]
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "the race must never double-bill"
        # The retry's numbers won, so the recovery path really did re-apply
        # the manifest rather than quietly keeping the winner's row.
        assert rows[0].input_tokens == 999
        assert rows[0].wall_clock_ms == 77

    async def test_the_run_ids_survive_the_rollback(
        self, client, step_ctx, db_session, monkeypatch
    ):
        """The specific F3.2 regression: the run ids and the execution id are
        read AFTER the rollback. Held as live ORM rows they were expired by
        then and the lazy refresh raised MissingGreenlet; materialized into
        `_RunRefs` first, they are just strings."""
        await _post(client, step_ctx, manifest())

        state = {"blinded": False}
        self._blind_the_first_select(monkeypatch, state)
        response = await _post(client, step_ctx, manifest(role="planner"))
        monkeypatch.undo()

        assert state["blinded"] is True
        assert response.status_code == 200
        row = await _row(db_session, step_ctx)
        assert row.step_run_id == step_ctx["step_run_id"]
        assert row.pipeline_run_id == step_ctx["pipeline_run_id"]
        assert row.role == "planner"

    async def test_the_response_still_names_the_row_after_a_race(
        self, client, step_ctx, monkeypatch
    ):
        """The endpoint answers from the recovered row, so the runtime gets a
        usable body rather than a 200 full of nulls."""
        await _post(client, step_ctx, manifest())

        state = {"blinded": False}
        self._blind_the_first_select(monkeypatch, state)
        body = (await _post(client, step_ctx, manifest(cost_usd="0.5"))).json()
        monkeypatch.undo()

        assert body["usage_id"]
        assert body["cost_usd"] == "0.500000"
        assert body["cost_source"] == "cli-reported"


# -----------------------------------------------------------------------------
# Rollup query shape (12.5 review finding F3.4)
# -----------------------------------------------------------------------------

class TestRollupQueryShape:
    async def test_the_rollup_does_not_fetch_the_capped_text_columns(
        self, client, step_ctx, async_engine
    ):
        """`raw` (8 KiB cap) and `determinism` are the two TEXT columns on
        StepUsage, and the rollup response reads NEITHER. Selecting the whole
        entity dragged both back for every step of every run - on the one
        endpoint marked read-heavy (api-surface s6)."""
        from sqlalchemy import event

        await _post(client, step_ctx, manifest())

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(async_engine.sync_engine, "before_cursor_execute", record)
        try:
            response = await client.get(
                f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage"
            )
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", record)

        assert response.status_code == 200
        touched = [s for s in statements if "step_usages" in s]
        assert touched, "the rollup issued no query against step_usages"
        for statement in touched:
            assert "step_usages.raw" not in statement
            assert "step_usages.determinism" not in statement

    async def test_the_rollup_still_reports_every_number(self, client, step_ctx):
        """The narrowed select must still cover every field the schema reads:
        a column dropped by mistake shows up as a null in the response, not
        as an error."""
        await _post(
            client,
            step_ctx,
            manifest(
                cost_usd="0.250000",
                input_tokens=11,
                output_tokens=22,
                cache_read_tokens=33,
                cache_write_tokens=44,
                wall_clock_ms=555,
                container_seconds=6.5,
                role="planner",
                model="claude-haiku-4-5",
                provider="anthropic",
            ),
        )

        body = (
            await client.get(f"/api/pipeline-runs/{step_ctx['pipeline_run_id']}/usage")
        ).json()

        step = body["steps"][0]
        assert step["usage_id"]
        assert step["step_execution_id"] == step_ctx["execution_id"]
        assert step["step_run_id"] == step_ctx["step_run_id"]
        assert step["step_index"] == step_ctx["step_index"]
        assert step["step_name"] == "agent"
        assert step["provider"] == "anthropic"
        assert step["model"] == "claude-haiku-4-5"
        assert step["role"] == "planner"
        assert step["input_tokens"] == 11
        assert step["output_tokens"] == 22
        assert step["cost_usd"] == "0.250000"
        assert step["cost_source"] == "cli-reported"
        assert step["wall_clock_ms"] == 555
        assert step["container_seconds"] == 6.5

        bucket = body["by_role"]["planner"]
        assert bucket["cache_read_tokens"] == 33
        assert bucket["cache_write_tokens"] == 44
        assert bucket["wall_clock_ms"] == 555
        assert body["total_cost_usd"] == "0.250000"
