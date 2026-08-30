"""Per-runner identity tokens - Phase 12.6 upgrade seam.

12.6's shipped security posture is a SHARED enrollment secret
(``settings.runner_auth_secret``) checked at the WebSocket handshake, plus
duplicate-connection rejection, plus the step gate. A shared secret does not
bind an identity: any holder can claim any ``runner_id``.

The named upgrade is a per-runner JWT - minted on first enrollment, returned
in ``registered``, persisted by the agent, presented on every later connect.
This module ships that mint/verify pair NOW, with tests and with **no caller
on the default path**, so enabling it is a change to
``authenticate_runner_connection`` plus one line in the agent rather than a
design change under time pressure.

That is a deliberate, stated, one-module-with-tests seam (R4) - not a stub
branch. There is no half-wired code path here to mistake for a live one:
nothing in the backend calls these functions, and a grep for either name
finds this file and its tests.

Claims are ``{typ: "runner", sub: <runner_id>, iat, exp}``. ``typ`` is what
keeps a step token from ever being accepted as a runner token and vice
versa - the two secrets may be configured identically in dev, and a token
that authenticates the wrong kind of principal is exactly the confusion this
claim exists to prevent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import jwt

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"

#: Claim value that identifies a RUNNER identity token. A token without it is
#: rejected even when it verifies against the same secret.
TOKEN_TYPE = "runner"

#: Default lifetime: long enough that a runner does not re-enroll on every
#: restart, short enough that a leaked token expires without a revocation
#: list (which 12.6 does not have).
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _secret() -> str:
    """The signing secret, read at CALL time.

    Never captured at import: tests and deployments override
    LAZYAF_RUNNER_AUTH_SECRET, and a module-level snapshot would silently
    keep signing with the dev constant.
    """
    from app.config import get_settings

    return get_settings().runner_auth_secret


def mint_runner_token(runner_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint an identity token for one runner.

    Args:
        runner_id: the runner this token speaks for; lands in ``sub``.
        ttl: lifetime in seconds.

    Returns:
        Encoded HS256 JWT.
    """
    if not runner_id:
        raise ValueError("runner_id is required to mint a runner token")

    now = datetime.utcnow()
    payload = {
        "typ": TOKEN_TYPE,
        "sub": runner_id,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_runner_token(token: str) -> dict | None:
    """Verify a runner identity token.

    Returns the decoded claims, or None for anything that is not a valid,
    unexpired, correctly-typed runner token. Returning None rather than
    raising keeps the caller's auth branch a single truthiness check -
    an exception escaping the handshake is how failure_01 accepted sockets
    it had already decided to refuse.
    """
    if not token:
        return None
    try:
        claims = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.InvalidTokenError:
        return None

    if claims.get("typ") != TOKEN_TYPE:
        # A validly-signed token of the WRONG kind (e.g. a step token minted
        # with an identically-configured secret) is not a runner identity.
        logger.warning(
            "rejecting token with typ=%r as a runner token", claims.get("typ")
        )
        return None
    if not claims.get("sub"):
        return None
    return claims


__all__ = [
    "TOKEN_TYPE",
    "DEFAULT_TTL_SECONDS",
    "mint_runner_token",
    "verify_runner_token",
]
