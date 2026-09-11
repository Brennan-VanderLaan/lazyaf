"""Tests for scripts/preflight.py's port parsing.

`scripts/preflight.py` is the third step of QUICKSTART: it is what tells a new
user whether their machine is ready before they bring the stack up. It had no
test file at all, and that is how the following shipped.

THE REGRESSION THIS FILE EXISTS FOR. The Rung 0 security hardening changed the
shipped defaults in `.env.example` from a bare `8000` / `5173` to
`127.0.0.1:8000` / `127.0.0.1:5173`, because the value is interpolated straight
into a docker compose port mapping and therefore takes the full
`[HOST_IP:]PORT` form docker accepts. `resolve_port` did a bare `int(raw)`, so
the very next thing a correctly-configured user ran said:

    FAIL  LAZYAF_BACKEND_PORT is not a number
          Set it to a port number in .env, or remove it to use 8000.

...and the only way to follow that advice was to strip the `127.0.0.1:`, which
republishes an unauthenticated API on every interface. A preflight that fails a
good config is bad; one that then talks the user into a worse one is the R1
failure in its purest form, and it was invisible because nothing here ran.

So the assertions below are mostly about the forms a real `.env` can hold,
including the two the project itself ships.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "preflight.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


@pytest.fixture(scope="module")
def preflight():
    """Import the script as a module.

    It is stdlib-only by design (it runs before anything is installed), so a
    plain spec load is enough and needs no environment.
    """
    spec = importlib.util.spec_from_file_location("lazyaf_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("lazyaf_preflight", module)
    spec.loader.exec_module(module)
    return module


def resolve(preflight, raw, capsys):
    """`resolve_port` for one raw value, with its report output discarded."""
    values = {} if raw is None else {"LAZYAF_BACKEND_PORT": raw}
    result = preflight.resolve_port(values, "LAZYAF_BACKEND_PORT", 8000, "backend API")
    capsys.readouterr()
    return result


class TestTheFormsAValidValueTakes:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, 8000),
            ("", 8000),
            ("   ", 8000),
            ("8000", 8000),
            ("9001", 9001),
            # The shipped defaults. These are THE regression cases.
            ("127.0.0.1:8000", 8000),
            ("127.0.0.1:5173", 5173),
            # Deliberately widening it is legal and must still parse.
            ("0.0.0.0:8000", 8000),
            ("192.168.1.10:8080", 8080),
        ],
    )
    def test_it_resolves(self, preflight, capsys, raw, expected):
        assert resolve(preflight, raw, capsys) == expected

    def test_the_shipped_env_example_values_parse(self, preflight, capsys):
        """Read from `.env.example` itself, so the two cannot drift apart.

        A hardcoded "127.0.0.1:8000" here would keep passing after someone
        changed the template; this fails instead, which is the point.
        """
        shipped = {}
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LAZYAF_BACKEND_PORT=") or line.startswith(
                "LAZYAF_FRONTEND_PORT="
            ):
                name, _, value = line.partition("=")
                shipped[name] = value

        assert set(shipped) == {"LAZYAF_BACKEND_PORT", "LAZYAF_FRONTEND_PORT"}, (
            "the port variables left .env.example, or were commented out - "
            "preflight reads them, so this test needs updating deliberately"
        )
        for name, value in shipped.items():
            port = preflight.resolve_port(shipped, name, 8000, "check")
            capsys.readouterr()
            assert port is not None, (
                f"{name}={value!r} is what the project ships, and preflight "
                f"refuses it - a new user's third command fails on a correct config"
            )
            assert 1 <= port <= 65535


class TestTheFormsThatAreGenuinelyWrong:
    @pytest.mark.parametrize("raw", ["banana", "127.0.0.1:nope", "127.0.0.1:", ":", "80 00"])
    def test_it_refuses(self, preflight, capsys, raw):
        assert resolve(preflight, raw, capsys) is None

    @pytest.mark.parametrize("raw", ["0", "65536", "127.0.0.1:0", "-1"])
    def test_out_of_range_is_refused(self, preflight, capsys, raw):
        assert resolve(preflight, raw, capsys) is None

    def test_the_refusal_does_not_advise_removing_the_host_ip(self, preflight, capsys):
        """The remedy must not be "make it a bare port number".

        That was the old advice, and following it republishes the API on every
        interface. Whatever the message says now, it must offer the
        `HOST_IP:PORT` form and must not tell the user their only option is a
        bare number.
        """
        resolve(preflight, "banana", capsys)  # prime, then re-run captured
        preflight.resolve_port(
            {"LAZYAF_BACKEND_PORT": "banana"}, "LAZYAF_BACKEND_PORT", 8000, "backend API"
        )
        out = capsys.readouterr().out

        assert "HOST_IP:PORT" in out or "0.0.0.0:" in out, (
            "the refusal does not tell the user the host-IP form is allowed: " + out
        )
        assert "127.0.0.1" in out, (
            "the refusal does not name the shipped loopback default, so a user "
            "fixing it has no reason to keep the binding narrow: " + out
        )
