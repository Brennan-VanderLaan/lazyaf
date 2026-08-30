"""Model endpoint registry API (M14.1, wave8 s1.5).

    GET    /api/model-endpoints                 list + derived health + in_flight
    POST   /api/model-endpoints                 create, then probe SYNCHRONOUSLY
    GET    /api/model-endpoints/{id}            one row (id OR name)
    PATCH  /api/model-endpoints/{id}            edit; capability-resetting fields
    DELETE /api/model-endpoints/{id}            409 while in-flight
    POST   /api/model-endpoints/{id}/probe      200 EVEN WHEN THE ENDPOINT IS DOWN
    POST   /api/model-endpoints/{id}/probe-result   step JWT; the runner-local report
    GET    /api/model-endpoints/{id}/usage      rollup via step_usages.gpu_node_id

Auth is the operator's (open, like the rest of this API) except `probe-result`,
which is authenticated by the STEP JWT and additionally fenced on
`step_execution.model_endpoint_id == id` - the split-brain guard borrowed from
12.6, so a token minted for one step cannot rewrite another endpoint's record.

POST probes synchronously (unless `?probe=false`) and returns the row WITH the
probe record, so the operator learns "this model cannot tool-call" at the
moment of registration rather than at the first 30-minute step.

-----------------------------------------------------------------------------
NOTE TO AGENT C (wave8 s10): this module is owned by A and the CRUD lands
first. `probe-result` IS IMPLEMENTED HERE, because it is three lines on top of
`apply_probe_result` and A's tests pin its shape. **Append only the
`ANY /{id}/proxy/v1/{path:path}` broker** - a second `probe-result` handler
would shadow this one silently.
-----------------------------------------------------------------------------
"""
import asyncio
import json
import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.model_endpoint import (
    IN_FLIGHT_STEP_STATUSES,
    ModelEndpoint,
    default_gpu_node_id,
    default_runner_label,
)
from app.models.pipeline import StepExecution
from app.models.usage import StepUsage
from app.schemas.model_endpoint import (
    CAPABILITY_INVALIDATING_FIELDS,
    EndpointInFlight,
    EndpointUsageRollup,
    ModelEndpointCreate,
    ModelEndpointRead,
    ModelEndpointUpdate,
    ProbeResponse,
    coerce_probe_result,
    endpoint_read,
    endpoint_ws_payload,
    validate_auth_fields,
)
from app.schemas.usage import COST_SOURCES
from app.services.model_endpoints.probe import (
    ProbeResult,
    apply_probe_result,
    probe_endpoint,
)
from app.services.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model-endpoints", tags=["model-endpoints"])


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

async def _load(db: AsyncSession, reference: str) -> ModelEndpoint:
    """Fetch by id OR by name.

    Names are accepted on an id-shaped path deliberately: `endpoint:<name>` is
    the coordinate every other surface uses (contract #4), and forcing a
    uuid lookup first would make the CLI and the dogfood YAML carry ids that
    change on every reseed.
    """
    result = await db.execute(
        select(ModelEndpoint).where(
            or_(ModelEndpoint.id == reference, ModelEndpoint.name == reference)
        )
    )
    endpoint = result.scalars().first()
    if endpoint is None:
        raise HTTPException(
            status_code=404, detail=f"Model endpoint '{reference}' not found"
        )
    return endpoint


async def _in_flight_count(db: AsyncSession, endpoint_id: str) -> int:
    """Slots held right now. READ FROM THE DATABASE (contract #9), never from
    an in-memory counter that a restart loses."""
    result = await db.execute(
        select(func.count())
        .select_from(StepExecution)
        .where(
            StepExecution.model_endpoint_id == endpoint_id,
            StepExecution.status.in_(IN_FLIGHT_STEP_STATUSES),
        )
    )
    return int(result.scalar_one() or 0)


async def _in_flight_rows(db: AsyncSession, endpoint_id: str) -> list[EndpointInFlight]:
    result = await db.execute(
        select(StepExecution).where(
            StepExecution.model_endpoint_id == endpoint_id,
            StepExecution.status.in_(IN_FLIGHT_STEP_STATUSES),
        )
    )
    return [
        EndpointInFlight(
            step_execution_id=row.id,
            step_run_id=row.step_run_id,
            status=row.status,
            started_at=row.started_at,
        )
        for row in result.scalars().all()
    ]


async def _runner_count(db: AsyncSession, endpoint: ModelEndpoint) -> Optional[int]:
    """How many CONNECTED runners carry this endpoint's label.

    `0` on a runner-local endpoint is precisely the reason a step would sit at
    NO_RUNNER_TIMEOUT, and showing it BEFORE anyone dispatches is the whole
    point (R1). It counts the intersection of two facts, because either alone
    lies: `runner_registry._connections` says which sockets THIS PROCESS holds
    (an "idle" row left behind by a crashed backend is indistinguishable from
    a live one in the database alone), and the row carries the labels.

    Lazily imported and fully guarded: the registry lives under
    `app.services.execution`, whose package `__init__` pulls in docker, and a
    list endpoint must not depend on a container runtime being present. None
    means "not applicable or not knowable", never 0.
    """
    if endpoint.reach != "runner-local":
        return None
    label = endpoint.runner_label or default_runner_label(endpoint.name)
    try:
        from app.models.runner import Runner
        from app.services.execution.runner_registry import runner_registry

        connected = list(getattr(runner_registry, "_connections", {}) or {})
        if not connected:
            return 0
        result = await db.execute(select(Runner).where(Runner.id.in_(connected)))
        return sum(
            1
            for runner in result.scalars().all()
            if label in (runner.get_labels().get("has") or [])
        )
    except Exception:  # the registry is another lane's; never 500 a list
        logger.debug("runner label count unavailable for %s", endpoint.name)
        return None


async def _read(db: AsyncSession, endpoint: ModelEndpoint) -> ModelEndpointRead:
    return endpoint_read(
        endpoint,
        in_flight=await _in_flight_count(db, endpoint.id),
        runner_count=await _runner_count(db, endpoint),
    )


async def _publish(db: AsyncSession, endpoint: ModelEndpoint) -> None:
    """Emit `model_endpoint_status`. Never raises: a broadcast failure must
    not turn a successful write into a 500."""
    try:
        payload = endpoint_ws_payload(
            endpoint,
            in_flight=await _in_flight_count(db, endpoint.id),
            runner_count=await _runner_count(db, endpoint),
        )
        await manager.publish_model_endpoint_status(endpoint.id, payload)
    except Exception:
        logger.exception("model_endpoint_status broadcast failed for %s", endpoint.id)


def _step_id_from_token(authorization: Optional[str]) -> Optional[str]:
    """The `step_id` claim inside a step JWT, or None.

    Lets a step-authenticated route accept a caller that does not restate its
    own id in the query string (`runner_common.endpoint_probe` and the
    harness's proxy client both hold the token and nothing else). It WIDENS
    THE CALLING CONVENTION, NOT THE TRUST BOUNDARY: `verify_step_auth` still
    validates the signature, the expiry and that the claim matches the row.
    """
    raw = authorization or ""
    if not raw.startswith("Bearer "):
        return None
    from app.services.control_layer.auth import decode_step_token

    claims = decode_step_token(raw[7:])
    return (claims or {}).get("step_id")


def _reset_capability_record(endpoint: ModelEndpoint) -> None:
    """A capability observed against a DIFFERENT model is not evidence about
    this one. Nulls the three booleans and the discovered context window, and
    returns the row to `unprobed` - which dispatch then refuses until the
    operator re-probes."""
    endpoint.supports_tools = None
    endpoint.supports_streaming = None
    endpoint.reports_usage = None
    endpoint.probe_status = "unprobed"
    endpoint.probed_at = None
    endpoint.probed_from = None
    endpoint.probe_harness_version = None
    endpoint.set_probe_detail({})


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------

@router.get("", response_model=list[ModelEndpointRead])
async def list_model_endpoints(
    db: AsyncSession = Depends(get_db),
) -> list[ModelEndpointRead]:
    """Every registered endpoint, with derived health and live in-flight.

    Never returns a secret value - there is no field in the response schema
    that could hold one.
    """
    result = await db.execute(select(ModelEndpoint).order_by(ModelEndpoint.name))
    return [await _read(db, endpoint) for endpoint in result.scalars().all()]


@router.post("", response_model=ProbeResponse, status_code=201)
async def create_model_endpoint(
    payload: ModelEndpointCreate,
    probe: bool = Query(
        True,
        description=(
            "Probe synchronously before returning. Disable only when the "
            "endpoint is known to be down; an unprobed endpoint REFUSES "
            "dispatch until it is probed."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> ProbeResponse:
    """Register an endpoint and, by default, probe it immediately.

    The synchronous probe is the point: the operator learns "this model
    cannot tool-call" here, at registration, instead of at the first
    30-minute agent step.
    """
    existing = await db.execute(
        select(ModelEndpoint).where(ModelEndpoint.name == payload.name)
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A model endpoint named '{payload.name}' already exists. The "
                f"name is the handle every other surface uses "
                f"(model: 'endpoint:{payload.name}'), so it has to be unique."
            ),
        )

    endpoint = ModelEndpoint(
        name=payload.name,
        description=payload.description,
        base_url=payload.base_url,
        model=payload.model,
        server_kind=payload.server_kind,
        auth_style=payload.auth_style,
        auth_secret_ref=payload.auth_secret_ref,
        auth_header_name=payload.auth_header_name,
        reach=payload.reach,
        runner_label=(
            payload.runner_label
            if payload.runner_label
            else (
                default_runner_label(payload.name)
                if payload.reach == "runner-local"
                else None
            )
        ),
        rate_usd_hour=payload.rate_usd_hour,
        gpu_node_id=payload.gpu_node_id or default_gpu_node_id(payload.name),
        max_concurrency=payload.max_concurrency,
        request_timeout_seconds=payload.request_timeout_seconds,
        context_window=payload.context_window,
        max_output_tokens=payload.max_output_tokens,
        enabled=payload.enabled,
        probe_status="unprobed",
        probe_detail="{}",
        consecutive_failures=0,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)

    detail = None
    if probe:
        probed, reason = await _probe_now(db, endpoint, force=True)
        detail = reason if not probed else None
    else:
        detail = (
            "created without probing; dispatch will refuse this endpoint until "
            f"POST /api/model-endpoints/{endpoint.id}/probe succeeds"
        )

    await _publish(db, endpoint)
    return ProbeResponse(
        endpoint=await _read(db, endpoint), cached=False, detail=detail
    )


@router.get("/{reference}", response_model=ModelEndpointRead)
async def get_model_endpoint(
    reference: str, db: AsyncSession = Depends(get_db)
) -> ModelEndpointRead:
    return await _read(db, await _load(db, reference))


@router.patch("/{reference}", response_model=ModelEndpointRead)
async def update_model_endpoint(
    reference: str,
    payload: ModelEndpointUpdate,
    db: AsyncSession = Depends(get_db),
) -> ModelEndpointRead:
    """Edit an endpoint.

    Changing `base_url`, `model`, `server_kind` or any `auth_*` field RESETS
    the capability record to `unprobed` and nulls the three capability
    booleans. Changing `description`, `rate_usd_hour`, `max_concurrency`,
    `enabled` and friends does not: those say nothing about what the model
    can do.
    """
    endpoint = await _load(db, reference)
    changes = payload.model_dump(exclude_unset=True)

    merged_style = changes.get("auth_style", endpoint.auth_style)
    merged_ref = changes.get("auth_secret_ref", endpoint.auth_secret_ref)
    merged_header = changes.get("auth_header_name", endpoint.auth_header_name)
    try:
        # Validated on the MERGED result so a two-step edit cannot assemble a
        # combination a single POST would have refused.
        validate_auth_fields(merged_style, merged_ref, merged_header)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    invalidated = sorted(CAPABILITY_INVALIDATING_FIELDS & set(changes))
    for field, value in changes.items():
        setattr(endpoint, field, value)

    if invalidated:
        _reset_capability_record(endpoint)
        logger.info(
            "endpoint %s: %s changed; capability record reset to unprobed",
            endpoint.name,
            ", ".join(invalidated),
        )

    await db.commit()
    await db.refresh(endpoint)
    await _publish(db, endpoint)
    return await _read(db, endpoint)


@router.delete("/{reference}", status_code=204)
async def delete_model_endpoint(
    reference: str, db: AsyncSession = Depends(get_db)
) -> None:
    """Delete an endpoint. **409 while any step holds one of its slots.**

    Historical `step_usages` rows keep their `gpu_node_id` string and stay
    priceable from `settings.gpu_node_rates`, which is exactly why the usage
    join goes through that column instead of a foreign key.
    """
    endpoint = await _load(db, reference)
    holders = await _in_flight_rows(db, endpoint.id)
    if holders:
        raise HTTPException(
            status_code=409,
            detail=(
                f"endpoint '{endpoint.name}' has {len(holders)} step(s) in "
                f"flight: "
                + ", ".join(f"{h.step_execution_id} ({h.status})" for h in holders)
                + ". Wait for them or cancel them first."
            ),
        )

    # Explicit, because this app never enables PRAGMA foreign_keys and the
    # column deliberately carries no DB-level FK (see models/pipeline.py).
    stale = await db.execute(
        select(StepExecution).where(StepExecution.model_endpoint_id == endpoint.id)
    )
    for row in stale.scalars().all():
        row.model_endpoint_id = None

    endpoint_id = endpoint.id
    await db.delete(endpoint)
    await db.commit()
    try:
        await manager.broadcast("model_endpoint_status", {"id": endpoint_id, "endpoint": None})
    except Exception:
        logger.exception("model_endpoint_status delete broadcast failed")


# -----------------------------------------------------------------------------
# Probe
# -----------------------------------------------------------------------------

async def _probe_now(
    db: AsyncSession, endpoint: ModelEndpoint, *, force: bool
) -> tuple[bool, str | None]:
    """Probe from the position that matters for this endpoint's reach.

    | reach          | probed from                                        |
    |----------------|----------------------------------------------------|
    | `direct`       | the backend, in-process (httpx)                    |
    | `proxy`        | the backend - reachability from here IS the premise |
    | `runner-local` | a real one-step run ON the runner (see below)      |

    A `runner-local` endpoint is unreachable from the backend BY DEFINITION,
    so probing it uses the machinery that already reaches that host: a
    one-step ad-hoc run pinned by `requires: {has: [<runner_label>]}`, which
    reports back to `/probe-result`. That run is scheduled by
    `agent_run.start_endpoint_probe_run` (agent C's lane). Until it lands, a
    runner-local probe returns 200 and SAYS SO - it does not silently probe
    from the backend, which would record a reachability fact about the wrong
    machine.
    """
    if endpoint.reach == "runner-local":
        try:
            from app.services.agent_run import start_endpoint_probe_run
        except ImportError:
            return False, (
                f"endpoint '{endpoint.name}' has reach=runner-local, so it is "
                f"probed BY A RUN on the runner carrying label "
                f"'{endpoint.runner_label or default_runner_label(endpoint.name)}' "
                f"- not from the backend, which cannot see that network. "
                f"agent_run.start_endpoint_probe_run is not available in this "
                f"build; the capability record is unchanged. Use reach=direct "
                f"if the backend can reach this base_url."
            )
        try:
            run = await start_endpoint_probe_run(db, endpoint)
        except Exception as exc:
            # A PROBE IS AN OBSERVATION and the operator's button must never
            # 500. "the run could not be scheduled" is itself the answer, and
            # the reason (no repo to provision a workspace from, no runner
            # carrying the label, ...) is the true reason the endpoint is
            # unusable - so it is returned as the record's detail, at 200.
            logger.warning(
                "could not schedule the runner-local probe run for %s: %s",
                endpoint.name,
                exc,
            )
            return False, (
                f"endpoint '{endpoint.name}' has reach=runner-local, so it is "
                f"probed BY A RUN on the runner carrying label "
                f"'{endpoint.runner_label or default_runner_label(endpoint.name)}'"
                f" - not from the backend, which cannot see that network. That "
                f"run could not be scheduled: {exc}"
            )
        run_id = getattr(run, "id", None) or (
            run.get("id") if isinstance(run, dict) else None
        )
        return False, f"probe run {run_id} scheduled on the runner"

    return await probe_endpoint(db, endpoint, force=force)


@router.post("/{reference}/probe", response_model=ProbeResponse)
async def probe_model_endpoint(
    reference: str,
    force: bool = Query(
        False,
        description=(
            "Bypass the PROBE_MIN_INTERVAL_SECONDS floor. The floor exists to "
            "protect the model server from a spinner-clicking operator."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> ProbeResponse:
    """Re-probe an endpoint.

    **Returns 200 with the record even when the endpoint is down.** A probe
    is an observation, and "it is down" is a successful observation; a 502
    would make the operator's UI show a request error where it should show a
    red endpoint.
    """
    endpoint = await _load(db, reference)
    probed, detail = await _probe_now(db, endpoint, force=force)
    probe_run_id = None
    if not probed and detail and detail.startswith("probe run "):
        probe_run_id = detail.split()[2]

    if probed:
        await _publish(db, endpoint)

    return ProbeResponse(
        endpoint=await _read(db, endpoint),
        cached=not probed,
        probe_run_id=probe_run_id,
        detail=detail,
    )


@router.post("/{reference}/probe-result", response_model=ProbeResponse)
async def report_probe_result(
    reference: str,
    payload: dict,
    step_id: Optional[str] = Query(
        None,
        description=(
            "The StepExecution reporting this observation (its JWT "
            "authenticates it). OPTIONAL: when absent it is read out of the "
            "token's own `step_id` claim, which is what "
            "`runner_common.endpoint_probe` relies on - a reporter that holds "
            "the token already IS the step, so making it re-state its own id "
            "in the query string was one coupling too many."
        ),
    ),
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> ProbeResponse:
    """The runner-local probe reports here, authenticated by the STEP JWT.

    Two fences, both borrowed from 12.6:

    1. the token must be valid for `step_id` (`verify_step_auth`);
    2. `step_execution.model_endpoint_id` must equal this endpoint - the
       split-brain guard, so a token minted for one step cannot rewrite
       another endpoint's capability record.

    `probed_from` is stamped SERVER-SIDE from `step_execution.runner_id` and
    never read from the payload: a runner that could name its own vantage
    point could claim the backend's.
    """
    from app.routers.steps import verify_step_auth

    endpoint = await _load(db, reference)
    step_id = step_id or _step_id_from_token(authorization)
    if not step_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "probe-result needs a step JWT: pass ?step_id= and an "
                "Authorization: Bearer <step token> header, or a token "
                "carrying its own step_id claim"
            ),
        )
    execution = await verify_step_auth(step_id, authorization, db)

    if execution.model_endpoint_id != endpoint.id:
        raise HTTPException(
            status_code=403,
            detail=(
                "this step is not probing this endpoint "
                f"(step {step_id} carries model_endpoint_id="
                f"{execution.model_endpoint_id!r})"
            ),
        )

    try:
        parsed = coerce_probe_result(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"malformed probe result: {exc}") from exc

    result = ProbeResult(
        reachable=parsed.reachable,
        probe_status=parsed.probe_status,
        model_listed=parsed.model_listed,
        supports_tools=parsed.supports_tools,
        supports_streaming=parsed.supports_streaming,
        reports_usage=parsed.reports_usage,
        context_window=parsed.context_window,
        context_window_source=parsed.context_window_source,
        detail=parsed.detail,
        error=parsed.error,
        elapsed_ms=parsed.elapsed_ms,
    )
    probed_from = f"runner:{execution.runner_id}" if execution.runner_id else "runner:unknown"
    apply_probe_result(endpoint, result, probed_from=probed_from)
    await db.commit()
    await db.refresh(endpoint)
    await _publish(db, endpoint)
    return ProbeResponse(endpoint=await _read(db, endpoint), cached=False)


# -----------------------------------------------------------------------------
# Usage rollup
# -----------------------------------------------------------------------------

@router.get("/{reference}/usage", response_model=EndpointUsageRollup)
async def endpoint_usage(
    reference: str, db: AsyncSession = Depends(get_db)
) -> EndpointUsageRollup:
    """What this endpoint has actually cost.

    Joined through `step_usages.gpu_node_id` - the reason `gpu_node_id` is
    NOT NULL on the endpoint row and defaults to `endpoint:<name>`. No
    `model_endpoint_id` was added to the usage table: a materialized copy
    would be a second writer for a fact the join already carries, and
    historical rows must stay priceable after the endpoint is deleted.
    """
    endpoint = await _load(db, reference)
    result = await db.execute(
        select(StepUsage).where(StepUsage.gpu_node_id == endpoint.gpu_node_id)
    )
    rows = list(result.scalars().all())

    by_source = {source: 0 for source in COST_SOURCES}
    input_tokens = 0
    output_tokens = 0
    saw_input = False
    saw_output = False
    total_cost = Decimal("0")
    priced_rows = 0
    wall_clocks: list[int] = []

    for row in rows:
        by_source[row.cost_source] = by_source.get(row.cost_source, 0) + 1
        if row.input_tokens is not None:
            input_tokens += row.input_tokens
            saw_input = True
        if row.output_tokens is not None:
            output_tokens += row.output_tokens
            saw_output = True
        if row.cost_usd is not None:
            total_cost += Decimal(row.cost_usd)
            priced_rows += 1
        if row.wall_clock_ms is not None:
            wall_clocks.append(int(row.wall_clock_ms))

    wall_clocks.sort()
    median = None
    if wall_clocks:
        middle = len(wall_clocks) // 2
        median = (
            wall_clocks[middle]
            if len(wall_clocks) % 2
            else (wall_clocks[middle - 1] + wall_clocks[middle]) // 2
        )

    return EndpointUsageRollup(
        endpoint_id=endpoint.id,
        gpu_node_id=endpoint.gpu_node_id,
        steps=len(rows),
        # NULL, not 0: a zero is a claim ("it used no tokens"), a null is an
        # absence ("nothing reported any"). The distinction is the whole
        # cost-coverage story.
        input_tokens=input_tokens if saw_input else None,
        output_tokens=output_tokens if saw_output else None,
        cost_usd=str(total_cost.quantize(Decimal("0.000001"))) if priced_rows else None,
        by_source=by_source,
        cost_coverage=(priced_rows / len(rows)) if rows else 0.0,
        median_wall_clock_ms=median,
    )


# -----------------------------------------------------------------------------
# The proxy broker (M14 s6.3) - AGENT C's half of this module
# -----------------------------------------------------------------------------
#
# `reach="proxy"` is the mode where the BACKEND makes the inference call on the
# container's behalf. It exists for one deployment shape: the model server is
# reachable from the backend and NOT from the step container (a different
# network namespace, a VPN the backend holds, a vLLM box behind the backend's
# firewall). It is opt-in, never a default, and every dispatch that uses it
# logs a warning saying the backend is now a bottleneck for inference traffic.
#
# THE ONE GENUINE ADVANTAGE, worth stating: **no endpoint secret ever reaches
# the container in proxy mode.** The container authenticates with the step JWT
# it already holds, and the upstream key is injected here, server-side. That is
# why `agent_secret_environment` returns an empty dict for a proxy endpoint.
#
# It is NOT a general egress hole, and four independent gates say so: the
# endpoint must be `reach="proxy"`, the calling step must be the step that
# holds this endpoint, the path must be in a four-entry allowlist, and the body
# is capped.

#: Request body cap. A chat completion with a whole file in it is large; 4 MiB
#: is generous and finite, which is the property that matters.
PROXY_MAX_BODY_BYTES = 4 * 1024 * 1024

#: How long an over-limit request waits for a concurrency slot before 503.
PROXY_QUEUE_TIMEOUT = 120

#: The ONLY upstream paths the broker will forward. `embeddings` is here
#: because the `ModelEndpoint` shape already accommodates it (wave8 s12);
#: nothing uses it yet.
PROXY_ALLOWED_PATHS = frozenset(
    {"chat/completions", "completions", "models", "embeddings"}
)

#: Headers the broker never forwards upstream. `host` would break virtual
#: hosting; `authorization` is the STEP's token and must never be sent to
#: someone else's model server; content-length/transfer-encoding are recomputed
#: by httpx from the body actually sent.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "authorization",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-authenticate",
    }
)

#: Per-endpoint concurrency, enforced HERE as well as at the admission gate:
#: the gate bounds how many STEPS run, this bounds how many REQUESTS are in
#: flight. Belt and braces on the one path where the backend can actually see
#: the concurrent request count.
_proxy_semaphores: dict = {}


def _proxy_semaphore(endpoint: ModelEndpoint) -> asyncio.Semaphore:
    """The semaphore for this endpoint at its CURRENT cap.

    Keyed on `(id, max_concurrency)` so raising the cap through PATCH takes
    effect on the next request instead of being pinned to whatever the value
    was the first time the broker was used.
    """
    key = f"{endpoint.id}:{endpoint.max_concurrency}"
    semaphore = _proxy_semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(int(endpoint.max_concurrency or 1), 1))
        _proxy_semaphores[key] = semaphore
    return semaphore


@router.api_route(
    "/{reference}/proxy/v1/{path:path}",
    # GET (`/models`) and POST (everything else) are the whole
    # OpenAI-compatible surface; a broker that forwards verbs no upstream
    # implements is a wider hole for no benefit.
    methods=["GET", "POST"],
    # Kept OUT of the OpenAPI schema, for the same reason `/ws/runner` is: it
    # is a machine-to-machine TRANSPORT authenticated by a step JWT, not an
    # operator REST surface, and one handler serving two verbs on a
    # `{path:path}` route generates colliding operation ids.
    include_in_schema=False,
)
async def proxy_to_endpoint(
    reference: str,
    path: str,
    request: Request,
    step_id: Optional[str] = Query(
        None,
        description=(
            "The StepExecution making this call. Optional: read from the step "
            "token's own claim when absent."
        ),
    ),
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Forward one OpenAI-compatible request to the endpoint, server-side.

    Gates, in order, each with its own status code so a misconfiguration is
    diagnosable from the response alone:

    | Condition                            | Result |
    |--------------------------------------|--------|
    | `endpoint.reach != "proxy"`          | 404 - the broker is not a general egress hole |
    | invalid / missing step JWT           | 401/403 (`verify_step_auth`) |
    | `step.model_endpoint_id != id`       | 403 - 12.6's split-brain fence |
    | `path` outside the allowlist         | 404 |
    | body over `PROXY_MAX_BODY_BYTES`     | 413 |
    | no slot inside `PROXY_QUEUE_TIMEOUT` | 503 + `Retry-After` |

    Streaming is passed through with `StreamingResponse` and NO buffering, so
    `supports_streaming` still means something on this path. A 503 here is
    handled by the harness's ordinary 5xx retry policy.
    """
    from app.routers.steps import verify_step_auth
    from app.services.model_endpoints.secrets import (
        EndpointSecretMissing,
        auth_headers,
        endpoint_secret_value,
        scrub_secrets,
    )

    endpoint = await _load(db, reference)
    if endpoint.reach != "proxy":
        # 404 and not 403: an endpoint that is not a proxy has no broker at
        # all, and "forbidden" would imply one exists behind a permission.
        raise HTTPException(
            status_code=404,
            detail=(
                f"endpoint '{endpoint.name}' has reach={endpoint.reach!r}; the "
                f"proxy broker exists only for reach='proxy'"
            ),
        )

    proxy_step_id = step_id or _step_id_from_token(authorization)
    if not proxy_step_id:
        raise HTTPException(
            status_code=401, detail="the proxy broker requires a step JWT"
        )
    execution = await verify_step_auth(proxy_step_id, authorization, db)

    if execution.model_endpoint_id != endpoint.id:
        raise HTTPException(
            status_code=403,
            detail=(
                "this step is not running on this endpoint (step "
                f"{proxy_step_id} carries model_endpoint_id="
                f"{execution.model_endpoint_id!r}). The broker forwards only "
                "for the step that holds the endpoint's slot."
            ),
        )

    clean_path = path.strip("/")
    if clean_path not in PROXY_ALLOWED_PATHS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"path {clean_path!r} is not brokered; the allowlist is "
                f"{', '.join(sorted(PROXY_ALLOWED_PATHS))}"
            ),
        )

    body = await request.body()
    if len(body) > PROXY_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"request body is {len(body)} bytes, over the broker's "
                f"{PROXY_MAX_BODY_BYTES}-byte cap"
            ),
        )

    try:
        secret_value = endpoint_secret_value(endpoint, required=True)
    except EndpointSecretMissing as exc:
        # Name the VARIABLE, never the value - the same rule dispatch follows.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    # Injected SERVER-SIDE. This is the whole point of the mode.
    headers.update(auth_headers(endpoint, secret_value))

    upstream = f"{endpoint.base_url.rstrip('/')}/{clean_path}"
    wants_stream = False
    if body:
        try:
            wants_stream = bool((json.loads(body) or {}).get("stream"))
        except (ValueError, AttributeError):
            wants_stream = False

    semaphore = _proxy_semaphore(endpoint)
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=PROXY_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        # A 503 with Retry-After is exactly what the harness's retry policy
        # already handles as an ordinary transient upstream failure.
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "message": (
                            f"endpoint '{endpoint.name}' is at its "
                            f"max_concurrency of {endpoint.max_concurrency} "
                            f"and no slot came free in {PROXY_QUEUE_TIMEOUT}s"
                        ),
                        "type": "endpoint_busy",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": str(PROXY_QUEUE_TIMEOUT)},
        )

    import httpx

    known = [secret_value] if secret_value else []
    if not wants_stream:
        try:
            async with httpx.AsyncClient(
                timeout=endpoint.request_timeout_seconds
            ) as client:
                response = await client.request(
                    request.method, upstream, content=body, headers=headers
                )
            # Scrubbed on the way OUT as well as into the database: a 401 body
            # that echoes the key back is a real failure mode.
            return Response(
                content=scrub_secrets(response.text, known),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "proxy to endpoint %s failed: %s", endpoint.name, type(exc).__name__
            )
            return Response(
                content=json.dumps(
                    {
                        "error": {
                            "message": scrub_secrets(
                                f"could not reach {upstream}: "
                                f"{type(exc).__name__}: {exc}",
                                known,
                            ),
                            "type": "endpoint_unreachable",
                        }
                    }
                ),
                status_code=502,
                media_type="application/json",
            )
        finally:
            semaphore.release()

    # Streaming: the slot is held for the whole stream and released by the
    # generator's `finally`, so a client that disconnects mid-stream cannot
    # leak it.
    async def _stream():
        client = httpx.AsyncClient(timeout=endpoint.request_timeout_seconds)
        try:
            async with client.stream(
                request.method, upstream, content=body, headers=headers
            ) as response:
                # `aiter_bytes`, not `aiter_raw`: the broker does not forward
                # the upstream's `content-encoding`, so handing the client
                # still-compressed bytes under a text/event-stream header
                # would be a stream nothing can parse.
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning(
                "proxy stream to endpoint %s failed: %s",
                endpoint.name,
                type(exc).__name__,
            )
        finally:
            await client.aclose()
            semaphore.release()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
