"""Standalone HTTP helpers for the QA-5 (UI workflow abuse) lane.

Deliberately self-contained: ``tdd/qa/conftest.py`` is shared with other QA
lanes that rewrite it, and these tests must not break when it changes. They
talk to a RUNNING backend over HTTP only and never import backend code, so
they stay runnable while the source tree is being edited.

    QA_BASE_URL=http://localhost:8790   (default)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

import pytest

BASE_URL = os.environ.get(
    "QA_BASE_URL", os.environ.get("LAZYAF_QA_BASE_URL", "http://localhost:8790")
).rstrip("/")


def api(method: str, path: str, body=None, timeout: float = 60.0):
    """Call the backend. Returns ``(status, parsed_body_or_text)``.

    Never raises on a non-2xx — the status code is usually the thing under
    test. Skips (rather than fails) when the stack is unreachable, so this
    lane is a no-op on a machine with no QA sandbox running.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode(errors="replace")
    except urllib.error.URLError as exc:
        pytest.skip(f"QA backend unreachable at {BASE_URL}: {exc}")


def require_backend():
    try:
        with urllib.request.urlopen(BASE_URL + "/health", timeout=15) as resp:
            if resp.status != 200:
                pytest.skip(f"QA backend unhealthy at {BASE_URL}")
    except Exception as exc:  # noqa: BLE001 — any failure means "no target"
        pytest.skip(f"QA backend unreachable at {BASE_URL}: {exc}")


def make_repo(prefix: str = "qa5"):
    """Create a uniquely named throwaway repo.

    The QA sandbox is shared and peers call ``/api/test/reset`` at will, so
    every test materializes its own repo rather than reusing one.
    """
    require_backend()
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    status, body = api("POST", "/api/repos", {"name": name, "default_branch": "main"})
    if status >= 400 or not isinstance(body, dict):
        pytest.skip(f"could not create repo on QA stack: {status} {body!r}")
    return body


def make_card(repo_id: str, title: str = "qa5 regression card"):
    status, body = api(
        "POST",
        f"/api/repos/{repo_id}/cards",
        {"title": title, "description": "created by tdd/qa (QA-5 lane)"},
    )
    if status >= 400 or not isinstance(body, dict):
        pytest.skip(f"could not create card on QA stack: {status} {body!r}")
    return body


def drop_repo(repo_id: str):
    api("DELETE", f"/api/repos/{repo_id}")
