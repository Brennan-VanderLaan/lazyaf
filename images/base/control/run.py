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
4. Executes the command (shell-wrapped, timeout enforced in-container)
5. Reports terminal status (completed / failed / timeout)

The container EXIT CODE remains the backend's ground truth for step outcome;
these reports carry telemetry, logs and liveness.

Environment:
- CONFIG_PATH: Override config file location (default: /workspace/.control/step_config.json)
"""
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

    # 7. Execute command (timeout enforced inside execute_command)
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
