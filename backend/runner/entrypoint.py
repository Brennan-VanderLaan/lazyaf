#!/usr/bin/env python3
"""
LazyAF Runner - Persistent worker that registers with backend and executes jobs.
"""

import os
import subprocess
import sys
import time
import threading
from pathlib import Path

import requests

# Configuration from environment
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RUNNER_NAME = os.environ.get("RUNNER_NAME", None)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))

# Global state
runner_id = None
session = requests.Session()


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
    """Register with the backend."""
    global runner_id
    try:
        response = session.post(
            f"{BACKEND_URL}/api/runners/register",
            json={"name": RUNNER_NAME},
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
    """Send heartbeat to backend."""
    if not runner_id:
        return False
    try:
        response = session.post(
            f"{BACKEND_URL}/api/runners/{runner_id}/heartbeat",
            timeout=5,
        )
        return response.status_code == 200
    except Exception:
        return False


def poll_for_job():
    """Poll for a job from the backend."""
    if not runner_id:
        return None
    try:
        response = session.get(
            f"{BACKEND_URL}/api/runners/{runner_id}/job",
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("job")
    except Exception as e:
        log(f"Failed to poll for job: {e}")
        return None


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


def execute_job(job: dict):
    """Execute a job."""
    log(f"Starting job {job['id']}: {job['card_title']}")

    repo_id = job.get("repo_id", "")
    repo_url = job.get("repo_url", "")
    repo_path = job.get("repo_path", "")
    base_branch = job.get("base_branch", "main")
    branch_name = job.get("branch_name", "")
    card_title = job.get("card_title", "")
    card_description = job.get("card_description", "")
    use_internal_git = job.get("use_internal_git", False)

    # If using internal git, construct URL from backend URL
    if use_internal_git and repo_id:
        repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
        log(f"Using internal git server: {repo_url}")

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

        # Create feature branch from current HEAD
        run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))

        # Build prompt for Claude
        prompt = build_prompt(card_title, card_description, workspace)

        # Invoke Claude Code
        log("Invoking Claude Code...")
        exit_code, stdout, stderr = run_command(
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

        complete_job(success=True, pr_url=pr_url)
        log("Job completed successfully!")

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(success=False, error=str(e))


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


def main():
    log(f"LazyAF Runner starting...")
    log(f"Backend URL: {BACKEND_URL}")

    # Keep trying to register
    while not register():
        log("Retrying registration in 5 seconds...")
        time.sleep(5)

    log("Waiting for jobs...")

    # Main loop
    try:
        while True:
            job = poll_for_job()
            if job:
                execute_job(job)
                log("Waiting for next job...")
            else:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log("Shutting down...")


if __name__ == "__main__":
    main()
