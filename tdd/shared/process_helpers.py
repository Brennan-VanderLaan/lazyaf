"""
Process control helpers for Phase 12 testing.

These helpers allow tests to kill processes, send signals, and simulate crashes
for testing resilience and recovery scenarios.
"""

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class ProcessHandle:
    """Handle to a spawned test process."""
    pid: int
    process: subprocess.Popen

    @property
    def is_running(self) -> bool:
        """Check if process is still running."""
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        """Get exit code if process has completed."""
        return self.process.poll()


def spawn_process(
    args: list[str],
    cwd: str | None = None,
    env: dict | None = None,
    stdout: int = subprocess.PIPE,
    stderr: int = subprocess.PIPE,
) -> ProcessHandle:
    """
    Spawn a subprocess for testing.

    Args:
        args: Command and arguments
        cwd: Working directory
        env: Environment variables (merged with current env)
        stdout: stdout handling (default: PIPE)
        stderr: stderr handling (default: PIPE)

    Returns:
        ProcessHandle with process info
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=full_env,
        stdout=stdout,
        stderr=stderr,
    )

    return ProcessHandle(pid=process.pid, process=process)


def kill_process(pid: int, force: bool = True) -> bool:
    """
    Kill a process.

    Args:
        pid: Process ID
        force: If True, use SIGKILL; otherwise SIGTERM

    Returns:
        True if signal was sent successfully
    """
    try:
        sig = signal.SIGKILL if force else signal.SIGTERM
        if sys.platform == "win32":
            # Windows doesn't have SIGKILL/SIGTERM in the same way
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def send_signal(pid: int, sig: int) -> bool:
    """
    Send a signal to a process.

    Args:
        pid: Process ID
        sig: Signal number (e.g., signal.SIGTERM, signal.SIGSTOP)

    Returns:
        True if signal was sent successfully
    """
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_process(pid: int) -> bool:
    """
    Stop (pause) a process using SIGSTOP.

    Note: Only works on Unix systems.

    Args:
        pid: Process ID

    Returns:
        True if stopped successfully
    """
    if sys.platform == "win32":
        # Windows doesn't support SIGSTOP
        return False
    return send_signal(pid, signal.SIGSTOP)


def continue_process(pid: int) -> bool:
    """
    Continue a stopped process using SIGCONT.

    Note: Only works on Unix systems.

    Args:
        pid: Process ID

    Returns:
        True if continued successfully
    """
    if sys.platform == "win32":
        # Windows doesn't support SIGCONT
        return False
    return send_signal(pid, signal.SIGCONT)


def is_process_running(pid: int) -> bool:
    """
    Check if a process is running.

    Args:
        pid: Process ID

    Returns:
        True if process exists and is running
    """
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks existence
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def wait_for_process(handle: ProcessHandle, timeout: float = 30.0) -> int:
    """
    Wait for a process to complete.

    Args:
        handle: ProcessHandle from spawn_process
        timeout: Maximum wait time in seconds

    Returns:
        Exit code

    Raises:
        TimeoutError if process doesn't complete in time
    """
    loop = asyncio.get_event_loop()

    try:
        exit_code = await asyncio.wait_for(
            loop.run_in_executor(None, handle.process.wait),
            timeout=timeout
        )
        return exit_code
    except asyncio.TimeoutError:
        raise TimeoutError(f"Process {handle.pid} did not complete within {timeout}s")


async def wait_for_condition(
    condition: Callable[[], bool],
    timeout: float = 10.0,
    interval: float = 0.1,
) -> bool:
    """
    Wait for a condition to become true.

    Args:
        condition: Callable that returns True when condition is met
        timeout: Maximum wait time in seconds
        interval: Check interval in seconds

    Returns:
        True if condition was met within timeout

    Raises:
        TimeoutError if condition not met within timeout
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)

    raise TimeoutError(f"Condition not met within {timeout}s")


class ProcessCrashSimulator:
    """
    Helper class to simulate process crashes at specific points.

    Usage:
        simulator = ProcessCrashSimulator()

        # In code under test:
        simulator.check("before_commit")  # Raises if crash point is set

        # In test:
        simulator.set_crash_point("before_commit")
    """

    def __init__(self):
        self._crash_points: set[str] = set()
        self._crash_count: dict[str, int] = {}

    def set_crash_point(self, point: str, after_n_calls: int = 0):
        """
        Set a crash point. When check() is called with this point, it raises.

        Args:
            point: Name of the crash point
            after_n_calls: Crash after N calls (0 = crash immediately)
        """
        self._crash_points.add(point)
        self._crash_count[point] = after_n_calls

    def clear_crash_point(self, point: str):
        """Clear a crash point."""
        self._crash_points.discard(point)
        self._crash_count.pop(point, None)

    def clear_all(self):
        """Clear all crash points."""
        self._crash_points.clear()
        self._crash_count.clear()

    def check(self, point: str):
        """
        Check if we should crash at this point.

        Raises:
            SimulatedCrash if this crash point is set
        """
        if point in self._crash_points:
            if self._crash_count.get(point, 0) > 0:
                self._crash_count[point] -= 1
                return
            raise SimulatedCrash(f"Simulated crash at: {point}")


class SimulatedCrash(Exception):
    """Exception raised by ProcessCrashSimulator to simulate crashes."""
    pass
