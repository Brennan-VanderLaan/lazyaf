#!/usr/bin/env python3
"""
verify_executor.py - dogfood exit-gate ratchet (R1), 12.3 edition.

Asserts, for the CURRENT pipeline run:
  1. (12.2-INT) every script/docker StepRun was executed by the
     LocalExecutor (executor == 'local'); and
  2. (12.3) every ALREADY-PASSED script/docker StepRun delivered logs.
     The dogfood steps run on control-layer images, where the ONLY writer
     of StepRun.logs is the in-container control runtime POSTing to
     /api/steps/{id}/logs (the stdout consumer drops log events in control
     mode) - so a passed step with empty logs means the control-layer
     reporting path silently failed. Backend-appended '[lazyaf] ' marker
     lines (e.g. '[lazyaf] exit code: 0') do NOT count: they are written
     by the backend itself, so a marker-only log field is exactly the
     clobber shape a broken reporting path produces. At least one
     NON-marker log byte is required. The step running this script is
     exempt (its own logs are still streaming).

A vacuous pass (no script/docker step runs found) is a failure (R4).

Env contract (injected into every step container by LocalExecutor; the
backend URL default matches settings.container_backend_url):
  LAZYAF_PIPELINE_RUN_ID  - required; the run to verify
  LAZYAF_STEP_INDEX       - optional; this step's own index (exempt from
                            the log-delivery check)
  LAZYAF_BACKEND_URL      - optional; defaults to http://backend:8000

Stdlib-only on purpose: this runs inside a bare lazyaf-base step container.
"""
import json
import os
import sys
import urllib.request

DEFAULT_BACKEND_URL = "http://backend:8000"
EXECUTED_STEP_TYPES = ("script", "docker")
# Lines the BACKEND appends to StepRun.logs itself (e.g. '[lazyaf] exit
# code: 0'). They prove nothing about the in-container reporting path, so
# the log-delivery probe ignores them.
LOG_MARKER_PREFIX = "[lazyaf] "


def has_delivered_logs(logs) -> bool:
    """True iff logs contain at least one non-blank, NON-marker line.

    A logs field that is empty, whitespace, or made up solely of backend
    '[lazyaf] ' marker lines means the control runtime's POST
    /api/steps/{id}/logs batches never landed - a vacuous pass stays a
    failure.
    """
    for line in (logs or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(LOG_MARKER_PREFIX.strip()):
            return True
    return False


def fetch_json(base_url: str, path: str, timeout: float = 30.0):
    """GET base_url+path and decode the JSON response body."""
    with urllib.request.urlopen(base_url + path, timeout=timeout) as resp:
        return json.load(resp)


def verify_run(base_url: str, run_id: str, self_index: int | None = None) -> str:
    """Verify executor='local' + log delivery for run_id's script steps.

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
    silent = []
    for sr in run["step_runs"]:
        if step_types.get(sr["step_index"], "script") not in EXECUTED_STEP_TYPES:
            continue
        checked += 1
        if sr["executor"] != "local":
            bad.append(
                f"step {sr['step_index']} '{sr['step_name']}' -> "
                f"executor={sr['executor']!r}"
            )
        # 12.3 control-path probe: passed steps (other than this one) must
        # have NON-marker logs on record - in control mode those only exist
        # if the in-container runtime's POST /api/steps/{id}/logs batches
        # landed. Backend-appended '[lazyaf] ' marker lines don't count.
        if (
            sr["step_index"] != self_index
            and sr.get("status") == "passed"
            and not has_delivered_logs(sr.get("logs"))
        ):
            silent.append(
                f"step {sr['step_index']} '{sr['step_name']}' passed "
                f"with EMPTY logs (no non-marker log lines)"
            )

    if not checked:
        raise SystemExit(
            "FAIL: no script/docker step runs found (vacuous pass = fail, R4)"
        )
    if bad:
        raise SystemExit(
            "FAIL: steps not executed by LocalExecutor:\n  " + "\n  ".join(bad)
        )
    if silent:
        raise SystemExit(
            "FAIL: control-layer reporting path delivered no logs "
            "(POST /api/steps/*/logs never landed?):\n  " + "\n  ".join(silent)
        )
    return (
        f"OK: {checked} script step run(s) all have executor='local' "
        f"and passed steps delivered logs"
    )


def main() -> None:
    base_url = os.environ.get("LAZYAF_BACKEND_URL", DEFAULT_BACKEND_URL)
    run_id = os.environ.get("LAZYAF_PIPELINE_RUN_ID")
    if not run_id:
        raise SystemExit(
            "FAIL: LAZYAF_PIPELINE_RUN_ID is not set - the local "
            "execution path did not inject its env contract"
        )
    raw_index = os.environ.get("LAZYAF_STEP_INDEX")
    self_index = int(raw_index) if raw_index is not None else None
    print(verify_run(base_url, run_id, self_index=self_index))


if __name__ == "__main__":
    main()
