"""The shared auth secrets are generated, never defaulted - Phase 12.7.

WHAT BROKE (found by the release verifier)
    ``LAZYAF_STEP_AUTH_SECRET`` and ``LAZYAF_RUNNER_AUTH_SECRET`` fell back to
    constants written into backend/app/config.py. A constant in a public
    repository is not a secret: the first mints the JWTs the control layer
    trusts on /api/steps/*, the second enrols runner agents. On a laptop bound
    to localhost that is harmless. The moment a stranger runs the release
    stack it is a remote credential-minting oracle with a published key.

WHAT THIS FILE PINS
    1. No usable default. Absent -> the process refuses to start.
    2. The failure NAMES the variable and says how to fix it.
    3. The retired public constants are treated as ABSENT, so an inherited
       .env fails loudly instead of quietly keeping the hole open.
    4. ``<NAME>_FILE`` is read, and beats the inline variable (docker secrets,
       kubernetes mounted Secrets).
    5. Fail-closed is the DEFAULT; the ephemeral generator is opt-in behind an
       explicit flag and warns about exactly what it costs.
    6. No secret value ever reaches a log record.
"""
import logging

import pytest

from app.config import (
    DEV_EPHEMERAL_FLAG,
    MissingSecretError,
    RETIRED_PUBLIC_SECRETS,
    SECRET_PURPOSES,
    get_settings,
    is_placeholder_secret,
    reset_ephemeral_secrets,
    resolve_secret,
)

STEP = "LAZYAF_STEP_AUTH_SECRET"
RUNNER = "LAZYAF_RUNNER_AUTH_SECRET"

#: Long enough not to trip the short-value warning, so a test asserting on log
#: records is asserting about the thing it named.
REAL = "H8sQ2vN4pLxT7yRbF1mKcW3jZaE6dUgV9oInBhSlYtQrXwMz"


@pytest.fixture(autouse=True)
def _clean_ephemerals():
    """Ephemerals are process-cached on purpose; don't leak between tests."""
    reset_ephemeral_secrets()
    yield
    reset_ephemeral_secrets()


# ---------------------------------------------------------------------------
# 1 + 2: no default, and the failure carries the fix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", [STEP, RUNNER])
def test_unset_secret_fails_closed(var):
    with pytest.raises(MissingSecretError) as excinfo:
        resolve_secret(var, env={})
    assert excinfo.value.var_name == var


@pytest.mark.parametrize("var", [STEP, RUNNER])
def test_failure_names_the_variable_and_every_way_to_fix_it(var):
    with pytest.raises(MissingSecretError) as excinfo:
        resolve_secret(var, env={})
    message = str(excinfo.value)

    assert var in message, "the message must name the variable it is about"
    assert f"{var}_FILE" in message, "the docker/k8s route must be spelled out"
    assert "scripts/bootstrap_secrets.py" in message, "the dev route must be spelled out"
    assert "token_urlsafe" in message, "the manual route must be spelled out"
    assert DEV_EPHEMERAL_FLAG in message, "the escape hatch must be discoverable"
    # ...and it must say WHAT the secret does, not just that it is missing.
    assert SECRET_PURPOSES[var].split()[2] in message


@pytest.mark.parametrize("var", [STEP, RUNNER])
def test_empty_string_is_not_a_value(var):
    with pytest.raises(MissingSecretError):
        resolve_secret(var, env={var: "   "})


@pytest.mark.parametrize("retired", sorted(RETIRED_PUBLIC_SECRETS))
def test_the_retired_public_constants_count_as_unset(retired):
    """An inherited .env must not keep the hole open silently."""
    with pytest.raises(MissingSecretError) as excinfo:
        resolve_secret(STEP, env={STEP: retired})
    message = str(excinfo.value)
    assert "published" in message
    assert "retired public development default" in message


def test_obvious_placeholders_count_as_unset():
    for placeholder in ("changeme", "CHANGE-ME", "TODO", "xxxxxxxx", "<generate>"):
        assert is_placeholder_secret(placeholder), placeholder
        with pytest.raises(MissingSecretError):
            resolve_secret(STEP, env={STEP: placeholder})


def test_a_real_value_is_returned_unchanged():
    assert resolve_secret(STEP, env={STEP: f"  {REAL}  "}) == REAL


# ---------------------------------------------------------------------------
# 4: the *_FILE convention
# ---------------------------------------------------------------------------

def test_secret_file_is_read(tmp_path):
    path = tmp_path / "step_secret"
    path.write_text(REAL + "\n", encoding="utf-8")
    assert resolve_secret(STEP, env={f"{STEP}_FILE": str(path)}) == REAL


def test_secret_file_takes_precedence_over_the_inline_value(tmp_path):
    """A mounted Secret must win, or a stale inline value silently shadows it."""
    path = tmp_path / "step_secret"
    path.write_text("from-the-mounted-file-and-long-enough-to-be-plausible", encoding="utf-8")
    resolved = resolve_secret(
        STEP,
        env={f"{STEP}_FILE": str(path), STEP: REAL},
    )
    assert resolved == "from-the-mounted-file-and-long-enough-to-be-plausible"


def test_a_missing_secret_file_is_an_error_not_a_fallback(tmp_path):
    """Pointing at a path is a statement about where the secret lives.

    Falling through to the inline value would turn a broken volume mount into
    "it works on my machine, and signs with the wrong key on yours".
    """
    with pytest.raises(MissingSecretError) as excinfo:
        resolve_secret(
            STEP,
            env={f"{STEP}_FILE": str(tmp_path / "absent"), STEP: REAL},
        )
    assert f"{STEP}_FILE" in str(excinfo.value)


def test_an_empty_secret_file_is_an_error(tmp_path):
    path = tmp_path / "empty"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(MissingSecretError):
        resolve_secret(STEP, env={f"{STEP}_FILE": str(path)})


def test_a_broken_secret_file_is_an_error_even_with_the_dev_flag(tmp_path):
    """The dev flag rescues UNSET, not MISCONFIGURED."""
    with pytest.raises(MissingSecretError):
        resolve_secret(
            STEP,
            env={f"{STEP}_FILE": str(tmp_path / "absent"), DEV_EPHEMERAL_FLAG: "1"},
        )


# ---------------------------------------------------------------------------
# 5: the opt-in ephemeral path
# ---------------------------------------------------------------------------

def test_dev_flag_generates_a_strong_ephemeral_value():
    value = resolve_secret(STEP, env={DEV_EPHEMERAL_FLAG: "1"})
    assert len(value) >= 43, "token_urlsafe(48) is 64 chars; anything short is a bug"
    assert value not in RETIRED_PUBLIC_SECRETS


def test_ephemeral_value_is_stable_within_the_process():
    """A per-CALL value would invalidate the token it just minted."""
    env = {DEV_EPHEMERAL_FLAG: "1"}
    assert resolve_secret(STEP, env=env) == resolve_secret(STEP, env=env)


def test_ephemeral_values_differ_per_variable():
    env = {DEV_EPHEMERAL_FLAG: "1"}
    assert resolve_secret(STEP, env=env) != resolve_secret(RUNNER, env=env)


def test_ephemeral_generation_warns_about_what_it_costs(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        resolve_secret(STEP, env={DEV_EPHEMERAL_FLAG: "1"})
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "generating a secret out of thin air must be loud"
    text = " ".join(r.getMessage() for r in warnings)
    assert "EPHEMERAL" in text
    assert "restart" in text, "must say tokens do not survive a restart"
    assert "runner agent" in text, "must say remote runners cannot authenticate"


def test_the_flag_must_be_opted_into_explicitly():
    """Fail-closed is the default. Only truthy spellings turn it off."""
    for falsey in ("", "0", "false", "no", "off"):
        with pytest.raises(MissingSecretError):
            resolve_secret(STEP, env={DEV_EPHEMERAL_FLAG: falsey})
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        reset_ephemeral_secrets()
        assert resolve_secret(STEP, env={DEV_EPHEMERAL_FLAG: truthy})


# ---------------------------------------------------------------------------
# 6: nothing logs a secret
# ---------------------------------------------------------------------------

def test_a_short_secret_warns_without_printing_it(caplog):
    short = "hunter2"
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert resolve_secret(STEP, env={STEP: short}) == short
    text = " ".join(r.getMessage() for r in caplog.records)
    assert short not in text, "the warning about a weak secret must not quote it"
    assert str(len(short)) in text, "it should say how short, which is not the value"


def test_no_secret_value_appears_in_any_message_or_log(caplog, tmp_path):
    """One sentinel, every path: file, inline, failure text, ephemeral."""
    sentinel = "SENTINEL-STEP-SECRET-DO-NOT-LEAK-0123456789abcdef"
    path = tmp_path / "secret"
    path.write_text(sentinel, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        assert resolve_secret(STEP, env={f"{STEP}_FILE": str(path)}) == sentinel
        assert resolve_secret(RUNNER, env={RUNNER: sentinel}) == sentinel
        try:
            resolve_secret(STEP, env={STEP: sentinel[:5]})
        except MissingSecretError as exc:  # pragma: no cover - not expected
            assert sentinel not in str(exc)

    emitted = " ".join(record.getMessage() for record in caplog.records)
    assert sentinel not in emitted


def test_the_failure_message_never_echoes_the_rejected_value():
    """A placeholder is still something the operator typed. Do not repeat it."""
    with pytest.raises(MissingSecretError) as excinfo:
        resolve_secret(STEP, env={STEP: "changeme"})
    assert "changeme" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Settings-level wiring: this is what actually stops the process
# ---------------------------------------------------------------------------

class TestGetSettings:
    @pytest.fixture(autouse=True)
    def _isolated_settings(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.delenv(STEP, raising=False)
        monkeypatch.delenv(RUNNER, raising=False)
        monkeypatch.delenv(f"{STEP}_FILE", raising=False)
        monkeypatch.delenv(f"{RUNNER}_FILE", raising=False)
        monkeypatch.delenv(DEV_EPHEMERAL_FLAG, raising=False)
        yield
        get_settings.cache_clear()

    def test_backend_refuses_to_build_settings_without_the_step_secret(self, monkeypatch):
        monkeypatch.setenv(RUNNER, REAL)
        with pytest.raises(MissingSecretError) as excinfo:
            get_settings()
        assert excinfo.value.var_name == STEP

    def test_backend_refuses_to_build_settings_without_the_runner_secret(self, monkeypatch):
        monkeypatch.setenv(STEP, REAL)
        with pytest.raises(MissingSecretError) as excinfo:
            get_settings()
        assert excinfo.value.var_name == RUNNER

    def test_both_set_produces_usable_settings(self, monkeypatch):
        monkeypatch.setenv(STEP, REAL)
        monkeypatch.setenv(RUNNER, REAL + "-runner")
        settings = get_settings()
        assert settings.step_auth_secret == REAL
        assert settings.runner_auth_secret == REAL + "-runner"

    def test_file_convention_works_end_to_end(self, monkeypatch, tmp_path):
        step_file = tmp_path / "step"
        runner_file = tmp_path / "runner"
        step_file.write_text(REAL + "\n", encoding="utf-8")
        runner_file.write_text(REAL + "-runner\n", encoding="utf-8")
        monkeypatch.setenv(f"{STEP}_FILE", str(step_file))
        monkeypatch.setenv(f"{RUNNER}_FILE", str(runner_file))
        # Stale inline values that the mounted files must beat.
        monkeypatch.setenv(STEP, "stale-inline-step-value-long-enough-to-pass")
        monkeypatch.setenv(RUNNER, "stale-inline-runner-value-long-enough-pass")

        settings = get_settings()
        assert settings.step_auth_secret == REAL
        assert settings.runner_auth_secret == REAL + "-runner"

    def test_dev_flag_boots_with_generated_values(self, monkeypatch):
        monkeypatch.setenv(DEV_EPHEMERAL_FLAG, "1")
        settings = get_settings()
        assert settings.step_auth_secret
        assert settings.runner_auth_secret
        assert settings.step_auth_secret != settings.runner_auth_secret


def test_no_public_constant_survives_in_the_config_module():
    """A regression guard with teeth: grep the source, not the behaviour.

    Behaviour tests pass just as happily against a module that still holds the
    constant but stopped USING it, and the next person to add a fallback would
    reach for exactly that.
    """
    import app.config as config_module
    from pathlib import Path

    source = Path(config_module.__file__).read_text(encoding="utf-8")
    for retired in RETIRED_PUBLIC_SECRETS:
        # It appears once, in RETIRED_PUBLIC_SECRETS, as a value to REJECT.
        assert source.count(retired) == 1, (
            f"{retired!r} appears more than once in config.py - it must exist "
            "only in the rejection list, never as a fallback"
        )
    assert 'step_auth_secret: str\n' in source, "step_auth_secret must be required"
    assert 'runner_auth_secret: str\n' in source, "runner_auth_secret must be required"
