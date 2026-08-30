"""Endpoint health from REAL step outcomes (wave8 s5.4).

A probe is one observation taken at one moment from one network position. The
work itself is a much better instrument, and this module is where it gets to
correct the record.

The demotion is the teeth behind "a probe that lies": an endpoint that passes
the tool probe and then never actually emits `tool_calls` in real work gets its
capability record corrected BY THE WORK, visibly, within two steps - instead of
every step burning its whole budget on prose while the record keeps saying the
endpoint is fine.

Called from `usage_ingestion` inside a try/except that logs and swallows: the
never-fail-a-step rule reaches here too, and a health update is not worth a 500
on a telemetry POST.
"""
import logging
from datetime import datetime

from sqlalchemy import select

from app.models.model_endpoint import ModelEndpoint

logger = logging.getLogger(__name__)

#: Consecutive drifting steps before `supports_tools` is demoted. Two, not
#: one: a single prose-only step is a bad prompt as often as it is a lying
#: server, and demoting on it would flip a working endpoint into the fallback
#: protocol for the rest of its life.
PROBE_DRIFT_DEMOTION_THRESHOLD = 2

#: Where the consecutive-drift counter lives. `probe_detail` rather than a
#: column: it is probe metadata, it is already scrubbed and capped, and a
#: column would be a schema change for a counter that resets constantly.
DRIFT_KEY = "consecutive_probe_drift"


async def record_step_outcome(db, endpoint_id: str, raw_harness: dict) -> None:
    """Fold one step's harness record into the endpoint's health.

    - no endpoint HTTP errors -> `last_success_at = now`,
      `consecutive_failures = 0`. A healthy endpoint therefore never drifts
      into the stale-and-failing state through disuse of the probe button.
    - `stop_reason == "endpoint"` -> `consecutive_failures += 1`, `last_error`
      set from the harness's own scrubbed reason.
    - `probe_drift` on two consecutive steps of an endpoint whose stored
      `supports_tools` is True -> demote to False, `probe_status = "degraded"`,
      and say why in `probe_detail.demoted_reason`.

    A MODEL-capability failure is deliberately NOT an endpoint failure:
    unparseable output (`stop_reason == "unparseable"`) leaves
    `consecutive_failures` alone, because conflating the two would make a
    perfectly working endpoint look down.
    """
    if not endpoint_id or not isinstance(raw_harness, dict):
        return

    result = await db.execute(
        select(ModelEndpoint).where(ModelEndpoint.id == endpoint_id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        logger.info(
            "usage record names model endpoint %s which no longer exists; "
            "health update skipped (the usage row keeps its gpu_node_id and "
            "stays priceable)",
            endpoint_id,
        )
        return

    now = datetime.utcnow()
    http_errors = raw_harness.get("endpoint_http_errors")
    stop_reason = raw_harness.get("stop_reason")
    detail = endpoint.get_probe_detail()

    if stop_reason == "endpoint":
        endpoint.consecutive_failures = int(endpoint.consecutive_failures or 0) + 1
        reason = raw_harness.get("stop_error") or "harness stopped: endpoint fatal"
        from app.services.model_endpoints.probe import LAST_ERROR_MAX_CHARS
        from app.services.model_endpoints.secrets import scrub_secrets

        endpoint.last_error = scrub_secrets(str(reason))[:LAST_ERROR_MAX_CHARS]
    elif isinstance(http_errors, int) and http_errors == 0:
        endpoint.last_success_at = now
        endpoint.consecutive_failures = 0
        endpoint.last_error = None

    # -- the demotion ---------------------------------------------------------
    if raw_harness.get("probe_drift"):
        drift = int(detail.get(DRIFT_KEY) or 0) + 1
        detail[DRIFT_KEY] = drift
        if drift >= PROBE_DRIFT_DEMOTION_THRESHOLD and endpoint.supports_tools:
            endpoint.supports_tools = False
            endpoint.probe_status = "degraded"
            detail["demoted_reason"] = "tools advertised but never emitted"
            detail["demoted_at"] = now.isoformat()
            logger.warning(
                "endpoint %s demoted to supports_tools=False after %s "
                "consecutive drifting steps: the probe passed but real work "
                "never produced tool_calls",
                endpoint.name,
                drift,
            )
    elif detail.get(DRIFT_KEY):
        detail[DRIFT_KEY] = 0

    endpoint.set_probe_detail(detail)
    await db.commit()
