"""Harvest the literal secret values THIS process holds, for pass 1.

The redactor's exact-value pass is only as good as this list. Everything here
is about making the list complete without making it dangerous.

FOUR SOURCES, and the second one is the one that earns its keep:

  1. The named secret Settings fields. Enumerated EXPLICITLY, never by
     "any attribute whose name contains 'key'": a rename would silently drop
     a secret out of the redactor while every test still passed.

  2. Every environment variable whose NAME says credential, via
     `patterns.SECRET_ENV_NAME` - the same rule the image scanner already
     uses. This is the ONLY rule in the system that catches a provider
     nobody has modelled: a `MISTRAL_API_KEY` has no entry in PATTERNS and
     no field in Settings, and it is still redacted, because we hold its
     literal value. `BENIGN_ENV_VALUES` keeps `DEBUG_TOKEN=1` out.

  3. Endpoint auth values. The database stores a REF, never a value (the
     Milestone 14 doctrine in models/model_endpoint.py), so each ref is
     resolved through `model_endpoints.secrets._read_env_ref` - the SAME
     reader dispatch uses. Reimplementing the lookup would miss the
     `<NAME>_FILE` indirection, and a docker-secrets deployment delivers
     every endpoint key that way.

  4. Every `LAZYAF_ENDPOINT_*` variable, whether or not a row references it.
     A ref that was deleted from the database leaves the variable set, and
     the value is just as live.

THE `_FILE` INDIRECTION IS FOLLOWED EVERYWHERE. `config.resolve_secret` and
`secrets._read_env_ref` both implement `<NAME>_FILE` wins over `<NAME>`; this
module calls them rather than reading `os.environ` directly, because a
redactor that only knows the inline form is blind on exactly the
deployments that took secret handling most seriously.

THE FLOOR IS 8 CHARACTERS HERE, not the engine's 4. A harvested value is a
GUESS about what is secret - nobody asserted it - and a 4-character guess
turns ordinary prose into confetti in a document a human then has to read
carefully. `MIN_RECOMMENDED_SECRET_LENGTH` is 32 and `token_urlsafe(48)` is
64 characters, so nothing real is lost. Callers who hold a value and KNOW it
is a secret pass it to the Redactor directly, where the floor stays 4.

NOTHING HERE MAY RAISE. `get_settings()` throws `MissingSecretError` when the
shared secrets are unset, which is correct at startup and catastrophic here:
the one moment you most need a bug report is the moment the process is
misconfigured. Every source degrades to "contributed nothing".
"""

import logging
import os
from typing import Iterable, List, Optional

from .patterns import BENIGN_ENV_VALUES, SECRET_ENV_NAME
from .redactor import KnownSecret

logger = logging.getLogger(__name__)

#: Settings fields that ARE secrets, listed by hand. See the docstring for
#: why this is not computed from the field names.
SECRET_SETTINGS_FIELDS = (
    "step_auth_secret",
    "runner_auth_secret",
    "anthropic_api_key",
    "gemini_api_key",
)

#: Environment variables carrying the same values, for the case where
#: `get_settings()` cannot be constructed at all.
SECRET_ENV_FALLBACKS = (
    "LAZYAF_STEP_AUTH_SECRET",
    "LAZYAF_RUNNER_AUTH_SECRET",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)

#: See the docstring. Deliberately higher than the engine's default of 4.
MIN_DISCOVERED_LENGTH = 8


def discover_known_secrets(
    *,
    settings=None,
    env: Optional[dict] = None,
    endpoint_refs: Iterable[str] = (),
) -> List[KnownSecret]:
    """Every literal secret value this process can see, labelled by source.

    `endpoint_refs` is an iterable of `ModelEndpoint.auth_secret_ref` strings.
    It is a PARAMETER rather than a database query so this module imports no
    models and stays unit-testable without a session; the caller that has the
    rows passes them in.

    Labels name the SOURCE (`step-auth-secret`, `env:MISTRAL_API_KEY`). That
    reveals nothing the bundle does not already publish on purpose - the
    settings inventory lists which secrets are set, by name, with no values -
    and it turns a placeholder into a diagnostic.
    """
    environ = os.environ if env is None else env
    found: List[KnownSecret] = []
    seen: set = set()

    def add(value, label: str) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < MIN_DISCOVERED_LENGTH or value in seen:
            return
        if value.strip().lower() in BENIGN_ENV_VALUES:
            return
        seen.add(value)
        found.append(KnownSecret(value=value, label=label))

    # 1. Named settings fields.
    if settings is None:
        settings = _settings_or_none()
    if settings is not None:
        for name in SECRET_SETTINGS_FIELDS:
            add(getattr(settings, name, None), name.replace("_", "-"))
    else:
        # Settings could not be built - which is a bug-report-worthy state in
        # itself. Fall back to the raw variables so the redactor is not
        # empty-handed exactly when it matters.
        for name in SECRET_ENV_FALLBACKS:
            add(_resolve(name, environ), f"env:{name}")

    # 2. Any variable whose NAME says credential. The catch-all.
    for name in sorted(environ):
        try:
            if not SECRET_ENV_NAME.search(name):
                continue
        except Exception:  # noqa: BLE001 - a non-str key in a fake env
            continue
        # `X_SECRET_FILE` matches SECRET_ENV_NAME, and its VALUE IS A PATH,
        # not a secret. Harvesting it verbatim - which the first version of
        # this did - redacts every mention of a filesystem path from the
        # bundle and, worse, publishes that path's placeholder as if it were
        # a credential. Collapse to the base name instead; `_resolve` reads
        # `<base>_FILE` first anyway, so a deployment that sets ONLY the
        # `_FILE` form is still harvested. Duplicates collapse in `add`.
        base = name[: -len("_FILE")] if name.endswith("_FILE") else name
        add(_resolve(base, environ), f"env:{base}")

    # 3. Endpoint refs, resolved the way dispatch resolves them.
    for ref in endpoint_refs or ():
        if not isinstance(ref, str) or not ref:
            continue
        add(_read_endpoint_ref(ref), f"endpoint:{ref}")

    # 4. Every LAZYAF_ENDPOINT_* variable, referenced or not.
    prefix = _endpoint_prefix()
    for name in sorted(environ):
        if isinstance(name, str) and name.startswith(prefix):
            base = name[: -len("_FILE")] if name.endswith("_FILE") else name
            add(_resolve(base, environ), f"env:{base}")

    return found


# --------------------------------------------------------------------------
# every helper below degrades to None rather than raising
# --------------------------------------------------------------------------


def _settings_or_none():
    try:
        from app.config import get_settings

        return get_settings()
    except Exception as exc:  # noqa: BLE001 - MissingSecretError, ImportError...
        logger.debug("redaction discovery: settings unavailable (%s)", type(exc).__name__)
        return None


def _resolve(name: str, environ) -> Optional[str]:
    """`<NAME>_FILE` first, then `<NAME>` - the platform's convention.

    Deliberately NOT `config.resolve_secret`, which raises on a placeholder
    and warns on a short value. Here a placeholder simply contributes
    nothing; a redactor is not the place to enforce secret hygiene.
    """
    file_path = (environ.get(f"{name}_FILE") or "").strip()
    if file_path:
        try:
            from pathlib import Path

            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    value = environ.get(name)
    return value.strip() if isinstance(value, str) else None


def _read_endpoint_ref(ref: str) -> Optional[str]:
    try:
        from app.services.model_endpoints.secrets import _read_env_ref

        return _read_env_ref(ref)
    except Exception:  # noqa: BLE001
        return None


def _endpoint_prefix() -> str:
    try:
        from app.services.model_endpoints.secrets import ENDPOINT_SECRET_PREFIX

        return ENDPOINT_SECRET_PREFIX
    except Exception:  # noqa: BLE001 - keep the literal in ONE place normally
        return "LAZYAF_ENDPOINT_"
