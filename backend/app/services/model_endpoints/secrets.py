"""Endpoint auth: a REFERENCE in the row, the value only in the 12.5 channel.

THE SECURITY DECISION OF THIS PHASE, stated because it is easy to undo by
accident: **the database never stores a secret value.** `auth_secret_ref`
names an environment variable ON THE BACKEND, resolved at dispatch exactly as
`agent_secret_environment` already resolves `ANTHROPIC_API_KEY` from settings.

Why not just store the key? LazyAF has no secret-at-rest story: no encryption
key, no KMS, SQLite backups are plain files, and `GET /api/model-endpoints` is
unauthenticated like the rest of the operator API. A stored key would be a new
class of exposure introduced for the convenience of one form field.

THE PREFIX ALLOWLIST IS LOAD-BEARING. Without it,
`auth_secret_ref: "LAZYAF_STEP_AUTH_SECRET"` or `"ANTHROPIC_API_KEY"` would
exfiltrate the platform's own credentials into a container the operator does
not control - a stored config would become an exfiltration route. A ref
failing the regex is a **422 at CREATE time**, not a dispatch failure. A ref
that PASSES the regex but resolves to nothing is a **dispatch failure naming
the variable** (12.5's precedent, verbatim): burning 30 seconds of container
start to reach an opaque 401 is the outcome that rule exists to prevent.

`auth_style == "none"` is FIRST-CLASS, not a special case. LAN ollama and
vLLM behind a firewall genuinely have no key, and a module that makes "no
auth" the exceptional branch is a module that will grow a fake key.
"""
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: Every backend env var a ModelEndpoint may reference starts with this.
ENDPOINT_SECRET_PREFIX = "LAZYAF_ENDPOINT_"

#: The allowlist. Anchored at both ends, uppercase-only, so
#: `ANTHROPIC_API_KEY`, `LAZYAF_STEP_AUTH_SECRET`, `LAZYAF_RUNNER_AUTH_SECRET`
#: and `../../etc/passwd` are all structurally unreachable.
ENDPOINT_SECRET_REF_RE = re.compile(r"^LAZYAF_ENDPOINT_[A-Z0-9_]{1,48}$")

#: The FIXED env var the harness reads INSIDE the container, whatever the
#: backend-side ref is called. One name container-side means the harness never
#: has to be told where to look (cross-agent contract #3: this literal is
#: spelled here and nowhere else).
HARNESS_API_KEY_ENV = "LAZYAF_ENDPOINT_API_KEY"

#: Shapes that are a secret whatever they are called. Scrubbed unconditionally.
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")

#: What a scrubbed span becomes.
REDACTION = "***"

#: Shortest value worth substring-scrubbing. A one- or two-character "secret"
#: would redact half of every error body and tell the operator nothing.
_MIN_SCRUBBABLE = 4


class EndpointSecretMissing(RuntimeError):
    """A valid `auth_secret_ref` resolves to nothing in the backend env.

    Raised at DISPATCH (and surfaced by the probe as a reachability-shaped
    failure), never at create: the operator may legitimately register the
    endpoint before the variable is set. The message names the VARIABLE and
    never the value.
    """

    def __init__(self, ref: str, endpoint_name: str) -> None:
        self.ref = ref
        self.endpoint_name = endpoint_name
        super().__init__(
            f"endpoint '{endpoint_name}' references backend environment "
            f"variable {ref}, which is not set (neither {ref} nor {ref}_FILE). "
            f"Set it on the backend and retry; the value is never stored in "
            f"the database."
        )


def is_valid_secret_ref(ref: str | None) -> bool:
    """Does `ref` name a variable this platform will read for an endpoint?"""
    return bool(ref) and bool(ENDPOINT_SECRET_REF_RE.match(ref or ""))


def secret_ref_refusal(ref: str | None) -> str | None:
    """The 422 detail for a bad ref, or None when it is acceptable.

    Written as a sentence an operator can act on, because the alternative -
    "value is not a valid enumeration member" - teaches nothing about WHY
    the platform will not read `ANTHROPIC_API_KEY` for them.
    """
    if ref is None or ref == "":
        return None
    if is_valid_secret_ref(ref):
        return None
    return (
        f"auth_secret_ref must name a backend environment variable matching "
        f"{ENDPOINT_SECRET_REF_RE.pattern} (e.g. {ENDPOINT_SECRET_PREFIX}LOCAL_4090). "
        f"'{ref}' is refused: the allowlist is what stops a stored endpoint "
        f"from referencing the platform's own credentials."
    )


def _read_env_ref(ref: str) -> str | None:
    """`<REF>_FILE` first, then `<REF>` - the platform's secret convention.

    A `_FILE` that is set but unreadable is treated as ABSENT here (and the
    caller raises a naming failure) rather than crashing a list endpoint: the
    remedy is the same either way and it is stated in the exception.
    """
    file_ref = os.environ.get(f"{ref}_FILE")
    if file_ref:
        try:
            value = Path(file_ref).read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "%s_FILE points at %r which could not be read; treating the "
                "endpoint secret as unset",
                ref,
                file_ref,
            )
            return None
        return value or None
    value = os.environ.get(ref)
    return value.strip() if value and value.strip() else None


def endpoint_secret_value(endpoint, *, required: bool = True) -> str | None:
    """Resolve the endpoint's API key from the BACKEND environment.

    Returns None - with no error - for `auth_style == "none"`: that is the
    default path, not a degraded one. For bearer/header a missing value
    raises `EndpointSecretMissing` naming the variable when `required`, and
    returns None when not (the probe uses `required=False` so a
    misconfigured secret shows up as a scrubbed 401 on the record rather
    than as a 500 on the operator's probe button).
    """
    if endpoint.auth_style == "none":
        return None
    ref = endpoint.auth_secret_ref
    if not is_valid_secret_ref(ref):
        # Unreachable through the API (422 at create/patch); reachable if a
        # row was written by hand. Refuse rather than read an arbitrary var.
        raise EndpointSecretMissing(str(ref), endpoint.name)
    value = _read_env_ref(ref)
    if value is None and required:
        raise EndpointSecretMissing(ref, endpoint.name)
    return value


def secret_present(endpoint) -> bool:
    """Is the referenced variable actually set? Computed for the API's
    `secret_present` field, which is how the UI renders "not set in the
    backend environment" in red WITHOUT ever seeing the value."""
    if endpoint.auth_style == "none":
        return True
    if not is_valid_secret_ref(endpoint.auth_secret_ref):
        return False
    return _read_env_ref(endpoint.auth_secret_ref) is not None


def auth_headers(endpoint, value: str | None) -> dict[str, str]:
    """The request headers for this endpoint's auth style.

    | style    | header                                |
    |----------|---------------------------------------|
    | `none`   | (none)                                |
    | `bearer` | `Authorization: Bearer <secret>`      |
    | `header` | `<auth_header_name>: <secret>`        |
    """
    if endpoint.auth_style == "none" or not value:
        return {}
    if endpoint.auth_style == "bearer":
        return {"Authorization": f"Bearer {value}"}
    if endpoint.auth_style == "header":
        name = (endpoint.auth_header_name or "").strip()
        if not name:
            return {}
        return {name: value}
    return {}


def scrub_secrets(text, known_values=()) -> str:
    """Redact anything that could be a credential before it is persisted.

    Applied to EVERY upstream string this phase stores or logs -
    `probe_detail`, `last_error`, proxy error bodies, harness log lines.
    A 401 body that echoes the key back is a real failure mode, and it must
    not be the thing that puts the key in the database.

    Three passes, in order: the known value(s) verbatim, `Bearer <x>`, and
    `sk-...` shapes. Non-string input is coerced, because the callers include
    JSON encoders handling whatever an unfamiliar server returned.
    """
    if text is None:
        return ""
    out = text if isinstance(text, str) else str(text)
    for value in known_values or ():
        if value and isinstance(value, str) and len(value) >= _MIN_SCRUBBABLE:
            out = out.replace(value, REDACTION)
    out = _BEARER_RE.sub(f"Bearer {REDACTION}", out)
    out = _SK_RE.sub(REDACTION, out)
    return out
