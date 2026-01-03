"""
Gemini runner implementation.

Extends BaseRunner to execute tasks using the Gemini CLI.
"""

import json
import os
from pathlib import Path

from .base_runner import BaseRunner, JobResult
from .command_helpers import run_command, run_command_streaming
from .test_helpers import parse_test_output


def setup_gemini_config(workspace: Path) -> None:
    """
    Set up Gemini CLI configuration for automated operation.

    Creates a .gemini-settings.json in the workspace with settings
    optimized for non-interactive operation.
    """
    settings = {
        "autoApprove": True,
        "confirmBeforeApply": False,
    }

    settings_path = workspace / ".gemini-settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))


class GeminiRunner(BaseRunner):
    """
    Runner that executes tasks using Gemini CLI.

    Usage:
        runner = GeminiRunner()
        runner.run()
    """

    runner_type = "gemini"

    def __init__(
        self,
        backend_url: str | None = None,
        runner_name: str | None = None,
        poll_interval: int = 5,
        workspace_path: str = "/workspace/repo",
        gemini_model: str | None = None,
    ):
        """
        Initialize Gemini runner.

        Args:
            backend_url: Backend URL
            runner_name: Runner name
            poll_interval: Seconds between job polls
            workspace_path: Path to workspace
            gemini_model: Gemini model to use
        """
        super().__init__(
            backend_url=backend_url,
            runner_name=runner_name,
            poll_interval=poll_interval,
            workspace_path=workspace_path,
        )
        self.gemini_model = gemini_model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    def execute_agent(
        self,
        prompt: str,
        workspace: Path,
        job: dict,
    ) -> JobResult:
        """
        Execute Gemini CLI for the task.

        Args:
            prompt: The task prompt
            workspace: Workspace path
            job: Job dict from backend

        Returns:
            JobResult with execution status
        """
        self.log(f"Executing Gemini CLI (model: {self.gemini_model})")

        # Set up config for non-interactive operation
        setup_gemini_config(workspace)

        # Build command
        # Note: Gemini CLI invocation may vary - this is a placeholder
        # that matches the expected interface
        cmd = [
            "gemini",
            "--model", self.gemini_model,
            "--auto-approve",
            "--message", prompt,
        ]

        # Execute with streaming
        returncode, stdout, stderr = run_command_streaming(
            cmd,
            cwd=str(workspace),
            log_func=self.log,
        )

        output = stdout + ("\n" + stderr if stderr else "")

        if returncode != 0:
            return JobResult(
                success=False,
                error=f"Gemini CLI failed with exit code {returncode}",
                logs=output,
            )

        # Check for test output in the logs
        test_results = None
        if "passed" in output.lower() or "failed" in output.lower():
            test_results = parse_test_output(output)

        return JobResult(
            success=True,
            logs=output,
            test_results=test_results,
        )
