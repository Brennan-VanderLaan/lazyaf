"""
Command execution helpers for runners.

Provides functions for running commands with logging and streaming output.
"""

import subprocess
import threading
from pathlib import Path
from typing import Callable


def run_command(
    cmd: list[str],
    cwd: str | Path | None = None,
    log_func: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """
    Run a command and return exit code, stdout, stderr.

    Args:
        cmd: Command and arguments
        cwd: Working directory
        log_func: Optional function to log output lines

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    if log_func:
        log_func(f"$ {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    if log_func:
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                log_func(f"  {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                log_func(f"  [stderr] {line}")

    return result.returncode, result.stdout, result.stderr


def run_command_streaming(
    cmd: list[str],
    cwd: str | Path | None = None,
    log_func: Callable[[str], None] | None = None,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """
    Run a command with real-time output streaming.

    Used for long-running commands like agent invocations where
    we want to see output as it happens.

    Args:
        cmd: Command and arguments
        cwd: Working directory
        log_func: Optional function to log output lines
        env: Optional environment variables (merged with current env)

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    import os

    if log_func:
        log_func(f"$ {' '.join(cmd)}")

    # Prepare environment
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    # Use Popen for real-time output
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
        env=full_env,
    )

    # Read stdout and stderr in separate threads
    def read_stdout():
        if process.stdout:
            for line in process.stdout:
                line = line.rstrip("\n")
                stdout_lines.append(line)
                if log_func:
                    log_func(f"  {line}")

    def read_stderr():
        if process.stderr:
            for line in process.stderr:
                line = line.rstrip("\n")
                stderr_lines.append(line)
                if log_func:
                    log_func(f"  [stderr] {line}")

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)

    stdout_thread.start()
    stderr_thread.start()

    # Wait for process to complete
    process.wait()

    # Wait for threads to finish reading
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    return process.returncode, "\n".join(stdout_lines), "\n".join(stderr_lines)


def cleanup_workspace(workspace_path: str | Path) -> None:
    """
    Clean up the workspace directory.

    Args:
        workspace_path: Path to workspace
    """
    import shutil

    workspace = Path(workspace_path)
    if workspace.exists():
        try:
            shutil.rmtree(workspace)
        except Exception:
            # Try with subprocess as fallback (handles permission issues)
            result = subprocess.run(
                ["rm", "-rf", str(workspace)],
                capture_output=True,
            )
            if result.returncode != 0:
                # Last resort - try with sudo
                subprocess.run(
                    ["sudo", "rm", "-rf", str(workspace)],
                    capture_output=True,
                )

    # Clean up any stale git locks
    git_lock = workspace / ".git" / "index.lock"
    if git_lock.exists():
        try:
            git_lock.unlink()
        except Exception:
            pass
