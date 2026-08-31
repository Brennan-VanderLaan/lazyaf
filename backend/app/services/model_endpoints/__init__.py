"""Model endpoint services (Milestone 14).

Deliberately thin: this package re-exports the names other lanes import so a
consumer never has to know which submodule a constant lives in, and NOTHING
here imports the router or the pipeline executor (that direction would make
`app.models` -> `app.services.model_endpoints` -> `app.services.execution`
a real cycle).

Cross-agent contracts pinned here:

- #3 `HARNESS_API_KEY_ENV` is the ONE container-side variable name, defined
  in `secrets.py` and spelled as a literal nowhere else.
- #4 `ENDPOINT_MODEL_PREFIX` is the ONE `endpoint:<name>` sugar spelling,
  defined in `resolve.py` and parsed only by `resolve_step_endpoint`.
"""
from app.services.model_endpoints.secrets import (  # noqa: F401
    ENDPOINT_SECRET_PREFIX,
    ENDPOINT_SECRET_REF_RE,
    HARNESS_API_KEY_ENV,
    auth_headers,
    endpoint_secret_value,
    is_valid_secret_ref,
    scrub_secrets,
    secret_present,
)
from app.services.model_endpoints.probe import (  # noqa: F401
    DEFAULT_ASSUMED_CONTEXT,
    DEFAULT_MAX_OUTPUT_TOKENS,
    PROBE_MIN_INTERVAL_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    PROBE_TOTAL_TIMEOUT_SECONDS,
    PROBE_TTL_SECONDS,
    MODALITY_REASONS,
    ProbeResult,
    ProbeSpec,
    apply_probe_result,
    background_reprobe,
    modality_probe_body,
    modality_state,
    probe_endpoint,
    run_probe,
    spec_for_endpoint,
)
from app.services.model_endpoints.resolve import (  # noqa: F401
    ENDPOINT_MODEL_PREFIX,
    STEP_ATTACHMENTS_KEY,
    UNTAGGED_ATTACHMENT,
    endpoint_dispatch_refusal,
    endpoint_dispatch_warning,
    endpoint_modality_refusal,
    parse_endpoint_reference,
    resolve_step_endpoint,
    step_modality_needs,
)
from app.services.model_endpoints.health import (  # noqa: F401
    record_step_outcome,
)

__all__ = [
    "ENDPOINT_SECRET_PREFIX",
    "ENDPOINT_SECRET_REF_RE",
    "HARNESS_API_KEY_ENV",
    "ENDPOINT_MODEL_PREFIX",
    "MODALITY_REASONS",
    "STEP_ATTACHMENTS_KEY",
    "UNTAGGED_ATTACHMENT",
    "DEFAULT_ASSUMED_CONTEXT",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "PROBE_MIN_INTERVAL_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_TOTAL_TIMEOUT_SECONDS",
    "PROBE_TTL_SECONDS",
    "ProbeResult",
    "ProbeSpec",
    "apply_probe_result",
    "auth_headers",
    "background_reprobe",
    "endpoint_dispatch_refusal",
    "endpoint_dispatch_warning",
    "endpoint_modality_refusal",
    "endpoint_secret_value",
    "is_valid_secret_ref",
    "modality_probe_body",
    "modality_state",
    "parse_endpoint_reference",
    "probe_endpoint",
    "record_step_outcome",
    "resolve_step_endpoint",
    "run_probe",
    "scrub_secrets",
    "secret_present",
    "spec_for_endpoint",
    "step_modality_needs",
]
