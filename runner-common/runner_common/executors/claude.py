"""
Claude Code executor implementation.

This executor invokes the Claude Code CLI with the appropriate flags.
"""

from .base import AgentExecutor, ExecutorConfig


class ClaudeExecutor(AgentExecutor):
    """
    Executor for Claude Code CLI.

    Invokes: claude -p <prompt> --dangerously-skip-permissions [--model MODEL] [--agents JSON]
    """

    @property
    def name(self) -> str:
        return "Claude Code"

    @property
    def runner_type(self) -> str:
        return "claude-code"

    def build_command(self, config: ExecutorConfig) -> list[str]:
        """
        Build the Claude CLI command.

        Args:
            config: Executor configuration with prompt, model, agents, etc.

        Returns:
            Command list: ['claude', '-p', prompt, '--dangerously-skip-permissions', ...]
        """
        cmd = [
            "claude",
            "-p", config.prompt,
            "--dangerously-skip-permissions",
        ]

        # Add model override if specified
        if config.model:
            cmd.extend(["--model", config.model])

        # Add agents JSON if specified (for custom agent files)
        if config.agents_json:
            cmd.extend(["--agents", config.agents_json])

        return cmd
