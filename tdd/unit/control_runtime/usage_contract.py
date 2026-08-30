"""THE usage-manifest wire contract (Phase 12.5, cross-agent #2 and #3).

ONE module, imported by BOTH sides of the wire, so a drift on either side
fails a test that names the side that drifted:

- PRODUCER  = runner_common.agent_wrapper (writes
              /workspace/.control/usage.<step_execution_id>.json)
- SHIPPER   = images/base/control/run.py  (overwrites the timing fields,
              adds the role/gpu env fields, POSTs it — and posts the
              FALLBACK record when no manifest exists at all)
- SERVER    = backend POST /api/steps/{id}/usage
              (backend/app/schemas/usage.py is the source of truth)

The pinned shape is EXACTLY the api-surface 2.2 `UsageManifest`::

    {"version": 1,
     "provider": "anthropic"|"google"|"openai-compatible"|"self-hosted",
     "model": str|None, "model_version": str|None,
     "input_tokens": int|None, "output_tokens": int|None,
     "cache_read_tokens": int|None, "cache_write_tokens": int|None,
     "cost_usd": str|None,          # dollars are STRINGS, never floats
     "cost_source": "cli-reported"|"gpu-node"|"estimated"|"unknown",
     "wall_clock_ms": int,
     "container_seconds": float|None,
     "gpu_node_id": str|None, "gpu_fraction": float|None,
     "determinism": dict, "role": str|None, "raw": dict|None}

`version` and `provider`/`cost_source` are the only hard vocabularies;
everything else is nullable BY DESIGN, because the never-fail-a-step rule
(api-surface 2.4) means a step whose CLI reported nothing must still produce
a valid manifest: `cost_source="unknown"` with null tokens and null dollars
is a RECORDED FACT, not a gap.

`trial_iteration_id` is deliberately absent in 12.5 and MUST NOT appear:
nothing writes it and there is no table to reference. `role` IS present
(null in 12.5) because it is unrecoverable after the fact.

Strictness note: this validator is deliberately STRICTER than the server's
pydantic model on the producer side. Pydantic in lax mode coerces `"184220"`
to `184220` and ignores unknown keys, which is the right behaviour for an
endpoint that must not fail a step over a sloppy field — but a PRODUCER that
emits a stringly-typed token count or an out-of-phase key has drifted, and
this module is where that is caught. `INVALID_MANIFESTS` therefore names
everything no PRODUCER may emit; the subset the SERVER must answer 422 to is
listed separately in the endpoint's own tests.

This module is NOT a test module (it must not match ``python_files``); it is
plain importable code::

    from tdd.unit.control_runtime.usage_contract import (
        CANONICAL_MANIFEST, assert_manifest_conforms,
    )
"""

USAGE_VERSION = 1

#: Exactly the allowed top-level keys.
TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "provider",
        "model",
        "model_version",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "cost_source",
        "wall_clock_ms",
        "container_seconds",
        "gpu_node_id",
        "gpu_fraction",
        "determinism",
        "role",
        "raw",
    }
)

#: The only keys a producer may omit (the server defaults them). `version`,
#: `provider`, `cost_source` and `wall_clock_ms` are REQUIRED — a manifest
#: without them is not a usage record, it is noise.
REQUIRED_KEYS = frozenset({"version", "provider", "cost_source", "wall_clock_ms"})

#: Keys that must NEVER appear on the 12.5 wire (design section 3.6).
FORBIDDEN_KEYS = frozenset({"trial_iteration_id", "trial_id", "cost", "tokens"})

PROVIDERS = frozenset({"anthropic", "google", "openai-compatible", "self-hosted"})

COST_SOURCES = frozenset({"cli-reported", "gpu-node", "estimated", "unknown"})

#: Integer-or-null token fields.
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)

#: A manifest that every side MUST accept, byte-shape included: the
#: claude-code happy path.
CANONICAL_MANIFEST = {
    "version": 1,
    "provider": "anthropic",
    "model": "claude-haiku-4-5",
    "model_version": "claude-haiku-4-5-20260210",
    "input_tokens": 18422,
    "output_tokens": 3110,
    "cache_read_tokens": 240110,
    "cache_write_tokens": 12004,
    "cost_usd": "0.1841",
    "cost_source": "cli-reported",
    "wall_clock_ms": 184220,
    "container_seconds": 191.4,
    "gpu_node_id": None,
    "gpu_fraction": None,
    "determinism": {"temperature": 0.0, "seed": None, "top_p": None},
    "role": None,
    "raw": {"total_cost_usd": 0.1841, "usage": {"input_tokens": 18422}},
}

#: The manifest a step whose CLI reported NOTHING must still produce — the
#: never-fail-a-step record. Every side must accept it (design section 3.4).
FALLBACK_MANIFEST = {
    "version": 1,
    "provider": "self-hosted",
    "cost_usd": None,
    "cost_source": "unknown",
    "wall_clock_ms": 1204,
    "container_seconds": 2.1,
}


def manifest_violations(manifest) -> list:
    """Return a list of human-readable contract violations (empty == valid)."""
    problems = []
    if not isinstance(manifest, dict):
        return [f"manifest is {type(manifest).__name__}, expected dict"]

    keys = set(manifest)

    unknown = keys - set(TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"unknown top-level keys {sorted(unknown)}")

    forbidden = keys & set(FORBIDDEN_KEYS)
    if forbidden:
        problems.append(
            f"keys {sorted(forbidden)} are NOT on the 12.5 wire "
            "(trial_iteration_id lands with M13's trials table)"
        )

    missing = set(REQUIRED_KEYS) - keys
    if missing:
        problems.append(f"missing required keys {sorted(missing)}")

    if manifest.get("version") != USAGE_VERSION:
        problems.append(f"version {manifest.get('version')!r} != {USAGE_VERSION}")

    provider = manifest.get("provider")
    if provider is not None and provider not in PROVIDERS:
        problems.append(f"provider {provider!r} not in {sorted(PROVIDERS)}")

    cost_source = manifest.get("cost_source")
    if cost_source is not None and cost_source not in COST_SOURCES:
        problems.append(f"cost_source {cost_source!r} not in {sorted(COST_SOURCES)}")

    wall_clock = manifest.get("wall_clock_ms")
    if not isinstance(wall_clock, int) or isinstance(wall_clock, bool):
        problems.append(f"wall_clock_ms {wall_clock!r} is not an int")

    for key in TOKEN_KEYS:
        value = manifest.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            problems.append(f"{key} {value!r} is not int|None")

    cost_usd = manifest.get("cost_usd")
    if isinstance(cost_usd, float):
        problems.append(
            f"cost_usd {cost_usd!r} is a float — dollars travel as STRINGS "
            "(api-surface 0: no floats for money, ever)"
        )
    elif cost_usd is not None and not isinstance(cost_usd, (str, int)):
        problems.append(f"cost_usd {cost_usd!r} is not str|None")

    container_seconds = manifest.get("container_seconds")
    if container_seconds is not None and not isinstance(
        container_seconds, (int, float)
    ):
        problems.append(f"container_seconds {container_seconds!r} is not a number|None")

    gpu_fraction = manifest.get("gpu_fraction")
    if gpu_fraction is not None and not isinstance(gpu_fraction, (int, float)):
        problems.append(f"gpu_fraction {gpu_fraction!r} is not a number|None")

    for key in ("model", "model_version", "gpu_node_id", "role"):
        value = manifest.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{key} {value!r} is not str|None")

    determinism = manifest.get("determinism")
    if determinism is not None and not isinstance(determinism, dict):
        problems.append(f"determinism {determinism!r} is not a dict")

    raw = manifest.get("raw")
    if raw is not None and not isinstance(raw, dict):
        problems.append(f"raw {raw!r} is not dict|None")

    return problems


def assert_manifest_conforms(manifest, side: str) -> None:
    """Assert ``manifest`` matches the wire contract.

    ``side`` names WHO produced this value ("PRODUCER (agent wrapper)",
    "SHIPPER (control runtime)", "SERVER (usage router)") so a failure
    message says which side drifted from the shared contract.
    """
    problems = manifest_violations(manifest)
    assert not problems, (
        f"{side} DRIFTED from the 12.5 usage contract "
        f"(tdd/unit/control_runtime/usage_contract.py):\n  - "
        + "\n  - ".join(problems)
        + f"\noffending value: {manifest!r}"
    )


#: Values NO side may accept as a valid manifest. Each is (label, value).
INVALID_MANIFESTS = [
    ("bare list", [{"version": 1}]),
    ("bare string", "not a manifest"),
    ("null", None),
    ("version missing", {k: v for k, v in CANONICAL_MANIFEST.items() if k != "version"}),
    ("version 2", {**CANONICAL_MANIFEST, "version": 2}),
    ("version is a string", {**CANONICAL_MANIFEST, "version": "1"}),
    ("unknown provider", {**CANONICAL_MANIFEST, "provider": "acme-ai"}),
    ("unknown cost_source", {**CANONICAL_MANIFEST, "cost_source": "guessed"}),
    (
        "wall_clock_ms missing",
        {k: v for k, v in CANONICAL_MANIFEST.items() if k != "wall_clock_ms"},
    ),
    ("wall_clock_ms is a string", {**CANONICAL_MANIFEST, "wall_clock_ms": "184220"}),
    ("tokens are a string", {**CANONICAL_MANIFEST, "input_tokens": "18422"}),
    ("determinism is a list", {**CANONICAL_MANIFEST, "determinism": []}),
    ("raw is a list", {**CANONICAL_MANIFEST, "raw": [1, 2, 3]}),
    (
        "carries trial_iteration_id",
        {**CANONICAL_MANIFEST, "trial_iteration_id": "ti_1"},
    ),
]
