"""Standalone HTTP + concurrency helpers for the QA-3 lane (races).

QA-3 hunts lost updates, duplicate side effects, and teardown races. Every
helper here speaks plain HTTP to a RUNNING backend and never imports backend
code, so the lane stays runnable while the source tree is being edited.

    LAZYAF_QA_BASE_URL=http://localhost:8790   (default)

Deliberately self-contained (same discipline as ``qa5_http.py``): the shared
``tdd/qa/conftest.py`` belongs to another lane and may be rewritten.

Concurrency is done with THREADS behind a ``threading.Barrier`` rather than
asyncio, because ``tdd/qa/pytest.ini`` does not enable pytest-asyncio. A
barrier is the point: requests that merely "go out quickly" do not reproduce
a TOCTOU window, requests released in the same instant do.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

BASE_URL = os.environ.get(
    "LAZYAF_QA_BASE_URL", os.environ.get("QA_BASE_URL", "http://localhost:8790")
).rstrip("/")

#: Repo these tests ingest. Stable so a sibling lane's reset just recreates it.
QA_REPO_NAME = "qa3-races"

#: Terminal pipeline-run statuses.
TERMINAL = frozenset({"passed", "failed", "cancelled", "error", "success"})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def api(method: str, path: str, body=None, timeout: float = 60.0):
    """Call the backend. Returns ``(status, parsed_body_or_text)``.

    Never raises on a non-2xx - the status code is usually the subject of the
    assertion. Skips (rather than fails) when the stack is unreachable, so
    this lane is a no-op on a machine with no QA sandbox running.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            try:
                return response.status, json.loads(payload)
            except ValueError:
                return response.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, payload.decode("utf-8", "replace")
    except urllib.error.URLError as exc:  # pragma: no cover - env dependent
        pytest.skip(f"QA stack at {BASE_URL} is unreachable: {exc}")


def require_stack():
    """Skip the whole module when the QA sandbox is not answering."""
    try:
        status, _ = api("GET", "/health", timeout=10)
    except Exception as exc:  # pragma: no cover - env dependent
        pytest.skip(f"QA stack at {BASE_URL} is unreachable: {exc}")
    if status != 200:
        pytest.skip(f"QA stack at {BASE_URL} is not healthy (status {status})")


# ---------------------------------------------------------------------------
# Simultaneity
# ---------------------------------------------------------------------------

def fire_together(count: int, call):
    """Run ``call(i)`` ``count`` times, all released from one barrier.

    Returns the list of results in submission order. An exception raised by a
    worker is returned in its slot rather than propagated, so a transport-level
    failure is data the test can assert on.
    """
    barrier = threading.Barrier(count, timeout=60)

    def worker(index):
        barrier.wait()
        try:
            return call(index)
        except Exception as exc:  # noqa: BLE001 - the exception IS the result
            return exc

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(worker, range(count)))


def status_counts(results):
    """Summarise fire_together results as {status_or_exception_name: n}."""
    counts = {}
    for result in results:
        if isinstance(result, Exception):
            key = type(result).__name__
        elif isinstance(result, tuple):
            key = result[0]
        else:  # pragma: no cover - defensive
            key = repr(result)
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Fixtures-as-functions (this module is imported, not a conftest)
# ---------------------------------------------------------------------------

def ensure_repo() -> str:
    """An ingested repo id with a real initial commit.

    Uses ``POST /api/test/seed`` because the seeded repo already has a commit
    on ``main`` (workspace population fails against an empty repo). The QA
    sandbox is shared and any lane may reset it, so this is called per test
    rather than cached.
    """
    status, body = api("POST", "/api/test/seed", timeout=90)
    if status != 200:
        pytest.skip(f"could not seed the QA stack: {status} {body}")
    return body["repo"]["id"]


def make_card(repo_id: str, title: str = "qa3-card", **extra) -> str:
    payload = {"title": title, "description": "", "step_type": "agent"}
    payload.update(extra)
    status, body = api("POST", f"/api/repos/{repo_id}/cards", payload)
    assert status == 201, f"card create failed: {status} {body}"
    return body["id"]


def graph_pipeline(repo_id: str, configs: list[dict], name_prefix: str = "qa3") -> str:
    """Create a v2 graph pipeline whose steps are ALL parallel entry points."""
    steps = {
        f"s{i}": {"id": f"s{i}", "name": f"S{i}", "type": "script", "config": config}
        for i, config in enumerate(configs)
    }
    payload = {
        "name": f"{name_prefix}-{uuid.uuid4().hex[:8]}",
        "description": "QA-3 race probe",
        # No `steps` key. 12.8 §4.4 makes a body carrying BOTH definitions a
        # 422 - an empty array squeaks through only because it is falsy, and
        # a create that says it authors an array when it authors a graph is
        # the ambiguity the rule exists to remove.
        "steps_graph": {
            "steps": steps,
            "edges": [],
            "entry_points": [f"s{i}" for i in range(len(configs))],
            "version": 2,
        },
        "triggers": [],
    }
    status, body = api("POST", f"/api/repos/{repo_id}/pipelines", payload)
    assert status == 201, f"pipeline create failed: {status} {body}"
    return body["id"]


def start_run(pipeline_id: str, body: dict | None = None) -> str:
    status, payload = api("POST", f"/api/pipelines/{pipeline_id}/run", body or {})
    assert status == 200, f"run start failed: {status} {payload}"
    return payload["id"]


def get_run(run_id: str):
    return api("GET", f"/api/pipeline-runs/{run_id}")


def wait_terminal(run_id: str, timeout: float = 180.0, interval: float = 1.0):
    """Poll a run until it reaches a terminal status; return the final body.

    Returns ``None`` if the run row disappeared (a sibling lane reset the
    stack), which callers treat as "skip, not fail".
    """
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        status, body = get_run(run_id)
        if status == 404:
            return None
        if status == 200 and body.get("status") in TERMINAL:
            return body
        time.sleep(interval)
    return body


def step_errors(run_body) -> list[str]:
    return [
        (step.get("error") or "")
        for step in run_body.get("step_runs", [])
        if step.get("status") != "passed"
    ]
