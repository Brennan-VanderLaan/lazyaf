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


#: THE one spelling of "this step's inputs are not just text". A step config
#: carries `attachments: [...]`; each entry is either a bare modality string
#: or an object carrying a `modality`, a `type`, or a `media_type`. The wire
#: shape of an attachment ITSELF - where the bytes live, how they are
#: versioned - is not settled here; this parser reads only what modality it
#: is, which is all dispatch needs in order to know whether the endpoint can
#: be asked for it.
STEP_ATTACHMENTS_KEY = "attachments"

#: Fields an attachment may declare its modality in, in precedence order.
#: `media_type` is here because `schemas.playground.PlaygroundAttachment`
#: carries a sniffed MIME type and NOT a modality tag - so once
#: `ATTACHMENTS_REACH_THE_MODEL` flips and those objects start reaching a step
#: config, a parser that only read `modality`/`type` would find nothing and
#: wave the request through with no modality check at all.
_MODALITY_TAG_FIELDS: tuple[str, ...] = ("modality", "type", "media_type")

#: Spellings that mean the same modality. `image_url` / `input_audio` are the
#: wire-format content-part names; `image` is what a human types.
_MODALITY_ALIASES: dict[str, str] = {
    "image": "images",
    "images": "images",
    "image_url": "images",
    "audio": "audio",
    "input_audio": "audio",
    "video": "video",
    "video_url": "video",
    "text": "text",
}

#: What an attachment that declares NOTHING this parser understands becomes.
#: Not a modality: a placeholder that `endpoint_modality_refusal` refuses by
#: name. Deliberately not in `WIRE_MODALITIES`.
UNTAGGED_ATTACHMENT = "untagged"


def _modality_of(item) -> str:
    """One attachment's modality, or `UNTAGGED_ATTACHMENT`."""
    tag = None
    if isinstance(item, str):
        tag = item
    elif isinstance(item, dict):
        for field in _MODALITY_TAG_FIELDS:
            candidate = item.get(field)
            if isinstance(candidate, str) and candidate.strip():
                tag = candidate
                break
    if not tag:
        return UNTAGGED_ATTACHMENT

    cleaned = tag.strip().lower()
    if "/" in cleaned:
        # A MIME type: `image/png` -> images, `audio/wav` -> audio.
        family = cleaned.split("/", 1)[0]
        return _MODALITY_ALIASES.get(family, family)
    return _MODALITY_ALIASES.get(cleaned, cleaned)


def step_modality_needs(step_config: dict | None) -> frozenset:
    """Which modalities this step's inputs require. THE only parser.

    Everything unrecognised SURVIVES rather than being dropped - an unknown
    tag is kept verbatim, and an attachment declaring no tag at all becomes
    `UNTAGGED_ATTACHMENT`. `endpoint_modality_refusal` then refuses on it by
    name. This looks pedantic and is the whole point: silently ignoring an
    attachment nobody taught this parser about would run the step with less
    input than its author attached and report SUCCESS, which is the same class
    of invisible downgrade as dropping an image - and harder to notice,
    because nothing fails.
    """
    if not isinstance(step_config, dict):
        return frozenset()
    attachments = step_config.get(STEP_ATTACHMENTS_KEY)
    if not isinstance(attachments, list):
        return frozenset()

    needs = {_modality_of(item) for item in attachments}
    # Text is the base content type of every request; it is never a "need".
    needs.discard("text")
    return frozenset(needs)


def endpoint_modality_refusal(endpoint: ModelEndpoint, *, needs: frozenset) -> str | None:
    """Why this endpoint must not receive THIS step's ATTACHMENTS, or None.

    Deliberately separate from `endpoint_dispatch_refusal`, and the asymmetry
    is load-bearing. That function's `supports_tools is None` clause is
    UNCONDITIONAL because every harness step has to pick a protocol - tools
    mode or the fenced-block fallback - so a null there makes every step
    incoherent. Modalities are not like that: almost every step attaches
    nothing. An unconditional refusal on `supports_images is None` would take
    EVERY endpoint registered before M14.6 offline the moment 0013 landed, for
    a capability those steps never use. That is a self-inflicted outage
    dressed up as rigour.

    So the refusal is conditional on the step actually attaching something -
    and once it does, `None` REFUSES. It does not fall back to text-only and
    it does not strip the attachment. Dropping an image the author attached
    and running anyway is worse than the tools case, not better: the step
    would SUCCEED, and report an answer produced from a prompt that silently
    lost half its input.
    """
    from app.models.model_endpoint import UNREPRESENTABLE_MODALITIES, WIRE_MODALITIES
    from app.services.model_endpoints.probe import modality_state

    # `text` is the base content type of every chat-completions request and
    # has no column to consult. Dropped here as well as in
    # `step_modality_needs` so that an explicit `needs` from the Playground
    # cannot produce a nonsense "no text observation" refusal.
    needs = frozenset(needs) - {"text"}
    if not needs:
        return None

    # Order the refusals so the STRUCTURAL ones (no wire format can carry
    # this; we cannot even classify it) come before the per-endpoint ones. A
    # video attachment must not be reported as "re-probe the endpoint".
    for name in sorted(needs):
        if name in UNREPRESENTABLE_MODALITIES:
            return (
                f"step attaches {name}, and LazyAF cannot send {name} to ANY "
                f"endpoint: the OpenAI chat-completions wire format has no "
                f"{name} content part (it defines text, image_url, "
                f"input_audio and file). This is a property of the protocol, "
                f"not of endpoint '{endpoint.name}' - re-probing will not "
                f"change it. Sample the {name} into frames and attach those "
                f"as images if that is what you meant."
            )
        if name == UNTAGGED_ATTACHMENT:
            return (
                f"step attaches a file that declares no modality (no "
                f"`modality`, `type` or `media_type`), so LazyAF cannot tell "
                f"whether endpoint '{endpoint.name}' is able to receive it. "
                f"Refused rather than passed through: an attachment this "
                f"resolver cannot classify is one it cannot check, and a step "
                f"that quietly dropped it would still report success"
            )
        if name not in WIRE_MODALITIES:
            return (
                f"step attaches an unrecognised modality '{name}'. LazyAF "
                f"speaks {', '.join(WIRE_MODALITIES)} over this wire format "
                f"and refuses rather than dropping an attachment it does not "
                f"understand"
            )

    detail = endpoint.get_probe_detail()
    for name in sorted(needs):
        value = getattr(endpoint, f"supports_{name}", None)
        reason = detail.get(f"{name}_reason")
        caveat = detail.get(f"{name}_caveat")
        state = modality_state(
            value,
            reason if isinstance(reason, str) else None,
            caveat if isinstance(caveat, str) else None,
        )
        # An UNVERIFIED acceptance still dispatches. Refusing it would block a
        # capability that probably works, on the strength of a control that
        # merely failed to corroborate - and the honest place to surface the
        # doubt is the UI, where a human is choosing, not a refusal that reads
        # as "this endpoint cannot do images" when we do not know that.
        if state in ("supported", "supported_unverified"):
            continue

        if state == "unsupported":
            body = detail.get(f"{name}_body")
            quoted = f" ({reason}: {body})" if body else f" ({reason})"
            return (
                f"endpoint '{endpoint.name}' was probed and REFUSED {name} "
                f"content parts{quoted}. Remove the attachment, or pick an "
                f"endpoint whose {name} capability is green"
            )
        if state == "undetectable":
            return (
                f"endpoint '{endpoint.name}' accepted an {name} content part "
                f"at probe time but the attachment never reached the prompt "
                f"(prompt_tokens did not move against the control request), "
                f"so it was silently discarded. LazyAF refuses rather than "
                f"letting this step SUCCEED on a prompt that quietly lost its "
                f"{name}. Verify by hand, or pick another endpoint"
            )
        if state == "probe_failed":
            return (
                f"endpoint '{endpoint.name}' has no {name} observation: the "
                f"probe itself failed ({reason}). This is NOT a 'no' - read "
                f"the probe detail before re-probing. LazyAF will not send "
                f"{name} to an endpoint that has not demonstrated it accepts "
                f"it, and will not silently drop it"
            )
        return (
            f"endpoint '{endpoint.name}' has no {name} observation "
            f"(supports_{name} is null - it was registered before LazyAF knew "
            f"how to ask); re-probe it with "
            f"POST /api/model-endpoints/{endpoint.id}/probe. LazyAF will not "
            f"send {name} to an endpoint that has not demonstrated it accepts "
            f"one, and will not silently drop it"
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
    db, step_config: dict | None, step_name: str, *, needs: frozenset | None = None
) -> ModelEndpoint:
    """The ONE resolver. Raises `ValueError` with an actionable message.

    Every refusal names the endpoints that DO exist, because "unknown
    endpoint 'local-4090'" without that list is one round trip short of
    useful when the actual problem is a typo.

    `needs` DEFAULTS TO DERIVING ITSELF from `step_config`, and that default
    is deliberate: `needs=frozenset()` would have meant every caller that
    forgot to pass it silently skipped the modality check, which is a
    no-check dressed as a default. Pass it explicitly only for the surfaces
    whose attachments do not live in `step_config` (the Playground's ad-hoc
    run) - and even then it is `step_modality_needs`'s vocabulary, so there
    is still exactly one parser.
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

    modality_refusal = endpoint_modality_refusal(
        endpoint,
        needs=step_modality_needs(step_config) if needs is None else needs,
    )
    if modality_refusal:
        raise ValueError(f"step '{step_name}': {modality_refusal}")

    return endpoint
