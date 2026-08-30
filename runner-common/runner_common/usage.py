"""
Usage telemetry: scrape what the agent CLI reported, write the manifest.

Phase 12.5, protocol channel #4 (cross-agent contract #2). The wrapper writes
``/workspace/.control/usage.<step_execution_id>.json``; ``/control/run.py``
ships it to ``POST /api/steps/{id}/usage`` and deletes it on every outcome —
the SAME sidecar-manifest pattern 12.2.6 established for test results.

NON-NEGOTIABLE RULE, threaded through every function here:
**telemetry never fails a step.** Every public function in this module
swallows its own exceptions. A missing number is a recorded
``cost_source="unknown"`` — the fact that the provider told us nothing — not
a gap, and never a red step.

SCRAPE FAILURE IS NOT "UNKNOWN" (12.5 review finding F3.1). Two very
different facts used to land on the same ``cost_source="unknown"`` row:

- the provider genuinely reported no dollars (gemini today: it prints
  tokens and no price), and
- the scraper could not FIND the CLI's result object at all — i.e. the
  vendor changed its output and every future step of that agent is now
  recorded as costing nothing.

The second one silently destroys M13's cost axis, so the scrapers now say
which one happened. ``scrape_failure_reason()`` reads it; ``build_manifest``
stamps a DURABLE ``{"_scrape_failed": true, "_scrape_error": ...}`` marker
into the manifest's ``raw`` object (a free-form dict on the wire, so it
survives run.py and the server verbatim and lands in the database); the
wrapper logs ``SCRAPE_FAILED_LOG_MARKER`` onto the step's log stream; and
``scripts/verify_executor.py`` FAILS THE PUSH on either signal. The step
itself is still untouched — loud, never red.

OWNERSHIP SPLIT (R3: one writer per datum).
- HERE (the wrapper side): ``provider``, ``model``, ``model_version``, every
  token count, ``cost_usd``, ``cost_source``, ``determinism``, ``raw``.
- ``run.py``: ``wall_clock_ms`` (it overwrites the value written here),
  ``container_seconds``, ``role`` / ``gpu_node_id`` / ``gpu_fraction`` from
  container env. ``wall_clock_ms`` is written here anyway so a manifest is
  self-describing when read by hand.
- The server: ``step_run_id``, ``pipeline_run_id``, ``cost_usd`` when
  ``cost_source == "gpu-node"``, the ``role`` fallback.

WIRE SHAPE is owned by ``backend/app/schemas/usage.py`` (``UsageManifest``,
api-surface 2.2) and pinned for both sides by
``tdd/unit/control_runtime/usage_contract.py``. Money is a STRING on the wire
— never a float, ever.

Stdlib only.
"""
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

#: The ONE manifest version (Literal[1] server-side: anything else is a 422).
USAGE_VERSION = 1

#: Env var carrying the per-step manifest path. PLATFORM-OWNED: injected by
#: run.py into config.environment at exec time, exactly like
#: LAZYAF_TEST_RESULTS_PATH, so a step can never point the platform at
#: another step's manifest.
USAGE_PATH_ENV = "LAZYAF_USAGE_PATH"

#: agent vocabulary (contract #5) -> UsageManifest.provider vocabulary.
PROVIDER_BY_AGENT = {
    "claude-code": "anthropic",
    "gemini": "google",
    "mock": "self-hosted",
}

#: Fallback when the agent is unknown to the map (never a hard failure).
DEFAULT_PROVIDER = "self-hosted"

#: ``raw`` is capped at 8 KiB server-side; cap it here too so a chatty CLI
#: cannot push a megabyte of blob through the log-budget POST.
RAW_MAX_BYTES = 8192

# --------------------------------------------------------------------------
# scrape-failure signalling (F3.1)
# --------------------------------------------------------------------------

#: Keys a SCRAPER adds to its own result dict to say whether it found the
#: CLI's report. INTERNAL to the wrapper side: ``build_manifest`` reads them
#: by name and they never appear on the wire under these names (a manifest
#: key run.py does not own is dropped with a warning).
SCRAPE_OK_KEY = "scrape_ok"
SCRAPE_ERROR_KEY = "scrape_error"

#: The DURABLE marker, written inside the manifest's ``raw`` object. ``raw``
#: is a free-form dict on the pinned wire (api-surface 2.2), so this marker
#: needs no change to the manifest schema, reaches the database through
#: run.py untouched, and is readable from ``GET /api/steps/{id}/usage``.
RAW_SCRAPE_FAILED = "_scrape_failed"
RAW_SCRAPE_ERROR = "_scrape_error"

#: Longest reason string carried in the marker (the marker must never be the
#: reason ``raw`` gets truncated).
SCRAPE_ERROR_MAX_CHARS = 256

#: Stable log marker the wrapper prints onto the step's stdout — the step log
#: stream — and that ``scripts/verify_executor.py`` greps for. Same shape as
#: the 12.2.6 ``[control] WARNING: test results manifest`` marker that the
#: ratchet already watches.
SCRAPE_FAILED_LOG_MARKER = "[agent] WARNING: usage scrape failed"

#: The cost_source a scrape failure DESERVES. It is NOT emitted yet: the
#: vocabulary is pinned on two sides this module does not own
#: (``backend/app/schemas/usage.py`` ``CostSource`` and run.py's
#: ``USAGE_COST_SOURCES``), and emitting an out-of-vocabulary value would be
#: nulled back to "unknown" in transit. Named here so the follow-up that
#: widens both sides has exactly one place to point at; until then the
#: ``raw`` marker above carries the fact.
COST_SOURCE_SCRAPE_FAILED = "scrape-failed"


def scrape_failure_reason(usage: Any) -> Optional[str]:
    """Why a scrape FAILED, or None when it did not.

    Only a SCRAPER marks failure. A hand-written usage block, the mock
    agent's synthesized usage, and ``usage=None`` (the executor raised, or
    the watchdog killed us) all answer None: those are legitimately
    "nothing was reported", not "we could not read what was reported".
    """
    if not isinstance(usage, dict):
        return None
    if usage.get(SCRAPE_OK_KEY, True):
        return None
    reason = usage.get(SCRAPE_ERROR_KEY)
    return str(reason) if reason else "the CLI reported no usage"


# --------------------------------------------------------------------------
# scrapers
# --------------------------------------------------------------------------

def _int_or_none(value: Any) -> Optional[int]:
    """Coerce a CLI-reported token count, or None. Bools are NOT ints here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.replace(",", "").strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _money_or_none(value: Any) -> Optional[str]:
    """Coerce a CLI-reported dollar amount to a WIRE STRING, or None.

    Decimal(str(value)) even for floats: the CLI hands us a float, and the
    string form of that float is the most faithful decimal rendering of what
    it meant. The server quantizes to 6dp.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount < 0:
        return None
    return str(amount)


def _cap_raw(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a dict small enough to ship, or a truncation marker.

    Never raises and never returns a non-dict (``raw`` is ``dict | None`` on
    the wire): an unserializable blob becomes a marker, not a 422.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raw = {"value": repr(raw)[:RAW_MAX_BYTES]}
    try:
        encoded = json.dumps(raw)
    except (TypeError, ValueError):
        return {"_truncated": True, "_reason": "not JSON-serializable"}
    if len(encoded.encode("utf-8")) <= RAW_MAX_BYTES:
        return raw
    # Keep the scalars (the numbers a dispute would be re-derived from) and
    # drop the prose.
    trimmed = {
        key: value
        for key, value in raw.items()
        if isinstance(value, (int, float, bool, type(None)))
    }
    trimmed["_truncated"] = True
    try:
        if len(json.dumps(trimmed).encode("utf-8")) > RAW_MAX_BYTES:
            return {"_truncated": True, "_reason": "oversized"}
    except (TypeError, ValueError):
        return {"_truncated": True, "_reason": "oversized"}
    return trimmed


def _json_objects(text: str):
    """Yield every JSON object found in ``text``, last line FIRST.

    Handles both claude output formats with one scanner:
    - ``--output-format stream-json --verbose`` emits newline-delimited
      events whose LAST event is the result object,
    - ``--output-format json`` emits one object spanning the whole stream.
    """
    if not text:
        return
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            whole = json.loads(stripped)
        except ValueError:
            pass
        else:
            if isinstance(whole, dict):
                yield whole
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def scrape_claude_usage(
    stdout: str,
    stderr: str = "",
    fallback_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull tokens + dollars out of the Claude CLI's own result object.

    The result event (``type == "result"``, or any object carrying
    ``total_cost_usd``) is identical between ``stream-json`` and ``json``, so
    one scraper serves both — which is what makes the deliberate deviation
    from api-surface 2.3 (``stream-json --verbose`` instead of ``json``, so a
    20-minute agent step is not dark in the UI) contract-preserving.

    Returns a partial manifest dict; ``cost_source`` is ``"unknown"`` with
    null tokens when no result object was found — AND that case is flagged as
    a SCRAPE FAILURE (``scrape_ok=False``), because the claude CLI always
    emits a result object. Missing one means its output format changed, which
    is a vendor regression that must be loud rather than a free step (F3.1).
    """
    result: Dict[str, Any] = {
        "provider": "anthropic",
        "model": fallback_model,
        "model_version": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
        "cost_source": "unknown",
        "raw": None,
        SCRAPE_OK_KEY: False,
        SCRAPE_ERROR_KEY: (
            "no result object in the claude CLI output — the CLI emits one on "
            "every run, so its --output-format or result schema changed"
        ),
    }
    try:
        event = None
        for candidate in _json_objects(stdout or ""):
            if candidate.get("type") == "result" or "total_cost_usd" in candidate:
                event = candidate
                break
        if event is None:
            return result

        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        model = event.get("model")
        if isinstance(model, str) and model:
            result["model"] = model
            result["model_version"] = model

        result["input_tokens"] = _int_or_none(usage.get("input_tokens"))
        result["output_tokens"] = _int_or_none(usage.get("output_tokens"))
        result["cache_read_tokens"] = _int_or_none(
            usage.get("cache_read_input_tokens")
        )
        result["cache_write_tokens"] = _int_or_none(
            usage.get("cache_creation_input_tokens")
        )
        result["cost_usd"] = _money_or_none(event.get("total_cost_usd"))
        result["cost_source"] = "cli-reported"
        result["raw"] = _cap_raw(event)
        result[SCRAPE_OK_KEY] = True
        result[SCRAPE_ERROR_KEY] = None
    except Exception as exc:  # a scraper crash is a telemetry miss, nothing more
        result[SCRAPE_OK_KEY] = False
        result[SCRAPE_ERROR_KEY] = f"claude usage scrape raised {exc!r}"
        print(f"{SCRAPE_FAILED_LOG_MARKER}: {exc!r}", file=sys.stderr)
    return result


#: Tolerant token patterns for the Gemini CLI's usage summary. SPECULATIVE
#: until a real run is captured — safe by construction: a miss costs one
#: cost_source="unknown" row, never a red step. First real gemini run:
#: capture stdout to tests/fixtures/gemini_usage_*.txt and tighten these in
#: the same commit.
_GEMINI_PATTERNS = {
    "input_tokens": re.compile(
        r"(?:input|prompt)[\s_-]*tokens?\s*[:=]?\s*([\d,]+)", re.IGNORECASE
    ),
    "output_tokens": re.compile(
        r"(?:output|completion|candidates?)[\s_-]*tokens?\s*[:=]?\s*([\d,]+)",
        re.IGNORECASE,
    ),
    "total_tokens": re.compile(
        r"total[\s_-]*tokens?\s*[:=]?\s*([\d,]+)", re.IGNORECASE
    ),
}


def scrape_gemini_usage(
    stdout: str,
    stderr: str = "",
    fallback_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull token counts out of the Gemini CLI's usage summary.

    Dollars are not reported by the CLI, so ``cost_usd`` stays null and
    ``cost_source`` stays ``"unknown"`` EVEN WHEN tokens are found: a token
    count is not a price, and pretending otherwise is how a board reports a
    quietly-too-cheap median.

    Finding NO tokens at all is a different fact and is flagged as a SCRAPE
    FAILURE (F3.1): these patterns are speculative until a real run is
    captured, so the day the CLI's summary wording changes must be the day
    the dogfood gate goes red — not the day gemini steps quietly become free.
    """
    result: Dict[str, Any] = {
        "provider": "google",
        "model": fallback_model,
        "model_version": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
        "cost_source": "unknown",
        "raw": None,
        SCRAPE_OK_KEY: False,
        SCRAPE_ERROR_KEY: (
            "no token counts in the gemini CLI output — its usage summary "
            "wording changed, or the CLI stopped printing one"
        ),
    }
    try:
        haystack = f"{stdout or ''}\n{stderr or ''}"
        found: Dict[str, int] = {}
        for key, pattern in _GEMINI_PATTERNS.items():
            matches = pattern.findall(haystack)
            if not matches:
                continue
            value = _int_or_none(matches[-1])
            if value is not None:
                found[key] = value

        result["input_tokens"] = found.get("input_tokens")
        result["output_tokens"] = found.get("output_tokens")
        if (
            result["output_tokens"] is None
            and "total_tokens" in found
            and result["input_tokens"] is not None
        ):
            derived = found["total_tokens"] - result["input_tokens"]
            result["output_tokens"] = derived if derived >= 0 else None

        if found:
            result["raw"] = _cap_raw(dict(found))
            result[SCRAPE_OK_KEY] = True
            result[SCRAPE_ERROR_KEY] = None
    except Exception as exc:
        result[SCRAPE_OK_KEY] = False
        result[SCRAPE_ERROR_KEY] = f"gemini usage scrape raised {exc!r}"
        print(f"{SCRAPE_FAILED_LOG_MARKER}: {exc!r}", file=sys.stderr)
    return result


# --------------------------------------------------------------------------
# manifest assembly + write
# --------------------------------------------------------------------------

def build_manifest(
    agent: str,
    usage: Optional[Dict[str, Any]] = None,
    *,
    wall_clock_ms: int = 0,
    role: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble a complete ``UsageManifest`` payload.

    Every key of the wire shape is present, nulls included: run.py overwrites
    the fields it owns, and the server must never have to distinguish "absent"
    from "unknown". ``usage=None`` (the executor raised, or reported nothing)
    yields the honest ``cost_source="unknown"`` record.

    When the usage block records a SCRAPE FAILURE (F3.1), the returned
    manifest's ``raw`` carries ``_scrape_failed``/``_scrape_error`` — always,
    even if that means dropping the rest of an oversized blob to fit. That
    marker is the one thing a later reader must not lose: without it a vendor
    output change is indistinguishable from a genuinely free step.
    """
    usage = usage if isinstance(usage, dict) else {}
    provider = usage.get("provider") or PROVIDER_BY_AGENT.get(agent, DEFAULT_PROVIDER)
    cost_source = usage.get("cost_source") or "unknown"
    manifest: Dict[str, Any] = {
        "version": USAGE_VERSION,
        "provider": provider,
        "model": usage.get("model") or model,
        "model_version": usage.get("model_version"),
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cache_read_tokens": _int_or_none(usage.get("cache_read_tokens")),
        "cache_write_tokens": _int_or_none(usage.get("cache_write_tokens")),
        "cost_usd": _money_or_none(usage.get("cost_usd")),
        "cost_source": cost_source,
        # run.py owns timing and node attribution; these are the wrapper's
        # best knowledge, overwritten by the one component present for
        # script steps too.
        "wall_clock_ms": max(int(wall_clock_ms or 0), 0),
        "container_seconds": None,
        "gpu_node_id": None,
        "gpu_fraction": None,
        # Nothing any of the three CLIs exposes lets us report temperature /
        # seed / top_p, so determinism is an honest empty object rather than
        # invented defaults.
        "determinism": usage.get("determinism") or {},
        "role": role,
        "raw": _mark_scrape_failure(_cap_raw(usage.get("raw")), usage),
    }
    return manifest


def _mark_scrape_failure(
    raw: Optional[Dict[str, Any]], usage: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Stamp the durable scrape-failure marker into ``raw``. Never raises.

    Returns ``raw`` unchanged when the scrape did not fail. When it did, the
    marker is added AFTER the cap and the result is re-measured: if the
    combined object no longer fits, the blob is discarded and the marker is
    kept, because the marker is the load-bearing half.
    """
    reason = scrape_failure_reason(usage)
    if reason is None:
        return raw
    marker = {
        RAW_SCRAPE_FAILED: True,
        RAW_SCRAPE_ERROR: reason[:SCRAPE_ERROR_MAX_CHARS],
    }
    merged: Dict[str, Any] = dict(raw or {})
    merged.update(marker)
    try:
        if len(json.dumps(merged).encode("utf-8")) <= RAW_MAX_BYTES:
            return merged
    except (TypeError, ValueError):
        pass
    marker["_truncated"] = True
    return marker


def write_usage_manifest(
    path: Optional[str],
    agent: str,
    usage: Optional[Dict[str, Any]] = None,
    *,
    wall_clock_ms: int = 0,
    role: Optional[str] = None,
    model: Optional[str] = None,
) -> bool:
    """Write the usage manifest to ``path``. Returns True when written.

    HARD RULE: this function NEVER raises. It runs in the wrapper's
    ``finally`` and in its SIGTERM handler; an exception here would turn a
    telemetry miss into a lost step outcome.

    ``path`` of ``None``/empty is a no-op (the step is not in control mode,
    or run.py chose not to collect usage) — not an error.

    The write is ATOMIC (temp file + ``os.replace`` in the destination
    directory), so run.py can never ship a half-written manifest if the
    watchdog kills the wrapper mid-write.
    """
    if not path:
        return False
    try:
        manifest = build_manifest(
            agent,
            usage,
            wall_clock_ms=wall_clock_ms,
            role=role,
            model=model,
        )
        payload = json.dumps(manifest)
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=directory, prefix=".lazyaf-usage-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        print(
            f"[agent] WARNING: could not write usage manifest to {path}: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return False
