"""How a step selects an endpoint - THE resolver (wave8 s6.1).

Cross-agent contract #4: `ENDPOINT_MODEL_PREFIX` is the ONE `endpoint:<name>`
sugar spelling, and `resolve_step_endpoint` is its only parser. There is no
second one anywhere - the four selection surfaces (card model picker,
playground, pipeline step form, experiment matrix) all PRODUCE the string and
this module is the only thing that reads it.

Why the sugar is worth having: `step_config["model"]` is the field all four
surfaces already populate, so spelling a self-hosted model as
`"endpoint:local-4090"` reaches the dispatcher from every one of them with
**zero schema changes anywhere** - which is what makes a 12.6.5 matrix mixing
API and self-hosted models cost nothing.

There is NO default endpoint, for the same reason there is no default agent:
guessing which GPU to bill is not a recoverable mistake.
"""
import logging

from sqlalchemy import select

from app.models.model_endpoint import ENDPOINT_FAILURE_THRESHOLD, ModelEndpoint

logger = logging.getLogger(__name__)

#: THE sugar spelling (cross-agent contract #4).
ENDPOINT_MODEL_PREFIX = "endpoint:"


def parse_endpoint_reference(step_config: dict | None) -> str | None:
    """The name-or-id a step is asking for, or None.

    Precedence:
      1. `step_config["endpoint"]` - the explicit spelling
      2. `step_config["model"]` starting with `endpoint:` - the sugar
      3. neither -> None (the caller raises; see the module docstring)
    """
    if not isinstance(step_config, dict):
        return None
    explicit = step_config.get("endpoint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    model = step_config.get("model")
    if isinstance(model, str) and model.startswith(ENDPOINT_MODEL_PREFIX):
        reference = model[len(ENDPOINT_MODEL_PREFIX):].strip()
        return reference or None
    return None


def endpoint_dispatch_refusal(endpoint: ModelEndpoint) -> str | None:
    """Why this endpoint must NOT be dispatched to, or None to proceed.

    Four refusals, each with the whole fix in the message (wave8 s2.4):

    - never probed. NOT a probe-on-first-use: a 30-minute agent step is not
      the place to discover the model cannot tool-call, and an implicit probe
      would put a 60s stall inside the step's timeout budget. This is the
      refusal that makes `supports_tools = None` mean something.
    - three consecutive failures. Not a blip.
    - disabled.
    - probed OK but tools were never established and the wire says nothing.
      `supports_tools is None` on an otherwise-ok record means the record is
      incoherent; refusing beats silently routing the fallback protocol,
      which is exactly the invisible downgrade R1 forbids.

    STALENESS IS NOT HERE ON PURPOSE. A stale record RUNS, warns and triggers
    a background re-probe: blocking on staleness would make a working endpoint
    stop working overnight.
    """
    if not endpoint.enabled:
        return f"endpoint '{endpoint.name}' is disabled"
    if endpoint.probe_status == "unprobed":
        return (
            f"endpoint '{endpoint.name}' has never been probed; "
            f"POST /api/model-endpoints/{endpoint.id}/probe first"
        )
    if (
        endpoint.probe_status == "unreachable"
        and int(endpoint.consecutive_failures or 0) >= ENDPOINT_FAILURE_THRESHOLD
    ):
        return (
            f"endpoint '{endpoint.name}' has failed "
            f"{endpoint.consecutive_failures} consecutive times; last error: "
            f"{endpoint.last_error or 'unknown'}"
        )
    if endpoint.supports_tools is None:
        return (
            f"endpoint '{endpoint.name}' has no tool-calling observation "
            f"(supports_tools is null); re-probe it before dispatch - "
            f"defaulting to the no-tools fallback would be a silent downgrade"
        )
    return None


def endpoint_dispatch_warning(endpoint: ModelEndpoint) -> str | None:
    """What dispatch must SAY OUT LOUD before running on this endpoint.

    R1: warn plus refresh is the only honest option for a stale capability
    record. Blocking on staleness would make a working endpoint stop working
    overnight; running blind would hide it. The caller logs this into the
    step's own log stream and schedules `background_reprobe`.
    """
    notes = []
    if endpoint.probe_stale:
        age_hours = int((endpoint.probe_age_seconds or 0) // 3600)
        notes.append(
            f"endpoint {endpoint.name} capability record is {age_hours}h old; "
            f"re-probing in the background"
        )
    if endpoint.reach == "proxy":
        notes.append(
            f"endpoint '{endpoint.name}' uses reach=proxy; inference traffic "
            f"flows through the backend and the backend is a bottleneck for it"
        )
    if endpoint.effective_context_window is None:
        notes.append(
            f"endpoint {endpoint.name} declares no context window; the harness "
            f"will assume 8192 tokens"
        )
    if endpoint.reports_usage is False:
        notes.append(
            f"endpoint {endpoint.name} reported no usage block at probe time; "
            f"this step's token counts will be null"
        )
    return "; ".join(notes) if notes else None


async def _load_by_reference(db, reference: str) -> ModelEndpoint | None:
    result = await db.execute(
        select(ModelEndpoint).where(ModelEndpoint.name == reference)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is not None:
        return endpoint
    result = await db.execute(
        select(ModelEndpoint).where(ModelEndpoint.id == reference)
    )
    return result.scalar_one_or_none()


async def _enabled_names(db) -> list[str]:
    result = await db.execute(
        select(ModelEndpoint.name)
        .where(ModelEndpoint.enabled.is_(True))
        .order_by(ModelEndpoint.name)
    )
    return [row[0] for row in result.all()]


async def resolve_step_endpoint(
    db, step_config: dict | None, step_name: str
) -> ModelEndpoint:
    """The ONE resolver. Raises `ValueError` with an actionable message.

    Every refusal names the endpoints that DO exist, because "unknown
    endpoint 'local-4090'" without that list is one round trip short of
    useful when the actual problem is a typo.
    """
    reference = parse_endpoint_reference(step_config)
    if not reference:
        raise ValueError(
            f"step '{step_name}' uses agent 'openai-harness' but names no "
            f"endpoint: set config.endpoint: <name>, or "
            f"config.model: '{ENDPOINT_MODEL_PREFIX}<name>'. There is no "
            f"default endpoint - guessing which GPU to bill is not a "
            f"recoverable mistake."
        )

    endpoint = await _load_by_reference(db, reference)
    if endpoint is None:
        known = await _enabled_names(db)
        raise ValueError(
            f"step '{step_name}' names unknown model endpoint '{reference}'. "
            f"Enabled endpoints: {', '.join(known) if known else '(none registered)'}"
        )

    refusal = endpoint_dispatch_refusal(endpoint)
    if refusal:
        raise ValueError(f"step '{step_name}': {refusal}")

    return endpoint
