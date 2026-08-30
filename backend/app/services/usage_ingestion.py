"""
Usage-manifest ingestion (Phase 12.5, cross-agent contract #3).

Turns one step's usage manifest into a `StepUsage` row joined to the
StepExecution -> StepRun -> PipelineRun chain. What the wire may state and
what the SERVER derives is a hard split (R3: one writer per datum):

| Datum                                    | Owner            |
|------------------------------------------|------------------|
| provider, model, tokens, determinism, raw | the agent wrapper (from the CLI's own report) |
| wall_clock_ms, container_seconds          | the control runtime (run.py) |
| role, gpu_node_id, gpu_fraction           | run.py, from container env |
| step_run_id, pipeline_run_id              | HERE — never trusted from the wire |
| cost_usd + cost_source                    | HERE — precedence below |

Cost precedence (api-surface 2.5), applied server-side so history can be
re-priced when a rate is corrected:

1. `cost_usd` present in the manifest -> use it, `cost_source="cli-reported"`.
2. else a `gpu_node_id` with a configured rate -> price it by occupancy,
   `cost_source="gpu-node"`.
3. else `cost_source="unknown"`, `cost_usd=None`.

The manifest's own `cost_source` is therefore ADVISORY: the server states how
the number it stored was actually arrived at, so a runtime that mislabels
itself cannot make an unpriced row look priced. `estimated` stays in the
vocabulary and is written by nothing.

Idempotency is keyed on `step_execution_id` (a UNIQUE index): a retrying
runtime UPDATES its row and never double-bills. A concurrent POST racing the
same insert is absorbed with the codebase's rollback/re-select idiom rather
than 500ing away a whole accounting record.

THE SAFE HALF OF THAT IDIOM (12.5 review finding F3.2) is `_RunRefs` below:
every scalar the recovery path needs is MATERIALIZED into a plain dataclass
BEFORE the rollback, exactly the way `test_ingestion._RunContext` does it.
`db.rollback()` expires every live ORM instance in the session, so touching
one afterwards fires a lazy refresh — which under asyncio is not a slow
query, it is `MissingGreenlet`, i.e. a 500 that loses the whole accounting
record precisely in the case the rollback exists to survive. That includes
the caller's `StepExecution`: its id is copied out up front for the same
reason. Nothing in this module may hold a live ORM row across a rollback.

Never-fail-a-step (api-surface 2.4) reaches back to here: this module raises
only for a genuinely absent StepRun (a broken FK chain, which is a 500 worth
having). Everything soft — an unpriceable node, an oversized `raw`, an
unresolvable role — is degraded and recorded, never rejected. A `StepUsage`
with `cost_source="unknown"` is the RECORDED FACT that the provider told us
nothing; it is not a gap.
"""
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StepExecution, StepRun, StepUsage, UsageCostSource
from app.schemas.usage import UsageManifest
from app.services.usage_pricing import CENTS, gpu_node_cost_usd, resolve_node_rate

logger = logging.getLogger(__name__)

#: `raw` is capped rather than rejected (api-surface 2.2): it exists so a
#: disputed number can be re-derived later, not as a second source of truth.
RAW_MAX_BYTES = 8 * 1024

#: Room for the truncation marker inside RAW_MAX_BYTES.
_RAW_EXCERPT_BYTES = RAW_MAX_BYTES - 512


def _quantize(value: Decimal | None) -> Decimal | None:
    """6dp, matching NUMERIC(18,6).

    Quantizing on WRITE is what makes SQLite's REAL storage round-trip
    exactly: `Decimal("0.1841")` is stored as 0.184100 and read back as
    `Decimal("0.184100")`.
    """
    if value is None:
        return None
    try:
        return Decimal(value).quantize(CENTS)
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("Unquantizable cost_usd %r — recording as unpriced", value)
        return None


#: Providers whose runtime CANNOT have received a bill, so `cli-reported` is
#: structurally unavailable to them (M14 s5.2). Only the harness's own
#: provider is listed: `self-hosted` predates this rule and is written by the
#: mock agent, which reports no cost at all, so adding it would be a rule with
#: no case behind it.
UNBILLABLE_PROVIDERS = frozenset({"openai-compatible"})


def _may_claim_a_bill(provider: str | None) -> bool:
    """Can a manifest from this provider legitimately state `cost_usd`?"""
    return (provider or "") not in UNBILLABLE_PROVIDERS


async def _resolve_cost(
    db: AsyncSession, manifest: UsageManifest
) -> tuple[Decimal | None, str]:
    """api-surface 2.5 precedence. Returns (cost_usd, cost_source).

    M14: the rate lookup goes through `resolve_node_rate` (contract #7), which
    reads the model endpoint ROW first and `settings.gpu_node_rates` second.
    The PRECEDENCE ITSELF is unchanged - manifest cost, then node rate, then
    unknown - because that is what makes a `cli-reported` row mean "the
    provider billed us this amount".
    """
    if manifest.cost_usd is not None and not _may_claim_a_bill(manifest.provider):
        # M14 s5.2, and the reason it is enforced HERE rather than trusted:
        # `cli-reported` is what the board reads as "THE PROVIDER BILLED US
        # THIS AMOUNT", and no self-hosted OpenAI-compatible endpoint can make
        # that claim - there is no invoice behind it. A runtime that sends one
        # anyway (a fork, a hand-built manifest, a future executor that
        # borrows the provider) must not be able to launder an estimate into
        # a bill. The claim is DROPPED and SAID OUT LOUD, and the row falls
        # through to node pricing, which is the honest basis.
        logger.warning(
            "usage manifest for provider %r claims cost_usd=%s; a self-hosted "
            "endpoint cannot report a provider bill, so the claim is dropped "
            "and the row is priced from its gpu node instead",
            manifest.provider,
            manifest.cost_usd,
        )
    elif manifest.cost_usd is not None:
        priced = _quantize(manifest.cost_usd)
        if priced is not None:
            return priced, UsageCostSource.CLI_REPORTED.value

    if manifest.gpu_node_id and manifest.container_seconds is not None:
        rate = await resolve_node_rate(db, manifest.gpu_node_id)
        if rate is not None:
            fraction = (
                manifest.gpu_fraction if manifest.gpu_fraction is not None else 1.0
            )
            return (
                gpu_node_cost_usd(rate, manifest.container_seconds, fraction),
                UsageCostSource.GPU_NODE.value,
            )
        logger.info(
            "Step usage names gpu node %r with no configured rate — recording "
            "cost_source='unknown' (set LAZYAF_GPU_NODE_RATES to price it)",
            manifest.gpu_node_id,
        )

    return None, UsageCostSource.UNKNOWN.value


def _resolve_role(manifest: UsageManifest, refs: "_RunRefs | None") -> str | None:
    """api-surface 2.6 role resolution: manifest -> step config -> None.

    Source 1 (the manifest) is the ONLY live source in 12.5, and it is null
    everywhere: the wrapper writes `role: null` because nothing assigns roles
    until M13's strategy fan-out. It is on the wire now because `cost_by_role`
    is unrecoverable after the fact.

    Sources 2 and 3 (`step_config.role`, then
    `StepRun.step_config["experiment_context"]["role"]`) both read a
    `StepRun.step_config` column that does not exist yet; it arrives WITH M13's
    trials work. THIS FUNCTION is the seam where they land — deliberately a
    seam and a comment rather than a dead branch pretending to be tested (R4).
    When they do land they must read `step_config` off a SCALAR materialized
    into `_RunRefs`, not off a live `StepRun` (F3.2): this runs downstream of
    a possible rollback.

    A step whose role stays None is aggregated under "unattributed" in every
    rollup and counted in the coverage warning; it is never silently dropped.
    """
    if manifest.role and manifest.role.strip():
        return manifest.role.strip()[:64]
    return None


def _encode_determinism(determinism: dict | None) -> str:
    """{temperature, seed, top_p} as the provider exposed it, as JSON text."""
    if not determinism:
        return "{}"
    try:
        return json.dumps(determinism, sort_keys=True, default=str)
    except (TypeError, ValueError):
        logger.warning("Unserializable determinism block — recording {}")
        return "{}"


def _encode_raw(raw: dict | None) -> str | None:
    """The CLI's blob as JSON text, capped at 8 KiB — truncated, never rejected.

    An oversized blob is replaced by a VALID JSON object carrying an excerpt
    and an explicit `_truncated` marker, so a later reader can tell "this was
    cut" apart from "this is all there was".
    """
    if raw is None:
        return None
    try:
        encoded = json.dumps(raw, sort_keys=True, default=str)
    except (TypeError, ValueError):
        logger.warning("Unserializable raw usage blob — recording the truncation marker")
        return json.dumps({"_truncated": True, "_reason": "unserializable"})

    if len(encoded.encode("utf-8")) <= RAW_MAX_BYTES:
        return encoded

    excerpt = encoded.encode("utf-8")[:_RAW_EXCERPT_BYTES].decode("utf-8", "ignore")
    logger.info(
        "raw usage blob is %d bytes (> %d) — truncating with a marker",
        len(encoded.encode("utf-8")),
        RAW_MAX_BYTES,
    )
    return json.dumps(
        {
            "_truncated": True,
            "_original_bytes": len(encoded.encode("utf-8")),
            "_excerpt": excerpt,
        }
    )


@dataclass(frozen=True)
class _RunRefs:
    """The run ids, as PLAIN SCALARS (F3.2).

    Not an ORM row: this object is read after a possible `db.rollback()`,
    and a live `StepRun` there would emit a lazy refresh that raises
    `MissingGreenlet` under asyncio.
    """

    step_execution_id: str
    step_run_id: str
    pipeline_run_id: str


async def _load_run_refs(db: AsyncSession, execution: StepExecution) -> _RunRefs:
    """Walk StepExecution -> StepRun for the run ids (never trusted from the wire).

    A COLUMN select, not an entity select: the caller keeps only the scalars,
    so nothing it holds can be expired by a later rollback. `execution`'s own
    ids are copied out here too, while the session is still clean.
    """
    step_execution_id = execution.id
    step_run_id = execution.step_run_id
    row = (
        await db.execute(
            select(StepRun.id, StepRun.pipeline_run_id).where(
                StepRun.id == step_run_id
            )
        )
    ).one_or_none()
    if row is None:
        raise LookupError(
            f"Step run {step_run_id} not found for execution {step_execution_id}"
        )
    return _RunRefs(
        step_execution_id=step_execution_id,
        step_run_id=row.id,
        pipeline_run_id=row.pipeline_run_id,
    )


async def _apply(
    db: AsyncSession, usage: StepUsage, manifest: UsageManifest, refs: _RunRefs
) -> StepUsage:
    """Write every wire-owned and every derived field onto the row.

    Shared by insert and update so a re-POST cannot leave a stale field
    behind: an idempotent write replaces the record, it does not merge it.

    Async since M14 only because the cost resolution reads the model endpoint
    row for a node rate; nothing else here touches the session.
    """
    cost_usd, cost_source = await _resolve_cost(db, manifest)

    usage.step_run_id = refs.step_run_id
    usage.pipeline_run_id = refs.pipeline_run_id
    usage.provider = manifest.provider
    usage.model = manifest.model
    usage.model_version = manifest.model_version
    usage.input_tokens = manifest.input_tokens
    usage.output_tokens = manifest.output_tokens
    usage.cache_read_tokens = manifest.cache_read_tokens
    usage.cache_write_tokens = manifest.cache_write_tokens
    usage.cost_usd = cost_usd
    usage.cost_source = cost_source
    usage.wall_clock_ms = manifest.wall_clock_ms
    usage.container_seconds = manifest.container_seconds
    usage.gpu_node_id = manifest.gpu_node_id
    usage.gpu_fraction = manifest.gpu_fraction
    usage.role = _resolve_role(manifest, refs)
    usage.determinism = _encode_determinism(manifest.determinism)
    usage.raw = _encode_raw(manifest.raw)
    return usage


async def _select_existing(db: AsyncSession, step_execution_id: str) -> StepUsage | None:
    return (
        await db.execute(
            select(StepUsage).where(StepUsage.step_execution_id == step_execution_id)
        )
    ).scalar_one_or_none()


async def ingest_usage(
    db: AsyncSession,
    execution: StepExecution,
    manifest: UsageManifest,
) -> StepUsage:
    """Ingest one step's usage manifest. Commits once; idempotent per
    step_execution_id (see module docstring)."""
    # Every scalar the recovery path below needs, materialized BEFORE any
    # rollback can expire the objects it came from (F3.2).
    refs = await _load_run_refs(db, execution)
    # Materialized BEFORE any rollback for the same F3.2 reason as `refs`: the
    # endpoint health fold runs at the very end, long after `execution` may
    # have been expired.
    model_endpoint_id = execution.model_endpoint_id

    usage = await _select_existing(db, refs.step_execution_id)
    if usage is None:
        usage = StepUsage(step_execution_id=refs.step_execution_id)
        await _apply(db, usage, manifest, refs)
        db.add(usage)
        try:
            await db.flush()
        except IntegrityError:
            # A concurrent POST for the same execution won the race. Absorb
            # it with the rollback/re-select idiom (services/execution/
            # idempotency.py) — the race costs a retry, never the record.
            #
            # From here on ONLY `refs` (plain strings) and `manifest` (a
            # pydantic model) are in hand: the rollback expired `execution`,
            # the losing StepUsage and anything else the session held.
            await db.rollback()
            logger.info(
                "Concurrent usage insert for step execution %s — updating instead",
                refs.step_execution_id,
            )
            existing = await _select_existing(db, refs.step_execution_id)
            if existing is None:
                raise
            usage = await _apply(db, existing, manifest, refs)
    else:
        await _apply(db, usage, manifest, refs)

    await db.commit()
    await db.refresh(usage)

    await _fold_endpoint_health(db, model_endpoint_id, manifest)
    return usage


async def _fold_endpoint_health(
    db: AsyncSession, model_endpoint_id: str | None, manifest: UsageManifest
) -> None:
    """Let the WORK correct the endpoint's capability record (M14 s5.4).

    A probe is one observation taken at one moment from one network position;
    the work itself is a much better instrument. This is where an endpoint that
    passed the tool probe and then never emitted `tool_calls` gets demoted, and
    where a healthy endpoint's `consecutive_failures` is zeroed so it never
    drifts into stale-and-failing through disuse of the probe button alone.

    AFTER the usage commit and inside a try/except that logs and swallows: the
    never-fail-a-step rule reaches here too, and a health update is not worth a
    500 on a telemetry POST that has already stored the accounting record.
    """
    if not model_endpoint_id:
        return
    raw_harness = ((manifest.raw or {}).get("harness") or {})
    if not isinstance(raw_harness, dict) or not raw_harness:
        return
    try:
        from app.services.model_endpoints.health import record_step_outcome

        await record_step_outcome(db, model_endpoint_id, raw_harness)
    except Exception:
        logger.exception(
            "endpoint health update failed for endpoint %s (the usage row is "
            "already committed)",
            model_endpoint_id,
        )
