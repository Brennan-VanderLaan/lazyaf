"""Per-runner identity tokens (Phase 12.6 upgrade seam).

`runner_token.py` ships with tests and NO caller on the default path, so
turning per-runner JWTs on is a config flip rather than a design change under
time pressure. These tests are what make that a real seam instead of dead
code nobody dares enable: the mint/verify pair is proven now, while the
shared-enrollment-secret posture is still what ships.

The `typ` claim is the sharp edge here. Step tokens and runner tokens are
both HS256 and may be configured with the same secret in dev; a token that
authenticates the wrong KIND of principal is exactly the confusion the claim
exists to prevent.
"""
import sys
import time
from pathlib import Path

import jwt
import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.config import get_settings
from app.services.control_layer.auth import generate_step_token
from app.services.execution.runner_token import (
    DEFAULT_TTL_SECONDS,
    TOKEN_TYPE,
    mint_runner_token,
    verify_runner_token,
)


class TestMint:
    def test_mint_returns_a_verifiable_token(self):
        claims = verify_runner_token(mint_runner_token("pi-workshop-1"))
        assert claims is not None
        assert claims["sub"] == "pi-workshop-1"
        assert claims["typ"] == TOKEN_TYPE

    def test_mint_requires_a_runner_id(self):
        with pytest.raises(ValueError, match="runner_id"):
            mint_runner_token("")

    def test_default_ttl_is_generous_but_finite(self):
        """Long enough not to re-enroll on every restart, short enough that a
        leaked token expires without a revocation list (12.6 has none)."""
        assert 0 < DEFAULT_TTL_SECONDS <= 90 * 24 * 3600

    def test_token_carries_iat_and_exp(self):
        claims = verify_runner_token(mint_runner_token("r1", ttl=60))
        assert claims["exp"] - claims["iat"] == 60


class TestVerify:
    def test_empty_token_is_rejected(self):
        assert verify_runner_token("") is None

    def test_garbage_is_rejected(self):
        assert verify_runner_token("not-a-jwt") is None

    def test_expired_token_is_rejected(self):
        token = mint_runner_token("r1", ttl=-1)
        time.sleep(0.01)
        assert verify_runner_token(token) is None

    def test_token_signed_with_another_secret_is_rejected(self):
        forged = jwt.encode(
            {"typ": TOKEN_TYPE, "sub": "r1", "iat": 0, "exp": 9999999999},
            "some-other-secret",
            algorithm="HS256",
        )
        assert verify_runner_token(forged) is None

    def test_a_step_token_is_not_a_runner_token(self):
        """Validly signed, wrong kind: refused. The two token families must
        never be interchangeable even when the secrets match."""
        step_token = generate_step_token(step_id="s1", execution_key="k")
        assert verify_runner_token(step_token) is None

    def test_a_correctly_signed_token_with_the_wrong_typ_is_rejected(self):
        forged = jwt.encode(
            {"typ": "step", "sub": "r1", "exp": 9999999999},
            get_settings().runner_auth_secret,
            algorithm="HS256",
        )
        assert verify_runner_token(forged) is None

    def test_a_token_without_a_subject_is_rejected(self):
        forged = jwt.encode(
            {"typ": TOKEN_TYPE, "exp": 9999999999},
            get_settings().runner_auth_secret,
            algorithm="HS256",
        )
        assert verify_runner_token(forged) is None

    def test_verify_never_raises(self):
        """Returning None rather than raising keeps the caller's auth branch a
        single truthiness check - an exception escaping the handshake is how
        failure_01 accepted sockets it had already decided to refuse."""
        for candidate in ("", "a.b.c", "x" * 500, "..", "e30=.e30=.x"):
            assert verify_runner_token(candidate) is None


class TestSeamIsNotWired:
    def test_no_default_path_caller_exists(self):
        """R4: this is a stated one-module seam, not a stub branch. If a
        caller appears, it is a deliberate posture change and this test is
        the place that says so."""
        import re
        from pathlib import Path as _Path

        app_dir = _Path(backend_path) / "app"
        pattern = re.compile(r"\b(mint_runner_token|verify_runner_token)\b")
        offenders = []
        for path in app_dir.rglob("*.py"):
            if path.name == "runner_token.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path))
        assert offenders == [], (
            "runner_token is wired into the default path: "
            f"{offenders}. Enabling per-runner identity is a security posture "
            "change - update this test deliberately, do not delete it."
        )


class TestSecretIsReadAtCallTime:
    def test_secret_is_not_captured_at_import(self, monkeypatch):
        """Tests and deployments override LAZYAF_RUNNER_AUTH_SECRET; a
        module-level snapshot would silently keep signing with the dev
        constant."""
        token = mint_runner_token("r1")
        assert verify_runner_token(token) is not None

        get_settings.cache_clear()
        monkeypatch.setenv("LAZYAF_RUNNER_AUTH_SECRET", "a-different-secret")
        try:
            # The old token no longer verifies under the new secret...
            assert verify_runner_token(token) is None
            # ...and a freshly minted one does.
            assert verify_runner_token(mint_runner_token("r1")) is not None
        finally:
            monkeypatch.delenv("LAZYAF_RUNNER_AUTH_SECRET", raising=False)
            get_settings.cache_clear()
