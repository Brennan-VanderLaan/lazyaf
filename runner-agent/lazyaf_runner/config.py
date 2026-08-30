"""Runner agent configuration - Phase 12.6, section 4.5.

Env first, CLI overrides on top. Every value has a documented default; nothing
here is discovered by probing, because a runner that guesses its own identity
is a runner whose rows go stale on every restart.

Two deliberate non-settings:

* ``arch`` is NOT configurable. The agent always reports raw
  ``platform.machine()`` and the BACKEND normalizes it (cross-agent contract
  #5), so there is exactly one alias table in the system.
* ``heartbeat_interval`` / ``death_timeout`` are NOT configurable. They arrive
  in the ``registered`` frame. failure_01 had the agent's 10s, the server's 20s
  read deadline and the 30s death timeout drifting apart independently with
  nothing to reconcile them.
"""
from __future__ import annotations

import logging
import os
import platform
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

#: Where the shared enrollment secret can come from, highest precedence first.
#: A ``*_FILE`` variant beats its inline twin because that is how docker
#: secrets and kubernetes mounted Secrets deliver a value, and the agent-
#: specific name beats the backend's own name so one k8s Secret can be mounted
#: into both workloads under whichever key is convenient.
#:
#: There is NO default. The backend no longer ships one either (a constant in a
#: public repo is not a secret), so an agent with nothing configured would fail
#: the /ws/runner handshake anyway - better to say so before dialling.
TOKEN_SOURCES = (
    "LAZYAF_RUNNER_TOKEN_FILE",
    "LAZYAF_RUNNER_TOKEN",
    "LAZYAF_RUNNER_AUTH_SECRET_FILE",
    "LAZYAF_RUNNER_AUTH_SECRET",
)

#: The constants LazyAF shipped before 12.7. Public, therefore not secrets:
#: treated exactly as "unset" so an inherited config fails loudly instead of
#: enroling a fleet anyone on the internet could join.
RETIRED_PUBLIC_TOKENS = frozenset(
    {
        "lazyaf-runner-auth-secret-key-change-in-production",
        "lazyaf-step-auth-secret-key-change-in-production",
    }
)

DEFAULT_BACKEND_URL = "http://localhost:8000"
DEFAULT_ORCHESTRATOR = "docker"
DEFAULT_RUNNER_TYPE = "generic"
DEFAULT_STEP_NETWORK = "bridge"

#: Path the WS endpoint is mounted at.
RUNNER_WS_PATH = "/ws/runner"

#: Hosts that count as loopback for the plaintext guard.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


class ConfigError(ValueError):
    """A configuration the agent refuses to run with."""


def parse_labels(raw: str | None) -> dict:
    """Parse ``arch=arm64,has=gpio,has=camera`` into a label dict.

    A repeated key becomes a list, which is how ``has`` gets more than one
    entry from a flat env string. A single occurrence stays a scalar; the
    backend's ``normalize_labels`` coerces ``has`` to a list either way, so
    both spellings match identically.

    A bare token with no ``=`` is read as ``token=true`` rather than being
    dropped, so ``--labels gpu`` does something visible instead of nothing.
    """
    labels: dict = {}
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" in item:
            key, _, value = item.partition("=")
            key, value = key.strip(), value.strip()
        else:
            key, value = item, "true"
        if not key:
            continue
        if key in labels:
            existing = labels[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                labels[key] = [existing, value]
        else:
            labels[key] = value
    return labels


def websocket_url(backend_url: str) -> str:
    """``http(s)://host[:port][/base]`` -> ``ws(s)://host[:port][/base]/ws/runner``.

    Scheme swap only: a backend behind a path prefix keeps it, and an explicit
    ``ws://``/``wss://`` backend URL is honored as given.
    """
    parsed = urlparse(backend_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    if scheme not in ("ws", "wss"):
        raise ConfigError(
            f"backend url {backend_url!r} must use http, https, ws or wss "
            f"(got {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise ConfigError(f"backend url {backend_url!r} has no host")
    base = parsed.path.rstrip("/")
    return urlunparse((scheme, parsed.netloc, f"{base}{RUNNER_WS_PATH}", "", "", ""))


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _env_flag(env: dict, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_token(env: dict) -> str:
    """The enrollment secret, from the first source in ``TOKEN_SOURCES``.

    Returns ``""`` when nothing is configured rather than raising, because
    ``--token`` is applied AFTER ``from_env`` and is a legitimate way to supply
    it. ``validate()`` is where a still-empty token becomes fatal.

    A ``*_FILE`` that is set but unreadable IS fatal here: pointing at a file is
    an explicit statement about where the secret lives, and quietly falling
    through to a different source would hide a broken mount behind a handshake
    rejection an hour later.
    """
    for name in TOKEN_SOURCES:
        raw = (env.get(name) or "").strip()
        if not raw:
            continue
        if name.endswith("_FILE"):
            try:
                value = Path(raw).read_text(encoding="utf-8").strip()
            except OSError as exc:
                detail = getattr(exc, "strerror", None) or exc.__class__.__name__
                raise ConfigError(
                    f"{name}={raw!r} could not be read ({detail}). It should be a "
                    "path to a file containing the shared enrollment secret - "
                    "that is how docker secrets and kubernetes mounted Secrets "
                    "deliver one."
                ) from exc
            if not value:
                raise ConfigError(f"{name}={raw!r} points at an empty file.")
        else:
            value = raw
        if value in RETIRED_PUBLIC_TOKENS:
            raise ConfigError(
                f"{name} holds the retired public development default. That "
                "value shipped in LazyAF's source, so it is published and "
                "anyone can use it to enrol a runner. Generate a real one on "
                "the backend host with `python scripts/bootstrap_secrets.py` "
                "and set the SAME value here."
            )
        return value
    return ""


#: The whole fix, in the failure - the agent is often the thing running on a
#: host you cannot easily read docs from.
_NO_TOKEN_MESSAGE = (
    "no enrollment secret configured. The agent authenticates to the backend "
    "with the shared secret the backend knows as LAZYAF_RUNNER_AUTH_SECRET; "
    "without it the /ws/runner upgrade is rejected before `register` and this "
    "host never appears in the runner list.\n"
    "\n"
    "Supply it, highest precedence first:\n"
    "  --token <value>                     (overrides the environment)\n"
    "  LAZYAF_RUNNER_TOKEN_FILE=<path>     (docker secrets / k8s mounted Secret)\n"
    "  LAZYAF_RUNNER_TOKEN=<value>\n"
    "  LAZYAF_RUNNER_AUTH_SECRET_FILE=<path>\n"
    "  LAZYAF_RUNNER_AUTH_SECRET=<value>   (the backend's own variable name)\n"
    "\n"
    "It must EQUAL the backend's LAZYAF_RUNNER_AUTH_SECRET. On the backend "
    "host, `python scripts/bootstrap_secrets.py` generates one into .env."
)


@dataclass
class RunnerConfig:
    """Everything the agent needs to know about itself and its backend."""

    backend_url: str = DEFAULT_BACKEND_URL
    runner_id: str = ""
    name: str = ""
    runner_type: str = DEFAULT_RUNNER_TYPE
    labels: dict = field(default_factory=dict)
    orchestrator: str = DEFAULT_ORCHESTRATOR
    #: Shared enrollment secret. Empty means "not configured", which
    #: ``validate()`` rejects. There is no usable default by design.
    token: str = ""
    #: Overrides ``config.backend_url`` for the STEP CONTAINER (section 3.4).
    #: ``http://backend:8000`` is meaningless off the compose network; this is
    #: the single most likely remote deployment failure and this is its fix.
    step_backend_url: str = ""
    #: Overrides ``workspace.clone_url``. ``{repo_id}`` is substituted.
    git_url_template: str = ""
    step_network: str = DEFAULT_STEP_NETWORK
    allow_insecure: bool = False
    bind_allowlist: tuple[str, ...] = ()
    #: Images whose absence is reported as ``has: [images:stale]`` at register
    #: time, so an operator sees a stale host in the runner list rather than in
    #: a step failure ten minutes later.
    expect_images: tuple[str, ...] = ()
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        self.apply_defaults()

    def apply_defaults(self) -> None:
        """Derive the values that depend on other values. IDEMPOTENT.

        The CLI applies its overrides on top of an already-constructed config
        and calls this again, so running twice must be indistinguishable from
        running once - a second pass that re-warns about a label the first pass
        set is a warning about the agent's own behavior, which teaches an
        operator to ignore warnings.
        """
        if not self.runner_id:
            self.runner_id = f"{socket.gethostname()}-{self.orchestrator}"
        if not self.name:
            self.name = self.runner_id
        # arch is never operator-supplied: one alias table, backend-side.
        machine = platform.machine()
        supplied = self.labels.pop("arch", None)
        if supplied is not None and str(supplied) != machine:
            logger.warning(
                "Ignoring configured label arch=%s: the agent always reports raw "
                "platform.machine() (%s) and the backend normalizes it",
                supplied,
                machine,
            )
        self.labels["arch"] = machine

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict | None = None) -> "RunnerConfig":
        env = dict(os.environ if env is None else env)
        orchestrator = env.get("LAZYAF_ORCHESTRATOR") or DEFAULT_ORCHESTRATOR
        return cls(
            backend_url=env.get("LAZYAF_BACKEND_URL") or DEFAULT_BACKEND_URL,
            runner_id=env.get("LAZYAF_RUNNER_ID") or "",
            name=env.get("LAZYAF_RUNNER_NAME") or "",
            runner_type=env.get("LAZYAF_RUNNER_TYPE") or DEFAULT_RUNNER_TYPE,
            labels=parse_labels(env.get("LAZYAF_RUNNER_LABELS")),
            orchestrator=orchestrator,
            token=resolve_token(env),
            step_backend_url=env.get("LAZYAF_STEP_BACKEND_URL") or "",
            git_url_template=env.get("LAZYAF_GIT_URL_TEMPLATE") or "",
            step_network=env.get("LAZYAF_STEP_NETWORK") or DEFAULT_STEP_NETWORK,
            allow_insecure=_env_flag(env, "LAZYAF_RUNNER_ALLOW_INSECURE"),
            bind_allowlist=_split_csv(env.get("LAZYAF_BIND_ALLOWLIST")),
            expect_images=_split_csv(env.get("LAZYAF_EXPECT_IMAGES")),
            log_level=env.get("LAZYAF_RUNNER_LOG_LEVEL") or "INFO",
        )

    # ------------------------------------------------------------------
    @property
    def ws_url(self) -> str:
        return websocket_url(self.backend_url)

    def validate(self) -> None:
        """Refuse configurations that would leak or misbehave.

        Two guards, in the order a misconfiguration is most likely:

        The MISSING TOKEN check comes first because it is the one that turns
        into a silent empty runner list. Without a secret the HTTP upgrade is
        rejected before ``accept()``, so the operator sees "no runners" with
        nothing in the agent's log to explain it.

        The plaintext guard is the one with teeth: ``execute_step.config``
        carries the step JWT and ``secret_environment`` inside
        ``control_files``. Sending that across a real network in the clear is
        not a default worth having, so ``ws://`` to a non-loopback host needs
        an explicit ``LAZYAF_RUNNER_ALLOW_INSECURE=1``.
        """
        if not (self.token or "").strip():
            raise ConfigError(_NO_TOKEN_MESSAGE)
        if self.token.strip() in RETIRED_PUBLIC_TOKENS:
            raise ConfigError(
                "the configured enrollment secret is the retired public "
                "development default, which is published in LazyAF's source. "
                "Use the value the backend generated - see "
                "`python scripts/bootstrap_secrets.py` on the backend host."
            )

        url = self.ws_url
        if url.startswith("ws://") and not is_loopback(url) and not self.allow_insecure:
            raise ConfigError(
                f"refusing to register over plaintext ws:// to non-loopback host "
                f"{urlparse(url).hostname!r}: execute_step carries the step JWT and "
                "secret_environment. Use wss://, or set "
                "LAZYAF_RUNNER_ALLOW_INSECURE=1 to accept the risk."
            )

    def resolve_backend_url(self, from_config: str) -> str:
        """Backend URL the STEP CONTAINER should use (section 3.4)."""
        return self.step_backend_url or from_config

    def resolve_clone_url(self, from_config: str, repo_id: str) -> str:
        """Git URL the workspace clone should use (section 3.4)."""
        if self.git_url_template:
            return self.git_url_template.format(repo_id=repo_id)
        return from_config

    def redacted(self) -> dict:
        """Loggable form: the token never appears, only whether it is default."""
        return {
            "backend_url": self.backend_url,
            "ws_url": self.ws_url,
            "runner_id": self.runner_id,
            "name": self.name,
            "runner_type": self.runner_type,
            "labels": self.labels,
            "orchestrator": self.orchestrator,
            "token": "<set>" if (self.token or "").strip() else "<unset>",
            "step_backend_url": self.step_backend_url or "<from config>",
            "git_url_template": self.git_url_template or "<from config>",
            "step_network": self.step_network,
            "allow_insecure": self.allow_insecure,
            "bind_allowlist": list(self.bind_allowlist),
        }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


__all__ = [
    "DEFAULT_BACKEND_URL",
    "DEFAULT_ORCHESTRATOR",
    "DEFAULT_RUNNER_TYPE",
    "DEFAULT_STEP_NETWORK",
    "RETIRED_PUBLIC_TOKENS",
    "RUNNER_WS_PATH",
    "TOKEN_SOURCES",
    "ConfigError",
    "RunnerConfig",
    "is_loopback",
    "parse_labels",
    "resolve_token",
    "websocket_url",
]
