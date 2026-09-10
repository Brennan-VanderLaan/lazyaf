"""Shim. The rules now live in backend/app/services/redaction/patterns.py.

WHY THEY MOVED. The backend has to import them - the bug-report bundle
redacts credentials out of logs before a human attaches them to an issue on
a PUBLIC repo - and `backend/Dockerfile` copies only `app/` into the image.
`.github/` does not exist at runtime, so the canonical table could not stay
here. The direction of the move was decided by one line of a Dockerfile, not
by preference.

WHY IT LOADS BY PATH RATHER THAN IMPORTING A PACKAGE. Both scanners run on
the bare `python3` of a GitHub runner and of a dogfood step container, with
nothing installed and no `backend` on `sys.path`. `importlib` from an
absolute path is the only mechanism that works in all three places, and it
is the same constraint `scan_repo_secrets.py` already lives under.

The public names below are exactly the ones `scan_repo_secrets.py` and
`scan_image_secrets.py` already import, so NEITHER SCANNER CHANGED. `redact`
maps to `redact_for_ci`, which is the same function under a name that says
which audience it is for - it prints ten live characters of a matched value,
which is right for an operator who has to go rotate a key and wrong for a
file attached to a public issue.
"""

import importlib.util
import pathlib
import sys

_CANONICAL = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "app"
    / "services"
    / "redaction"
    / "patterns.py"
)

if not _CANONICAL.is_file():
    raise SystemExit(
        f"secret_patterns: canonical rules not found at {_CANONICAL}.\n"
        "This shim resolves them relative to the repository root, so it needs "
        "a full checkout (backend/ included), not just .github/."
    )

_spec = importlib.util.spec_from_file_location("lazyaf_secret_patterns", _CANONICAL)
_module = importlib.util.module_from_spec(_spec)
# Registered before exec so the module is importable by name from anywhere
# else in the same process, and so a re-import is cheap.
sys.modules.setdefault("lazyaf_secret_patterns", _module)
_spec.loader.exec_module(_module)

PATTERNS = _module.PATTERNS
REDACTION_ONLY_PATTERNS = _module.REDACTION_ONLY_PATTERNS
REDACT_PATTERNS = _module.REDACT_PATTERNS
BLOCK_PATTERNS = _module.BLOCK_PATTERNS
ALLOWLIST = _module.ALLOWLIST
SECRET_ENV_NAME = _module.SECRET_ENV_NAME
BENIGN_ENV_VALUES = _module.BENIGN_ENV_VALUES
find_secrets = _module.find_secrets
redact_for_ci = _module.redact_for_ci
#: The name both scanners call.
redact = _module.redact_for_ci
