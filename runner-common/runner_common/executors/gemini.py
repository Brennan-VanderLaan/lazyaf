"""
Gemini CLI executor implementation.

This executor invokes the Gemini CLI with the appropriate flags.
"""

import json
from pathlib import Path
from typing import Optional, Callable

from .base import AgentExecutor, ExecutorConfig, ExecutorResult


class GeminiExecutor(AgentExecutor):
    """
    Executor for Gemini CLI.

    Invokes: gemini -p <prompt> --yolo

    Note: Requires setup_config() to be called before first execution
    to configure auto-accept and trusted folders.
    """

    @property
    def name(self) -> str:
        return "Gemini"

    @property
    def runner_type(self) -> str:
        return "gemini"

    def build_command(self, config: ExecutorConfig) -> list[str]:
        """
        Build the Gemini CLI command.

        Args:
            config: Executor configuration with prompt.

        Returns:
            Command list: ['gemini', '-p', prompt, '--yolo']
        """
        return [
            "gemini",
            "-p", config.prompt,
            "--yolo",
        ]

    def setup_config(self, log_callback: Optional[Callable[[str], None]] = None) -> None:
        """
        Set up Gemini CLI configuration for automated operation.

        Creates:
        - ~/.gemini/settings.json with autoAccept enabled
        - ~/.gemini/trustedFolders.json with workspace paths

        Args:
            log_callback: Optional callback for log messages.
        """
        gemini_dir = Path.home() / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)

        # Configure settings for auto-accept and no prompts
        settings_path = gemini_dir / "settings.json"
        settings = {
            "tools": {
                "autoAccept": True,
            },
            "security": {
                "disableYoloMode": False,
            }
        }

        try:
            settings_path.write_text(json.dumps(settings, indent=2))
            if log_callback:
                log_callback(f"Configured Gemini settings at {settings_path}")
        except Exception as e:
            if log_callback:
                log_callback(f"Warning: Failed to configure Gemini settings: {e}")

        # Trust the workspace folder
        trusted_path = gemini_dir / "trustedFolders.json"
        trusted = {
            "folders": ["/workspace", "/workspace/repo"]
        }

        try:
            trusted_path.write_text(json.dumps(trusted, indent=2))
            if log_callback:
                log_callback(f"Configured trusted folders at {trusted_path}")
        except Exception as e:
            if log_callback:
                log_callback(f"Warning: Failed to configure trusted folders: {e}")

    def execute(
        self,
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        streaming: bool = True,
    ) -> ExecutorResult:
        """
        Execute Gemini with the given configuration.

        Automatically sets up Gemini config before first execution.

        Args:
            config: Executor configuration.
            log_callback: Optional callback for log lines.
            streaming: If True, stream output in real-time.

        Returns:
            ExecutorResult with success status, output, and any errors.
        """
        # Ensure config is set up
        self.setup_config(log_callback)

        # Call parent execute
        return super().execute(config, log_callback, streaming)
