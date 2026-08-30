"""ModelEndpoint - a self-hosted OpenAI-compatible (server, model) pair (M14.1).

One row is one addressable (server, model) pair, NOT one row per server:
ollama on one box serving `qwen2.5-coder:32b` and `llama3.1:8b` is two
endpoints, because every decision the platform makes - tool support, context
window, rate, concurrency - is a property of the MODEL on that server, not of
the server. Two rows sharing a `base_url` is normal and cheap.

THE TWO PROPERTIES THAT MATTER MOST (wave8 s1.1/s1.2, pinned by tests):

1. **The database never stores a secret value.** `auth_secret_ref` names an
   environment variable ON THE BACKEND, prefix-allowlisted to
   `LAZYAF_ENDPOINT_*` (see `app.services.model_endpoints.secrets`) so a
   stored row can never reference `ANTHROPIC_API_KEY` or
   `LAZYAF_STEP_AUTH_SECRET`. LazyAF has no secret-at-rest story - no
   encryption key, no KMS, SQLite backups are plain files, and
   `GET /api/model-endpoints` is unauthenticated like the rest of the
   operator API - so a stored key would be a new class of exposure
   introduced for the convenience of one form field.

2. **`supports_tools` is THREE-STATE.** `None` is not "assume no": it is
   "we have not asked", and dispatch REFUSES on it
   (`app.services.model_endpoints.resolve`). A default of `False` would
   silently route every new endpoint down the no-tools fallback protocol,
   which is exactly the kind of invisible downgrade R1 exists to forbid.

Derived state is computed HERE and nowhere else (`probe_age_seconds`,
`probe_stale`, `health`, `effective_context_window`). A second stored health
column would be a second writer that drifts from the first.

Import note (the `models/runner.py` precedent): the probe's timing constants
live in `app.services.model_endpoints.probe`, which imports THIS module. The
properties that need them import lazily, inside the method - a models import
must not drag in a service package.
"""
import json
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: Consecutive failures (probe OR real step outcome) after which dispatch
#: refuses. Three in a row is not a blip. Read by `resolve.py` (the refusal)
#: and by `health` below (the colour), so the two cannot disagree.
ENDPOINT_FAILURE_THRESHOLD = 3

#: StepExecution statuses that hold one of an endpoint's concurrency slots.
#: The admission gate (12.6-shaped CAS, owner C) counts rows in these states;
#: DELETE refuses while any exist. Kept here so the gate and the API agree.
IN_FLIGHT_STEP_STATUSES: tuple[str, ...] = (
    "assigned",
    "preparing",
    "running",
    "completing",
)

#: `server_kind` is FORENSICS AND PROBE HINTS ONLY - never behavior. The one
#: place it is read is the context-window discovery order (probe request 4).
SERVER_KINDS: tuple[str, ...] = ("ollama", "vllm", "llamacpp", "lmstudio", "other")
AUTH_STYLES: tuple[str, ...] = ("none", "bearer", "header")
REACH_MODES: tuple[str, ...] = ("direct", "runner-local", "proxy")
PROBE_STATUSES: tuple[str, ...] = ("unprobed", "ok", "degraded", "unreachable")

#: The health vocabulary the UI renders. DERIVED, never stored.
HEALTH_STATES: tuple[str, ...] = (
    "healthy",
    "stale",
    "degraded",
    "unhealthy",
    "unprobed",
)

#: `name` regex (wave8 s1.1). Capped so `endpoint:<name>` fits `gpu_node_id`'s
#: String(64) with room to spare.
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,38}$"

#: The one spelling of an endpoint's node coordinate. `gpu_node_id` defaults
#: to this and joins `step_usages.gpu_node_id`, which is why no
#: `model_endpoint_id` column was added to the usage table: a materialized
#: copy would be a second writer for a fact the join already carries.
NODE_ID_PREFIX = "endpoint:"


def default_gpu_node_id(name: str) -> str:
    """`endpoint:<name>` - the usage-join coordinate for this endpoint."""
    return f"{NODE_ID_PREFIX}{name}"


def default_runner_label(name: str) -> str:
    """`endpoint:<name>` - the `requires: {has: [...]}` label a runner-local
    endpoint injects, and the label an operator puts in
    LAZYAF_RUNNER_LABELS on the box that hosts the model."""
    return f"{NODE_ID_PREFIX}{name}"


class ModelEndpoint(Base):
    """A registered self-hosted OpenAI-compatible (server, model) pair."""

    __tablename__ = "model_endpoints"
    __table_args__ = (
        # The handle every other surface uses (`model: "endpoint:local-4090"`).
        Index("ix_model_endpoints_name", "name", unique=True),
        # The usage rollup's only predicate.
        Index("ix_model_endpoints_gpu_node_id", "gpu_node_id"),
        # The dispatcher/UI scan: "which endpoints can take work right now".
        Index("ix_model_endpoints_enabled_reach", "enabled", "reach"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The OpenAI-compatible ROOT **including** the version segment, e.g.
    #: `http://192.168.1.50:11434/v1`. Stored normalized (trailing `/`
    #: stripped); a URL not ending in `/v1` is accepted with a WARNING on the
    #: record, never rewritten - guessing at someone's reverse proxy layout
    #: is how a working endpoint becomes an unexplainable 404.
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The id sent in the request body (`qwen2.5-coder:32b`).
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    server_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="other", server_default="other"
    )

    auth_style: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    #: The NAME of a backend env var. **Never a value.** See the module
    #: docstring and `services/model_endpoints/secrets.py`.
    auth_secret_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_header_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    reach: Mapped[str] = mapped_column(
        String(16), nullable=False, default="direct", server_default="direct"
    )
    #: `runner-local` only; defaults to `endpoint:<name>`.
    runner_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: NULL = unpriced -> `cost_source="unknown"`, `cost_usd=NULL`.
    #: `0.000000` is a LEGAL, DIFFERENT, meaningful value: owned hardware,
    #: marginal cash cost, priced `gpu-node` at $0. Keeping those two
    #: distinguishable is the whole point of the cost decision.
    rate_usd_hour: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    gpu_node_id: Mapped[str] = mapped_column(String(64), nullable=False)

    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    #: Per HTTP request, not per step. 300s because a cold ollama loading a
    #: 32B model can genuinely take a minute on the FIRST request.
    request_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )

    #: Operator OVERRIDE only. The probe never writes this column; what the
    #: probe discovered lives in `probe_detail["context_window"]`, and
    #: `effective_context_window` applies the precedence. One column, one
    #: writer, and an override that survives every re-probe.
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Operator override; NULL means DEFAULT_MAX_OUTPUT_TOKENS at use time.
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: PROBED. NULL = never probed = dispatch refuses (see module docstring).
    supports_tools: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_streaming: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Does the server return a `usage` block at all? The whole cost story
    #: depends on this one.
    reports_usage: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    probe_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unprobed", server_default="unprobed"
    )
    #: JSON, scrubbed and capped at 4 KiB by the probe service.
    probe_detail: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    probed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: `backend` or `runner:<runner_id>`. Stamped SERVER-SIDE, never from a
    #: payload: "the backend could reach it" must never be silently read as
    #: "the step can".
    probed_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    probe_harness_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Bumped by probes AND by real step outcomes; zeroed by either
    #: succeeding. A healthy endpoint therefore never drifts into the
    #: stale-and-failing state through disuse of the probe button alone.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Scrubbed, 512 chars. A 401 body that echoes the key back is a real
    #: failure mode and it must not be the thing that puts the key in the DB.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # -- probe_detail ---------------------------------------------------------

    def get_probe_detail(self) -> dict:
        """Decode `probe_detail`. Malformed content reads as `{}`: a corrupt
        blob must make an endpoint look UNEXPLAINED, never crash the list
        endpoint that renders every row."""
        if not self.probe_detail:
            return {}
        try:
            decoded = json.loads(self.probe_detail)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def set_probe_detail(self, detail: dict | None) -> None:
        from app.services.model_endpoints.probe import PROBE_DETAIL_MAX_BYTES

        blob = json.dumps(detail or {}, sort_keys=True, default=str)
        if len(blob.encode("utf-8")) > PROBE_DETAIL_MAX_BYTES:
            blob = json.dumps(
                {
                    "truncated": True,
                    "reason": (
                        f"probe_detail exceeded {PROBE_DETAIL_MAX_BYTES} bytes"
                    ),
                    "probe_status": (detail or {}).get("probe_status"),
                },
                sort_keys=True,
            )
        self.probe_detail = blob

    # -- derived capability state --------------------------------------------

    @property
    def probe_age_seconds(self) -> float | None:
        """Seconds since the capability record was written, or None if never."""
        if self.probed_at is None:
            return None
        return max(0.0, (datetime.utcnow() - self.probed_at).total_seconds())

    @property
    def probe_stale(self) -> bool:
        """True when the capability record is older than PROBE_TTL_SECONDS.

        A stale record still WORKS: dispatch runs, warns and schedules a
        background re-probe. It is amber, not red. Blocking on staleness
        would make a working endpoint stop working overnight; running blind
        would hide it.
        """
        from app.services.model_endpoints.probe import PROBE_TTL_SECONDS

        age = self.probe_age_seconds
        return age is not None and age > PROBE_TTL_SECONDS

    @property
    def context_window_source(self) -> str | None:
        """Where `effective_context_window` came from: `override` (the
        operator set the column), the probe's own source string, or None."""
        if self.context_window is not None:
            return "override"
        return self.get_probe_detail().get("context_window_source")

    @property
    def effective_context_window(self) -> int | None:
        """Precedence (wave8 s2.2): operator override, then whatever the
        probe discovered, then None - and None means the harness assumes
        DEFAULT_ASSUMED_CONTEXT and SAYS SO, loudly, in the step log."""
        if self.context_window is not None:
            return self.context_window
        discovered = self.get_probe_detail().get("context_window")
        return discovered if isinstance(discovered, int) else None

    @property
    def health(self) -> str:
        """healthy | stale | degraded | unhealthy | unprobed.

        DERIVED from probe_status + probe age + consecutive_failures. Order
        matters and is stated: never-asked outranks everything (we know
        nothing); repeated failure outranks a stale capability record
        (the endpoint is down, whatever it could once do); a degraded
        capability outranks staleness (it will behave differently and the
        operator needs to know WHY, not just that it is old).
        """
        if self.probe_status == "unprobed":
            return "unprobed"
        if (
            self.probe_status == "unreachable"
            or self.consecutive_failures >= ENDPOINT_FAILURE_THRESHOLD
        ):
            return "unhealthy"
        if self.probe_status == "degraded":
            return "degraded"
        if self.probe_stale:
            return "stale"
        return "healthy"

    @property
    def gpu_fraction(self) -> float:
        """`1.0 / max_concurrency` - the one place this is computed.

        The node bills by the hour regardless of how many steps share it, so
        charging each of K concurrent steps 1.0 multiplies the node's real
        cost by K and would inflate exactly the measurement M14 exists to
        enable. Dividing by the concurrency CAP under-attributes when the
        node is idle, which is the smaller, stated error; `container_seconds`
        and `max_concurrency` are both recorded so any figure here is
        re-derivable later under a different model.
        """
        return 1.0 / max(int(self.max_concurrency or 1), 1)

    @property
    def priced(self) -> bool:
        """True when a rate exists at all. `0.000000` is PRICED (owned
        hardware, marginal cash cost); NULL is "we do not know"."""
        return self.rate_usd_hour is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ModelEndpoint {self.name} model={self.model} reach={self.reach} "
            f"probe={self.probe_status} health={self.health}>"
        )
