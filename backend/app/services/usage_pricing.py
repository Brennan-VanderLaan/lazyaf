"""
The gpu-node cost model (Phase 12.5) — api-surface 2.5, verbatim.

Self-hosted nodes have no per-token bill, so dollars come from node rate x
occupancy. **The server prices it, not the runtime**, so the rate table lives
in one place and history can be re-priced when a rate is corrected.

Rates are configuration, addressed by node id, and read from
`settings.gpu_node_rates` (env `LAZYAF_GPU_NODE_RATES`, JSON, default `{}`):

    {"runpod-a100-80g": {"rate_usd_hour": "1.89", "currency": "USD",
                         "note": "on-demand list price 2026-08"},
     "local-4090":      {"rate_usd_hour": "0.00",
                         "note": "owned hardware; electricity not modelled"}}

A `rate_usd_hour` of "0.00" on owned hardware is honest, not a bug: the
write-up states that self-hosted trials are priced at marginal cash cost and
that the comparison to API pricing is therefore favourable to self-hosting.
Disclosed, not hidden.

There is deliberately NO token-price table (owner decision 2026-08-29): while
the CLIs report cost, a second pricing table is a second source of truth that
will drift. `cost_source="estimated"` stays in the vocabulary for a future
price-table backfill and is written by nothing today.

Nothing in 12.5 sets `LAZYAF_GPU_NODE_ID`, so this module's branch is reached
only by API tests with a hand-built manifest — real code on a real path
(R4: no `pass # architecture ensures this`). 12.6 puts real nodes on it.
"""
import logging
from decimal import Decimal, InvalidOperation

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Money resolution everywhere in the usage channel (NUMERIC(18,6)).
CENTS = Decimal("0.000001")


def gpu_node_cost_usd(
    node_rate_usd_hour: Decimal,
    container_seconds: float,
    gpu_fraction: float = 1.0,
) -> Decimal:
    """Occupancy pricing: you rent the node, not the tokens.

    cost = rate_per_hour * (container_seconds / 3600) * gpu_fraction

    container_seconds is WALL time the container held the node, including
    model load and idle-in-step time, because that is what the node bills
    for. gpu_fraction < 1.0 only when the node is deliberately shared
    (MIG slice, multi-tenant vLLM); default 1.0 = exclusive.
    """
    return (
        node_rate_usd_hour
        * Decimal(str(container_seconds))
        / Decimal(3600)
        * Decimal(str(gpu_fraction))
    ).quantize(CENTS)


def node_rate_usd_hour(node_id: str | None) -> Decimal | None:
    """The configured hourly rate for `node_id`, or None if unpriced.

    None means "we do not know what this node costs" and drives
    `cost_source="unknown"` — never a guessed rate. A malformed entry is
    logged and treated as unpriced: a pricing typo must not 500 a telemetry
    POST (the never-fail-a-step rule reaches all the way back here).
    """
    if not node_id:
        return None

    entry = (get_settings().gpu_node_rates or {}).get(node_id)
    if entry is None:
        return None

    # Accept both {"rate_usd_hour": "1.89"} and a bare "1.89" — the shape in
    # api-surface 2.5 is the dict; the scalar is tolerated rather than
    # silently unpriced, because a config typo should still bill something
    # visible rather than vanish into "unknown".
    raw = entry.get("rate_usd_hour") if isinstance(entry, dict) else entry

    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning(
            "Unparseable gpu node rate %r for node %r (LAZYAF_GPU_NODE_RATES) "
            "— pricing this node as unknown",
            raw,
            node_id,
        )
        return None

    if rate < 0:
        logger.warning(
            "Negative gpu node rate %s for node %r — pricing this node as unknown",
            rate,
            node_id,
        )
        return None

    return rate
