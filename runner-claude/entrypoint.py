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
RUNNER_TYPE = os.environ.get("RUNNER_TYPE", "claude-code")
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
            json={"runner_id": RUNNER_UUID, "name": RUNNER_NAME, "runner_type": RUNNER_TYPE},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        runner_id = data["runner_id"]
        log(f"Registered as {data['name']} (type: {RUNNER_TYPE}, id: {runner_id})")
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


def report_job_status(job_id: str, status: str, error: str = None, pr_url: str = None, test_results: dict = None):
    """Report job status via callback endpoint."""
    try:
        payload = {"status": status, "error": error, "pr_url": pr_url}
        if test_results:
            payload["test_results"] = test_results
        session.post(
            f"{BACKEND_URL}/api/jobs/{job_id}/callback",
            json=payload,
            timeout=10,
        )
    except Exception as e:
        log(f"Failed to report job status: {e}")


def complete_job(success: bool, error: str = None, pr_url: str = None, test_results: dict = None):
    """Mark job as complete."""
    if not runner_id:
        return
    try:
        payload = {"success": success, "error": error, "pr_url": pr_url}
        if test_results:
            payload["test_results"] = test_results
        session.post(
            f"{BACKEND_URL}/api/runners/{runner_id}/complete",
            json=payload,
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


def normalize_agent_name(name: str) -> str:
    """Normalize agent name to CLI-safe format (lowercase, hyphenated)."""
    import re
    normalized = name.lower().strip()
    normalized = re.sub(r'[^a-z0-9-]', '-', normalized)  # Replace non-alphanumeric with hyphens
    normalized = re.sub(r'-+', '-', normalized)          # Collapse multiple hyphens
    normalized = re.sub(r'^-|-$', '', normalized)        # Remove leading/trailing hyphens
    return normalized


def build_agents_json(agent_files: list[dict]) -> str | None:
    """Build JSON string for --agents CLI flag from agent files."""
    if not agent_files:
        return None

    agents_dict = {}
    for agent_file in agent_files:
        name = agent_file.get("name", "")
        content = agent_file.get("content", "")
        description = agent_file.get("description", "")
        if not name or not content:
            log(f"Skipping invalid agent file: {agent_file}")
            continue

        # Normalize name for CLI compatibility
        cli_name = normalize_agent_name(name)
        if not cli_name:
            log(f"Skipping agent with invalid name: {name}")
            continue

        # Build agent definition for Claude Code CLI
        agents_dict[cli_name] = {
            "description": description or f"Agent: {cli_name}",
            "prompt": content,
        }
        log(f"  Configured agent: @{cli_name}")

    if not agents_dict:
        return None

    import json
    return json.dumps(agents_dict)


def execute_script_step(job: dict):
    """Execute a script step (run shell command directly)."""
    import tempfile

    job_id = job['id']
    step_config = job.get('step_config', {}) or {}
    command = step_config.get('command', '')

    if not command:
        log("ERROR: No command specified in step_config")
        complete_job(success=False, error="No command specified in step_config")
        return

    # Normalize line endings (Windows -> Unix) and handle escaped newlines from JSON
    command = command.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n')

    # Log script preview (first 200 chars)
    preview = command[:200] + ('...' if len(command) > 200 else '')
    log(f"Executing script step:\n{preview}")

    # Start heartbeat
    heartbeat_thread = start_heartbeat_thread()

    # Report running status
    report_job_status(job_id, "running")

    try:
        # Clone repo for context (script may need repo files)
        repo_id = job.get("repo_id", "")
        use_internal_git = job.get("use_internal_git", False)
        workspace = Path("/workspace/repo")

        if use_internal_git and repo_id:
            repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
            log(f"Cloning from internal git: {repo_url}")

            # Configure git
            run_command(["git", "config", "--global", "user.email", "lazyaf@localhost"])
            run_command(["git", "config", "--global", "user.name", "LazyAF Agent"])

            if workspace.exists():
                run_command(["sudo", "rm", "-rf", str(workspace)])
            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")

            working_dir = str(workspace)
        else:
            working_dir = step_config.get('working_dir', '/workspace')

        # Write script to temp file for reliable multi-line execution
        script_path = Path(working_dir) / ".lazyaf_script.sh"
        with open(script_path, 'w') as f:
            f.write("#!/bin/bash\nset -e\n")  # Exit on first error
            f.write(command)
            f.write("\n")

        # Make executable
        run_command(["chmod", "+x", str(script_path)])

        # Execute the script file
        log(f"Running script in {working_dir}...")
        exit_code, stdout, stderr = run_command_streaming(
            ["bash", str(script_path)],
            cwd=working_dir,
        )

        # Clean up script file
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
        stop_heartbeat_thread()
        cleanup_workspace()


def execute_docker_step(job: dict):
    """Execute a docker step (run command in specified container image)."""
    job_id = job['id']
    step_config = job.get('step_config', {}) or {}
    image = step_config.get('image', '')
    command = step_config.get('command', '')

    if not image:
        log("ERROR: No image specified in step_config")
        complete_job(success=False, error="No image specified in step_config")
        return

    if not command:
        log("ERROR: No command specified in step_config")
        complete_job(success=False, error="No command specified in step_config")
        return

    # Normalize line endings (Windows -> Unix) and handle escaped newlines from JSON
    command = command.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n')

    # Log script preview (first 200 chars)
    preview = command[:200] + ('...' if len(command) > 200 else '')
    log(f"Executing docker step: {image}\n{preview}")

    # Start heartbeat
    heartbeat_thread = start_heartbeat_thread()

    # Report running status
    report_job_status(job_id, "running")

    try:
        # Clone repo first (so we can mount it into the container)
        repo_id = job.get("repo_id", "")
        use_internal_git = job.get("use_internal_git", False)
        workspace = Path("/workspace/repo")

        if use_internal_git and repo_id:
            repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
            log(f"Cloning from internal git: {repo_url}")

            # Configure git
            run_command(["git", "config", "--global", "user.email", "lazyaf@localhost"])
            run_command(["git", "config", "--global", "user.name", "LazyAF Agent"])

            if workspace.exists():
                run_command(["sudo", "rm", "-rf", str(workspace)])
            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")

        # Write script to temp file for reliable multi-line execution
        script_path = workspace / ".lazyaf_script.sh"
        with open(script_path, 'w') as f:
            f.write("#!/bin/bash\nset -e\n")  # Exit on first error
            f.write(command)
            f.write("\n")

        # Make executable
        run_command(["chmod", "+x", str(script_path)])

        # Build docker run command
        env_vars = step_config.get('env', {})
        volumes = step_config.get('volumes', [])

        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{workspace}:/workspace",
            "-w", "/workspace",
        ]

        # Add environment variables
        for key, value in env_vars.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

        # Add additional volumes
        for vol in volumes:
            docker_cmd.extend(["-v", vol])

        docker_cmd.append(image)
        docker_cmd.extend(["bash", "/workspace/.lazyaf_script.sh"])

        log(f"Running: docker run {image} ...")
        exit_code, stdout, stderr = run_command_streaming(docker_cmd, cwd=str(workspace))

        # Clean up script file
        try:
            script_path.unlink()
        except:
            pass

        if exit_code == 0:
            log("Docker step completed successfully")
            complete_job(success=True)
        else:
            log(f"Docker step failed with exit code {exit_code}")
            complete_job(success=False, error=f"Docker command failed with exit code {exit_code}")

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(success=False, error=str(e))

    finally:
        stop_heartbeat_thread()
        cleanup_workspace()


def execute_agent_step(job: dict):
    """Execute an agent step (Claude Code implements feature). This is the original execute_job logic."""
    job_id = job['id']
    log(f"Starting agent job {job_id}: {job['card_title']}")

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
    prompt_template = job.get("prompt_template", "")

    # If using internal git, construct URL from backend URL
    if use_internal_git and repo_id:
        repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
        log(f"Using internal git server: {repo_url}")

    # Fetch agent files and build --agents JSON
    agents_json = None
    if agent_file_ids:
        agent_files = fetch_agent_files(agent_file_ids)
        agents_json = build_agents_json(agent_files)
        if agents_json:
            log(f"Built agents JSON for {len(agent_files)} agent(s)")

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

        # Build prompt for Claude - use custom template if provided, else default
        if prompt_template:
            # Replace placeholders in the template
            prompt = prompt_template.replace("{{title}}", card_title).replace("{{description}}", card_description)
            log("Using custom prompt template")
        else:
            prompt = build_prompt(card_title, card_description, workspace)
            log("Using default prompt template")

        # Build Claude command
        claude_cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]

        # Add --agents flag if we have agent files
        if agents_json:
            claude_cmd.extend(["--agents", agents_json])
            log(f"Added --agents flag with {len(agent_file_ids)} agent(s)")

        # Invoke Claude Code with streaming output
        log("Invoking Claude Code (streaming output)...")
        exit_code, stdout, stderr = run_command_streaming(
            claude_cmd,
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

        # Run tests after code changes (Phase 8)
        test_results = run_tests(workspace)

        # Determine success based on test results
        job_success = True
        if test_results and not test_results.get("tests_passed"):
            job_success = False
            log("Job marked as failed due to test failures")

        log("Reporting job completion...")
        try:
            complete_job(success=job_success, pr_url=pr_url, test_results=test_results)
            if job_success:
                log("Job completed successfully!")
            else:
                log("Job completed with test failures")
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


def execute_job(job: dict):
    """Execute a job based on its step type."""
    step_type = job.get('step_type', 'agent')
    job_id = job.get('id', 'unknown')

    log(f"Job {job_id[:8]}: step_type={step_type}")

    if step_type == 'script':
        execute_script_step(job)
    elif step_type == 'docker':
        execute_docker_step(job)
    else:
        # Default to agent step
        execute_agent_step(job)


# ============================================================================
# Test Framework Detection and Execution (Phase 8)
# ============================================================================

TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "300"))  # 5 minutes default


def detect_test_framework(workspace: Path) -> tuple[str | None, list[str]]:
    """
    Detect the test framework and return the command to run tests.
    Returns (framework_name, command_list) or (None, []) if no framework detected.
    """
    # Priority order as per PLAN.md

    # 1. package.json -> scripts.test -> "npm test"
    package_json = workspace / "package.json"
    if package_json.exists():
        try:
            import json
            pkg = json.loads(package_json.read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                test_script = scripts["test"]
                # Skip if test script is a placeholder or error
                if test_script and "no test" not in test_script.lower() and "exit 1" not in test_script:
                    log(f"Detected npm test framework: {test_script}")
                    return ("npm", ["npm", "test", "--", "--reporter=verbose"])
        except Exception as e:
            log(f"Failed to parse package.json: {e}")

    # 2. pytest.ini / pyproject.toml [tool.pytest] -> "pytest"
    pytest_ini = workspace / "pytest.ini"
    pyproject = workspace / "pyproject.toml"
    if pytest_ini.exists():
        log("Detected pytest (pytest.ini)")
        return ("pytest", ["pytest", "-v", "--tb=short"])
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if "[tool.pytest" in content or "[pytest" in content:
                log("Detected pytest (pyproject.toml)")
                return ("pytest", ["pytest", "-v", "--tb=short"])
        except Exception:
            pass

    # Check for tests directory with Python files (common pytest setup)
    tests_dir = workspace / "tests"
    test_dir = workspace / "test"
    if (tests_dir.exists() and any(tests_dir.glob("test_*.py"))) or \
       (test_dir.exists() and any(test_dir.glob("test_*.py"))):
        log("Detected pytest (tests directory)")
        return ("pytest", ["pytest", "-v", "--tb=short"])

    # 3. Cargo.toml -> "cargo test"
    cargo_toml = workspace / "Cargo.toml"
    if cargo_toml.exists():
        log("Detected Cargo test framework")
        return ("cargo", ["cargo", "test"])

    # 4. go.mod -> "go test ./..."
    go_mod = workspace / "go.mod"
    if go_mod.exists():
        log("Detected Go test framework")
        return ("go", ["go", "test", "-v", "./..."])

    # 5. Makefile with test target -> "make test"
    makefile = workspace / "Makefile"
    if makefile.exists():
        try:
            content = makefile.read_text()
            if "test:" in content or "test :" in content:
                log("Detected Makefile test target")
                return ("make", ["make", "test"])
        except Exception:
            pass

    log("No test framework detected")
    return (None, [])


def parse_test_output(framework: str, output: str, exit_code: int) -> dict:
    """
    Parse test output to extract pass/fail counts.
    Returns dict with tests_run, tests_passed, pass_count, fail_count, skip_count.
    """
    result = {
        "tests_run": True,
        "tests_passed": exit_code == 0,
        "pass_count": None,
        "fail_count": None,
        "skip_count": None,
        "output": output[-10000:] if len(output) > 10000 else output,  # Truncate if needed
    }

    import re

    if framework == "pytest":
        # Match: "5 passed, 2 failed, 1 skipped"
        match = re.search(r"(\d+) passed", output)
        if match:
            result["pass_count"] = int(match.group(1))
        match = re.search(r"(\d+) failed", output)
        if match:
            result["fail_count"] = int(match.group(1))
        match = re.search(r"(\d+) skipped", output)
        if match:
            result["skip_count"] = int(match.group(1))

    elif framework == "npm":
        # Jest format: "Tests: 5 passed, 2 failed, 7 total"
        match = re.search(r"Tests:\s*(\d+)\s*passed", output)
        if match:
            result["pass_count"] = int(match.group(1))
        match = re.search(r"Tests:\s*\d+\s*passed,\s*(\d+)\s*failed", output)
        if match:
            result["fail_count"] = int(match.group(1))
        # Mocha format: "5 passing" "2 failing"
        if result["pass_count"] is None:
            match = re.search(r"(\d+)\s+passing", output)
            if match:
                result["pass_count"] = int(match.group(1))
        if result["fail_count"] is None:
            match = re.search(r"(\d+)\s+failing", output)
            if match:
                result["fail_count"] = int(match.group(1))

    elif framework == "cargo":
        # Match: "test result: ok. 5 passed; 0 failed; 0 ignored"
        match = re.search(r"(\d+)\s+passed", output)
        if match:
            result["pass_count"] = int(match.group(1))
        match = re.search(r"(\d+)\s+failed", output)
        if match:
            result["fail_count"] = int(match.group(1))
        match = re.search(r"(\d+)\s+ignored", output)
        if match:
            result["skip_count"] = int(match.group(1))

    elif framework == "go":
        # Match: "ok" and "FAIL" lines, or "--- PASS:" and "--- FAIL:"
        pass_count = len(re.findall(r"--- PASS:", output))
        fail_count = len(re.findall(r"--- FAIL:", output))
        if pass_count or fail_count:
            result["pass_count"] = pass_count
            result["fail_count"] = fail_count

    return result


def run_tests(workspace: Path) -> dict | None:
    """
    Detect and run tests in the workspace.
    Returns test results dict or None if no tests detected.
    """
    framework, cmd = detect_test_framework(workspace)
    if not framework:
        return None

    log(f"Running tests with {framework}...")

    try:
        # Run tests with timeout
        result = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
        )

        output = result.stdout + "\n" + result.stderr
        exit_code = result.returncode

        # Log test output
        for line in output.strip().split("\n")[-50:]:  # Last 50 lines
            log(f"  [test] {line}")

        test_results = parse_test_output(framework, output, exit_code)

        if test_results["tests_passed"]:
            log(f"Tests PASSED (passed={test_results['pass_count']}, failed={test_results['fail_count']})")
        else:
            log(f"Tests FAILED (passed={test_results['pass_count']}, failed={test_results['fail_count']})")

        return test_results

    except subprocess.TimeoutExpired:
        log(f"Tests timed out after {TEST_TIMEOUT}s")
        return {
            "tests_run": True,
            "tests_passed": False,
            "pass_count": None,
            "fail_count": None,
            "skip_count": None,
            "output": f"Tests timed out after {TEST_TIMEOUT} seconds",
        }
    except Exception as e:
        log(f"Test execution error: {e}")
        return {
            "tests_run": True,
            "tests_passed": False,
            "pass_count": None,
            "fail_count": None,
            "skip_count": None,
            "output": f"Test execution error: {e}",
        }


# ============================================================================


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
    log(f"Runner Type: {RUNNER_TYPE}")
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
