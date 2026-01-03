"""
Job communication helpers for runner-backend communication.

Provides functions for heartbeat, status reporting, job polling,
and log streaming to the backend.
"""

import threading
from typing import Any

import requests


class HeartbeatError(Exception):
    """Raised when heartbeat fails in a critical way."""
    pass


class ConnectionError(Exception):
    """Raised when connection to backend fails."""
    pass


def send_heartbeat(
    backend_url: str,
    runner_id: str,
    session: requests.Session | None = None,
    timeout: float = 5.0,
) -> bool:
    """
    Send a heartbeat to the backend.

    Args:
        backend_url: Backend base URL
        runner_id: Runner ID
        session: Optional requests session (uses new session if not provided)
        timeout: Request timeout in seconds

    Returns:
        True if heartbeat was successful (200), False if runner not found (404)

    Raises:
        ConnectionError: If connection to backend fails
    """
    if session is None:
        session = requests.Session()

    url = f"{backend_url}/api/runners/{runner_id}/heartbeat"

    try:
        response = session.post(url, timeout=timeout)

        if response.status_code == 404:
            return False

        return response.status_code == 200

    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f"Failed to connect to backend: {e}") from e
    except requests.exceptions.Timeout as e:
        raise ConnectionError(f"Heartbeat timed out: {e}") from e


def report_status(
    backend_url: str,
    runner_id: str,
    status: str,
    error: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> bool:
    """
    Report job status to the backend.

    Args:
        backend_url: Backend base URL
        runner_id: Runner ID
        status: Status to report (e.g., "running", "completed", "failed")
        error: Optional error message
        session: Optional requests session
        timeout: Request timeout in seconds

    Returns:
        True if report was successful
    """
    if session is None:
        session = requests.Session()

    url = f"{backend_url}/api/jobs/status"

    payload = {"status": status}
    if error is not None:
        payload["error"] = error

    try:
        response = session.post(url, json=payload, timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def complete_job(
    backend_url: str,
    runner_id: str,
    success: bool,
    error: str | None = None,
    pr_url: str | None = None,
    test_results: dict | None = None,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> bool:
    """
    Report job completion to the backend.

    Args:
        backend_url: Backend base URL
        runner_id: Runner ID
        success: Whether the job succeeded
        error: Optional error message
        pr_url: Optional PR URL if created
        test_results: Optional test results dict
        session: Optional requests session
        timeout: Request timeout in seconds

    Returns:
        True if completion was reported successfully
    """
    if session is None:
        session = requests.Session()

    url = f"{backend_url}/api/runners/{runner_id}/complete"

    payload: dict[str, Any] = {"success": success}
    if error is not None:
        payload["error"] = error
    if pr_url is not None:
        payload["pr_url"] = pr_url
    if test_results is not None:
        payload["test_results"] = test_results

    try:
        response = session.post(url, json=payload, timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def send_log(
    backend_url: str,
    runner_id: str,
    lines: list[str],
    session: requests.Session | None = None,
    timeout: float = 5.0,
) -> bool:
    """
    Send log lines to the backend.

    This is a best-effort operation - failures are silently ignored
    since logging should not block job execution.

    Args:
        backend_url: Backend base URL
        runner_id: Runner ID
        lines: Log lines to send
        session: Optional requests session
        timeout: Request timeout in seconds

    Returns:
        True if logs were sent successfully, False otherwise
    """
    if session is None:
        session = requests.Session()

    url = f"{backend_url}/api/runners/{runner_id}/logs"

    try:
        response = session.post(
            url,
            json={"lines": lines},
            timeout=timeout,
        )
        return response.status_code == 200
    except Exception:
        return False


def poll_for_job(
    backend_url: str,
    runner_id: str,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> dict | None:
    """
    Poll the backend for a job.

    Args:
        backend_url: Backend base URL
        runner_id: Runner ID
        session: Optional requests session
        timeout: Request timeout in seconds

    Returns:
        Job dictionary if a job is available, None otherwise
    """
    if session is None:
        session = requests.Session()

    url = f"{backend_url}/api/runners/{runner_id}/job"

    try:
        response = session.get(url, timeout=timeout)

        if response.status_code == 404:
            return None

        if response.status_code != 200:
            return None

        data = response.json()
        return data.get("job")

    except Exception:
        return None


class JobHelpers:
    """
    Stateful wrapper for job communication.

    Maintains a session and provides convenient methods for
    runner-backend communication.

    Usage:
        helpers = JobHelpers("http://localhost:8000", "runner-123")
        helpers.log("Starting job...")
        helpers.heartbeat()
        helpers.complete(success=True)
    """

    def __init__(
        self,
        backend_url: str,
        runner_id: str,
        heartbeat_interval: float = 10.0,
    ):
        """
        Initialize JobHelpers.

        Args:
            backend_url: Backend base URL
            runner_id: Runner ID
            heartbeat_interval: Seconds between heartbeats
        """
        self.backend_url = backend_url
        self.runner_id = runner_id
        self.heartbeat_interval = heartbeat_interval
        self.session = requests.Session()

        # Heartbeat thread state
        self._heartbeat_stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._needs_reregister = threading.Event()

    def log(self, message: str) -> None:
        """
        Log a message to console and send to backend.

        Args:
            message: Message to log
        """
        print(f"[runner] {message}", flush=True)
        send_log(
            self.backend_url,
            self.runner_id,
            [message],
            session=self.session,
        )

    def heartbeat(self) -> bool:
        """
        Send a heartbeat.

        Returns:
            True if heartbeat was successful
        """
        try:
            return send_heartbeat(
                self.backend_url,
                self.runner_id,
                session=self.session,
            )
        except ConnectionError:
            self._needs_reregister.set()
            return False

    def complete(
        self,
        success: bool,
        error: str | None = None,
        pr_url: str | None = None,
        test_results: dict | None = None,
    ) -> bool:
        """
        Report job completion.

        Args:
            success: Whether the job succeeded
            error: Optional error message
            pr_url: Optional PR URL if created
            test_results: Optional test results dict

        Returns:
            True if completion was reported successfully
        """
        return complete_job(
            self.backend_url,
            self.runner_id,
            success=success,
            error=error,
            pr_url=pr_url,
            test_results=test_results,
            session=self.session,
        )

    def poll(self) -> dict | None:
        """
        Poll for a job.

        Returns:
            Job dictionary if available, None otherwise
        """
        return poll_for_job(
            self.backend_url,
            self.runner_id,
            session=self.session,
        )

    def start_heartbeat_thread(self) -> None:
        """Start background heartbeat thread."""
        self._heartbeat_stop_event.clear()

        def heartbeat_loop():
            while not self._heartbeat_stop_event.is_set():
                self.heartbeat()
                self._heartbeat_stop_event.wait(timeout=self.heartbeat_interval)

        self._heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop_heartbeat_thread(self) -> None:
        """Stop background heartbeat thread."""
        self._heartbeat_stop_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    @property
    def needs_reregister(self) -> bool:
        """Check if runner needs to re-register with backend."""
        return self._needs_reregister.is_set()

    def clear_reregister_flag(self) -> None:
        """Clear the re-register flag."""
        self._needs_reregister.clear()
