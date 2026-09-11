"""Every env var the backend READS must be something an operator can SET.

THE REGRESSION THIS FILE EXISTS FOR. The Rung 0 hardening added
`LAZYAF_STEP_BIND_ALLOWLIST`: the opt-in that lets a pipeline step bind-mount
the host docker socket via `needs: [docker]`. It was documented in
`.env.example`, read by `config.get_settings()` through `os.getenv`, and
refused with a message naming the variable...

...and it was forwarded by NO compose file. `app.config.Settings` is a plain
pydantic `BaseModel`, not `BaseSettings`, so there is no `env_file` magic to
fall back on, and every compose service enumerates its environment explicitly.
The variable therefore never reached the backend process. The result was the
worst shape an error can take: `needs: [docker]` failed unconditionally, and
the failure told the operator to set something that could not be set. LazyAF's
own dogfood pipeline is a `needs: [docker]` user, so this dark-by-omission
setting took the project's CI with it.

The general rule, which is what is actually tested here: a setting wired from
`os.getenv` in `get_settings()` is part of the operator's interface. If the
backend reads it, a compose service must pass it through, or it does not exist
as far as a deployed install is concerned (R1 - nothing dark).

Compose files are parsed as text rather than YAML: the assertion is about the
variable being present and interpolated from the host environment, and a YAML
load would need a parser this stdlib-only corner of the suite does not have.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PY = REPO_ROOT / "backend" / "app" / "config.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Compose files that run a BACKEND. The e2e backend in docker-compose.yml is
#: a second service in the same file, which is why occurrence counts are not
#: asserted - presence per file is the contract.
BACKEND_COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.release.yml",
    "docker-compose.qa.yml",
)

#: Read by `get_settings()` but deliberately NOT an operator knob, with the
#: reason. Anything here is excluded from the sweep; adding an entry is a
#: decision, which is the point of making it explicit.
NOT_OPERATOR_FACING = {
    # Set by the test harness, never by a deployment.
    "LAZYAF_DEV_EPHEMERAL_SECRETS": "development convenience, refuses to be used for real",
    "LAZYAF_TEST_MODE": "test-only router gate",
}


def _settings_env_vars() -> set[str]:
    """Every LAZYAF_* name `config.py` reads out of the process environment."""
    source = CONFIG_PY.read_text(encoding="utf-8")
    names = set(re.findall(r'os\.getenv\(\s*["\'](LAZYAF_[A-Z0-9_]+)["\']', source))
    names |= set(re.findall(r'resolve_secret\(\s*["\'](LAZYAF_[A-Z0-9_]+)["\']', source))
    return names


def test_the_scan_actually_finds_something():
    """A regex that silently matches nothing would make every test below pass."""
    found = _settings_env_vars()
    assert len(found) >= 3, (
        "the config scan found almost nothing - the pattern has probably "
        f"drifted from how config.py reads env vars. Found: {sorted(found)}"
    )


def test_the_bind_allowlist_is_reachable_in_every_backend_compose():
    """The specific regression, named so a re-break is unambiguous."""
    missing = [
        name
        for name in BACKEND_COMPOSE_FILES
        if "LAZYAF_STEP_BIND_ALLOWLIST" not in (REPO_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not missing, (
        "LAZYAF_STEP_BIND_ALLOWLIST is read by the backend and documented in "
        f".env.example, but {missing} do not forward it. `needs: [docker]` then "
        "fails unconditionally with an error naming a setting the operator has "
        "no way to apply - including for this repo's own dogfood pipeline."
    )


@pytest.mark.parametrize("compose_file", BACKEND_COMPOSE_FILES)
def test_the_forwarded_value_comes_from_the_host_environment(compose_file):
    """`- VAR=${VAR:-}`, not a hardcoded value baked into the compose file."""
    text = (REPO_ROOT / compose_file).read_text(encoding="utf-8")
    for line in text.splitlines():
        if "LAZYAF_STEP_BIND_ALLOWLIST" in line and not line.strip().startswith("#"):
            assert "${LAZYAF_STEP_BIND_ALLOWLIST" in line, (
                f"{compose_file} pins a literal value instead of passing the "
                f"operator's through: {line.strip()!r}"
            )
            return
    pytest.fail(f"{compose_file} does not forward LAZYAF_STEP_BIND_ALLOWLIST")


def test_every_operator_facing_setting_reaches_a_backend_container():
    """The general sweep the specific test above is one instance of."""
    read_by_backend = _settings_env_vars() - set(NOT_OPERATOR_FACING)
    composes = {
        name: (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in BACKEND_COMPOSE_FILES
    }

    unreachable = sorted(
        name
        for name in read_by_backend
        if not any(name in text for text in composes.values())
    )
    assert not unreachable, (
        "these are read by backend/app/config.py but forwarded by NO compose "
        f"file, so a deployed operator cannot set them: {unreachable}. Either "
        "forward them in the backend service's `environment:` block, or add "
        "them to NOT_OPERATOR_FACING here with the reason."
    )


def test_the_bind_allowlist_is_documented_where_an_operator_will_look():
    """It is opt-in, so the template is the only place they will learn it."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "LAZYAF_STEP_BIND_ALLOWLIST" in text, (
        ".env.example does not mention LAZYAF_STEP_BIND_ALLOWLIST; an opt-in "
        "nobody is told about is the same as no opt-in"
    )
