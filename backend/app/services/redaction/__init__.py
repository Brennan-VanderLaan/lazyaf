"""LazyAF's one definition of a credential, and the one thing that removes it.

    patterns.py    the rules. stdlib-only; also loaded by the CI scanners
                   through the .github/scripts/secret_patterns.py shim.
    redactor.py    the two-pass engine and the placeholder contract.
    discovery.py   where pass 1's literal values come from.

`discovery` is NOT re-exported here on purpose. It imports
`app.services.model_endpoints.secrets`, which imports `redactor` - pulling
that into this `__init__` would make the package's own initialisation
re-enter itself. Import it explicitly:

    from app.services.redaction.discovery import discover_known_secrets
"""

from .patterns import (
    ALLOWLIST,
    BENIGN_ENV_VALUES,
    BLOCK_PATTERNS,
    PATTERNS,
    REDACT_PATTERNS,
    REDACTION_ONLY_PATTERNS,
    SECRET_ENV_NAME,
    find_secrets,
    redact_for_ci,
)
from .redactor import (
    MARKER,
    WITHHELD,
    KnownSecret,
    RedactionResult,
    Redactor,
    marker_render,
)

__all__ = [
    "ALLOWLIST",
    "BENIGN_ENV_VALUES",
    "BLOCK_PATTERNS",
    "KnownSecret",
    "MARKER",
    "PATTERNS",
    "REDACTION_ONLY_PATTERNS",
    "REDACT_PATTERNS",
    "RedactionResult",
    "Redactor",
    "SECRET_ENV_NAME",
    "WITHHELD",
    "find_secrets",
    "marker_render",
    "redact_for_ci",
]
