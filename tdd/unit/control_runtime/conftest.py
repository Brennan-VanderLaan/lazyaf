"""
Conftest for the control-runtime unit tests (Phase 12.3).

The in-container control runtime lives at images/base/control (built into
lazyaf-base at /control). It imports nothing from backend/app; these tests
import it as the `control` package by putting images/base on sys.path.
"""
import sys
from pathlib import Path

import pytest

IMAGES_BASE = Path(__file__).resolve().parents[3] / "images" / "base"
if str(IMAGES_BASE) not in sys.path:
    sys.path.insert(0, str(IMAGES_BASE))


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
def fast_retries(monkeypatch):
    """Collapse the client's retry backoff so failure paths run in ms."""
    from control.backend_client import BackendClient

    monkeypatch.setattr(BackendClient, "BASE_BACKOFF", 0.001)
    monkeypatch.setattr(BackendClient, "MAX_BACKOFF", 0.002)
    monkeypatch.setattr(BackendClient, "TOTAL_TIMEOUT", 5.0)
    monkeypatch.setattr(BackendClient, "LOG_TOTAL_TIMEOUT", 5.0)
