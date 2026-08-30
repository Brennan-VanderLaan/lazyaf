#!/usr/bin/env python3
"""
LazyAF Control Layer - Container-side step execution manager.

Main entrypoint for control-mode step containers (exec'd by entrypoint.sh when
LAZYAF_CONTROL=1). It:
1. Reads step configuration from the CONFIG_PATH env var (the backend writes
   a PER-STEP file /workspace/.control/<step_execution_id>.json and sets
   CONFIG_PATH to it) and DELETES that exact path in a finally that runs on
   EVERY outcome, parse failure included (consume-once: the workspace volume
   persists across steps; a stale config re-executing a previous step is the
   landmine 12.3 exists to kill)
2. Reports "running" status to backend
3. Starts heartbeat thread
4. Executes the command (shell-wrapped, timeout enforced in-container) with
   LAZYAF_TEST_RESULTS_PATH injected PER-STEP into the step environment
   (12.2.6 contract #2: /workspace/.control/test_results.<step_execution_id>
   .json — derived as test_results.<step_id>.json NEXT TO the step config,
   so the same config-collision lesson applies: two steps sharing the
   workspace volume can never race on one manifest path)
5. If the step left a manifest at that path, POSTs it to
   /api/steps/{id}/test-results and deletes the file (consume-once).
   Delivery failure NEVER fails the step — it is recorded loudly in the
   terminal status error instead (the dropped-log-lines pattern).
6. Reports terminal status (completed / failed / timeout)

The container EXIT CODE remains the backend's ground truth for step outcome;
these reports carry telemetry, logs and liveness.

Environment:
- CONFIG_PATH: Override config file location (default: /workspace/.control/step_config.json)
"""
import json
import os
import signal
import sys
from pathlib import Path

if __package__:  # imported as control.run (unit tests)
    from .backend_client import BackendClient
    from .config import load_step_config
    from .executor import execute_command
    from .heartbeat import HeartbeatManager
else:  # executed as a script: python3 /control/run.py
    from backend_client import BackendClient
    from config import load_step_config
    from executor import execute_command
    from heartbeat import HeartbeatManager

# Default config location
DEFAULT_CONFIG_PATH = Path("/workspace/.control/step_config.json")


def test_results_path(config_path: Path, step_id: str) -> Path:
    """Per-step manifest path (12.2.6 contract #2).

    Derived NEXT TO the step config file — in the image that directory is
    /workspace/.control, so the value is
    /workspace/.control/test_results.<step_execution_id>.json. Per-step by
    construction (the config-collision lesson): concurrent steps on one
    workspace volume can never race on a shared manifest path.
    """
    return config_path.parent / f"test_results.{step_id}.json"


# --- Manifest wire contract (12.2.6, cross-agent #2) -------------------------
# The ONE pinned shape, mirrored by the shared contract module the tests on
# both sides import (tdd/unit/control_runtime/manifest_contract.py):
#   {"version": 1, "results": [{"lazyaf_test_id": str (non-empty),
#                               "status": "passed"|"failed"|"skipped",
#                               "duration_ms": int|None,
#                               "file_path": str|None}]}
# file_path is REPO-ROOT-relative with "/" separators (cross-agent #3).
MANIFEST_VERSION = 1
MANIFEST_STATUSES = frozenset({"passed", "failed", "skipped"})
MANIFEST_RESULT_KEYS = ("lazyaf_test_id", "status", "duration_ms", "file_path")


def _coerce_result_entry(entry, index, warnings):
    """Normalize one result entry to the pinned shape, or None to SKIP it.

    A step's manifest is UNTRUSTED input (any command can write that path):
    a bad entry is dropped with a loud warning, never propagated to the
    backend and never allowed to break terminal status reporting.
    """
    if not isinstance(entry, dict):
        warnings.append(f"results[{index}] is not an object (dropped)")
        return None

    test_id = entry.get("lazyaf_test_id")
    if not isinstance(test_id, str) or not test_id:
        warnings.append(
            f"results[{index}] has no usable lazyaf_test_id (dropped)"
        )
        return None

    status = entry.get("status")
    if status not in MANIFEST_STATUSES:
        warnings.append(
            f"results[{index}] ({test_id}) has unknown status {status!r} "
            "(dropped)"
        )
        return None

    duration = entry.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        if duration is not None:
            warnings.append(
                f"results[{index}] ({test_id}) duration_ms {duration!r} is "
                "not a number (nulled)"
            )
        duration = None
    else:
        duration = int(duration)
        if duration < 0:
            warnings.append(
                f"results[{index}] ({test_id}) duration_ms was negative "
                "(nulled)"
            )
            duration = None

    file_path = entry.get("file_path")
    if file_path is not None and not isinstance(file_path, str):
        warnings.append(
            f"results[{index}] ({test_id}) file_path is not a string (nulled)"
        )
        file_path = None
    if isinstance(file_path, str):
        file_path = file_path.replace("\\", "/") or None

    extra = set(entry) - set(MANIFEST_RESULT_KEYS)
    if extra:
        warnings.append(
            f"results[{index}] ({test_id}) carried unknown keys "
            f"{sorted(extra)} (dropped from the wire payload)"
        )

    return {
        "lazyaf_test_id": test_id,
        "status": status,
        "duration_ms": duration,
        "file_path": file_path,
    }


def normalize_manifest(raw):
    """Validate/coerce an untrusted manifest into the pinned wire shape.

    Returns ``(manifest_or_None, warnings)``. ``None`` means there is
    nothing worth POSTing (unusable envelope, or every entry dropped);
    ``warnings`` is a list of human-readable strings for the terminal
    status error. NEVER raises — a malformed manifest must not be able to
    stop terminal status reporting.
    """
    warnings: list = []

    if not isinstance(raw, dict):
        return None, [
            f"top level is a {type(raw).__name__}, expected an object with a "
            "'results' list (whole manifest dropped)"
        ]

    results = raw.get("results")
    if not isinstance(results, list):
        return None, [
            f"'results' is {type(results).__name__}, expected a list "
            "(whole manifest dropped)"
        ]

    version = raw.get("version", MANIFEST_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        warnings.append(
            f"'version' {version!r} is not an int (sent as {MANIFEST_VERSION})"
        )
        version = MANIFEST_VERSION

    entries = []
    for index, entry in enumerate(results):
        coerced = _coerce_result_entry(entry, index, warnings)
        if coerced is not None:
            entries.append(coerced)

    if not entries:
        warnings.append("no usable results (nothing sent)")
        return None, warnings

    return {"version": version, "results": entries}, warnings


def ship_test_results(manifest_path: Path, client) -> "str | None":
    """Pick up the step's test-results manifest, POST it, consume the file.

    Returns a warning string when pickup/validation/delivery failed
    (unreadable JSON, malformed shape, retry budget exhausted) — the caller
    appends it to the terminal status error so the drop is LOUD, but
    manifest delivery never changes the step outcome. The file is deleted on
    EVERY path (consume-once, like the step config: the workspace volume
    outlives this step).

    HARD RULE: this function never raises. The manifest is written by the
    step's own command, i.e. arbitrary untrusted bytes; ANY exception here
    is a delivery failure, never a reason for the step to die without
    reporting its terminal status.
    """
    warning = None
    try:
        if not manifest_path.exists():
            return None

        try:
            try:
                with open(manifest_path, "r") as f:
                    raw = json.load(f)
            except (OSError, ValueError) as e:
                warning = (
                    f"[control] WARNING: test results manifest unreadable: {e}"
                )
            else:
                manifest, problems = normalize_manifest(raw)
                if problems:
                    warning = (
                        "[control] WARNING: test results manifest problems: "
                        + "; ".join(problems)
                    )
                if manifest is not None:
                    print(
                        "[control] Shipping test results manifest "
                        f"({len(manifest['results'])} results)...",
                        flush=True,
                    )
                    if not client.send_test_results(manifest):
                        deliver_warn = (
                            "[control] WARNING: test results manifest failed "
                            "to reach backend"
                        )
                        warning = (
                            f"{warning}\n{deliver_warn}"
                            if warning
                            else deliver_warn
                        )
        finally:
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                unlink_warn = (
                    "[control] WARNING: could not delete test results "
                    f"manifest: {e}"
                )
                warning = f"{warning}\n{unlink_warn}" if warning else unlink_warn
    except Exception as e:  # belt-and-braces: NOTHING escapes this function
        crash_warn = (
            f"[control] WARNING: test results manifest handling crashed: {e!r}"
        )
        warning = f"{warning}\n{crash_warn}" if warning else crash_warn

    if warning:
        print(warning, file=sys.stderr)
    return warning


def main() -> int:
    """
    Main control layer entry point.

    Returns:
        Exit code (0 = success, non-zero = failure, 124 = timeout)
    """
    # 1. Determine config path
    config_path_str = os.environ.get("CONFIG_PATH")
    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH

    print(f"[control] Loading config from {config_path}", flush=True)

    # 2. Load configuration; consume-once on EVERY path (parse failure
    # included) — the config and its token must not survive this step.
    config = None
    try:
        config = load_step_config(config_path)
    finally:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(
                f"[control] WARNING: could not delete step config: {e}",
                file=sys.stderr,
            )

    if config is None:
        print("[control] ERROR: Could not load step config", file=sys.stderr)
        return 1

    print(f"[control] Step ID: {config.step_id}", flush=True)
    print(f"[control] Command: {config.command}", flush=True)

    # 3. Initialize backend client
    client = BackendClient(
        backend_url=config.backend_url,
        step_id=config.step_id,
        auth_token=config.auth_token,
    )

    # 4. Report RUNNING status
    print("[control] Reporting running status...", flush=True)
    if not client.report_status("running"):
        print("[control] WARNING: Could not report running status", file=sys.stderr)

    # 5. Start heartbeat thread (interval is a module constant, not config)
    heartbeat = HeartbeatManager(client)
    heartbeat.start()

    # 6. Handle signals for graceful shutdown
    def handle_signal(signum, frame):
        print(f"\n[control] Received signal {signum}, stopping...", flush=True)
        heartbeat.stop()
        client.report_status(
            "failed", exit_code=-signum, error=f"Killed by signal {signum}"
        )
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # 7. Execute command (timeout enforced inside execute_command).
    # Inject the per-step test-results manifest path (12.2.6 contract #2)
    # into the step environment — platform-owned, so it overrides any
    # user-supplied value: a step must never write another step's manifest.
    manifest_path = test_results_path(config_path, config.step_id)
    config.environment["LAZYAF_TEST_RESULTS_PATH"] = str(manifest_path)

    print(f"[control] Executing command in {config.working_directory}", flush=True)
    print("-" * 60, flush=True)

    try:
        result = execute_command(config, client)
    except Exception as e:
        print(f"[control] ERROR: Command execution failed: {e}", file=sys.stderr)
        from types import SimpleNamespace

        result = SimpleNamespace(exit_code=1, timed_out=False)

    print("-" * 60, flush=True)

    # 8. Stop heartbeat
    heartbeat.stop()

    # 8.5. Ship the test-results manifest if the step produced one (12.2.6).
    # Runs on EVERY outcome (a timed-out pytest may still have written a
    # complete manifest — the plugin writes atomically). Never changes the
    # step outcome; failures surface in the terminal status error below.
    manifest_warning = ship_test_results(manifest_path, client)

    # 9. Report terminal status
    if result.timed_out:
        status = "timeout"
        error = f"Step exceeded timeout of {config.timeout_seconds}s"
        print(f"[control] Command timed out after {config.timeout_seconds}s", flush=True)
    elif result.exit_code == 0:
        status = "completed"
        error = None
        print("[control] Command completed successfully", flush=True)
    else:
        status = "failed"
        error = f"Command exited with code {result.exit_code}"
        print(f"[control] Command failed with exit code {result.exit_code}", flush=True)

    if manifest_warning:
        error = f"{error}\n{manifest_warning}" if error else manifest_warning

    if client.dropped_log_lines:
        warning = (
            f"[control] WARNING: {client.dropped_log_lines} log lines "
            "failed to reach backend"
        )
        print(warning, file=sys.stderr)
        error = f"{error}\n{warning}" if error else warning

    if not client.report_status(status, exit_code=result.exit_code, error=error):
        print("[control] WARNING: Could not report completion status", file=sys.stderr)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
