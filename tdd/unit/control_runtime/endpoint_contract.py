"""THE shared endpoint/harness wire contract (M14, cross-agent contract #2).

The same instrument `usage_contract.py`, `manifest_contract.py` and
`spec_context_contract.py` already are: ONE module declaring the wire shape,
imported by BOTH sides' tests in one process (this package's conftest puts
`images/base` and `runner-common` on `sys.path`, and the tier runs from
`backend/` so `app.*` is importable).

**Owned by agent C, imported by agent B's tests. Nobody adds a key without
editing this file**, which is the whole point: a wire contract with two
declarations is a wire contract that drifts, and the drift is invisible until
a step in the field loads a config it half understands.

The key TUPLES here are the assertion. `make_endpoint_payload` /
`make_harness_payload` are the fixtures, and they are built FROM the tuples so
a key added to one and not the other is a construction error rather than a
silent omission.
"""

# --------------------------------------------------------------------------
# The declared key sets (design section 4.3)
# --------------------------------------------------------------------------

ENDPOINT_BLOCK_KEYS = (
    "id",
    "name",
    "base_url",
    "model",
    "server_kind",
    "reach",
    "auth_style",
    "auth_env",
    "auth_header",
    "request_timeout_seconds",
    "capabilities",
    "pricing",
)

CAPABILITY_KEYS = (
    "supports_tools",
    "supports_streaming",
    "reports_usage",
    "context_window",
    "max_output_tokens",
    "probe_status",
    "probed_at",
    "probed_from",
    "probe_age_seconds",
    "stale",
)

PRICING_KEYS = ("gpu_node_id", "gpu_fraction", "priced")

HARNESS_KEYS = (
    "mode",
    "max_iterations",
    "max_total_tokens",
    "time_budget_seconds",
    "max_tool_calls_per_turn",
    "shell_timeout_seconds",
    "tool_output_max_bytes",
    "temperature",
    "top_p",
    "seed",
    "require_changes",
    "debug_transcript",
)

#: The agent that drives a model endpoint. Both sides spell it; this is where
#: the TEST spells it, so a rename that misses one side fails here.
HARNESS_AGENT = "openai-harness"

#: A sentinel that must never appear in a rendered agent config. Planted by
#: the secret-containment assertions on both sides.
SECRET_SENTINEL = "sk-planted-endpoint-key-do-not-leak-0000"


# --------------------------------------------------------------------------
# Fixtures, built FROM the key tuples
# --------------------------------------------------------------------------

_CAPABILITY_DEFAULTS = {
    "supports_tools": True,
    "supports_streaming": True,
    "reports_usage": True,
    "context_window": 32768,
    "max_output_tokens": 4096,
    "probe_status": "ok",
    "probed_at": "2026-08-30T09:14:22+00:00",
    "probed_from": "runner:workshop-1",
    "probe_age_seconds": 3821.0,
    "stale": False,
}

_PRICING_DEFAULTS = {
    "gpu_node_id": "endpoint:local-4090",
    "gpu_fraction": 1.0,
    "priced": True,
}

_ENDPOINT_DEFAULTS = {
    "id": "e7c1a4b2-0000-4000-8000-000000000001",
    "name": "local-4090",
    "base_url": "http://172.17.0.1:11434/v1",
    "model": "qwen2.5-coder:32b",
    "server_kind": "ollama",
    "reach": "runner-local",
    "auth_style": "bearer",
    # Filled from the backend's own HARNESS_API_KEY_ENV by
    # `make_endpoint_payload` so the literal is spelled in exactly one place
    # in the whole repo (cross-agent contract #3).
    "auth_env": None,
    "auth_header": None,
    "request_timeout_seconds": 300,
    "capabilities": None,
    "pricing": None,
}

_HARNESS_DEFAULTS = {
    "mode": "auto",
    "max_iterations": 40,
    "max_total_tokens": 400000,
    "time_budget_seconds": 1740,
    "max_tool_calls_per_turn": 4,
    "shell_timeout_seconds": 120,
    "tool_output_max_bytes": 8192,
    "temperature": 0,
    "top_p": None,
    "seed": 7,
    "require_changes": True,
    "debug_transcript": False,
}


def _harness_api_key_env() -> str:
    """The ONE container-side variable name, from its ONE definition."""
    from app.services.model_endpoints.secrets import HARNESS_API_KEY_ENV

    return HARNESS_API_KEY_ENV


def make_capability_payload(**overrides) -> dict:
    payload = {key: _CAPABILITY_DEFAULTS[key] for key in CAPABILITY_KEYS}
    payload.update(overrides)
    return payload


def make_pricing_payload(**overrides) -> dict:
    payload = {key: _PRICING_DEFAULTS[key] for key in PRICING_KEYS}
    payload.update(overrides)
    return payload


def make_endpoint_payload(**overrides) -> dict:
    """A wire-shaped `endpoint` block. Overrides are applied AFTER the key
    tuple is walked, so a caller can null a capability without being able to
    accidentally drop a key."""
    payload = {key: _ENDPOINT_DEFAULTS[key] for key in ENDPOINT_BLOCK_KEYS}
    payload["auth_env"] = _harness_api_key_env()
    payload["capabilities"] = make_capability_payload()
    payload["pricing"] = make_pricing_payload()
    payload.update(overrides)
    return payload


def make_harness_payload(**overrides) -> dict:
    payload = {key: _HARNESS_DEFAULTS[key] for key in HARNESS_KEYS}
    payload.update(overrides)
    return payload
