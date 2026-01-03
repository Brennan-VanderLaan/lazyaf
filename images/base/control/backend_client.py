"""
Backend HTTP Client.

Handles communication from control layer to backend for:
- Status updates (running, completed, failed)
- Log streaming
- Heartbeats
"""
import time
import random
from typing import Optional, List

import requests


class BackendClient:
    """HTTP client for control layer -> backend communication."""

    # Retry configuration
    MAX_RETRIES: int = 10
    BASE_BACKOFF: float = 1.0  # seconds
    MAX_BACKOFF: float = 30.0  # seconds
    TOTAL_TIMEOUT: int = 300  # 5 minutes

    def __init__(self, backend_url: str, step_id: str, token: str):
        """
        Initialize backend client.

        Args:
            backend_url: Base URL of backend (e.g., "http://backend:8000")
            step_id: Step execution key
            token: Authentication token
        """
        self.backend_url = backend_url.rstrip("/")
        self.step_id = step_id
        self.token = token
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> Optional[requests.Response]:
        """
        Make HTTP request with exponential backoff retry.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "status")
            **kwargs: Additional arguments passed to requests

        Returns:
            Response if successful, None if all retries failed
        """
        url = f"{self.backend_url}/api/steps/{self.step_id}/{endpoint}"
        start_time = time.time()
        backoff = self.BASE_BACKOFF

        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=10,
                    **kwargs,
                )

                # Success or client error (4xx) - don't retry
                if response.status_code < 500:
                    return response

                # Server error - retry
            except requests.RequestException:
                pass

            # Check total timeout
            elapsed = time.time() - start_time
            if elapsed > self.TOTAL_TIMEOUT:
                return None

            # Exponential backoff with jitter
            jitter = random.uniform(0, 1)
            sleep_time = min(backoff + jitter, self.MAX_BACKOFF)
            time.sleep(sleep_time)
            backoff = min(backoff * 2, self.MAX_BACKOFF)

        return None

    def report_status(
        self,
        status: str,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> bool:
        """
        Report step status to backend.

        Args:
            status: Status value ("running", "completed", "failed")
            exit_code: Process exit code (for completed/failed)
            error: Error message (for failed)

        Returns:
            True if successful, False otherwise
        """
        payload = {"status": status}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if error is not None:
            payload["error"] = error

        response = self._request_with_retry("POST", "status", json=payload)
        return response is not None and response.status_code == 200

    def send_logs(self, lines: List[str]) -> bool:
        """
        Send log lines to backend.

        Args:
            lines: List of log lines to append

        Returns:
            True if successful, False otherwise
        """
        if not lines:
            return True

        response = self._request_with_retry("POST", "logs", json={"lines": lines})
        return response is not None and response.status_code == 200

    def heartbeat(self) -> bool:
        """
        Send heartbeat to backend.

        Returns:
            True if successful, False otherwise
        """
        response = self._request_with_retry("POST", "heartbeat", json={})
        return response is not None and response.status_code == 200
