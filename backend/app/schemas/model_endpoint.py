"""Wire shapes for the model endpoint registry (M14.1, wave8 s1.5).

Vocabularies are `Literal[...]` so an unknown `reach` or `auth_style` is a 422
rather than a row that no consumer can interpret - the same idiom
`schemas/usage.py` uses for `provider` and `cost_source`.

TWO INVARIANTS THIS MODULE ENFORCES, both of them security properties:

1. **`auth_secret_ref` is prefix-allowlisted at CREATE time** (422), so a
   stored row can never reference `ANTHROPIC_API_KEY` or
   `LAZYAF_STEP_AUTH_SECRET`. A stored config must not be an exfiltration
   route.
2. **No schema here has a field that could hold a secret VALUE.**
   `ModelEndpointRead` returns the ref (a NAME) and a computed
   `secret_present: bool`, and nothing else - so a `GET` cannot leak what the
   database never held in the first place.

Money follows the platform convention: `Decimal` in Python and in the DB,
STRING on the wire out. `rate_usd_hour = null` means UNPRICED
(`cost_source="unknown"`); `"0.000000"` is a different, meaningful value -
owned hardware, marginal cash cost - and keeping the two distinguishable is
the entire point.
"""
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null

#: Money resolution on the wire out (NUMERIC(18,6)).
_MONEY = Decimal("0.000001")

# -----------------------------------------------------------------------------
# Vocabularies (pinned; an out-of-vocabulary value is a 422)
# -----------------------------------------------------------------------------

ServerKind = Literal["ollama", "vllm", "llamacpp", "lmstudio", "other"]
AuthStyle = Literal["none", "bearer", "header"]
Reach = Literal["direct", "runner-local", "proxy"]
ProbeStatus = Literal["unprobed", "ok", "degraded", "unreachable"]
Health = Literal["healthy", "stale", "degraded", "unhealthy", "unprobed"]

#: M14.6. Mirrors `models.model_endpoint.MODALITY_NAMES` / `MODALITY_STATES` /
#: `MODALITY_SOURCES` - the same Literal-mirrors-tuple idiom the four
#: vocabularies above use.
ModalityName = Literal["text", "images", "audio", "video"]
ModalityState = Literal[
    "supported",
    "supported_unverified",
    "unsupported",
    "unprobed",
    "undetectable",
    "probe_failed",
    "unrepresentable",
]
ModalitySource = Literal["ollama_capabilities", "wire_probe", "wire_format"]

#: `^[a-z0-9][a-z0-9-]{0,38}$` - capped so `endpoint:<name>` fits
#: `step_usages.gpu_node_id`'s String(64).
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,38}$"


def _money_out(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(_MONEY))


def _normalize_base_url(value: str) -> str:
    """Trailing `/` stripped. A URL not ending in `/v1` is ACCEPTED with a
    warning on the record, never rewritten: guessing at someone's reverse
    proxy layout is how a working endpoint becomes an unexplainable 404."""
    return (value or "").strip().rstrip("/")


def base_url_warning(base_url: str) -> str | None:
    """The non-fatal note surfaced on the record (R1: say it, do not fix it)."""
    if not base_url:
        return None
    if base_url.rstrip("/").endswith("/v1"):
        return None
    return (
        f"base_url '{base_url}' does not end in /v1. The OpenAI-compatible "
        f"root usually includes the version segment (e.g. "
        f"http://host:11434/v1). LazyAF sends requests to "
        f"{base_url}/chat/completions exactly as written and never rewrites "
        f"the URL."
    )


def validate_auth_fields(
    auth_style: str | None,
    auth_secret_ref: str | None,
    auth_header_name: str | None,
) -> None:
    """Shared by Create and the merged result of a PATCH, so a two-step edit
    cannot assemble a combination a single POST would have refused."""
    from app.services.model_endpoints.secrets import secret_ref_refusal

    refusal = secret_ref_refusal(auth_secret_ref)
    if refusal:
        raise ValueError(refusal)
    if auth_style in ("bearer", "header") and not auth_secret_ref:
        raise ValueError(
            f"auth_style '{auth_style}' requires auth_secret_ref - the NAME of "
            f"a backend environment variable (the value is never stored)"
        )
    if auth_style == "header" and not (auth_header_name or "").strip():
        raise ValueError("auth_style 'header' requires auth_header_name, e.g. x-api-key")


# -----------------------------------------------------------------------------
# Capability snapshot
# -----------------------------------------------------------------------------

class Modality(BaseModel):
    """One modality's answer WITH its provenance (M14.6).

    This exists because `bool | None` cannot carry the six distinctions a
    human needs, and deriving them in the frontend would be a SECOND
    derivation of a backend fact - exactly what `ModelEndpoint.health` refuses
    when it says a second stored health column would be a second writer that
    drifts from the first. Computed once, here, beside `health`.

    The two collapses that would be wrong, named so a reviewer can look for
    them:

    * `unprobed` vs `probe_failed`. Both are a null column and both refuse at
      dispatch, but one says PRESS PROBE and the other says the probe ran and
      broke - read the error before you press it again.
    * `undetectable` vs `unsupported`. `unsupported` is a positive refusal you
      can quote back ("HTTP 400: this model does not support image input").
      `undetectable` is a request that SUCCEEDS WHILE DOING NOTHING, which is
      the more dangerous of the two and the one R1 exists to surface.
    """

    modality: ModalityName
    state: ModalityState
    #: `ollama_capabilities` - free, from `/api/show` (images only).
    #: `wire_probe`          - one or two real requests against the model.
    #: `wire_format`         - a CONSTANT of the protocol, not an observation:
    #:                         text always works, video never can.
    source: ModalitySource | None = None
    #: The `MODALITY_REASONS` entry behind `state`, or null when there is
    #: nothing to explain (a plain `supported`).
    reason: str | None = None
    #: The upstream refusal, scrubbed and capped at 256 chars. This is what
    #: makes `unsupported` actionable instead of merely red.
    evidence: str | None = None
    #: Narrows a `supported`: `no_usage_no_control` / `control_unavailable`
    #: mean the endpoint ACCEPTED the shape but no token ledger was available
    #: to prove the attachment actually entered the prompt.
    caveat: str | None = None


class EndpointCapabilities(BaseModel):
    """What the last probe observed. A SNAPSHOT, never a live reference:
    a step must behave identically if someone re-probes mid-run, and M13
    needs to attribute a result to the capabilities that were in force.

    `supports_tools` is three-state on purpose. `null` is not "assume no": it
    is "we have not asked", and dispatch REFUSES on it. `supports_images` and
    `supports_audio` inherit that doctrine whole - and note that every
    endpoint registered before M14.6 reads `null` for both until it is
    re-probed, which the UI must render as NOT PROBED and never as "does not
    support images".
    """

    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    reports_usage: bool | None = None
    #: M14.6, three-state. True means the endpoint ACCEPTED the content part
    #: and (where a usage block made it measurable) the attachment entered the
    #: prompt. It is NOT a claim that the model is any good at vision/audio.
    supports_images: bool | None = None
    supports_audio: bool | None = None
    #: EFFECTIVE window: operator override, else what the probe discovered,
    #: else null (and null means the harness assumes 8192 and says so).
    context_window: int | None = None
    max_output_tokens: int | None = None
    probe_status: ProbeStatus = "unprobed"
    probed_at: UTCDateTime | None = None
    probed_from: str | None = None
    probe_age_seconds: float | None = None
    stale: bool = False
    #: DERIVED, one entry per `MODALITY_NAMES`, always all four and always in
    #: that order - a modality the UI has to look up by name is a modality the
    #: UI can silently fail to render. Includes `video`, which is permanently
    #: `unrepresentable` and is here precisely so the human sees the row and
    #: reads WHY rather than wondering where video went.
    modalities: list[Modality] = Field(default_factory=list)


class EndpointPricing(BaseModel):
    """The cost coordinates that travel to the container on the wire."""

    gpu_node_id: str
    gpu_fraction: float
    #: True when a rate exists AT ALL. `0.00/hr` is priced (owned hardware);
    #: `null` is "we do not know" and yields `cost_source="unknown"`.
    priced: bool


class EndpointInFlight(BaseModel):
    """One step currently holding a slot on this endpoint."""

    step_execution_id: str
    step_run_id: str | None = None
    status: str
    started_at: UTCDateTime | None = None


class ProbeResult(BaseModel):
    """One probe observation - the shape `run_probe` produces and the shape
    the runner-local probe POSTs back to `/probe-result`. Two spellings of
    ONE contract (`services/model_endpoints/probe.ProbeResult.to_dict`)."""

    reachable: bool
    probe_status: ProbeStatus
    model_listed: bool | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    reports_usage: bool | None = None
    #: M14.6. Optional with a `None` default so a runner-local probe running
    #: an OLDER runner image - which does not know how to ask these questions
    #: yet - reports "we did not ask" rather than failing validation or, far
    #: worse, defaulting to False and recording a capability claim no probe
    #: ever made.
    supports_images: bool | None = None
    supports_audio: bool | None = None
    context_window: int | None = None
    context_window_source: str | None = None
    detail: dict = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0


# -----------------------------------------------------------------------------
# Create / Update / Read
# -----------------------------------------------------------------------------

class ModelEndpointCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, max_length=40)
    description: str | None = None
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=200)
    #: Forensics and probe HINTS only - never behavior. The one place it is
    #: read is the ollama context-window discovery (probe request 4).
    server_kind: ServerKind = "other"

    #: `none` is the DEFAULT and a first-class case: LAN ollama and vLLM
    #: behind a firewall genuinely have no key.
    auth_style: AuthStyle = "none"
    auth_secret_ref: str | None = Field(default=None, max_length=64)
    auth_header_name: str | None = Field(default=None, max_length=64)

    reach: Reach = "direct"
    runner_label: str | None = Field(default=None, max_length=64)

    rate_usd_hour: Decimal | None = Field(default=None, ge=0)
    gpu_node_id: str | None = Field(default=None, max_length=64)

    max_concurrency: int = Field(default=1, ge=1, le=64)
    request_timeout_seconds: int = Field(default=300, ge=1, le=3600)

    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)

    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _normalize_base_url(value)

    @model_validator(mode="after")
    def _check_auth(self) -> "ModelEndpointCreate":
        validate_auth_fields(self.auth_style, self.auth_secret_ref, self.auth_header_name)
        return self


class ModelEndpointUpdate(BaseModel):
    """PATCH body. Absent means "leave it"; an explicit `null` on a NOT NULL
    column is a 422 naming the field (the `_patch.not_null` idiom).

    Changing `base_url`, `model`, `server_kind` or `auth_*` RESETS the
    capability record to `unprobed` (router-side): a capability observed
    against a different model is not evidence about this one.
    """

    description: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    server_kind: ServerKind | None = None

    auth_style: AuthStyle | None = None
    auth_secret_ref: str | None = Field(default=None, max_length=64)
    auth_header_name: str | None = Field(default=None, max_length=64)

    reach: Reach | None = None
    runner_label: str | None = Field(default=None, max_length=64)

    rate_usd_hour: Decimal | None = Field(default=None, ge=0)
    gpu_node_id: str | None = Field(default=None, max_length=64)

    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)

    enabled: bool | None = None

    _reject_nulls = not_null(
        "base_url",
        "model",
        "server_kind",
        "auth_style",
        "reach",
        "gpu_node_id",
        "max_concurrency",
        "request_timeout_seconds",
        "enabled",
    )

    @field_validator("base_url")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_base_url(value)


#: The fields whose change invalidates every capability observation.
CAPABILITY_INVALIDATING_FIELDS: frozenset = frozenset(
    {"base_url", "model", "server_kind", "auth_style", "auth_secret_ref", "auth_header_name"}
)


class ModelEndpointRead(BaseModel):
    """The registry row as the operator API returns it.

    Contains `auth_secret_ref` (a NAME) and `secret_present` (a bool) and
    NEVER a secret value - there is no field here that could hold one.
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    name: str
    description: str | None = None
    base_url: str
    model: str
    server_kind: str

    auth_style: str
    auth_secret_ref: str | None = None
    auth_header_name: str | None = None
    #: Is the referenced backend variable actually set? Drives the UI's red
    #: "not set in the backend environment" hint without exposing anything.
    secret_present: bool = True

    reach: str
    runner_label: str | None = None
    #: How many CONNECTED runners carry `runner_label`. `0` on a runner-local
    #: endpoint is the reason a step would sit at NO_RUNNER_TIMEOUT, visible
    #: BEFORE anyone dispatches to it. None when the fact is not applicable.
    runner_count: int | None = None

    rate_usd_hour: str | None = None
    gpu_node_id: str
    gpu_fraction: float
    priced: bool

    max_concurrency: int
    request_timeout_seconds: int
    #: The raw OVERRIDE column (null = "let the probe decide"). The effective
    #: value is `capabilities.context_window`.
    context_window: int | None = None
    context_window_source: str | None = None
    max_output_tokens: int | None = None

    capabilities: EndpointCapabilities
    pricing: EndpointPricing
    health: Health
    probe_detail: dict = Field(default_factory=dict)
    consecutive_failures: int = 0
    last_success_at: UTCDateTime | None = None
    last_error: str | None = None
    #: Non-fatal note about the URL shape, or None. Stated, never fixed.
    warning: str | None = None

    enabled: bool
    in_flight: int = 0

    created_at: UTCDateTime
    updated_at: UTCDateTime


class ProbeResponse(BaseModel):
    """`POST /api/model-endpoints/{id}/probe`.

    **200 even when the endpoint is down.** A probe is an observation, and
    "it is down" is a successful observation; a 502 would make the operator's
    UI show a request error where it should show a red endpoint.
    """

    endpoint: ModelEndpointRead
    #: True when the record was returned without an upstream call (a probe
    #: inside PROBE_MIN_INTERVAL_SECONDS, or a runner-local probe that was
    #: dispatched rather than performed here).
    cached: bool = False
    #: For `reach == "runner-local"`: the pipeline run that carries the probe.
    probe_run_id: str | None = None
    #: Why, in one sentence, whenever `cached` is true or a run was scheduled.
    detail: str | None = None


class EndpointUsageRollup(BaseModel):
    """`GET /api/model-endpoints/{id}/usage` - the endpoint's cost story.

    Joined through `step_usages.gpu_node_id`, NOT through a materialized
    `model_endpoint_id`: a second writer for a fact the join already carries
    is exactly what 12.6.5 refused when it declined to copy `cost_usd` onto
    `experiment_runs`.
    """

    endpoint_id: str
    gpu_node_id: str
    steps: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: str | None = None
    #: Every cost_source, always present - a zero is a fact, an absent key is
    #: an ambiguity.
    by_source: dict = Field(default_factory=dict)
    #: Fraction of rows carrying a non-null cost. < 1.0 is a WARNING, not a
    #: rounding error.
    cost_coverage: float = 0.0
    median_wall_clock_ms: int | None = None


# -----------------------------------------------------------------------------
# Projection (ONE place a row becomes a payload)
# -----------------------------------------------------------------------------

#: The reason `video` carries, forever, on every endpoint.
VIDEO_REASON = "wire_format_has_no_video_content_part"
#: The reason `text` carries. Also a property of the protocol, not a probe.
TEXT_REASON = "wire_format_base_content_type"


def modalities_of(endpoint) -> list[Modality]:
    """All four modalities, always, in `MODALITY_NAMES` order (M14.6).

    Always all four, and never filtered: "consistently broken out" means the
    human sees the same four rows on every endpoint and reads a different
    STATE, rather than inferring meaning from which chips are missing.

    `text` and `video` are answered from the wire format itself and cost
    nothing. Neither is an observation about this server: text is the base
    content type of every chat-completions request, and video has no content
    part to send at all. Whether the server WORKS is `health`; these two rows
    are about what the protocol can express.
    """
    from app.models.model_endpoint import MODALITY_NAMES
    from app.services.model_endpoints.probe import modality_state

    detail = endpoint.get_probe_detail()
    columns = {
        "images": endpoint.supports_images,
        "audio": endpoint.supports_audio,
    }

    out: list[Modality] = []
    for name in MODALITY_NAMES:
        if name == "text":
            out.append(
                Modality(
                    modality="text",
                    state="supported",
                    source="wire_format",
                    reason=TEXT_REASON,
                )
            )
            continue
        if name == "video":
            out.append(
                Modality(
                    modality="video",
                    state="unrepresentable",
                    source="wire_format",
                    reason=VIDEO_REASON,
                )
            )
            continue

        value = columns[name]
        reason = detail.get(f"{name}_reason")
        reason = reason if isinstance(reason, str) else None
        source = detail.get(f"{name}_source")
        evidence = detail.get(f"{name}_body")
        caveat = detail.get(f"{name}_caveat")
        state = modality_state(value, reason, caveat if isinstance(caveat, str) else None)
        out.append(
            Modality(
                modality=name,
                state=state,
                # No source when nobody asked - an `unprobed` row that named a
                # source would be claiming a probe happened.
                source=source if source in ("ollama_capabilities", "wire_probe") else None,
                reason=reason,
                evidence=evidence if isinstance(evidence, str) else None,
                caveat=caveat if isinstance(caveat, str) else None,
            )
        )
    return out


def capabilities_of(endpoint) -> EndpointCapabilities:
    """The capability snapshot, computed in exactly one place so the API, the
    WS frame and the agent-config block cannot disagree."""
    return EndpointCapabilities(
        supports_tools=endpoint.supports_tools,
        supports_streaming=endpoint.supports_streaming,
        reports_usage=endpoint.reports_usage,
        supports_images=endpoint.supports_images,
        supports_audio=endpoint.supports_audio,
        modalities=modalities_of(endpoint),
        context_window=endpoint.effective_context_window,
        max_output_tokens=endpoint.max_output_tokens,
        probe_status=endpoint.probe_status,
        probed_at=endpoint.probed_at,
        probed_from=endpoint.probed_from,
        probe_age_seconds=endpoint.probe_age_seconds,
        stale=endpoint.probe_stale,
    )


def endpoint_read(
    endpoint,
    *,
    in_flight: int = 0,
    runner_count: int | None = None,
) -> ModelEndpointRead:
    """Row -> `ModelEndpointRead`. The single projection: every route, the WS
    frame and every test read the API through this function, so a field can
    never be present on one surface and missing on another."""
    from app.services.model_endpoints.secrets import secret_present

    return ModelEndpointRead(
        id=endpoint.id,
        name=endpoint.name,
        description=endpoint.description,
        base_url=endpoint.base_url,
        model=endpoint.model,
        server_kind=endpoint.server_kind,
        auth_style=endpoint.auth_style,
        auth_secret_ref=endpoint.auth_secret_ref,
        auth_header_name=endpoint.auth_header_name,
        secret_present=secret_present(endpoint),
        reach=endpoint.reach,
        runner_label=endpoint.runner_label,
        runner_count=runner_count,
        rate_usd_hour=_money_out(endpoint.rate_usd_hour),
        gpu_node_id=endpoint.gpu_node_id,
        gpu_fraction=endpoint.gpu_fraction,
        priced=endpoint.priced,
        max_concurrency=endpoint.max_concurrency,
        request_timeout_seconds=endpoint.request_timeout_seconds,
        context_window=endpoint.context_window,
        context_window_source=endpoint.context_window_source,
        max_output_tokens=endpoint.max_output_tokens,
        capabilities=capabilities_of(endpoint),
        pricing=EndpointPricing(
            gpu_node_id=endpoint.gpu_node_id,
            gpu_fraction=endpoint.gpu_fraction,
            priced=endpoint.priced,
        ),
        health=endpoint.health,
        probe_detail=endpoint.get_probe_detail(),
        consecutive_failures=endpoint.consecutive_failures,
        last_success_at=endpoint.last_success_at,
        last_error=endpoint.last_error,
        warning=base_url_warning(endpoint.base_url),
        enabled=endpoint.enabled,
        in_flight=in_flight,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def endpoint_ws_payload(endpoint, *, in_flight: int = 0, runner_count: int | None = None) -> dict:
    """The `model_endpoint_status` frame body - the same projection, JSON-mode
    dumped so datetimes carry their `+00:00` designator (schemas/_datetime)."""
    return endpoint_read(
        endpoint, in_flight=in_flight, runner_count=runner_count
    ).model_dump(mode="json")


def coerce_probe_result(payload: Any) -> ProbeResult:
    """Parse a runner-reported probe payload. Raises `ValueError` on anything
    that is not a probe observation - a runner may not write an arbitrary
    capability record just by POSTing one."""
    if not isinstance(payload, dict):
        raise ValueError("probe result must be a JSON object")
    return ProbeResult.model_validate(payload)
