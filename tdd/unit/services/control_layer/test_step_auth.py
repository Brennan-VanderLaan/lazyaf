

class TestNoDefaultSigningSecret:
    """The step-JWT secret must never have a usable default.

    It was a module-level constant with a public value, replaced only when
    main.py's lifespan called set_secret_key() - so any process that imported
    this module without running the lifespan signed AND accepted /api/steps/*
    credentials with a key published in git history. set_secret_key() had no
    test coverage, so removing that call would have been silent. These tests
    make both properties load-bearing.
    """

    def test_module_has_no_hardcoded_secret(self):
        import inspect

        from app.services.control_layer import auth

        source = inspect.getsource(auth)
        assert "change-in-production" not in source
        assert "_SECRET_KEY =" not in source

    def test_signing_reads_settings_at_call_time(self, monkeypatch):
        """Changing the configured secret changes the signature immediately,
        with no re-import and no set_secret_key() call."""
        from app.services.control_layer import auth

        monkeypatch.setattr(auth, "_SECRET_OVERRIDE", None)

        class _S:
            step_auth_secret = "secret-one"

        holder = {"s": _S()}
        monkeypatch.setattr("app.config.get_settings", lambda: holder["s"])
        first = auth.generate_step_token("s1", "k1")

        class _S2:
            step_auth_secret = "secret-two"

        holder["s"] = _S2()
        second = auth.generate_step_token("s1", "k1")

        assert first != second, "signature did not follow the configured secret"

    def test_a_token_signed_with_the_retired_constant_is_rejected(self, monkeypatch):
        import jwt

        from app.services.control_layer import auth

        monkeypatch.setattr(auth, "_SECRET_OVERRIDE", None)

        class _S:
            step_auth_secret = "a-real-configured-secret"

        monkeypatch.setattr("app.config.get_settings", lambda: _S())

        from datetime import datetime, timedelta

        forged = jwt.encode(
            {
                "step_id": "s1",
                "execution_key": "k1",
                "iat": datetime.utcnow(),
                "exp": datetime.utcnow() + timedelta(seconds=60),
            },
            "lazyaf-step-auth-secret-key-change-in-production",
            algorithm="HS256",
        )
        assert auth.validate_step_token(forged, "s1") is False
