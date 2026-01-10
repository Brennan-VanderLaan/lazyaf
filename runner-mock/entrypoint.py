#!/usr/bin/env python3
"""
LazyAF Runner - Mock Executor

A mock executor for E2E testing that simulates AI behavior with deterministic responses.
This exercises the full runner machinery (git, commits, WebSocket events) without invoking real AI.

Mock config format (passed via step_config.mock_config or read from /workspace/.control/mock_config.json):
{
    "response_mode": "streaming" | "batch",
    "delay_ms": 100,
    "file_operations": [
        {"action": "create", "path": "src/new_file.py", "content": "# content"},
        {"action": "modify", "path": "src/existing.py", "search": "old", "replace": "new"},
        {"action": "delete", "path": "src/obsolete.py"}
    ],
    "output_events": [
        {"type": "content", "text": "Analyzing..."},
        {"type": "tool_use", "tool": "Read", "path": "src/main.py"},
        {"type": "complete", "text": "Done!"}
    ],
    "exit_code": 0
}
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from uuid import uuid4

import requests

# Configuration from environment
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RUNNER_TYPE = os.environ.get("RUNNER_TYPE", "mock")
RUNNER_NAME = os.environ.get("RUNNER_NAME", None)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
HEARTBEAT_INTERVAL = 10
RECONNECT_INTERVAL = 5
MAX_RECONNECT_BACKOFF = 60

# Generate persistent runner ID
RUNNER_UUID = str(uuid4())

# Global state
runner_id = None
session = requests.Session()
heartbeat_stop_event = threading.Event()
needs_reregister = threading.Event()


def log(msg: str):
    """Log a message and send to backend."""
    print(f"[mock-runner] {msg}", flush=True)
    if runner_id:
        try:
            session.post(
                f"{BACKEND_URL}/api/runners/{runner_id}/logs",
                json={"lines": [msg]},
                timeout=5,
            )
        except Exception:
            pass


def register():
    """Register with the backend."""
    global runner_id
    try:
        response = session.post(
            f"{BACKEND_URL}/api/runners/register",
            json={"runner_id": RUNNER_UUID, "name": RUNNER_NAME, "runner_type": RUNNER_TYPE},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        runner_id = data["runner_id"]
        log(f"Registered as {data['name']} (type: {RUNNER_TYPE}, id: {runner_id})")
        return True
    except Exception as e:
        print(f"[mock-runner] Failed to register: {e}", flush=True)
        return False


def heartbeat():
    """Send heartbeat to backend."""
    if not runner_id:
        return False
    try:
        response = session.post(f"{BACKEND_URL}/api/runners/{runner_id}/heartbeat", timeout=5)
        if response.status_code == 404:
            log("Backend doesn't recognize this runner - will re-register")
            needs_reregister.set()
            return False
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        log("Lost connection to backend")
        needs_reregister.set()
        return False
    except Exception as e:
        log(f"Heartbeat error: {e}")
        return False


def heartbeat_thread_func():
    """Background thread to send heartbeats."""
    while not heartbeat_stop_event.is_set():
        heartbeat()
        heartbeat_stop_event.wait(timeout=HEARTBEAT_INTERVAL)


def start_heartbeat_thread():
    heartbeat_stop_event.clear()
    thread = threading.Thread(target=heartbeat_thread_func, daemon=True)
    thread.start()
    return thread


def stop_heartbeat_thread():
    heartbeat_stop_event.set()


def poll_for_job():
    """Poll for a job from the backend."""
    if not runner_id:
        return None
    try:
        response = session.get(f"{BACKEND_URL}/api/runners/{runner_id}/job", timeout=10)
        if response.status_code == 404:
            log("Backend doesn't recognize this runner - will re-register")
            needs_reregister.set()
            return None
        response.raise_for_status()
        return response.json().get("job")
    except requests.exceptions.ConnectionError:
        log("Lost connection to backend")
        needs_reregister.set()
        return None
    except Exception as e:
        log(f"Failed to poll for job: {e}")
        return None


def report_job_status(job_id: str, status: str, error: str = None, test_results: dict = None):
    """Report job status via callback endpoint."""
    try:
        payload = {"status": status, "error": error, "pr_url": None}
        if test_results:
            payload["test_results"] = test_results
        session.post(f"{BACKEND_URL}/api/jobs/{job_id}/callback", json=payload, timeout=10)
    except Exception as e:
        log(f"Failed to report job status: {e}")


def complete_job(success: bool, error: str = None, test_results: dict = None):
    """Mark job as complete."""
    if not runner_id:
        return
    try:
        payload = {"success": success, "error": error, "pr_url": None}
        if test_results:
            payload["test_results"] = test_results
        session.post(f"{BACKEND_URL}/api/runners/{runner_id}/complete", json=payload, timeout=10)
    except Exception as e:
        log(f"Failed to complete job: {e}")


def run_command(cmd: list[str], cwd: str = None) -> tuple[int, str, str]:
    """Run a command and return exit code, stdout, stderr."""
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            log(f"  {line}")
    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            log(f"  [stderr] {line}")
    return result.returncode, result.stdout, result.stderr


def cleanup_workspace():
    """Clean up the workspace directory."""
    workspace = Path("/workspace/repo")
    if workspace.exists():
        log("Cleaning up workspace...")
        try:
            result = subprocess.run(["rm", "-rf", str(workspace)], capture_output=True, text=True)
            if result.returncode != 0:
                subprocess.run(["sudo", "rm", "-rf", str(workspace)], capture_output=True, text=True)
            log("Workspace cleaned")
        except Exception as e:
            log(f"Warning: Failed to clean workspace: {e}")


# ============================================================================
# Mock Executor Logic
# ============================================================================


def load_mock_config(job: dict, workspace: Path) -> dict:
    """Load mock configuration from step_config or file."""
    step_config = job.get("step_config", {}) or {}

    # Check if mock_config is directly in step_config
    if "mock_config" in step_config:
        log("Loading mock config from step_config.mock_config")
        return step_config["mock_config"]

    # Check for mock config file in workspace
    config_path = workspace / ".control" / "mock_config.json"
    if config_path.exists():
        log(f"Loading mock config from {config_path}")
        return json.loads(config_path.read_text())

    # Default mock config - makes a simple change
    log("Using default mock config (no config provided)")
    return {
        "response_mode": "batch",
        "delay_ms": 50,
        "file_operations": [
            {
                "action": "create",
                "path": ".lazyaf-mock-marker",
                "content": f"# Mock executor ran at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            }
        ],
        "output_events": [
            {"type": "content", "text": "Mock executor starting..."},
            {"type": "content", "text": "Applying mock file operations..."},
            {"type": "complete", "text": "Mock execution complete."}
        ],
        "exit_code": 0
    }


def apply_file_operations(workspace: Path, operations: list[dict]):
    """Apply file operations as specified in mock config."""
    for op in operations:
        action = op.get("action", "")
        path = op.get("path", "")

        if not path:
            log(f"Skipping operation with no path: {op}")
            continue

        file_path = workspace / path

        if action == "create":
            log(f"[mock] Creating file: {path}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(op.get("content", ""))

        elif action == "modify":
            if not file_path.exists():
                log(f"[mock] File does not exist, creating: {path}")
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(op.get("content", ""))
            else:
                search = op.get("search", "")
                replace = op.get("replace", "")
                if search:
                    log(f"[mock] Modifying file: {path} (search/replace)")
                    content = file_path.read_text()
                    content = content.replace(search, replace)
                    file_path.write_text(content)
                elif "content" in op:
                    log(f"[mock] Modifying file: {path} (overwrite)")
                    file_path.write_text(op.get("content", ""))

        elif action == "delete":
            if file_path.exists():
                log(f"[mock] Deleting file: {path}")
                file_path.unlink()
            else:
                log(f"[mock] File to delete not found: {path}")

        elif action == "append":
            log(f"[mock] Appending to file: {path}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "a") as f:
                f.write(op.get("content", ""))

        else:
            log(f"[mock] Unknown action: {action}")


def stream_output_events(events: list[dict], delay_ms: int = 100):
    """Stream output events with delays to simulate real AI behavior."""
    delay_sec = delay_ms / 1000.0

    for event in events:
        event_type = event.get("type", "content")

        if event_type == "content":
            text = event.get("text", "")
            log(f"[AI] {text}")

        elif event_type == "tool_use":
            tool = event.get("tool", "Unknown")
            path = event.get("path", "")
            if path:
                log(f"[Tool: {tool}] {path}")
            else:
                log(f"[Tool: {tool}]")

        elif event_type == "complete":
            text = event.get("text", "Complete.")
            log(f"[AI] {text}")

        elif event_type == "error":
            text = event.get("text", "Error occurred")
            log(f"[AI ERROR] {text}")

        time.sleep(delay_sec)


def execute_mock_agent_step(job: dict):
    """Execute an agent step using mock configuration."""
    job_id = job["id"]
    log(f"Starting mock agent job {job_id}: {job.get('card_title', 'unnamed')}")

    # Pipeline context flags
    is_continuation = job.get("is_continuation", False)
    continue_in_context = job.get("continue_in_context", False)
    pipeline_run_id = job.get("pipeline_run_id")
    step_name = job.get("step_name", "mock-agent")

    # Log context information
    log("=" * 50)
    log("MOCK EXECUTOR - CONTEXT INFO:")
    if pipeline_run_id:
        log(f"  - Pipeline run: {pipeline_run_id[:8]}")
        log(f"  - Step: {job.get('step_index', 0)} ({step_name})")
    if is_continuation:
        log("  - Continuing from previous step (workspace preserved)")
    if continue_in_context:
        log("  - Will preserve workspace for next step")
    log("=" * 50)

    # Clean up workspace unless this is a continuation
    if not is_continuation:
        cleanup_workspace()

    # Report running status
    report_job_status(job_id, "running")

    repo_id = job.get("repo_id", "")
    base_branch = job.get("base_branch", "main")
    branch_name = job.get("branch_name", "")
    use_internal_git = job.get("use_internal_git", False)

    try:
        # Determine workspace path
        if pipeline_run_id:
            workspace = Path(f"/workspace/{pipeline_run_id[:8]}/repo")
        else:
            workspace = Path("/workspace/repo")

        # Clone repo if not a continuation
        if is_continuation and workspace.exists():
            log("Continuing from previous step - using existing workspace")
        elif use_internal_git and repo_id:
            repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
            log(f"Cloning from internal git: {repo_url}")

            # Configure git
            run_command(["git", "config", "--global", "user.email", "lazyaf-mock@localhost"])
            run_command(["git", "config", "--global", "user.name", "LazyAF Mock Agent"])

            if workspace.exists():
                run_command(["sudo", "rm", "-rf", str(workspace)])
            workspace.parent.mkdir(parents=True, exist_ok=True)

            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")
        else:
            # Create empty workspace if no repo
            workspace.mkdir(parents=True, exist_ok=True)

        # Create feature branch if specified
        if branch_name and not is_continuation:
            log(f"Creating branch: {branch_name}")
            exit_code, _, _ = run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))
            if exit_code != 0:
                # Branch might already exist, try to check it out
                run_command(["git", "checkout", branch_name], cwd=str(workspace))

        # Load mock configuration
        mock_config = load_mock_config(job, workspace)

        # Stream output events
        output_events = mock_config.get("output_events", [])
        delay_ms = mock_config.get("delay_ms", 100)
        stream_output_events(output_events, delay_ms)

        # Apply file operations
        file_operations = mock_config.get("file_operations", [])
        if file_operations:
            apply_file_operations(workspace, file_operations)

        # Check exit code
        exit_code = mock_config.get("exit_code", 0)

        if exit_code != 0:
            error_msg = mock_config.get("error_message", f"Mock executor failed with exit code {exit_code}")
            log(f"Mock execution failed: {error_msg}")
            complete_job(success=False, error=error_msg)
            return

        # Commit changes if any files were modified
        if file_operations:
            # Stage all changes
            run_command(["git", "add", "-A"], cwd=str(workspace))

            # Check if there are changes to commit
            exit_code, stdout, _ = run_command(["git", "diff", "--cached", "--quiet"], cwd=str(workspace))
            if exit_code != 0:  # Changes exist
                commit_msg = f"[mock] {job.get('card_title', 'Mock changes')}"
                run_command(["git", "commit", "-m", commit_msg], cwd=str(workspace))
                log("Changes committed")

                # Push changes
                if branch_name:
                    run_command(["git", "push", "-u", "origin", branch_name], cwd=str(workspace))
                    log(f"Pushed to branch: {branch_name}")

        log("Mock execution completed successfully")
        complete_job(success=True)

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(success=False, error=str(e))

    finally:
        if not continue_in_context:
            cleanup_workspace()


def execute_script_step(job: dict):
    """Execute a script step (same as other runners)."""
    job_id = job["id"]
    step_config = job.get("step_config", {}) or {}
    command = step_config.get("command", "")

    is_continuation = job.get("is_continuation", False)
    continue_in_context = job.get("continue_in_context", False)
    pipeline_run_id = job.get("pipeline_run_id")

    if not command:
        log("ERROR: No command specified in step_config")
        complete_job(success=False, error="No command specified")
        return

    # Normalize line endings
    command = command.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")

    log(f"Executing script step: {command[:100]}...")
    report_job_status(job_id, "running")

    try:
        # Determine workspace
        if pipeline_run_id:
            workspace = Path(f"/workspace/{pipeline_run_id[:8]}/repo")
        else:
            workspace = Path("/workspace/repo")

        # Clone repo if needed and not a continuation
        repo_id = job.get("repo_id", "")
        use_internal_git = job.get("use_internal_git", False)

        if is_continuation and workspace.exists():
            log("Continuing from previous step - using existing workspace")
            working_dir = str(workspace)
        elif use_internal_git and repo_id:
            repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
            log(f"Cloning from internal git: {repo_url}")

            run_command(["git", "config", "--global", "user.email", "lazyaf-mock@localhost"])
            run_command(["git", "config", "--global", "user.name", "LazyAF Mock Agent"])

            if workspace.exists():
                run_command(["sudo", "rm", "-rf", str(workspace)])
            workspace.parent.mkdir(parents=True, exist_ok=True)

            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")

            working_dir = str(workspace)
        else:
            working_dir = step_config.get("working_dir", "/workspace")

        # Write script to temp file
        script_path = Path(working_dir) / ".lazyaf_script.sh"
        with open(script_path, "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write(command)
            f.write("\n")

        run_command(["chmod", "+x", str(script_path)])

        # Execute script
        exit_code, stdout, stderr = run_command(["bash", str(script_path)], cwd=working_dir)

        # Clean up script
        try:
            script_path.unlink()
        except:
            pass

        if exit_code == 0:
            log("Script completed successfully")
            complete_job(success=True)
        else:
            log(f"Script failed with exit code {exit_code}")
            complete_job(success=False, error=f"Command failed with exit code {exit_code}")

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(success=False, error=str(e))

    finally:
        if not continue_in_context:
            cleanup_workspace()


def execute_job(job: dict):
    """Execute a job based on its step type."""
    step_type = job.get("step_type", "agent")
    job_id = job.get("id", "unknown")

    log(f"Job {job_id[:8]}: step_type={step_type}")

    if step_type == "script":
        execute_script_step(job)
    elif step_type == "docker":
        # For docker steps, the mock runner just runs the command directly
        # (Docker-in-Docker would be complex for a test runner)
        log("Docker step running as script (mock runner limitation)")
        execute_script_step(job)
    else:
        # Default to mock agent step
        execute_mock_agent_step(job)


def wait_for_backend():
    """Wait for the backend to be available."""
    backoff = RECONNECT_INTERVAL
    while True:
        try:
            response = session.get(f"{BACKEND_URL}/health", timeout=5)
            if response.status_code == 200:
                log("Backend is available")
                return
        except Exception:
            pass
        print(f"[mock-runner] Waiting for backend... (retry in {backoff}s)", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)


def main():
    global runner_id

    log("LazyAF Mock Runner starting...")
    log(f"Runner Type: {RUNNER_TYPE}")
    log(f"Runner UUID: {RUNNER_UUID}")
    log(f"Backend URL: {BACKEND_URL}")

    try:
        while True:
            needs_reregister.clear()

            # Wait for backend
            wait_for_backend()

            # Register with backend
            backoff = RECONNECT_INTERVAL
            while not register():
                log(f"Retrying registration in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)
                try:
                    session.get(f"{BACKEND_URL}/health", timeout=5)
                except Exception:
                    log("Backend went away during registration")
                    break

            if not runner_id:
                continue

            # Start heartbeat thread
            heartbeat_thread = start_heartbeat_thread()
            log("Started heartbeat thread")
            log("Waiting for jobs...")

            # Job polling loop
            while not needs_reregister.is_set():
                job = poll_for_job()
                if job:
                    execute_job(job)
                    if needs_reregister.is_set():
                        log("Re-registration required after job")
                        break
                    log("Waiting for next job...")
                else:
                    if needs_reregister.is_set():
                        log("Re-registration required")
                        break
                    time.sleep(POLL_INTERVAL)

            # Stop heartbeat when re-registering
            stop_heartbeat_thread()
            runner_id = None
            log("Disconnected, will attempt to reconnect...")

    except KeyboardInterrupt:
        log("Shutting down...")
    except Exception as e:
        log(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
