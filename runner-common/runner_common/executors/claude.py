"""
Claude Code executor implementation.

This executor invokes the Claude Code CLI with the appropriate flags.

Phase 12.5 additions:
- an optional ``output_format`` ("stream-json" | "json"), selected by the
  agent wrapper from the agent config's ``stream`` flag,
- ``ExecutorResult.usage`` populated from the CLI's own result object
  (cross-agent contract #4).

DELIBERATE DEVIATION from docs/milestone-13/api-surface.md 2.3, stated rather
than hidden: the binding doc says invoke with ``--output-format json``. This
executor defaults the agent path to ``--output-format stream-json --verbose``,
which emits the SAME final result object (same ``total_cost_usd``, same
``usage`` block) as newline-delimited events — so the contract's substance is
met — while plain ``json`` would make a 20-minute agent step completely dark
in the UI, which R1 does not allow. ``stream: false`` in the agent config
falls back to ``json`` and the scraper handles both shapes.

``output_format=None`` (the default) keeps ``build_command`` byte-identical to
the pre-12.5 command, so the legacy polling entrypoint's plain-text logs are
unchanged. Only the wrapper opts in.
"""

from typing import Callable, Optional

from .base import AgentExecutor, ExecutorConfig, ExecutorResult

#: The formats this executor knows how to ask for AND scrape.
OUTPUT_FORMATS = ("stream-json", "json")


class ClaudeExecutor(AgentExecutor):
    """
    Executor for Claude Code CLI.

    Invokes: claude -p <prompt> --dangerously-skip-permissions [--model MODEL]
             [--agents JSON] [--output-format FMT [--verbose]]
    """

    def __init__(self, output_format: Optional[str] = None):
        """
        Args:
            output_format: None (plain text, the legacy behavior),
                "stream-json" (adds --verbose, which the CLI requires for
                streamed events in -p mode) or "json".
        """
        if output_format is not None and output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"unknown output_format {output_format!r}; "
                f"expected one of {OUTPUT_FORMATS} or None"
            )
        self.output_format = output_format

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

        # Machine-readable output, when the caller asked for it (12.5).
        if self.output_format:
            cmd.extend(["--output-format", self.output_format])
            if self.output_format == "stream-json":
                # -p + stream-json REQUIRES --verbose or the CLI refuses.
                cmd.append("--verbose")

        return cmd

    def execute(
        self,
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        streaming: bool = True,
    ) -> ExecutorResult:
        """Execute Claude, then scrape its own usage report off stdout.

        Scraping NEVER changes the result's success/exit_code: usage is
        telemetry about the work, and telemetry must not be able to fail
        work (api-surface 2.4).
        """
        from ..usage import scrape_claude_usage

        result = super().execute(config, log_callback, streaming)
        if self.output_format:
            result.usage = scrape_claude_usage(
                result.stdout,
                result.stderr,
                fallback_model=config.model,
            )
        return result
