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

import requests


class RegistrationError(Exception):
    """Raised when runner registration fails."""
    pass


class NeedsReregister(Exception):
    """Raised when the runner needs to re-register with the backend."""
    pass


def _get_session() -> requests.Session:
    """Get a requests session."""
    return requests.Session()


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
    try:
        session = _get_session()
        response = session.post(
            f"{backend_url}/api/runners/{runner_id}/heartbeat",
            timeout=timeout,
        )
        return response.status_code == 200
    except (requests.RequestException, ConnectionError):
        return False


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
    payload = {"status": status}
    if error is not None:
        payload["error"] = error
    if test_results is not None:
        payload["test_results"] = test_results

    session = _get_session()
    session.post(
        f"{backend_url}/api/jobs/{job_id}/callback",
        json=payload,
        timeout=timeout,
    )


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
    payload = {"success": success}
    if error is not None:
        payload["error"] = error
    if pr_url is not None:
        payload["pr_url"] = pr_url
    if test_results is not None:
        payload["test_results"] = test_results

    session = _get_session()
    session.post(
        f"{backend_url}/api/runners/{runner_id}/complete",
        json=payload,
        timeout=timeout,
    )


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
    # Wrap single line in a list
    if isinstance(lines, str):
        lines = [lines]

    session = _get_session()
    session.post(
        f"{backend_url}/api/runners/{runner_id}/logs",
        json={"lines": lines},
        timeout=timeout,
    )


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
    payload = {"runner_type": runner_type}
    if name is not None:
        payload["name"] = name
    if runner_id is not None:
        payload["runner_id"] = runner_id

    try:
        session = _get_session()
        response = session.post(
            f"{backend_url}/api/runners",
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as e:
        raise RegistrationError(f"Registration failed: {e}") from e


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
    session = _get_session()
    response = session.get(
        f"{backend_url}/api/runners/{runner_id}/job",
        timeout=timeout,
    )

    if response.status_code == 404:
        raise NeedsReregister("Runner not recognized by backend")

    data = response.json()
    return data.get("job")


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
        while not self._stop_event.is_set():
            send_heartbeat(self.runner_id, self.backend_url)
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        self._stop_event.set()
        self.join(timeout=self.interval + 1)
