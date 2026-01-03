"""
Base runner class for LazyAF agent runners.

This is the unified runner implementation that handles:
- Registration with backend
- Job polling
- Workspace setup
- Test execution and reporting
- Context directory management
- Heartbeat and logging

Subclasses (ClaudeRunner, GeminiRunner) only need to implement
the agent-specific execution logic.
"""

import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import requests

from .git_helpers import clone, checkout, get_sha, push, commit, add, GitError
from .context_helpers import (
    init_context_directory,
    write_step_log,
    update_context_metadata,
    get_previous_step_logs,
    commit_context_changes,
    CONTEXT_DIR,
)
from .job_helpers import JobHelpers
from .command_helpers import run_command, run_command_streaming, cleanup_workspace
from .test_helpers import detect_test_framework, parse_test_output, TestResults


@dataclass
class JobResult:
    """Result of executing a job."""
    success: bool
    error: str | None = None
    pr_url: str | None = None
    test_results: TestResults | None = None
    logs: str = ""


class BaseRunner(ABC):
    """
    Base class for LazyAF runners.

    Handles all common runner functionality. Subclasses only need
    to implement execute_agent() for their specific agent CLI.
    """

    # Must be set by subclass
    runner_type: str = "generic"

    def __init__(
        self,
        backend_url: str | None = None,
        runner_name: str | None = None,
        poll_interval: int = 5,
        workspace_path: str = "/workspace/repo",
    ):
        """
        Initialize the runner.

        Args:
            backend_url: Backend URL (default from BACKEND_URL env)
            runner_name: Runner name (default from RUNNER_NAME env)
            poll_interval: Seconds between job polls
            workspace_path: Path to workspace directory
        """
        self.backend_url = backend_url or os.environ.get("BACKEND_URL", "http://localhost:8000")
        self.runner_name = runner_name or os.environ.get("RUNNER_NAME")
        self.poll_interval = poll_interval
        self.workspace_path = Path(workspace_path)

        # Generate persistent runner ID for this process
        self.runner_uuid = os.environ.get("RUNNER_UUID", str(uuid4()))

        # State
        self.runner_id: str | None = None
        self.session = requests.Session()
        self.helpers: JobHelpers | None = None
        self._stop_event = threading.Event()
        self._needs_reregister = threading.Event()
        self._log_buffer: list[str] = []

    def log(self, msg: str) -> None:
        """Log a message to console and backend."""
        print(f"[runner] {msg}", flush=True)
        self._log_buffer.append(msg)

        if self.helpers:
            self.helpers.log(msg)

    def register(self) -> bool:
        """
        Register with the backend.

        Returns:
            True if registration succeeded
        """
        try:
            response = self.session.post(
                f"{self.backend_url}/api/runners/register",
                json={
                    "runner_id": self.runner_uuid,
                    "name": self.runner_name,
                    "runner_type": self.runner_type,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            self.runner_id = data["runner_id"]
            self.helpers = JobHelpers(self.backend_url, self.runner_id)
            self.log(f"Registered as {data['name']} (type: {self.runner_type}, id: {self.runner_id})")
            return True
        except Exception as e:
            print(f"[runner] Failed to register: {e}", flush=True)
            return False

    def poll_for_job(self) -> dict | None:
        """
        Poll backend for a job.

        Returns:
            Job dict or None
        """
        if not self.runner_id:
            return None

        try:
            response = self.session.get(
                f"{self.backend_url}/api/runners/{self.runner_id}/job",
                timeout=10,
            )
            if response.status_code == 404:
                self.log("Backend doesn't recognize this runner - will re-register")
                self._needs_reregister.set()
                return None
            response.raise_for_status()
            return response.json().get("job")
        except requests.exceptions.ConnectionError:
            self.log("Lost connection to backend")
            self._needs_reregister.set()
            return None
        except Exception as e:
            self.log(f"Failed to poll for job: {e}")
            return None

    def setup_workspace(self, job: dict) -> bool:
        """
        Set up the workspace for a job.

        Args:
            job: Job dict from backend

        Returns:
            True if setup succeeded
        """
        repo_url = job.get("repo_url")
        base_branch = job.get("base_branch", "main")
        is_continuation = job.get("is_continuation", False)

        if is_continuation:
            self.log("Continuation job - reusing existing workspace")
            if not self.workspace_path.exists():
                self.log("ERROR: Workspace doesn't exist for continuation")
                return False
            return True

        # Clean up existing workspace
        if self.workspace_path.exists():
            cleanup_workspace(self.workspace_path)

        # Clone the repo
        try:
            self.log(f"Cloning {repo_url} (branch: {base_branch})")
            clone(repo_url, str(self.workspace_path), branch=base_branch)

            # Configure git for commits
            run_command(
                ["git", "config", "user.email", "lazyaf@local"],
                cwd=str(self.workspace_path),
            )
            run_command(
                ["git", "config", "user.name", "LazyAF Runner"],
                cwd=str(self.workspace_path),
            )

            return True

        except GitError as e:
            self.log(f"Failed to clone: {e}")
            return False

    def execute_job(self, job: dict) -> JobResult:
        """
        Execute a job.

        Args:
            job: Job dict from backend

        Returns:
            JobResult with success/failure info
        """
        step_type = job.get("step_type", "agent")
        step_config = job.get("step_config", {})

        # Set up workspace
        if not self.setup_workspace(job):
            return JobResult(
                success=False,
                error="Failed to set up workspace",
            )

        # Initialize context directory for pipeline jobs
        pipeline_run_id = job.get("pipeline_run_id")
        if pipeline_run_id:
            init_context_directory(self.workspace_path, pipeline_run_id)

        # Load previous step logs for context
        previous_logs = ""
        if job.get("is_continuation"):
            logs = get_previous_step_logs(self.workspace_path)
            if logs:
                previous_logs = "\n\n---\nPrevious step logs:\n" + "\n---\n".join(logs)

        try:
            if step_type == "agent":
                result = self._execute_agent_step(job, previous_logs)
            elif step_type == "script":
                result = self._execute_script_step(job, step_config)
            elif step_type == "docker":
                result = self._execute_docker_step(job, step_config)
            else:
                return JobResult(
                    success=False,
                    error=f"Unknown step type: {step_type}",
                )

            # Write step log if this is a pipeline job
            if pipeline_run_id:
                step_index = job.get("step_index", 0)
                step_name = job.get("step_name", "Step")
                step_id = job.get("step_id")

                write_step_log(
                    self.workspace_path,
                    step_index,
                    step_id,
                    step_name,
                    result.logs,
                )
                update_context_metadata(self.workspace_path, step_index, step_name)

                # Commit context if continuing
                if job.get("continue_in_context"):
                    commit_context_changes(self.workspace_path, step_name)

            return result

        except Exception as e:
            return JobResult(
                success=False,
                error=str(e),
            )

    def _execute_agent_step(self, job: dict, previous_logs: str) -> JobResult:
        """Execute an agent step."""
        # Build prompt with context
        card_title = job.get("card_title", "")
        card_description = job.get("card_description", "")
        prompt = f"{card_title}\n\n{card_description}"
        if previous_logs:
            prompt += previous_logs

        # Execute agent (subclass implements this)
        return self.execute_agent(
            prompt=prompt,
            workspace=self.workspace_path,
            job=job,
        )

    def _execute_script_step(self, job: dict, config: dict) -> JobResult:
        """Execute a script step."""
        command = config.get("command", "")
        if not command:
            return JobResult(success=False, error="No command specified")

        self.log(f"Running script: {command}")

        # Run the command
        returncode, stdout, stderr = run_command_streaming(
            ["bash", "-c", command],
            cwd=str(self.workspace_path),
            log_func=self.log,
        )

        success = returncode == 0
        output = stdout + "\n" + stderr if stderr else stdout

        # Check for test output
        test_results = None
        if "test" in command.lower() or "pytest" in command or "jest" in command:
            test_results = parse_test_output(output)

        return JobResult(
            success=success,
            error=None if success else f"Command failed with exit code {returncode}",
            test_results=test_results,
            logs=output,
        )

    def _execute_docker_step(self, job: dict, config: dict) -> JobResult:
        """Execute a docker step."""
        image = config.get("image", "")
        command = config.get("command", "")

        if not image:
            return JobResult(success=False, error="No image specified")

        self.log(f"Running in docker: {image}")

        # Build docker run command
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{self.workspace_path}:/workspace",
            "-w", "/workspace",
            image,
        ]
        if command:
            docker_cmd.extend(["bash", "-c", command])

        returncode, stdout, stderr = run_command_streaming(
            docker_cmd,
            log_func=self.log,
        )

        success = returncode == 0
        output = stdout + "\n" + stderr if stderr else stdout

        return JobResult(
            success=success,
            error=None if success else f"Docker command failed with exit code {returncode}",
            logs=output,
        )

    @abstractmethod
    def execute_agent(
        self,
        prompt: str,
        workspace: Path,
        job: dict,
    ) -> JobResult:
        """
        Execute the agent for a job.

        This is the only method subclasses need to implement.

        Args:
            prompt: The task prompt (card title + description + context)
            workspace: Path to workspace directory
            job: Full job dict from backend

        Returns:
            JobResult with success/failure and any test results
        """
        pass

    def complete_job(self, result: JobResult) -> None:
        """
        Report job completion to backend.

        Args:
            result: JobResult from execution
        """
        if not self.helpers:
            return

        test_results = result.test_results.to_dict() if result.test_results else None

        self.helpers.complete(
            success=result.success,
            error=result.error,
            pr_url=result.pr_url,
            test_results=test_results,
        )

    def run(self) -> None:
        """
        Main run loop.

        Registers with backend and polls for jobs until stopped.
        """
        self.log(f"Starting {self.runner_type} runner...")
        self.log(f"Backend: {self.backend_url}")
        self.log(f"Workspace: {self.workspace_path}")

        reconnect_delay = 5

        while not self._stop_event.is_set():
            # Register if needed
            if not self.runner_id or self._needs_reregister.is_set():
                if self.register():
                    self._needs_reregister.clear()
                    reconnect_delay = 5  # Reset backoff
                else:
                    # Exponential backoff
                    self.log(f"Registration failed, retrying in {reconnect_delay}s...")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
                    continue

            # Poll for job
            job = self.poll_for_job()

            if job:
                self.log(f"Got job: {job.get('card_title', 'Unknown')}")

                # Start heartbeat thread
                if self.helpers:
                    self.helpers.start_heartbeat_thread()

                try:
                    result = self.execute_job(job)
                    self.complete_job(result)
                finally:
                    # Stop heartbeat thread
                    if self.helpers:
                        self.helpers.stop_heartbeat_thread()
            else:
                # No job - wait before polling again
                time.sleep(self.poll_interval)

        self.log("Runner stopped")

    def stop(self) -> None:
        """Stop the run loop."""
        self._stop_event.set()
