"""
Tests for job_helpers module - defines the contract for job lifecycle operations.

These tests are written BEFORE the implementation to define expected behavior.
Job helpers handle heartbeat, status reporting, and backend communication.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests


class TestHeartbeat:
    """Tests for send_heartbeat() function."""

    def test_heartbeat_sends_to_backend(self, mock_backend):
        """send_heartbeat(runner_id) hits the correct endpoint."""
        from runner_common.job_helpers import send_heartbeat

        send_heartbeat("runner-123", backend_url=mock_backend.url)

        assert mock_backend.last_request.path == "/api/runners/runner-123/heartbeat"
        assert mock_backend.last_request.method == "POST"

    def test_heartbeat_returns_true_on_success(self, mock_backend):
        """send_heartbeat() returns True when backend responds 200."""
        from runner_common.job_helpers import send_heartbeat

        mock_backend.set_response(200, {})

        result = send_heartbeat("runner-123", backend_url=mock_backend.url)
        assert result is True

    def test_heartbeat_returns_false_on_404(self, mock_backend):
        """send_heartbeat() returns False when runner not found (404)."""
        from runner_common.job_helpers import send_heartbeat

        mock_backend.set_response(404, {"detail": "Runner not found"})

        result = send_heartbeat("runner-123", backend_url=mock_backend.url)
        assert result is False

    def test_heartbeat_returns_false_on_connection_error(self):
        """send_heartbeat() returns False on connection errors."""
        from runner_common.job_helpers import send_heartbeat

        # Use unreachable URL
        result = send_heartbeat("runner-123", backend_url="http://localhost:99999")
        assert result is False

    def test_heartbeat_with_timeout(self, mock_backend):
        """send_heartbeat() respects timeout parameter."""
        from runner_common.job_helpers import send_heartbeat

        mock_backend.set_delay(0.1)

        # Should succeed with longer timeout
        result = send_heartbeat("runner-123", backend_url=mock_backend.url, timeout=1.0)
        assert result is True


class TestReportStatus:
    """Tests for report_status() function."""

    def test_report_status_sends_correct_payload(self, mock_backend):
        """report_status() sends job status to callback endpoint."""
        from runner_common.job_helpers import report_status

        report_status(
            job_id="job-456",
            status="running",
            backend_url=mock_backend.url,
        )

        assert mock_backend.last_request.path == "/api/jobs/job-456/callback"
        assert mock_backend.last_request.json["status"] == "running"

    def test_report_status_with_error(self, mock_backend):
        """report_status() includes error message when provided."""
        from runner_common.job_helpers import report_status

        report_status(
            job_id="job-456",
            status="failed",
            error="Something went wrong",
            backend_url=mock_backend.url,
        )

        payload = mock_backend.last_request.json
        assert payload["status"] == "failed"
        assert payload["error"] == "Something went wrong"

    def test_report_status_with_test_results(self, mock_backend):
        """report_status() includes test_results when provided."""
        from runner_common.job_helpers import report_status

        test_results = {
            "passed": 10,
            "failed": 2,
            "skipped": 1,
        }

        report_status(
            job_id="job-456",
            status="completed",
            test_results=test_results,
            backend_url=mock_backend.url,
        )

        payload = mock_backend.last_request.json
        assert payload["test_results"] == test_results


class TestCompleteJob:
    """Tests for complete_job() function."""

    def test_complete_job_sends_success(self, mock_backend):
        """complete_job(runner_id, success=True) reports success."""
        from runner_common.job_helpers import complete_job

        complete_job(
            runner_id="runner-123",
            success=True,
            backend_url=mock_backend.url,
        )

        assert mock_backend.last_request.path == "/api/runners/runner-123/complete"
        assert mock_backend.last_request.json["success"] is True

    def test_complete_job_sends_failure(self, mock_backend):
        """complete_job(runner_id, success=False) reports failure with error."""
        from runner_common.job_helpers import complete_job

        complete_job(
            runner_id="runner-123",
            success=False,
            error="Build failed",
            backend_url=mock_backend.url,
        )

        payload = mock_backend.last_request.json
        assert payload["success"] is False
        assert payload["error"] == "Build failed"

    def test_complete_job_with_pr_url(self, mock_backend):
        """complete_job() includes PR URL when provided."""
        from runner_common.job_helpers import complete_job

        complete_job(
            runner_id="runner-123",
            success=True,
            pr_url="https://github.com/org/repo/pull/42",
            backend_url=mock_backend.url,
        )

        payload = mock_backend.last_request.json
        assert payload["pr_url"] == "https://github.com/org/repo/pull/42"


class TestLogToBackend:
    """Tests for log_to_backend() function."""

    def test_log_sends_lines(self, mock_backend):
        """log_to_backend(runner_id, lines) sends log lines."""
        from runner_common.job_helpers import log_to_backend

        log_to_backend(
            runner_id="runner-123",
            lines=["Line 1", "Line 2"],
            backend_url=mock_backend.url,
        )

        assert mock_backend.last_request.path == "/api/runners/runner-123/logs"
        assert mock_backend.last_request.json["lines"] == ["Line 1", "Line 2"]

    def test_log_single_line(self, mock_backend):
        """log_to_backend() can send a single line as string."""
        from runner_common.job_helpers import log_to_backend

        log_to_backend(
            runner_id="runner-123",
            lines="Single log message",
            backend_url=mock_backend.url,
        )

        # Should be wrapped in a list
        assert mock_backend.last_request.json["lines"] == ["Single log message"]


class TestRegister:
    """Tests for register() function."""

    def test_register_returns_runner_id(self, mock_backend):
        """register() returns the assigned runner ID."""
        from runner_common.job_helpers import register

        mock_backend.set_response(200, {
            "runner_id": "runner-abc123",
            "name": "runner-abc123",
        })

        result = register(
            runner_type="claude-code",
            backend_url=mock_backend.url,
        )

        assert result["runner_id"] == "runner-abc123"

    def test_register_sends_correct_payload(self, mock_backend):
        """register() sends runner type and optional name."""
        from runner_common.job_helpers import register

        mock_backend.set_response(200, {"runner_id": "x", "name": "x"})

        register(
            runner_type="gemini",
            name="my-runner",
            runner_id="uuid-123",
            backend_url=mock_backend.url,
        )

        payload = mock_backend.last_request.json
        assert payload["runner_type"] == "gemini"
        assert payload["name"] == "my-runner"
        assert payload["runner_id"] == "uuid-123"

    def test_register_raises_on_failure(self, mock_backend):
        """register() raises RegistrationError on failure."""
        from runner_common.job_helpers import register, RegistrationError

        mock_backend.set_response(500, {"detail": "Server error"})

        with pytest.raises(RegistrationError):
            register(runner_type="claude-code", backend_url=mock_backend.url)


class TestPollForJob:
    """Tests for poll_for_job() function."""

    def test_poll_returns_job_when_available(self, mock_backend):
        """poll_for_job() returns job dict when one is assigned."""
        from runner_common.job_helpers import poll_for_job

        mock_backend.set_response(200, {
            "job": {
                "id": "job-123",
                "card_title": "Fix bug",
                "repo_url": "https://github.com/org/repo",
            }
        })

        job = poll_for_job("runner-123", backend_url=mock_backend.url)

        assert job is not None
        assert job["id"] == "job-123"

    def test_poll_returns_none_when_no_job(self, mock_backend):
        """poll_for_job() returns None when no job available."""
        from runner_common.job_helpers import poll_for_job

        mock_backend.set_response(200, {"job": None})

        job = poll_for_job("runner-123", backend_url=mock_backend.url)
        assert job is None

    def test_poll_returns_none_on_404(self, mock_backend):
        """poll_for_job() returns None and signals reregister on 404."""
        from runner_common.job_helpers import poll_for_job, NeedsReregister

        mock_backend.set_response(404, {"detail": "Runner not found"})

        with pytest.raises(NeedsReregister):
            poll_for_job("runner-123", backend_url=mock_backend.url)


class TestHeartbeatThread:
    """Tests for HeartbeatThread class."""

    def test_heartbeat_thread_sends_periodic_heartbeats(self, mock_backend):
        """HeartbeatThread sends heartbeats at specified interval."""
        from runner_common.job_helpers import HeartbeatThread

        thread = HeartbeatThread(
            runner_id="runner-123",
            backend_url=mock_backend.url,
            interval=0.1,
        )

        thread.start()
        time.sleep(0.35)  # Should get ~3 heartbeats
        thread.stop()

        assert mock_backend.request_count >= 2

    def test_heartbeat_thread_stops_cleanly(self, mock_backend):
        """HeartbeatThread.stop() stops the thread."""
        from runner_common.job_helpers import HeartbeatThread

        thread = HeartbeatThread(
            runner_id="runner-123",
            backend_url=mock_backend.url,
            interval=0.1,
        )

        thread.start()
        time.sleep(0.15)
        thread.stop()

        # Give it time to stop
        time.sleep(0.1)
        assert not thread.is_alive()


# ============================================================================
# Fixtures
# ============================================================================


class MockBackend:
    """Mock backend for testing HTTP calls."""

    def __init__(self):
        self.url = "http://mock-backend"
        self.last_request = None
        self.request_count = 0
        self._response_status = 200
        self._response_body = {}
        self._delay = 0

    def set_response(self, status: int, body: dict):
        self._response_status = status
        self._response_body = body

    def set_delay(self, delay: float):
        self._delay = delay


@pytest.fixture
def mock_backend(monkeypatch):
    """Mock requests.Session to capture and respond to requests."""
    backend = MockBackend()

    class MockRequest:
        def __init__(self, method, path, json=None):
            self.method = method
            self.path = path
            self.json = json

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    original_session = requests.Session

    class MockSession:
        def __init__(self):
            pass

        def _make_request(self, method, url, **kwargs):
            import time as t
            if backend._delay:
                t.sleep(backend._delay)

            path = url.replace(backend.url, "")
            backend.last_request = MockRequest(method, path, kwargs.get("json"))
            backend.request_count += 1
            return MockResponse(backend._response_status, backend._response_body)

        def get(self, url, **kwargs):
            return self._make_request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self._make_request("POST", url, **kwargs)

    monkeypatch.setattr(requests, "Session", MockSession)

    return backend
