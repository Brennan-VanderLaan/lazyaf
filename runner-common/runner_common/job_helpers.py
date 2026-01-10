"""
Job helper functions for backend communication.

This module handles all communication with the LazyAF backend:
- Runner registration
- Heartbeat sending
- Job polling
- Status reporting
- Log streaming
"""

import threading
from typing import Optional, List, Union


class RegistrationError(Exception):
    """Raised when runner registration fails."""
    pass


class NeedsReregister(Exception):
    """Raised when the runner needs to re-register with the backend."""
    pass


def send_heartbeat(
    runner_id: str,
    backend_url: str,
    timeout: float = 5.0,
) -> bool:
    """
    Send a heartbeat to the backend.

    Args:
        runner_id: The runner's ID
        backend_url: Backend base URL
        timeout: Request timeout in seconds

    Returns:
        True if heartbeat was acknowledged, False otherwise
    """
    raise NotImplementedError("TODO: Implement send_heartbeat()")


def report_status(
    job_id: str,
    status: str,
    backend_url: str,
    error: Optional[str] = None,
    test_results: Optional[dict] = None,
    timeout: float = 10.0,
) -> None:
    """
    Report job status to the backend.

    Args:
        job_id: The job ID
        status: Status string (e.g., "running", "completed", "failed")
        backend_url: Backend base URL
        error: Optional error message
        test_results: Optional test results dict
        timeout: Request timeout in seconds
    """
    raise NotImplementedError("TODO: Implement report_status()")


def complete_job(
    runner_id: str,
    success: bool,
    backend_url: str,
    error: Optional[str] = None,
    pr_url: Optional[str] = None,
    test_results: Optional[dict] = None,
    timeout: float = 10.0,
) -> None:
    """
    Mark the current job as complete.

    Args:
        runner_id: The runner's ID
        success: Whether the job succeeded
        backend_url: Backend base URL
        error: Optional error message (for failures)
        pr_url: Optional PR URL (for successful card jobs)
        test_results: Optional test results dict
        timeout: Request timeout in seconds
    """
    raise NotImplementedError("TODO: Implement complete_job()")


def log_to_backend(
    runner_id: str,
    lines: Union[str, List[str]],
    backend_url: str,
    timeout: float = 5.0,
) -> None:
    """
    Send log lines to the backend.

    Args:
        runner_id: The runner's ID
        lines: Single log line or list of lines
        backend_url: Backend base URL
        timeout: Request timeout in seconds
    """
    raise NotImplementedError("TODO: Implement log_to_backend()")


def register(
    runner_type: str,
    backend_url: str,
    name: Optional[str] = None,
    runner_id: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """
    Register the runner with the backend.

    Args:
        runner_type: Type of runner (e.g., "claude-code", "gemini", "mock")
        backend_url: Backend base URL
        name: Optional runner name
        runner_id: Optional persistent runner ID (for reconnection)
        timeout: Request timeout in seconds

    Returns:
        Dict with runner_id and name

    Raises:
        RegistrationError: If registration fails
    """
    raise NotImplementedError("TODO: Implement register()")


def poll_for_job(
    runner_id: str,
    backend_url: str,
    timeout: float = 10.0,
) -> Optional[dict]:
    """
    Poll the backend for an available job.

    Args:
        runner_id: The runner's ID
        backend_url: Backend base URL
        timeout: Request timeout in seconds

    Returns:
        Job dict if one is available, None otherwise

    Raises:
        NeedsReregister: If the runner is no longer recognized (404)
    """
    raise NotImplementedError("TODO: Implement poll_for_job()")


class HeartbeatThread(threading.Thread):
    """
    Background thread that sends periodic heartbeats.

    Usage:
        thread = HeartbeatThread(runner_id, backend_url, interval=10)
        thread.start()
        # ... do work ...
        thread.stop()
    """

    def __init__(
        self,
        runner_id: str,
        backend_url: str,
        interval: float = 10.0,
    ):
        """
        Initialize the heartbeat thread.

        Args:
            runner_id: The runner's ID
            backend_url: Backend base URL
            interval: Seconds between heartbeats
        """
        super().__init__(daemon=True)
        self.runner_id = runner_id
        self.backend_url = backend_url
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Run the heartbeat loop."""
        raise NotImplementedError("TODO: Implement HeartbeatThread.run()")

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        raise NotImplementedError("TODO: Implement HeartbeatThread.stop()")
