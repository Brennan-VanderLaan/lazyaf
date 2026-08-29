#!/usr/bin/env python3
"""
verify_executor.py - 12.2-INT exit-gate ratchet (R1).

Asserts that every script/docker StepRun of the CURRENT pipeline run was
executed by the LocalExecutor (executor == 'local'). A vacuous pass (no
script/docker step runs found) is a failure (R4).

Env contract (injected into every step container by LocalExecutor; the
backend URL default matches settings.container_backend_url):
  LAZYAF_PIPELINE_RUN_ID  - required; the run to verify
  LAZYAF_BACKEND_URL      - optional; defaults to http://backend:8000

Stdlib-only on purpose: this runs inside a bare python:3.12 step container.
"""
import json
import os
import sys
import urllib.request

DEFAULT_BACKEND_URL = "http://backend:8000"
EXECUTED_STEP_TYPES = ("script", "docker")


def fetch_json(base_url: str, path: str, timeout: float = 30.0):
    """GET base_url+path and decode the JSON response body."""
    with urllib.request.urlopen(base_url + path, timeout=timeout) as resp:
        return json.load(resp)


def verify_run(base_url: str, run_id: str) -> str:
    """Verify every script/docker StepRun of run_id has executor='local'.

    Returns an OK message on success; raises SystemExit with a FAIL
    message otherwise (vacuous pass = fail, R4).
    """
    run = fetch_json(base_url, f"/api/pipeline-runs/{run_id}")
    pipeline = fetch_json(base_url, f"/api/pipelines/{run['pipeline_id']}")
    step_types = {
        i: s.get("type", "script") for i, s in enumerate(pipeline["steps"])
    }

    checked = 0
    bad = []
    for sr in run["step_runs"]:
        if step_types.get(sr["step_index"], "script") not in EXECUTED_STEP_TYPES:
            continue
        checked += 1
        if sr["executor"] != "local":
            bad.append(
                f"step {sr['step_index']} '{sr['step_name']}' -> "
                f"executor={sr['executor']!r}"
            )

    if not checked:
        raise SystemExit(
            "FAIL: no script/docker step runs found (vacuous pass = fail, R4)"
        )
    if bad:
        raise SystemExit(
            "FAIL: steps not executed by LocalExecutor:\n  " + "\n  ".join(bad)
        )
    return f"OK: {checked} script step run(s) all have executor='local'"


def main() -> None:
    base_url = os.environ.get("LAZYAF_BACKEND_URL", DEFAULT_BACKEND_URL)
    run_id = os.environ.get("LAZYAF_PIPELINE_RUN_ID")
    if not run_id:
        raise SystemExit(
            "FAIL: LAZYAF_PIPELINE_RUN_ID is not set - the local "
            "execution path did not inject its env contract"
        )
    print(verify_run(base_url, run_id))


if __name__ == "__main__":
    main()
