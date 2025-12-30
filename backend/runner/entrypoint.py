#!/usr/bin/env python3
"""
LazyAF Runner - Persistent worker that registers with backend and executes jobs.
"""

import os
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
RUNNER_NAME = os.environ.get("RUNNER_NAME", None)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
HEARTBEAT_INTERVAL = 10  # Send heartbeat every 10 seconds during job execution
RECONNECT_INTERVAL = 5  # Seconds between reconnect attempts
MAX_RECONNECT_BACKOFF = 60  # Maximum backoff for reconnection attempts

# Generate persistent runner ID for lifetime of this process
# This allows reconnection without getting a new ID each time
RUNNER_UUID = str(uuid4())

# Global state
runner_id = None
session = requests.Session()
heartbeat_stop_event = threading.Event()
needs_reregister = threading.Event()  # Signal that we need to re-register


def log(msg: str):
    """Log a message and send to backend."""
    print(f"[runner] {msg}", flush=True)
    if runner_id:
        try:
            session.post(
                f"{BACKEND_URL}/api/runners/{runner_id}/logs",
                json={"lines": [msg]},
                timeout=5,
            )
        except Exception:
            pass  # Don't fail if log sending fails


def register():
    """Register with the backend using our persistent UUID."""
    global runner_id
    try:
        response = session.post(
            f"{BACKEND_URL}/api/runners/register",
            json={"runner_id": RUNNER_UUID, "name": RUNNER_NAME},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        runner_id = data["runner_id"]
        log(f"Registered as {data['name']} (id: {runner_id})")
        return True
    except Exception as e:
        print(f"[runner] Failed to register: {e}", flush=True)
        return False


def heartbeat():
    """Send heartbeat to backend. Returns True if successful, False otherwise."""
    if not runner_id:
        return False
    try:
        response = session.post(
            f"{BACKEND_URL}/api/runners/{runner_id}/heartbeat",
            timeout=5,
        )
        if response.status_code == 404:
            # Runner no longer known by backend - need to re-register
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
    """Background thread to send heartbeats during job execution."""
    while not heartbeat_stop_event.is_set():
        heartbeat()
        # Wait for interval or until stop event is set
        heartbeat_stop_event.wait(timeout=HEARTBEAT_INTERVAL)


def start_heartbeat_thread():
    """Start background heartbeat thread."""
    heartbeat_stop_event.clear()
    thread = threading.Thread(target=heartbeat_thread_func, daemon=True)
    thread.start()
    return thread


def stop_heartbeat_thread():
    """Stop background heartbeat thread."""
    heartbeat_stop_event.set()


def poll_for_job():
    """Poll for a job from the backend. Returns job dict, None, or raises NeedsReregister."""
    if not runner_id:
        return None
    try:
        response = session.get(
            f"{BACKEND_URL}/api/runners/{runner_id}/job",
            timeout=10,
        )
        if response.status_code == 404:
            # Runner no longer known by backend
            log("Backend doesn't recognize this runner - will re-register")
            needs_reregister.set()
            return None
        response.raise_for_status()
        data = response.json()
        return data.get("job")
    except requests.exceptions.ConnectionError:
        log("Lost connection to backend")
        needs_reregister.set()
        return None
    except Exception as e:
        log(f"Failed to poll for job: {e}")
        return None


def report_job_status(job_id: str, status: str, error: str = None, pr_url: str = None):
    """Report job status via callback endpoint."""
    try:
        session.post(
            f"{BACKEND_URL}/api/jobs/{job_id}/callback",
            json={"status": status, "error": error, "pr_url": pr_url},
            timeout=10,
        )
    except Exception as e:
        log(f"Failed to report job status: {e}")


def complete_job(success: bool, error: str = None, pr_url: str = None):
    """Mark job as complete."""
    if not runner_id:
        return
    try:
        session.post(
            f"{BACKEND_URL}/api/runners/{runner_id}/complete",
            json={"success": success, "error": error, "pr_url": pr_url},
            timeout=10,
        )
    except Exception as e:
        log(f"Failed to complete job: {e}")


def cleanup_workspace():
    """Clean up the workspace directory between jobs."""
    workspace = Path("/workspace/repo")
    if workspace.exists():
        log("Cleaning up workspace...")
        try:
            # Use subprocess for more reliable cleanup (handles permission issues)
            result = subprocess.run(
                ["rm", "-rf", str(workspace)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Try with sudo as fallback
                subprocess.run(
                    ["sudo", "rm", "-rf", str(workspace)],
                    capture_output=True,
                    text=True,
                )
            log("Workspace cleaned")
        except Exception as e:
            log(f"Warning: Failed to clean workspace: {e}")

    # Also clean up any stale git locks
    git_lock = workspace / ".git" / "index.lock"
    if git_lock.exists():
        try:
            git_lock.unlink()
        except Exception:
            pass


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


def run_command_streaming(cmd: list[str], cwd: str = None) -> tuple[int, str, str]:
    """Run a command with real-time output streaming. Used for long-running commands like Claude."""
    log(f"$ {' '.join(cmd)}")

    stdout_lines = []
    stderr_lines = []

    # Use Popen for real-time output
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
    )

    # Read stdout and stderr in separate threads to avoid blocking
    def read_stdout():
        for line in process.stdout:
            line = line.rstrip('\n')
            stdout_lines.append(line)
            log(f"  {line}")

    def read_stderr():
        for line in process.stderr:
            line = line.rstrip('\n')
            stderr_lines.append(line)
            log(f"  [stderr] {line}")

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)

    stdout_thread.start()
    stderr_thread.start()

    # Wait for process to complete
    process.wait()

    # Wait for threads to finish reading
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    return process.returncode, '\n'.join(stdout_lines), '\n'.join(stderr_lines)


def fetch_agent_files(agent_file_ids: list[str]) -> list[dict]:
    """Fetch agent files from backend by their IDs."""
    if not agent_file_ids:
        return []

    try:
        response = session.post(
            f"{BACKEND_URL}/api/agent-files/batch",
            json=agent_file_ids,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log(f"Failed to fetch agent files: {e}")
        return []


def setup_agent_files(agent_files: list[dict]):
    """Write agent files to the .claude/agents directory."""
    if not agent_files:
        return

    agents_dir = Path.home() / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    log(f"Setting up {len(agent_files)} agent file(s)...")
    for agent_file in agent_files:
        name = agent_file.get("name", "")
        content = agent_file.get("content", "")
        if not name or not content:
            log(f"Skipping invalid agent file: {agent_file}")
            continue

        agent_path = agents_dir / name
        try:
            agent_path.write_text(content)
            log(f"  Wrote agent file: {name}")
        except Exception as e:
            log(f"  Failed to write agent file {name}: {e}")


def execute_job(job: dict):
    """Execute a job."""
    job_id = job['id']
    log(f"Starting job {job_id}: {job['card_title']}")

    # Clean up workspace from any previous job
    cleanup_workspace()

    # Start background heartbeat thread to keep runner alive during long operations
    heartbeat_thread = start_heartbeat_thread()
    log("Started background heartbeat thread")

    # Report running status immediately
    report_job_status(job_id, "running")

    repo_id = job.get("repo_id", "")
    repo_url = job.get("repo_url", "")
    repo_path = job.get("repo_path", "")
    base_branch = job.get("base_branch", "main")
    branch_name = job.get("branch_name", "")
    card_title = job.get("card_title", "")
    card_description = job.get("card_description", "")
    use_internal_git = job.get("use_internal_git", False)
    agent_file_ids = job.get("agent_file_ids", [])

    # If using internal git, construct URL from backend URL
    if use_internal_git and repo_id:
        repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
        log(f"Using internal git server: {repo_url}")

    # Fetch and setup agent files
    if agent_file_ids:
        agent_files = fetch_agent_files(agent_file_ids)
        setup_agent_files(agent_files)

    workspace = Path("/workspace/repo")

    try:
        # Configure git
        run_command(["git", "config", "--global", "user.email", "lazyaf@localhost"])
        run_command(["git", "config", "--global", "user.name", "LazyAF Agent"])
        run_command(["git", "config", "--global", "init.defaultBranch", "main"])

        # Clone or use local repo
        if repo_url:
            log(f"Cloning {repo_url}...")
            if workspace.exists():
                run_command(["sudo", "rm", "-rf", str(workspace)])
            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")
        elif workspace.exists():
            log("Using mounted repository")
        else:
            raise Exception("No repository available")

        # Create feature branch
        log(f"Creating branch {branch_name} from {base_branch}...")

        # Fetch all refs to ensure we have the base branch
        run_command(["git", "fetch", "--all"], cwd=str(workspace))

        # Try to checkout base branch (may already be on it after clone)
        exit_code, _, _ = run_command(["git", "checkout", base_branch], cwd=str(workspace))
        if exit_code != 0:
            # Base branch might not exist, try to create from origin or HEAD
            log(f"Could not checkout {base_branch}, trying origin/{base_branch}...")
            exit_code, _, _ = run_command(
                ["git", "checkout", "-b", base_branch, f"origin/{base_branch}"],
                cwd=str(workspace)
            )
            if exit_code != 0:
                # Just use whatever branch we're on (usually main/master after clone)
                log(f"Base branch {base_branch} not found, using current branch")
                exit_code, stdout, _ = run_command(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(workspace)
                )
                if exit_code == 0:
                    base_branch = stdout.strip()
                    log(f"Using branch: {base_branch}")

        # Store base commit before creating feature branch
        exit_code, stdout, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(workspace))
        base_commit = stdout.strip() if exit_code == 0 else None
        log(f"Base commit: {base_commit[:8] if base_commit else 'unknown'}")

        # Check if branch already exists on remote (from previous retry)
        exit_code, ls_output, _ = run_command(
            ["git", "ls-remote", "--heads", "origin", branch_name],
            cwd=str(workspace)
        )
        branch_exists_on_remote = exit_code == 0 and branch_name in ls_output

        if branch_exists_on_remote:
            # Branch exists from previous attempt - fetch and checkout
            log(f"Branch {branch_name} exists on remote, fetching...")
            run_command(["git", "fetch", "origin", branch_name], cwd=str(workspace))
            run_command(["git", "checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=str(workspace))
            # Merge any new changes from base branch
            log(f"Merging latest from {base_branch}...")
            exit_code, _, stderr = run_command(
                ["git", "merge", base_branch, "-m", f"Merge {base_branch} into {branch_name}"],
                cwd=str(workspace)
            )
            if exit_code != 0:
                log(f"Merge conflict or error, resetting to base: {stderr}")
                # If merge fails, reset to base branch and start fresh
                run_command(["git", "checkout", base_branch], cwd=str(workspace))
                run_command(["git", "branch", "-D", branch_name], cwd=str(workspace))
                run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))
        else:
            # Create new feature branch from current HEAD
            run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))

        # Build prompt for Claude
        prompt = build_prompt(card_title, card_description, workspace)

        # Invoke Claude Code with streaming output
        log("Invoking Claude Code (streaming output)...")
        exit_code, stdout, stderr = run_command_streaming(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            cwd=str(workspace),
        )

        if exit_code != 0:
            raise Exception(f"Claude Code failed with exit code {exit_code}")

        # Check for uncommitted changes first
        exit_code, stdout, _ = run_command(["git", "status", "--porcelain"], cwd=str(workspace))
        has_uncommitted = bool(stdout.strip())

        if has_uncommitted:
            # Commit any uncommitted changes Claude left behind
            log("Committing uncommitted changes...")
            run_command(["git", "add", "-A"], cwd=str(workspace))
            run_command(
                ["git", "commit", "-m", f"feat: {card_title}\n\nImplemented by LazyAF agent"],
                cwd=str(workspace),
            )

        # Check if there are any new commits (Claude may have committed directly)
        exit_code, stdout, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(workspace))
        current_commit = stdout.strip() if exit_code == 0 else None

        if base_commit and current_commit and base_commit == current_commit:
            log("No changes made by Claude Code")
            complete_job(success=True, error="No changes were needed")
            return

        # Count commits ahead of base
        if base_commit:
            exit_code, stdout, _ = run_command(
                ["git", "rev-list", "--count", f"{base_commit}..HEAD"],
                cwd=str(workspace)
            )
            commit_count = stdout.strip() if exit_code == 0 else "?"
            log(f"Branch has {commit_count} new commit(s)")

        # Push branch
        log(f"Pushing branch {branch_name}...")
        exit_code, _, _ = run_command(
            ["git", "push", "-u", "origin", branch_name],
            cwd=str(workspace),
        )
        if exit_code != 0:
            raise Exception("Failed to push branch")

        pr_url = None

        # Skip PR creation when using internal git (no external remote yet)
        if use_internal_git:
            log("Using internal git - skipping PR creation")
            log(f"Branch '{branch_name}' pushed to internal git server")
        else:
            # Create PR on external remote
            log("Creating pull request...")
            exit_code, stdout, _ = run_command(
                [
                    "gh", "pr", "create",
                    "--title", card_title,
                    "--body", f"{card_description}\n\n---\n_Created by LazyAF agent_",
                    "--base", base_branch,
                    "--head", branch_name,
                ],
                cwd=str(workspace),
            )

            if exit_code == 0 and stdout.strip():
                pr_url = stdout.strip().split("\n")[-1]
                log(f"Created PR: {pr_url}")

        log("Reporting job completion...")
        try:
            complete_job(success=True, pr_url=pr_url)
            log("Job completed successfully!")
        except Exception as ce:
            log(f"WARNING: Failed to report completion: {ce}")
            # Job succeeded but we couldn't report it - log but don't fail

    except Exception as e:
        log(f"ERROR: {e}")
        try:
            complete_job(success=False, error=str(e))
        except Exception as ce:
            log(f"WARNING: Failed to report failure: {ce}")

    finally:
        # Always stop the heartbeat thread
        stop_heartbeat_thread()
        log("Stopped background heartbeat thread")

        # Clean up workspace after job completion
        cleanup_workspace()


def build_prompt(title: str, description: str, repo_dir: Path) -> str:
    """Build the prompt for Claude Code."""
    readme_content = ""
    for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
        readme_path = repo_dir / readme_name
        if readme_path.exists():
            try:
                readme_content = readme_path.read_text()[:2000]
                break
            except Exception:
                pass

    prompt = f"""You are implementing a feature for this project.

## Feature Request
Title: {title}

Description:
{description}

## Instructions
1. Implement this feature following existing code patterns
2. Write tests if a test framework is present
3. Commit your changes with a clear message
4. Do not modify unrelated code
5. Keep changes minimal and focused
"""

    if readme_content:
        prompt += f"""
## Repository Context (from README)
{readme_content}
"""

    return prompt


def wait_for_backend():
    """Wait for backend to become available with exponential backoff."""
    backoff = RECONNECT_INTERVAL
    while True:
        try:
            response = session.get(f"{BACKEND_URL}/health", timeout=5)
            if response.status_code == 200:
                log("Backend is available")
                return True
        except requests.exceptions.ConnectionError:
            pass
        except Exception as e:
            log(f"Health check error: {e}")

        log(f"Backend not available, retrying in {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)


def main():
    global runner_id

    log(f"LazyAF Runner starting...")
    log(f"Runner UUID: {RUNNER_UUID}")
    log(f"Backend URL: {BACKEND_URL}")

    # Main loop with auto-reconnect
    try:
        while True:
            # Clear re-register flag
            needs_reregister.clear()

            # Wait for backend to be available
            wait_for_backend()

            # Register with backend
            backoff = RECONNECT_INTERVAL
            while not register():
                log(f"Retrying registration in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)
                # Check if backend is still up
                try:
                    session.get(f"{BACKEND_URL}/health", timeout=5)
                except Exception:
                    log("Backend went away during registration")
                    break

            if not runner_id:
                continue  # Restart the loop if registration failed

            log("Waiting for jobs...")

            # Job polling loop
            while not needs_reregister.is_set():
                job = poll_for_job()
                if job:
                    execute_job(job)
                    # Check if we need to re-register after job completion
                    if needs_reregister.is_set():
                        log("Re-registration required after job")
                        break
                    log("Waiting for next job...")
                else:
                    if needs_reregister.is_set():
                        break
                    time.sleep(POLL_INTERVAL)

            # If we get here, we need to re-register
            log("Connection lost - will reconnect...")
            runner_id = None
            time.sleep(RECONNECT_INTERVAL)

    except KeyboardInterrupt:
        log("Shutting down...")


if __name__ == "__main__":
    main()
