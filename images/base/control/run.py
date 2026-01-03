#!/usr/bin/env python3
"""
LazyAF Control Layer - Container-side step execution manager.

This is the main entrypoint for all step containers. It:
1. Reads step configuration from /workspace/.control/step_config.json
2. Reports "running" status to backend
3. Starts heartbeat thread
4. Executes the actual command
5. Reports completion status (completed/failed)

Environment:
- CONFIG_PATH: Override config file location (default: /workspace/.control/step_config.json)
"""
import sys
import signal
from pathlib import Path

# Default config location
DEFAULT_CONFIG_PATH = Path("/workspace/.control/step_config.json")


def main() -> int:
    """
    Main control layer entry point.

    Returns:
        Exit code (0 = success, non-zero = failure)
    """
    from config import load_step_config
    from backend_client import BackendClient
    from heartbeat import HeartbeatManager
    from executor import execute_command

    # 1. Determine config path
    import os
    config_path_str = os.environ.get("CONFIG_PATH")
    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH

    print(f"[control] Loading config from {config_path}", flush=True)

    # 2. Load configuration
    config = load_step_config(config_path)
    if config is None:
        print("[control] ERROR: Could not load step config", file=sys.stderr)
        return 1

    print(f"[control] Step ID: {config.step_id}", flush=True)
    print(f"[control] Command: {' '.join(config.command)}", flush=True)

    # 3. Initialize backend client
    client = BackendClient(
        backend_url=config.backend_url,
        step_id=config.step_id,
        token=config.token,
    )

    # 4. Report RUNNING status
    print("[control] Reporting running status...", flush=True)
    if not client.report_status("running"):
        print("[control] WARNING: Could not report running status", file=sys.stderr)

    # 5. Start heartbeat thread
    heartbeat = HeartbeatManager(client, interval=config.heartbeat_interval)
    heartbeat.start()

    # 6. Handle signals for graceful shutdown
    def handle_signal(signum, frame):
        print(f"\n[control] Received signal {signum}, stopping...", flush=True)
        heartbeat.stop()
        client.report_status("failed", exit_code=-signum, error=f"Killed by signal {signum}")
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # 7. Execute command
    print(f"[control] Executing command in {config.working_dir}", flush=True)
    print("-" * 60, flush=True)

    try:
        exit_code = execute_command(config, client)
    except Exception as e:
        print(f"[control] ERROR: Command execution failed: {e}", file=sys.stderr)
        exit_code = 1

    print("-" * 60, flush=True)

    # 8. Stop heartbeat
    heartbeat.stop()

    # 9. Report completion status
    if exit_code == 0:
        status = "completed"
        error = None
        print("[control] Command completed successfully", flush=True)
    else:
        status = "failed"
        error = f"Command exited with code {exit_code}"
        print(f"[control] Command failed with exit code {exit_code}", flush=True)

    if not client.report_status(status, exit_code=exit_code, error=error):
        print("[control] WARNING: Could not report completion status", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
