"""
Claude Code runner implementation.

Extends BaseRunner to execute tasks using the Claude Code CLI.
"""

import json
import os
import re
from pathlib import Path

from .base_runner import BaseRunner, JobResult
from .command_helpers import run_command, run_command_streaming
from .test_helpers import parse_test_output


def normalize_agent_name(name: str) -> str:
    """Normalize agent name to CLI-safe format (lowercase, hyphenated)."""
    normalized = name.lower().strip()
    normalized = re.sub(r"[^a-z0-9-]", "-", normalized)  # Replace non-alphanumeric with hyphens
    normalized = re.sub(r"-+", "-", normalized)  # Collapse multiple hyphens
    normalized = re.sub(r"^-|-$", "", normalized)  # Remove leading/trailing hyphens
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
            continue

        # Normalize name for CLI compatibility
        cli_name = normalize_agent_name(name)
        if not cli_name:
            continue

        # Build agent definition for Claude Code CLI
        agents_dict[cli_name] = {
            "description": description or f"Agent: {cli_name}",
            "prompt": content,
        }

    if not agents_dict:
        return None

    return json.dumps(agents_dict)


class ClaudeRunner(BaseRunner):
    """
    Runner that executes tasks using Claude Code CLI.

    Usage:
        runner = ClaudeRunner()
        runner.run()
    """

    runner_type = "claude-code"

    def __init__(
        self,
        backend_url: str | None = None,
        runner_name: str | None = None,
        poll_interval: int = 5,
        workspace_path: str = "/workspace/repo",
        claude_model: str | None = None,
    ):
        """
        Initialize Claude runner.

        Args:
            backend_url: Backend URL
            runner_name: Runner name
            poll_interval: Seconds between job polls
            workspace_path: Path to workspace
            claude_model: Claude model to use (default from env or opus)
        """
        super().__init__(
            backend_url=backend_url,
            runner_name=runner_name,
            poll_interval=poll_interval,
            workspace_path=workspace_path,
        )
        self.claude_model = claude_model or os.environ.get("CLAUDE_MODEL", "opus")

    def execute_agent(
        self,
        prompt: str,
        workspace: Path,
        job: dict,
    ) -> JobResult:
        """
        Execute Claude Code CLI for the task.

        Args:
            prompt: The task prompt
            workspace: Workspace path
            job: Job dict from backend

        Returns:
            JobResult with execution status
        """
        self.log(f"Executing Claude Code (model: {self.claude_model})")

        # Build command
        cmd = [
            "claude",
            "-p",  # Print mode (non-interactive)
            "--dangerously-skip-permissions",
            "--model", self.claude_model,
        ]

        # Add agents if provided
        agent_file_ids = job.get("agent_file_ids", [])
        if agent_file_ids:
            agent_files = self._fetch_agent_files(agent_file_ids)
            agents_json = build_agents_json(agent_files)
            if agents_json:
                cmd.extend(["--agents", agents_json])

        # Add the prompt
        cmd.append(prompt)

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
                error=f"Claude Code failed with exit code {returncode}",
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

    def _fetch_agent_files(self, agent_file_ids: list[str]) -> list[dict]:
        """Fetch agent files from backend."""
        if not agent_file_ids:
            return []

        try:
            response = self.session.post(
                f"{self.backend_url}/api/agent-files/batch",
                json=agent_file_ids,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to fetch agent files: {e}")
            return []
