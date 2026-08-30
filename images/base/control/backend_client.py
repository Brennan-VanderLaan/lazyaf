"""
Backend HTTP Client.

Handles communication from control layer to backend for:
- Status updates (running, completed, failed, timeout)
- Log streaming (LogLine objects — matches the backend's LogsRequest schema)
- Heartbeats (with extend_seconds)

Retry policy is asymmetric on purpose:
- status: patient budget (rare and load-bearing)
- heartbeat: capped budget strictly below the heartbeat interval, so a slow
  backend can never wedge the heartbeat loop past its own cadence
- logs: tight budget (MAX 3 tries, <=15s total) so a flapping backend cannot
  wedge the step behind its own log stream; failed batches are COUNTED
  (``dropped_log_lines``) and surfaced in the final status error by run.py.
  Container stdout still carries every line for docker-logs forensics.
- test-results (12.2.6): same tight budget as logs — manifest delivery runs
  at step shutdown and must never wedge or fail the step; a failed POST is
  surfaced in the final status error by run.py.
- usage (12.5, protocol channel #4): same tight budget as logs, same reason.
  Accounting is telemetry ABOUT the work; it must never be able to fail the
  work. A 409 (the StepExecution already went terminal) is a non-retryable
  drop, not an error to retry against.
"""
import threading
import time
import random
from typing import Dict, List, Optional

import requests


class BackendClient:
    """HTTP client for control layer -> backend communication.

    Thread-safety: the client is called from the main thread (status), the
    log-sender thread (logs) and the heartbeat thread (heartbeat).
    ``requests.Session`` is NOT thread-safe, so every request attempt is
    serialized through ``_session_lock`` (held only around the HTTP call,
    never across backoff sleeps).
    """

    # Patient retry budget (status)
    MAX_RETRIES: int = 10
    BASE_BACKOFF: float = 1.0  # seconds
    MAX_BACKOFF: float = 30.0  # seconds
    TOTAL_TIMEOUT: float = 300.0  # 5 minutes

    # Tight retry budget for the log path
    LOG_MAX_RETRIES: int = 3
    LOG_TOTAL_TIMEOUT: float = 15.0  # seconds

    # Heartbeat budget: worst case (attempts * request timeout + backoff)
    # MUST stay below heartbeat.HEARTBEAT_INTERVAL so a heartbeat attempt can
    # never wedge the loop past its own interval — and the heartbeat thread
    # never outlives stop() by more than one bounded attempt.
    HEARTBEAT_MAX_RETRIES: int = 2
    HEARTBEAT_TOTAL_TIMEOUT: float = 8.0  # seconds
    HEARTBEAT_REQUEST_TIMEOUT: float = 3.0  # per-attempt HTTP timeout

    def __init__(self, backend_url: str, step_id: str, auth_token: str):
        """
        Initialize backend client.

        Args:
            backend_url: Base URL of backend (e.g., "http://backend:8000")
            step_id: StepExecution id (the key of /api/steps/{step_id}/*)
            auth_token: Bearer JWT minted by the backend for this step
        """
        self.backend_url = backend_url.rstrip("/")
        self.step_id = step_id
        self.auth_token = auth_token
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {auth_token}"
        # requests.Session is not thread-safe; serialize request attempts.
        self._session_lock = threading.Lock()
        # Log lines that never reached the backend (tight budget exhausted).
        self.dropped_log_lines: int = 0

    def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        max_retries: Optional[int] = None,
        total_timeout: Optional[float] = None,
        request_timeout: float = 10.0,
        **kwargs,
    ) -> Optional[requests.Response]:
        """
        Make HTTP request with exponential backoff retry.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "status")
            max_retries: Override retry count (default: patient budget)
            total_timeout: Override total retry window seconds
            request_timeout: Per-attempt HTTP timeout seconds
            **kwargs: Additional arguments passed to requests

        Returns:
            Response if a request completed (any status < 500), None if the
            retry budget was exhausted.
        """
        if max_retries is None:
            max_retries = self.MAX_RETRIES
        if total_timeout is None:
            total_timeout = self.TOTAL_TIMEOUT

        url = f"{self.backend_url}/api/steps/{self.step_id}/{endpoint}"
        start_time = time.time()
        backoff = self.BASE_BACKOFF

        for attempt in range(max_retries):
            try:
                with self._session_lock:
                    response = self.session.request(
                        method,
                        url,
                        timeout=request_timeout,
                        **kwargs,
                    )
                # Success or client error (4xx) - don't retry
                if response.status_code < 500:
                    return response
                # Server error - retry
            except requests.RequestException:
                pass

            # Last attempt: no point sleeping
            if attempt == max_retries - 1:
                return None

            # Check total timeout
            if time.time() - start_time > total_timeout:
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
            status: Status value ("running", "completed", "failed", "timeout")
            exit_code: Process exit code (for terminal statuses)
            error: Error message (for failed/timeout)

        Returns:
            True if successful, False otherwise
        """
        payload: Dict = {"status": status}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if error is not None:
            payload["error"] = error

        response = self._request_with_retry("POST", "status", json=payload)
        return response is not None and response.status_code == 200

    def send_logs(self, lines: List[str], stream: str = "stdout") -> bool:
        """
        Send log lines to backend as LogLine objects.

        The backend's /logs endpoint concatenates ``content`` VERBATIM into
        StepRun.logs — so every line passed here must keep (or gain) its
        trailing newline. Plain-string payloads 422 against the LogsRequest
        schema; this wraps each line as {"content": ..., "stream": ...}.

        Failed batches (tight retry budget exhausted or non-200) increment
        ``dropped_log_lines`` by the batch size.

        Args:
            lines: Log lines, each expected to end with a newline
            stream: Stream name for every line in the batch

        Returns:
            True if successful, False otherwise
        """
        if not lines:
            return True

        payload = {
            "lines": [
                {
                    "content": line if line.endswith("\n") else line + "\n",
                    "stream": stream,
                }
                for line in lines
            ]
        }
        response = self._request_with_retry(
            "POST",
            "logs",
            max_retries=self.LOG_MAX_RETRIES,
            total_timeout=self.LOG_TOTAL_TIMEOUT,
            json=payload,
        )
        ok = response is not None and response.status_code == 200
        if not ok:
            self.dropped_log_lines += len(lines)
        return ok

    def send_test_results(self, manifest: Dict) -> bool:
        """
        POST a test-results manifest (12.2.6 contract #1) to
        /api/steps/{step_id}/test-results.

        Tight retry budget like /logs on purpose: manifest delivery runs at
        step shutdown and must never wedge the step behind a flapping
        backend. A failed delivery returns False — the caller (run.py)
        surfaces the drop loudly in the terminal status error, but the step
        outcome is never changed by it.

        Args:
            manifest: The manifest dict, sent as the JSON body verbatim.

        Returns:
            True if the backend accepted it (2xx), False otherwise.
        """
        response = self._request_with_retry(
            "POST",
            "test-results",
            max_retries=self.LOG_MAX_RETRIES,
            total_timeout=self.LOG_TOTAL_TIMEOUT,
            json=manifest,
        )
        return response is not None and 200 <= response.status_code < 300

    def send_usage(self, manifest: Dict) -> Optional[int]:
        """
        POST a usage manifest (12.5, cross-agent contract #2/#3) to
        /api/steps/{step_id}/usage.

        Returns the HTTP STATUS CODE rather than a bool — unlike
        /test-results, the caller must distinguish outcomes:

        - 2xx: recorded
        - 409: the StepExecution already went terminal. A non-retryable
          DROP: the run is over, the row cannot be written, and re-POSTing
          would only burn the shutdown budget. run.py WARNs and continues.
        - other 4xx (e.g. 422 on an unknown version): the manifest was
          rejected; WARN with the code so the drift is nameable.
        - None: the tight retry budget was exhausted (network / 5xx).

        Tight budget like /logs on purpose: delivery runs at step shutdown
        and must never wedge the step behind a flapping backend. NOTHING
        this method returns ever changes the step's exit code.
        """
        response = self._request_with_retry(
            "POST",
            "usage",
            max_retries=self.LOG_MAX_RETRIES,
            total_timeout=self.LOG_TOTAL_TIMEOUT,
            json=manifest,
        )
        return response.status_code if response is not None else None

    def heartbeat(self, extend_seconds: Optional[int] = None) -> bool:
        """
        Send heartbeat to backend.

        Args:
            extend_seconds: Requested StepExecution.timeout_at extension.
                (12.3 limitation: extends telemetry only — the executor's hard
                deadline does not move until 12.4.)

        Returns:
            True if successful, False otherwise
        """
        payload: Dict = {}
        if extend_seconds is not None:
            payload["extend_seconds"] = int(extend_seconds)

        response = self._request_with_retry(
            "POST",
            "heartbeat",
            max_retries=self.HEARTBEAT_MAX_RETRIES,
            total_timeout=self.HEARTBEAT_TOTAL_TIMEOUT,
            request_timeout=self.HEARTBEAT_REQUEST_TIMEOUT,
            json=payload,
        )
        return response is not None and response.status_code == 200
