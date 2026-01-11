#!/usr/bin/env python3
"""
Claude Agent Wrapper - Phase 12.3

Wrapper script for executing Claude CLI commands within the control layer.
"""
import subprocess
import sys
import os


def run_claude_command(prompt: str, working_dir: str = None) -> int:
    """
    Execute Claude CLI with the given prompt.

    Args:
        prompt: The prompt/instruction for Claude
        working_dir: Working directory for execution

    Returns:
        Exit code from Claude CLI
    """
    cmd = ["claude", "--print", prompt]

    env = os.environ.copy()

    result = subprocess.run(
        cmd,
        cwd=working_dir or "/workspace/repo",
        env=env,
        capture_output=False,
    )

    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: claude_wrapper.py <prompt>", file=sys.stderr)
        sys.exit(1)

    prompt = " ".join(sys.argv[1:])
    exit_code = run_claude_command(prompt)
    sys.exit(exit_code)
