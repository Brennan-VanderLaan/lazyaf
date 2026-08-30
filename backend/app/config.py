"""Backend settings.

SHARED SECRETS (Phase 12.7 hardening)
=====================================
``LAZYAF_STEP_AUTH_SECRET`` and ``LAZYAF_RUNNER_AUTH_SECRET`` used to fall back
to constants written into this file. A constant in a public repository is not a
secret: it minted the step JWTs the control layer trusts and it authenticated
every runner agent, so anyone who could read GitHub could mint both. Those
constants are GONE, and the old values are now treated as *unset* so an
inherited ``.env`` cannot quietly keep the hole open.

Resolution order for each secret, highest first:

1. ``<NAME>_FILE``  - a path whose contents are the value. This is how docker
   secrets and kubernetes mounted Secrets deliver a value, and it beats the
   inline variable. A ``_FILE`` that is set but unreadable/empty/placeholder is
   an ERROR, never a silent fallback: you said where the secret lives.
2. ``<NAME>``       - the value inline in the environment.
3. Nothing usable   -> ``MissingSecretError``, which stops the process.

The single escape hatch is ``LAZYAF_DEV_EPHEMERAL_SECRETS=1``: it generates a
per-process value and logs a loud warning. It is opt-in on purpose. Fail-closed
is the default; convenience never is.
"""
from pydantic import BaseModel
from functools import lru_cache
from pathlib import Path
import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Shared-secret resolution
# --------------------------------------------------------------------------

#: Opt in to per-process generated secrets. Development only - see the module
#: docstring and the warning text in ``_ephemeral_secret``.
DEV_EPHEMERAL_FLAG = "LAZYAF_DEV_EPHEMERAL_SECRETS"

#: The constants this file used to ship. They are public (git history, image
#: layers, every fork), so they are rejected exactly as an empty value is. An
#: operator upgrading with one of these still in .env gets the fail-fast, not a
#: false sense of having configured something.
RETIRED_PUBLIC_SECRETS = frozenset(
    {
        "lazyaf-step-auth-secret-key-change-in-production",
        "lazyaf-runner-auth-secret-key-change-in-production",
    }
)

#: "I copied the template and did not fill it in" shapes.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "change-in-production",
        "change-me",
        "change_me",
        "changeme",
        "generate-me",
        "generateme",
        "none",
        "null",
        "placeholder",
        "replace-me",
        "replaceme",
        "secret",
        "tbd",
        "todo",
        "unset",
        "your-secret-here",
        "<generate>",
    }
)

#: Below this we log (never fail) that the operator's value is short. 48 bytes
#: of ``token_urlsafe`` is 64 characters, so a real generated value never trips
#: this; a hand-typed word does.
MIN_RECOMMENDED_SECRET_LENGTH = 32

#: What each secret is FOR, spliced into the failure message so the person
#: reading it knows what they are turning on rather than just which variable.
SECRET_PURPOSES = {
    "LAZYAF_STEP_AUTH_SECRET": (
        "it signs the short-lived JWTs a step container presents to /api/steps/*"
    ),
    "LAZYAF_RUNNER_AUTH_SECRET": (
        "it is the enrollment secret a runner agent presents at the /ws/runner "
        "handshake, and it signs per-runner tokens"
    ),
}

#: Per-process ephemeral values, so one flagged process is self-consistent.
_EPHEMERAL_SECRETS: dict = {}


class MissingSecretError(RuntimeError):
    """A required shared secret is absent, retired, or a placeholder.

    Raised during settings construction, which happens at import of
    ``app.main`` - so the process dies at startup with an actionable message
    rather than minting tokens nobody else can verify.
    """

    def __init__(self, var_name: str, reason: str) -> None:
        self.var_name = var_name
        self.reason = reason
        super().__init__(_missing_secret_message(var_name, reason))


def _missing_secret_message(var_name: str, reason: str) -> str:
    """The whole fix, in the failure. No cross-referencing required."""
    file_var = f"{var_name}_FILE"
    purpose = SECRET_PURPOSES.get(var_name, "it authenticates part of the stack")
    generate = (
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
    return "\n".join(
        [
            f"{reason}",
            "",
            f"LazyAF will not start without it: {purpose}.",
            "",
            "There is deliberately no built-in default. A default compiled into the",
            "source is a published secret, and this one mints credentials the",
            "backend trusts.",
            "",
            "Set it one of these ways:",
            "",
            "  compose / local dev",
            "    python scripts/bootstrap_secrets.py",
            "    (generates strong values into .env; idempotent, and it never",
            "     overwrites a value you already set)",
            "",
            "  docker secrets / kubernetes",
            "    mount the value at a path and set",
            f"    {file_var}=/run/secrets/{var_name.lower()}",
            f"    ({file_var} takes precedence over {var_name})",
            "",
            "  anywhere else",
            f"    export {var_name}=\"$({generate})\"",
            "",
            f"Throwaway single-process run only: {DEV_EPHEMERAL_FLAG}=1 generates a",
            "value that lives and dies with this process. Tokens minted under it stop",
            "verifying on restart and runner agents in other containers cannot",
            "authenticate, so it is never right for compose, k8s, or anything shared.",
        ]
    )


def is_placeholder_secret(value: str | None) -> bool:
    """True when ``value`` is absent, retired, or an obvious fill-me-in."""
    if value is None:
        return True
    candidate = value.strip()
    if not candidate:
        return True
    if candidate in RETIRED_PUBLIC_SECRETS:
        return True
    lowered = candidate.lower()
    if lowered in _PLACEHOLDER_SECRETS:
        return True
    # "xxxxxxxx", "XXXX" - the shape people type to mean "not yet".
    if set(lowered) == {"x"}:
        return True
    return False


def _read_secret_file(var_name: str, path: str) -> str:
    """Contents of a ``*_FILE`` path, or a fail-fast explaining the path.

    Only ever reports the PATH. The contents are the secret.
    """
    file_var = f"{var_name}_FILE"
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        detail = getattr(exc, "strerror", None) or exc.__class__.__name__
        raise MissingSecretError(
            var_name,
            f"{file_var} points at {path!r}, which could not be read ({detail}).",
        ) from exc
    value = raw.strip()
    if is_placeholder_secret(value):
        raise MissingSecretError(
            var_name,
            f"{file_var} points at {path!r}, whose contents are empty or a "
            "placeholder rather than a real secret.",
        )
    return value


def _ephemeral_secret(var_name: str) -> str:
    """A generated per-process value, announced as loudly as it deserves."""
    if var_name not in _EPHEMERAL_SECRETS:
        _EPHEMERAL_SECRETS[var_name] = secrets.token_urlsafe(48)
        logger.warning(
            "%s: GENERATED AN EPHEMERAL SECRET because %s is set. This is a "
            "development convenience, not a deployment. The value exists only "
            "inside this process: every credential minted with it STOPS "
            "VERIFYING when the backend restarts, and no runner agent in "
            "another process or container can authenticate against it. Run "
            "`python scripts/bootstrap_secrets.py` (or set %s / %s_FILE) for a "
            "value that persists.",
            var_name,
            DEV_EPHEMERAL_FLAG,
            var_name,
            var_name,
        )
    return _EPHEMERAL_SECRETS[var_name]


def reset_ephemeral_secrets() -> None:
    """Drop cached ephemeral values. Tests only."""
    _EPHEMERAL_SECRETS.clear()


def _dev_ephemeral_enabled(env) -> bool:
    return (env.get(DEV_EPHEMERAL_FLAG) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def resolve_secret(var_name: str, env=None) -> str:
    """Resolve one shared secret, or raise ``MissingSecretError``.

    ``<NAME>_FILE`` wins over ``<NAME>``; see the module docstring.
    """
    env = os.environ if env is None else env

    file_path = (env.get(f"{var_name}_FILE") or "").strip()
    if file_path:
        value = _read_secret_file(var_name, file_path)
        source = f"{var_name}_FILE"
    else:
        inline = (env.get(var_name) or "").strip()
        if is_placeholder_secret(inline):
            if _dev_ephemeral_enabled(env):
                return _ephemeral_secret(var_name)
            raise MissingSecretError(var_name, _absent_reason(var_name, inline))
        value = inline
        source = var_name

    if len(value) < MIN_RECOMMENDED_SECRET_LENGTH:
        # Length only. The value never reaches a log record.
        logger.warning(
            "%s was read from %s but is only %d characters. It mints "
            "credentials the backend trusts; %d+ is strongly recommended "
            "(`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`).",
            var_name,
            source,
            len(value),
            MIN_RECOMMENDED_SECRET_LENGTH,
        )
    return value


def _absent_reason(var_name: str, inline: str) -> str:
    """Why this counts as unset - the three cases read very differently."""
    if not inline:
        return f"{var_name} is not set (and neither is {var_name}_FILE)."
    if inline in RETIRED_PUBLIC_SECRETS:
        return (
            f"{var_name} still holds the retired public development default. "
            "That value shipped in LazyAF's source, so it is published, and "
            "anyone can use it to mint credentials this backend would trust. "
            "It is treated as unset."
        )
    return (
        f"{var_name} holds a placeholder value rather than a real secret. "
        "It is treated as unset."
    )


# --------------------------------------------------------------------------


def _parse_gpu_node_rates(raw: str | None) -> dict:
    """Parse LAZYAF_GPU_NODE_RATES (JSON object) — never fatal.

    A malformed rate table must not stop the backend from booting: it is
    priced-as-unknown telemetry configuration, not a correctness input. The
    parse failure is logged loudly and the table falls back to empty.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "LAZYAF_GPU_NODE_RATES is not valid JSON — gpu-node steps will be "
            "priced as cost_source='unknown'"
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "LAZYAF_GPU_NODE_RATES must be a JSON object keyed by node id, got "
            "%s — gpu-node steps will be priced as cost_source='unknown'",
            type(parsed).__name__,
        )
        return {}
    return parsed


class Settings(BaseModel):
    app_name: str = "LazyAF"
    database_url: str = "sqlite+aiosqlite:///./lazyaf.db"
    cors_origins: list[str] = ["http://localhost:5173"]
    docker_host: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    default_runner_type: str = "any"  # any, claude-code, gemini
    default_prompt_template: str | None = None  # Global default prompt template for AI agents
    # Mounts the /api/test reset/seed endpoints (e2e harness only - never prod)
    test_mode: bool = False
    # --- Execution plumbing (Phase 12.2-INT) ---
    # Named docker network shared by backend, runners, and step/helper containers.
    container_network: str = "lazyaf-network"
    # Clone URL template used by workspace population helper containers.
    # Resolved on the container network (backend service DNS name), not localhost.
    container_git_url_template: str = "http://backend:8000/git/{repo_id}.git"
    # Image for the short-lived workspace population (git clone) helper container.
    workspace_clone_image: str = "python:3.12"
    # Backend base URL as seen FROM step/helper containers on the container
    # network (contract #2: injected into step env as LAZYAF_BACKEND_URL).
    container_backend_url: str = "http://backend:8000"
    # Default image for pipeline steps (moved here from LocalExecutor).
    # Full python image (not slim) until 12.3's lazyaf-base: bash/curl/git needed.
    step_default_image: str = "python:3.12"
    # Default working directory for step containers.
    step_working_dir: str = "/workspace/repo"
    # HOME inside step containers - lives on the shared workspace volume so
    # tools installed in one step survive to the next (12.3 persistence contract).
    step_home_dir: str = "/workspace/home"
    # Secret for step auth tokens (control layer <-> /api/steps/*). REQUIRED:
    # there is no default, and get_settings() raises MissingSecretError rather
    # than invent one. See the module docstring.
    step_auth_secret: str
    # --- Runner socket (Phase 12.6) ---
    # Shared ENROLLMENT secret checked at the /ws/runner HTTP upgrade, before
    # accept(). It authenticates the FLEET, not an identity - a runner_id is
    # client-asserted, and the real per-runner guards are duplicate-connection
    # rejection and the step gate. The per-runner JWT upgrade
    # (services/execution/runner_token.py) signs with this same secret.
    # REQUIRED, same as step_auth_secret.
    runner_auth_secret: str
    # --- Usage channel (Phase 12.5) ---
    # Self-hosted node hourly rates, addressed by node id (api-surface 2.5):
    #   {"runpod-a100-80g": {"rate_usd_hour": "1.89", "note": "..."}}
    # The SERVER prices gpu-node steps from this table so a corrected rate can
    # re-price history. Empty by default: nothing sets LAZYAF_GPU_NODE_ID
    # until 12.6 puts steps on real nodes.
    gpu_node_rates: dict = {}

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./lazyaf.db"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        docker_host=os.getenv("DOCKER_HOST"),
        default_runner_type=os.getenv("DEFAULT_RUNNER_TYPE", "any"),
        default_prompt_template=os.getenv("DEFAULT_PROMPT_TEMPLATE"),
        test_mode=os.getenv("LAZYAF_TEST_MODE", "").lower() in ("1", "true", "yes"),
        container_network=os.getenv("CONTAINER_NETWORK", "lazyaf-network"),
        container_git_url_template=os.getenv(
            "CONTAINER_GIT_URL_TEMPLATE", "http://backend:8000/git/{repo_id}.git"
        ),
        workspace_clone_image=os.getenv("WORKSPACE_CLONE_IMAGE", "python:3.12"),
        container_backend_url=os.getenv("CONTAINER_BACKEND_URL", "http://backend:8000"),
        step_default_image=os.getenv("STEP_DEFAULT_IMAGE", "python:3.12"),
        step_working_dir=os.getenv("STEP_WORKING_DIR", "/workspace/repo"),
        step_home_dir=os.getenv("STEP_HOME_DIR", "/workspace/home"),
        step_auth_secret=resolve_secret("LAZYAF_STEP_AUTH_SECRET"),
        runner_auth_secret=resolve_secret("LAZYAF_RUNNER_AUTH_SECRET"),
        gpu_node_rates=_parse_gpu_node_rates(os.getenv("LAZYAF_GPU_NODE_RATES")),
    )
