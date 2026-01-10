#!/usr/bin/env python3
"""
LazyAF Unified Runner Entrypoint

A single entrypoint that works for all runner types (claude-code, gemini, mock).
Uses shared modules for common functionality and dispatches to type-specific executors.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

import requests

from .job_helpers import (
    HeartbeatThread,
    NeedsReregister,
    complete_job,
    log_to_backend,
    poll_for_job,
    register,
    report_status,
)
from .git_helpers import clone, checkout, get_sha, push, configure_git, GitError
from .context_helpers import (
    init_context,
    write_step_log,
    update_metadata,
    read_metadata,
    cleanup_context,
    context_exists,
    get_context_path,
)
from .executors import (
    ClaudeExecutor,
    GeminiExecutor,
    MockExecutor,
    ExecutorConfig,
    ExecutorResult,
)


# Configuration from environment
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RUNNER_TYPE = os.environ.get("RUNNER_TYPE", "claude-code")
RUNNER_NAME = os.environ.get("RUNNER_NAME", None)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
RECONNECT_INTERVAL = 5
MAX_RECONNECT_BACKOFF = 60
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "300"))

# Generate persistent runner ID
RUNNER_UUID = str(uuid4())

# Global state
runner_id: Optional[str] = None
session = requests.Session()
needs_reregister_flag = False

# Executor registry
EXECUTORS = {
    "claude-code": ClaudeExecutor,
    "gemini": GeminiExecutor,
    "mock": MockExecutor,
}


def log(msg: str) -> None:
    """Log a message locally and to backend."""
    print(f"[runner] {msg}", flush=True)
    if runner_id:
        try:
            log_to_backend(runner_id, msg, BACKEND_URL)
        except Exception:
            pass


def get_workspace(pipeline_run_id: Optional[str] = None) -> Path:
    """Get workspace path, optionally scoped to pipeline run."""
    if pipeline_run_id:
        return Path(f"/workspace/{pipeline_run_id[:8]}/repo")
    return Path("/workspace/repo")


def cleanup_workspace(workspace: Optional[Path] = None) -> None:
    """Clean up workspace directory."""
    workspace = workspace or Path("/workspace/repo")
    if not workspace.exists():
        return

    log("Cleaning up workspace...")
    try:
        result = subprocess.run(
            ["rm", "-rf", str(workspace)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            subprocess.run(
                ["sudo", "rm", "-rf", str(workspace)],
                capture_output=True,
                text=True,
            )
        log("Workspace cleaned")
    except Exception as e:
        log(f"Warning: Failed to clean workspace: {e}")


def setup_repo(
    job: dict,
    workspace: Path,
    is_continuation: bool = False,
) -> Optional[str]:
    """
    Set up repository for job execution.

    Returns base commit SHA or None.
    """
    if is_continuation and workspace.exists():
        log("Continuing from previous step - using existing workspace")
        try:
            return get_sha(workspace)
        except GitError:
            return None

    repo_id = job.get("repo_id", "")
    use_internal_git = job.get("use_internal_git", False)
    base_branch = job.get("base_branch", "main")
    branch_name = job.get("branch_name")

    if not use_internal_git or not repo_id:
        return None

    repo_url = f"{BACKEND_URL}/git/{repo_id}.git"
    log(f"Cloning from internal git: {repo_url}")

    # Configure git
    configure_git("lazyaf@localhost", "LazyAF Agent")

    # Clean and clone
    if workspace.exists():
        cleanup_workspace(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    try:
        clone(repo_url, workspace)
    except GitError as e:
        raise Exception(f"Failed to clone repository: {e}")

    # Checkout branch
    if branch_name:
        try:
            checkout(workspace, branch_name)
        except GitError:
            # Create new branch from base
            log(f"Creating new branch: {branch_name}")
            try:
                checkout(workspace, base_branch)
            except GitError:
                pass
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(workspace),
                capture_output=True,
            )
    elif base_branch:
        try:
            checkout(workspace, base_branch)
        except GitError:
            try:
                checkout(workspace, f"origin/{base_branch}")
            except GitError:
                log(f"Warning: Could not checkout {base_branch}")

    # Get base commit
    try:
        return get_sha(workspace)
    except GitError:
        return None


def build_prompt(job: dict, workspace: Path, previous_logs: Optional[str] = None) -> str:
    """Build prompt for agent execution."""
    card_title = job.get("card_title", "")
    card_description = job.get("card_description", "")
    prompt_template = job.get("prompt_template", "")

    if prompt_template:
        prompt = prompt_template.replace("{{title}}", card_title)
        prompt = prompt.replace("{{description}}", card_description)
    else:
        # Default prompt
        readme_content = ""
        for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
            readme_path = workspace / readme_name
            if readme_path.exists():
                try:
                    readme_content = readme_path.read_text()[:2000]
                    break
                except Exception:
                    pass

        prompt = f"""You are implementing a feature for this project.

## Feature Request
Title: {card_title}

Description:
{card_description}

## Instructions
1. Implement this feature following existing code patterns
2. Write tests if a test framework is present
3. Commit your changes with a clear message
4. Do not modify unrelated code
5. Keep changes minimal and focused
"""
        if readme_content:
            prompt += f"\n## Repository Context (from README)\n{readme_content}\n"

    # Add previous step logs if available
    if previous_logs:
        prompt += f"""

## Previous Step Output
The previous pipeline step produced the following output:
```
{previous_logs}
```

Use this context when completing the current task.
"""

    return prompt


def get_executor():
    """Get the executor for the current runner type."""
    executor_class = EXECUTORS.get(RUNNER_TYPE)
    if not executor_class:
        raise ValueError(f"Unknown runner type: {RUNNER_TYPE}")
    return executor_class()


def run_tests(workspace: Path) -> Optional[dict]:
    """Detect and run tests in the workspace."""
    # Check for test frameworks
    package_json = workspace / "package.json"
    pyproject = workspace / "pyproject.toml"
    pytest_ini = workspace / "pytest.ini"
    setup_py = workspace / "setup.py"

    framework = None
    cmd = []

    if package_json.exists():
        try:
            import json
            pkg = json.loads(package_json.read_text())
            if "test" in pkg.get("scripts", {}):
                framework = "npm"
                cmd = ["npm", "test"]
        except Exception:
            pass

    if not framework and (pyproject.exists() or pytest_ini.exists() or setup_py.exists()):
        # Check for pytest
        tests_dir = workspace / "tests"
        test_dir = workspace / "test"
        if tests_dir.exists() or test_dir.exists():
            framework = "pytest"
            cmd = ["pytest", "-v", "--tb=short"]

    if not framework:
        return None

    log(f"Running tests with {framework}...")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
        )

        output = result.stdout + "\n" + result.stderr
        exit_code = result.returncode

        # Log last 50 lines
        for line in output.strip().split("\n")[-50:]:
            log(f"  [test] {line}")

        # Parse results (simplified)
        tests_passed = exit_code == 0

        return {
            "tests_run": True,
            "tests_passed": tests_passed,
            "pass_count": None,
            "fail_count": None,
            "skip_count": None,
            "output": output[-5000:],
        }

    except subprocess.TimeoutExpired:
        log(f"Tests timed out after {TEST_TIMEOUT}s")
        return {
            "tests_run": True,
            "tests_passed": False,
            "output": f"Tests timed out after {TEST_TIMEOUT} seconds",
        }
    except Exception as e:
        log(f"Test execution error: {e}")
        return {
            "tests_run": True,
            "tests_passed": False,
            "output": f"Test execution error: {e}",
        }


def execute_agent_step(job: dict) -> None:
    """Execute an agent step (AI implements feature)."""
    job_id = job["id"]
    pipeline_run_id = job.get("pipeline_run_id")
    is_continuation = job.get("is_continuation", False)
    continue_in_context = job.get("continue_in_context", False)
    step_name = job.get("step_name", "agent")
    step_index = job.get("step_index", 0)
    branch_name = job.get("branch_name")

    workspace = get_workspace(pipeline_run_id)

    # Log context info
    log("=" * 50)
    log("CONTEXT INFO:")
    if pipeline_run_id:
        log(f"  - Pipeline run: {pipeline_run_id[:8]}")
        log(f"  - Step: {step_index} ({step_name})")
    if is_continuation:
        log("  - Continuing from previous step")
    if continue_in_context:
        log("  - Will preserve workspace for next step")
    log("=" * 50)

    report_status(job_id, "running", BACKEND_URL)

    try:
        # Setup repo
        base_commit = setup_repo(job, workspace, is_continuation)

        # Initialize context for pipeline steps
        if pipeline_run_id and step_index == 0:
            init_context(workspace, pipeline_run_id)

        # Get previous step logs if continuing
        previous_logs = None
        if is_continuation and pipeline_run_id:
            prev_log = workspace / ".lazyaf-context" / f"step_{step_index - 1}.log"
            if prev_log.exists():
                previous_logs = prev_log.read_text()

        # Build prompt
        prompt = build_prompt(job, workspace, previous_logs)

        # Get executor and run
        executor = get_executor()
        log(f"Using executor: {executor.name}")

        # Build config
        config = ExecutorConfig(
            workspace=workspace,
            prompt=prompt,
            model=job.get("model"),
            agents_json=job.get("agents_json"),
        )

        # Execute
        result = executor.execute(config, log_callback=log)

        if not result.success:
            raise Exception(f"Agent failed: {result.error}")

        # Check for changes and commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
        )
        has_changes = bool(status_result.stdout.strip())

        if has_changes:
            log("Committing changes...")
            subprocess.run(["git", "add", "-A"], cwd=str(workspace))
            card_title = job.get("card_title", "Feature")
            subprocess.run(
                ["git", "commit", "-m", f"feat: {card_title}\n\nImplemented by LazyAF agent"],
                cwd=str(workspace),
            )

        # Push if we have a branch
        if branch_name:
            log(f"Pushing branch {branch_name}...")
            try:
                push(workspace, branch_name, set_upstream=True)
            except GitError as e:
                raise Exception(f"Failed to push: {e}")

        # Run tests
        test_results = run_tests(workspace)
        job_success = True
        if test_results and not test_results.get("tests_passed"):
            job_success = False
            log("Job marked as failed due to test failures")

        # Write context
        if pipeline_run_id:
            step_output = f"Step {step_index}: {step_name}\n"
            if test_results:
                step_output += f"Tests passed: {test_results.get('tests_passed')}\n"
            write_step_log(workspace, step_index, step_output, step_name)
            update_metadata(workspace, f"step_{step_index}_completed", True)

        complete_job(runner_id, job_success, BACKEND_URL, test_results=test_results)
        log("Job completed!" if job_success else "Job completed with failures")

    except Exception as e:
        log(f"ERROR: {e}")
        if pipeline_run_id:
            try:
                write_step_log(workspace, step_index, f"ERROR: {e}", step_name)
            except Exception:
                pass
        complete_job(runner_id, False, BACKEND_URL, error=str(e))

    finally:
        if not continue_in_context:
            cleanup_workspace(workspace)
        else:
            log("Preserving workspace for next step")


def execute_script_step(job: dict) -> None:
    """Execute a script step (shell command)."""
    job_id = job["id"]
    step_config = job.get("step_config", {}) or {}
    command = step_config.get("command", "")
    pipeline_run_id = job.get("pipeline_run_id")
    is_continuation = job.get("is_continuation", False)
    continue_in_context = job.get("continue_in_context", False)
    step_name = job.get("step_name", "script")
    step_index = job.get("step_index", 0)

    workspace = get_workspace(pipeline_run_id)

    if not command:
        log("ERROR: No command specified")
        complete_job(runner_id, False, BACKEND_URL, error="No command specified")
        return

    # Log context
    log("=" * 50)
    log(f"SCRIPT STEP: {step_name}")
    if pipeline_run_id:
        log(f"  - Pipeline: {pipeline_run_id[:8]}")
    log("=" * 50)

    report_status(job_id, "running", BACKEND_URL)

    try:
        # Setup repo
        setup_repo(job, workspace, is_continuation)

        if pipeline_run_id and step_index == 0:
            init_context(workspace, pipeline_run_id)

        # Normalize command
        command = command.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\r\n', '\n')

        # Write script
        script_path = workspace / ".lazyaf_script.sh"
        script_path.write_text(f"#!/bin/bash\nset -e\n{command}\n")
        subprocess.run(["chmod", "+x", str(script_path)])

        # Execute
        log(f"Running script in {workspace}...")
        result = subprocess.run(
            ["bash", str(script_path)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
        )

        # Clean up script
        try:
            script_path.unlink()
        except Exception:
            pass

        step_output = f"Exit code: {result.returncode}\n\n--- STDOUT ---\n{result.stdout}\n\n--- STDERR ---\n{result.stderr}"

        # Write context
        if pipeline_run_id:
            write_step_log(workspace, step_index, step_output, step_name)

        if result.returncode == 0:
            log("Script completed successfully")
            complete_job(runner_id, True, BACKEND_URL)
        else:
            log(f"Script failed with exit code {result.returncode}")
            complete_job(runner_id, False, BACKEND_URL, error=f"Exit code {result.returncode}")

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(runner_id, False, BACKEND_URL, error=str(e))

    finally:
        if not continue_in_context:
            cleanup_workspace(workspace)


def execute_docker_step(job: dict) -> None:
    """Execute a docker step (command in container)."""
    job_id = job["id"]
    step_config = job.get("step_config", {}) or {}
    image = step_config.get("image", "")
    command = step_config.get("command", "")
    pipeline_run_id = job.get("pipeline_run_id")
    is_continuation = job.get("is_continuation", False)
    continue_in_context = job.get("continue_in_context", False)
    step_name = job.get("step_name", "docker")
    step_index = job.get("step_index", 0)

    workspace = get_workspace(pipeline_run_id)

    if not image or not command:
        log("ERROR: Image and command required")
        complete_job(runner_id, False, BACKEND_URL, error="Image and command required")
        return

    log("=" * 50)
    log(f"DOCKER STEP: {step_name}")
    log(f"  Image: {image}")
    log("=" * 50)

    report_status(job_id, "running", BACKEND_URL)

    try:
        # Setup repo
        setup_repo(job, workspace, is_continuation)

        if pipeline_run_id and step_index == 0:
            init_context(workspace, pipeline_run_id)

        # Normalize command
        command = command.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\r\n', '\n')

        # Write script
        script_path = workspace / ".lazyaf_script.sh"
        script_path.write_text(f"#!/bin/bash\nset -e\n{command}\n")
        subprocess.run(["chmod", "+x", str(script_path)])

        # Build docker command
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{workspace}:/workspace",
            "-w", "/workspace",
        ]

        # Add env vars
        for key, value in step_config.get("env", {}).items():
            docker_cmd.extend(["-e", f"{key}={value}"])

        docker_cmd.extend([image, "bash", "/workspace/.lazyaf_script.sh"])

        # Execute
        log(f"Running in container: {image}")
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
        )

        # Clean up
        try:
            script_path.unlink()
        except Exception:
            pass

        step_output = f"Exit code: {result.returncode}\n\n--- STDOUT ---\n{result.stdout}\n\n--- STDERR ---\n{result.stderr}"

        if pipeline_run_id:
            write_step_log(workspace, step_index, step_output, step_name)

        if result.returncode == 0:
            log("Docker step completed successfully")
            complete_job(runner_id, True, BACKEND_URL)
        else:
            log(f"Docker step failed with exit code {result.returncode}")
            complete_job(runner_id, False, BACKEND_URL, error=f"Exit code {result.returncode}")

    except Exception as e:
        log(f"ERROR: {e}")
        complete_job(runner_id, False, BACKEND_URL, error=str(e))

    finally:
        if not continue_in_context:
            cleanup_workspace(workspace)


def execute_job(job: dict) -> None:
    """Execute a job based on its step type."""
    step_type = job.get("step_type", "agent")
    job_id = job.get("id", "unknown")
    is_playground = job.get("is_playground", False)

    log(f"Job {job_id[:8]}: step_type={step_type}, is_playground={is_playground}")

    # TODO: Add playground support
    if is_playground:
        log("Playground jobs not yet supported in unified entrypoint")
        complete_job(runner_id, False, BACKEND_URL, error="Playground not supported")
        return

    if step_type == "script":
        execute_script_step(job)
    elif step_type == "docker":
        execute_docker_step(job)
    else:
        execute_agent_step(job)


def wait_for_backend() -> bool:
    """Wait for backend to become available."""
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


def main() -> None:
    """Main entry point."""
    global runner_id, needs_reregister_flag

    log("LazyAF Unified Runner starting...")
    log(f"Runner Type: {RUNNER_TYPE}")
    log(f"Runner UUID: {RUNNER_UUID}")
    log(f"Backend URL: {BACKEND_URL}")

    # Validate runner type
    if RUNNER_TYPE not in EXECUTORS:
        log(f"ERROR: Unknown runner type '{RUNNER_TYPE}'")
        log(f"Available types: {list(EXECUTORS.keys())}")
        sys.exit(1)

    try:
        while True:
            needs_reregister_flag = False

            # Wait for backend
            wait_for_backend()

            # Register
            backoff = RECONNECT_INTERVAL
            while True:
                try:
                    result = register(RUNNER_TYPE, BACKEND_URL, RUNNER_NAME, RUNNER_UUID)
                    runner_id = result["runner_id"]
                    log(f"Registered as {result.get('name', runner_id)} (id: {runner_id})")
                    break
                except Exception as e:
                    log(f"Registration failed: {e}, retrying in {backoff}s...")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)

            # Start heartbeat thread
            heartbeat_thread = HeartbeatThread(runner_id, BACKEND_URL)
            heartbeat_thread.start()
            log("Started heartbeat thread")

            log("Waiting for jobs...")

            # Job polling loop
            while not needs_reregister_flag:
                try:
                    job = poll_for_job(runner_id, BACKEND_URL)
                    if job:
                        execute_job(job)
                        log("Waiting for next job...")
                    else:
                        time.sleep(POLL_INTERVAL)
                except NeedsReregister:
                    log("Backend requires re-registration")
                    needs_reregister_flag = True
                except Exception as e:
                    log(f"Polling error: {e}")
                    time.sleep(POLL_INTERVAL)

            # Cleanup
            log("Connection lost - will reconnect...")
            heartbeat_thread.stop()
            runner_id = None
            time.sleep(RECONNECT_INTERVAL)

    except KeyboardInterrupt:
        log("Shutting down...")


if __name__ == "__main__":
    main()
