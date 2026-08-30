"""
Conftest for the control-runtime unit tests (Phase 12.3 / 12.2.6).

The in-container control runtime lives at images/base/control (built into
lazyaf-base at /control). It imports nothing from backend/app; these tests
import it as the `control` package by putting images/base on sys.path.

runner-common (the PRODUCER side of the 12.2.6 manifest contract) is put on
sys.path too, so the shared contract test can drive the real pytest plugin
against the real control-runtime validator in ONE process.

Every shared harness piece lives HERE, not copied per module: a change to
the step-config shape or the fake session must be able to break every test
that depends on it at once (the duplicated-copy drift this conftest exists
to prevent).
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGES_BASE = REPO_ROOT / "images" / "base"
RUNNER_COMMON = REPO_ROOT / "runner-common"
for _path in (IMAGES_BASE, RUNNER_COMMON):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class FakeSession:
    """Capturing stand-in for requests.Session (no network, no sleep)."""

    def __init__(self, status_codes=None):
        # status_codes: list consumed per request; last value repeats.
        self.headers = {}
        self.requests = []  # (method, url, json_payload)
        self._status_codes = list(status_codes or [200])

    def request(self, method, url, timeout=None, json=None, **kwargs):
        self.requests.append((method, url, json))
        code = (
            self._status_codes.pop(0)
            if len(self._status_codes) > 1
            else self._status_codes[0]
        )
        return FakeResponse(code)


class SelectiveSession:
    """Fake session that 500s only the endpoints matching ``fail_suffix``.

    Used by the "a broken sub-path is LOUD but never fatal" tests (logs,
    test-results): everything else answers 200 so the step still completes
    and reports a terminal status.
    """

    def __init__(self, fail_suffix: str):
        self.headers = {}
        self.requests = []
        self._fail_suffix = fail_suffix

    def request(self, method, url, timeout=None, json=None, **kwargs):
        self.requests.append((method, url, json))
        return FakeResponse(500 if url.endswith(self._fail_suffix) else 200)


@pytest.fixture
def fake_session_factory(monkeypatch):
    """Patch requests.Session inside the control runtime's backend_client.

    Returns a factory: call with a status-code sequence, get the FakeSession
    every subsequently constructed BackendClient will use.
    """
    from control import backend_client as bc

    def _install(status_codes=None):
        session = FakeSession(status_codes)
        monkeypatch.setattr(bc.requests, "Session", lambda: session)
        return session

    return _install


@pytest.fixture
def selective_session_factory(monkeypatch, fast_retries):
    """Install a SelectiveSession that 500s one endpoint suffix."""
    from control import backend_client as bc

    def _install(fail_suffix):
        session = SelectiveSession(fail_suffix)
        monkeypatch.setattr(bc.requests, "Session", lambda: session)
        return session

    return _install


@pytest.fixture
def fast_retries(monkeypatch):
    """Collapse the client's retry backoff so failure paths run in ms."""
    from control.backend_client import BackendClient

    monkeypatch.setattr(BackendClient, "BASE_BACKOFF", 0.001)
    monkeypatch.setattr(BackendClient, "MAX_BACKOFF", 0.002)
    monkeypatch.setattr(BackendClient, "TOTAL_TIMEOUT", 5.0)
    monkeypatch.setattr(BackendClient, "LOG_TOTAL_TIMEOUT", 5.0)


@pytest.fixture
def quiet_heartbeat(monkeypatch):
    """The interval is a module constant — keep tests heartbeat-quiet."""
    from control import heartbeat as control_heartbeat

    monkeypatch.setattr(control_heartbeat, "HEARTBEAT_INTERVAL", 60.0)


@pytest.fixture
def write_config(tmp_path):
    """Write a per-step config file (contract #1:
    /workspace/.control/<step_execution_id>.json) and return its path.

    ONE definition of the config shape for the whole package — a shape
    change breaks every dependent test, not one of two stale copies.
    """

    def _write(command, **overrides):
        data = {
            "step_id": "exec-1",
            "step_run_id": "sr-1",
            "execution_key": "r:0:sr-1",
            "command": command,
            "backend_url": "http://backend:8000",
            "auth_token": "tok",
            "environment": {},
            "timeout_seconds": 30,
            "working_directory": str(tmp_path),
        }
        data.update(overrides)
        config_file = tmp_path / f"{data['step_id']}.json"
        config_file.write_text(json.dumps(data))
        return config_file

    return _write


@pytest.fixture
def run_main(monkeypatch, fake_session_factory, write_config, quiet_heartbeat):
    """Run control.run.main() for real against a faked requests.Session.

    Returns (exit_code, session, config_file).
    """
    from control import run as control_run

    def _run(command, codes=None, **config_overrides):
        session = fake_session_factory(codes)
        config_file = write_config(command, **config_overrides)
        monkeypatch.setenv("CONFIG_PATH", str(config_file))
        exit_code = control_run.main()
        return exit_code, session, config_file

    return _run


@pytest.fixture
def run_main_with(monkeypatch, write_config, quiet_heartbeat):
    """Like ``run_main`` but against a session the caller already installed
    (e.g. from ``selective_session_factory``)."""
    from control import run as control_run

    def _run(command, **config_overrides):
        config_file = write_config(command, **config_overrides)
        monkeypatch.setenv("CONFIG_PATH", str(config_file))
        return control_run.main(), config_file

    return _run


@pytest.fixture
def posts_to():
    """Filter a fake session's captured requests by URL suffix."""

    def _filter(session, suffix):
        return [p for _m, u, p in session.requests if u.endswith(suffix)]

    return _filter
