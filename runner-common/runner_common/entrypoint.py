#!/usr/bin/env python3
"""
Unified entrypoint for LazyAF runners.

This replaces the separate Claude and Gemini entrypoints with a single
unified entrypoint that dispatches based on the RUNNER_TYPE environment variable.

Usage:
    # As module
    python -m runner_common.entrypoint

    # Or directly
    ./entrypoint.py

Environment Variables:
    RUNNER_TYPE: "claude-code" or "gemini" (required)
    BACKEND_URL: Backend URL (default: http://localhost:8000)
    RUNNER_NAME: Runner name (optional)
    POLL_INTERVAL: Seconds between job polls (default: 5)
    CLAUDE_MODEL: Claude model for claude-code runner
    GEMINI_MODEL: Gemini model for gemini runner
"""

import os
import sys


def get_runner():
    """
    Get the appropriate runner based on RUNNER_TYPE environment variable.

    Returns:
        BaseRunner subclass instance

    Raises:
        ValueError: If RUNNER_TYPE is not set or invalid
    """
    runner_type = os.environ.get("RUNNER_TYPE", "").lower()

    if not runner_type:
        raise ValueError(
            "RUNNER_TYPE environment variable must be set to 'claude-code' or 'gemini'"
        )

    if runner_type == "claude-code":
        from .claude_runner import ClaudeRunner
        return ClaudeRunner()

    elif runner_type == "gemini":
        from .gemini_runner import GeminiRunner
        return GeminiRunner()

    else:
        raise ValueError(
            f"Unknown RUNNER_TYPE: {runner_type}. Must be 'claude-code' or 'gemini'"
        )


def main():
    """Main entry point."""
    try:
        runner = get_runner()
        runner.run()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
