"""Plain HTTP helpers for the QA-4 (resource abuse / pipeline-graph pathology) lane.

These tests drive a LIVE LazyAF backend over HTTP. They never import backend
code and never touch a database directly, so they are safe to run while the
source tree is being edited.

Target stack is chosen by env var, defaulting to the isolated QA sandbox:

    LAZYAF_QA_BASE_URL=http://localhost:8790

The QA sandbox is shared with other QA lanes, and any of them may call
``POST /api/test/reset`` at any moment. Every helper here therefore
re-materializes the repo it needs instead of assuming one survives.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import pytest

BASE_URL = os.environ.get("LAZYAF_QA_BASE_URL", "http://localhost:8790").rstrip("/")

#: Name of the repo these tests ingest. Stable so a reset just recreates it.
QA_REPO_NAME = "qa4-graph"


def api(method: str, path: str, body=None, timeout: float = 60.0, raw: bytes | None = None):
    """Call the backend. Returns (status_code, parsed_body_or_text).

    Never raises on a non-2xx: the status code IS the assertion subject in
    most of these tests.
    """
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
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


# ---------------------------------------------------------------------------
# Graph construction helpers (mirror app.schemas.pipeline shapes)
# ---------------------------------------------------------------------------

def step(step_id: str, command: str = "echo qa4", **overrides) -> dict:
    """A v2 graph step. Pass config=... to replace the whole config block."""
    node = {
        "id": step_id,
        "name": step_id,
        "type": "script",
        "config": {"command": command},
        "timeout": 300,
    }
    node.update(overrides)
    return node


def dead_step(step_id: str, **overrides) -> dict:
    """A step that fails during ROUTING, before any container is created.

    ``executor: legacy`` names the polling queue Phase 12.6 deleted, so the
    ExecutionRouter raises and the executor fails the step synchronously.
    This is the cheapest way to walk graph traversal without paying for a
    container, and it is a realistic user mistake (stale config).
    """
    node = step(step_id, **overrides)
    node["config"] = dict(node["config"])
    node["config"]["executor"] = "legacy"
    return node


def edge(edge_id: str, from_step: str, to_step: str, condition: str = "success") -> dict:
    return {"id": edge_id, "from_step": from_step, "to_step": to_step, "condition": condition}


def graph(steps: list[dict], edges: list[dict], entry_points: list[str], version: int = 2) -> dict:
    return {
        "steps": {s["id"]: s for s in steps},
        "edges": edges,
        "entry_points": entry_points,
        "version": version,
    }


def chain_graph(size: int, step_factory=dead_step) -> dict:
    """A single linear chain of `size` steps joined by `always` edges."""
    return graph(
        [step_factory(f"s{i}") for i in range(size)],
        [edge(f"e{i}", f"s{i}", f"s{i + 1}", "always") for i in range(size - 1)],
        ["s0"],
    )


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------

def start_run(pipeline_id: str, body=None, timeout: float = 300.0):
    return api("POST", f"/api/pipelines/{pipeline_id}/run", body or {"trigger_type": "manual"}, timeout=timeout)


def wait_for_run(run_id: str, seconds: float = 300.0) -> dict:
    """Poll a run to a terminal status. Returns the last body seen.

    Skips (rather than fails) if the run disappears - that means a sibling QA
    lane reset the sandbox mid-test, which is not a defect in the product.
    """
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        status, body = api("GET", f"/api/pipeline-runs/{run_id}")
        if status == 404:
            pytest.skip("QA sandbox was reset mid-test (run vanished)")
        if status != 200 or not isinstance(body, dict):
            time.sleep(1.0)
            continue
        last = body
        if body["status"] not in ("running", "pending"):
            return body
        time.sleep(1.5)
    if last is None:
        pytest.skip("QA sandbox never returned the run")
    return last


def run_to_completion(create, payload, seconds: float = 300.0) -> dict:
    """Create + run + wait. Returns the terminal run body."""
    status, pipeline = create(payload)
    assert status == 201, f"pipeline create failed: {status} {pipeline}"
    status, run = start_run(pipeline["id"])
    if status == 404:
        pytest.skip("QA sandbox was reset mid-test (pipeline vanished)")
    assert status == 200, f"run start failed: {status} {run}"
    return wait_for_run(run["id"], seconds)
