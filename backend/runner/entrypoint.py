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

    repo_url = job.get("repo_url", "")
    repo_path = job.get("repo_path", "")
    base_branch = job.get("base_branch", "main")
    branch_name = job.get("branch_name", "")
    card_title = job.get("card_title", "")
    card_description = job.get("card_description", "")

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
                run_command(["rm", "-rf", str(workspace)])
            exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
            if exit_code != 0:
                raise Exception("Failed to clone repository")
        elif workspace.exists():
            log("Using mounted repository")
        else:
            raise Exception("No repository available")

        # Create feature branch
        log(f"Creating branch {branch_name} from {base_branch}...")
        run_command(["git", "fetch", "origin"], cwd=str(workspace))
        run_command(["git", "checkout", base_branch], cwd=str(workspace))
        run_command(["git", "pull", "origin", base_branch], cwd=str(workspace))
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

        # Check for changes
        exit_code, stdout, _ = run_command(["git", "status", "--porcelain"], cwd=str(workspace))
        if not stdout.strip():
            log("No changes made by Claude Code")
            complete_job(success=True, error="No changes were needed")
            return

        # Commit changes
        log("Committing changes...")
        run_command(["git", "add", "-A"], cwd=str(workspace))
        run_command(
            ["git", "commit", "-m", f"feat: {card_title}\n\nImplemented by LazyAF agent"],
            cwd=str(workspace),
        )

        # Push branch
        log(f"Pushing branch {branch_name}...")
        exit_code, _, _ = run_command(
            ["git", "push", "-u", "origin", branch_name],
            cwd=str(workspace),
        )
        if exit_code != 0:
            raise Exception("Failed to push branch")

        # Create PR
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

        pr_url = None
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
