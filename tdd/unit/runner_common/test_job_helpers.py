"""
Tests for job_helpers module (Phase 12.0).

These tests DEFINE the contract for job communication with the backend.
Write tests first, then implement to make them pass.

Contract defined:
- send_heartbeat(backend_url, runner_id) -> bool
- report_status(backend_url, runner_id, status, error) -> bool
- complete_job(backend_url, runner_id, success, error, test_results) -> bool
- send_log(backend_url, runner_id, lines) -> bool
- poll_for_job(backend_url, runner_id) -> dict | None
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from runner_common.job_helpers import (
        JobHelpers,
        HeartbeatError,
        ConnectionError,
        send_heartbeat,
        report_status,
        complete_job,
        send_log,
        poll_for_job,
    )
    RUNNER_COMMON_AVAILABLE = True
except ImportError:
    RUNNER_COMMON_AVAILABLE = False
    # Define placeholders
    JobHelpers = None
    HeartbeatError = ConnectionError = Exception
    send_heartbeat = report_status = complete_job = send_log = poll_for_job = None


pytestmark = pytest.mark.skipif(
    not RUNNER_COMMON_AVAILABLE,
    reason="runner-common not yet implemented"
)


@pytest.fixture
def mock_session():
    """Create a mock requests session."""
    with patch("runner_common.job_helpers.requests.Session") as mock:
        session = MagicMock()
        mock.return_value = session
        yield session


@pytest.fixture
def job_helpers():
    """Create a JobHelpers instance for testing."""
    return JobHelpers(
        backend_url="http://localhost:8000",
        runner_id="test-runner-123",
    )


class TestSendHeartbeat:
    """Tests for send_heartbeat() function."""

    def test_sends_to_correct_endpoint(self, mock_session):
        """send_heartbeat() POSTs to /api/runners/{id}/heartbeat."""
        mock_session.post.return_value.status_code = 200

        result = send_heartbeat(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        mock_session.post.assert_called_once()
        call_url = mock_session.post.call_args[0][0]
        assert "/api/runners/runner-123/heartbeat" in call_url

    def test_returns_true_on_success(self, mock_session):
        """send_heartbeat() returns True on 200 response."""
        mock_session.post.return_value.status_code = 200

        result = send_heartbeat(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        assert result is True

    def test_returns_false_on_404(self, mock_session):
        """send_heartbeat() returns False when runner not found (404)."""
        mock_session.post.return_value.status_code = 404

        result = send_heartbeat(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        assert result is False

    def test_raises_on_connection_error(self, mock_session):
        """send_heartbeat() raises ConnectionError on network failure."""
        import requests
        mock_session.post.side_effect = requests.exceptions.ConnectionError()

        with pytest.raises(ConnectionError):
            send_heartbeat(
                backend_url="http://localhost:8000",
                runner_id="runner-123",
                session=mock_session,
            )

    def test_uses_timeout(self, mock_session):
        """send_heartbeat() uses a reasonable timeout."""
        mock_session.post.return_value.status_code = 200

        send_heartbeat(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert "timeout" in call_kwargs
        assert call_kwargs["timeout"] <= 10  # Should be short


class TestReportStatus:
    """Tests for report_status() function."""

    def test_sends_status_payload(self, mock_session):
        """report_status() sends status in JSON payload."""
        mock_session.post.return_value.status_code = 200

        report_status(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            status="running",
            error=None,
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["status"] == "running"

    def test_includes_error_when_provided(self, mock_session):
        """report_status() includes error message when provided."""
        mock_session.post.return_value.status_code = 200

        report_status(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            status="failed",
            error="Something went wrong",
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["error"] == "Something went wrong"

    def test_returns_true_on_success(self, mock_session):
        """report_status() returns True on success."""
        mock_session.post.return_value.status_code = 200

        result = report_status(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            status="running",
            error=None,
            session=mock_session,
        )

        assert result is True


class TestCompleteJob:
    """Tests for complete_job() function."""

    def test_sends_to_complete_endpoint(self, mock_session):
        """complete_job() POSTs to /api/runners/{id}/complete."""
        mock_session.post.return_value.status_code = 200

        complete_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            success=True,
            error=None,
            test_results=None,
            session=mock_session,
        )

        call_url = mock_session.post.call_args[0][0]
        assert "/api/runners/runner-123/complete" in call_url

    def test_sends_success_flag(self, mock_session):
        """complete_job() sends success flag in payload."""
        mock_session.post.return_value.status_code = 200

        complete_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            success=True,
            error=None,
            test_results=None,
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["success"] is True

    def test_sends_error_on_failure(self, mock_session):
        """complete_job() sends error message on failure."""
        mock_session.post.return_value.status_code = 200

        complete_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            success=False,
            error="Build failed",
            test_results=None,
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["success"] is False
        assert call_kwargs["json"]["error"] == "Build failed"

    def test_sends_test_results(self, mock_session):
        """complete_job() includes test results when provided."""
        mock_session.post.return_value.status_code = 200
        test_results = {
            "tests_run": True,
            "tests_passed": True,
            "pass_count": 10,
            "fail_count": 0,
            "skip_count": 2,
            "output": "All tests passed",
        }

        complete_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            success=True,
            error=None,
            test_results=test_results,
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["test_results"] == test_results


class TestSendLog:
    """Tests for send_log() function."""

    def test_sends_to_logs_endpoint(self, mock_session):
        """send_log() POSTs to /api/runners/{id}/logs."""
        mock_session.post.return_value.status_code = 200

        send_log(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            lines=["Line 1", "Line 2"],
            session=mock_session,
        )

        call_url = mock_session.post.call_args[0][0]
        assert "/api/runners/runner-123/logs" in call_url

    def test_sends_lines_as_list(self, mock_session):
        """send_log() sends lines as a list in JSON payload."""
        mock_session.post.return_value.status_code = 200

        send_log(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            lines=["Line 1", "Line 2", "Line 3"],
            session=mock_session,
        )

        call_kwargs = mock_session.post.call_args[1]
        assert call_kwargs["json"]["lines"] == ["Line 1", "Line 2", "Line 3"]

    def test_silent_failure_on_error(self, mock_session):
        """send_log() doesn't raise on failure (logs are best-effort)."""
        mock_session.post.side_effect = Exception("Network error")

        # Should not raise
        result = send_log(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            lines=["Log line"],
            session=mock_session,
        )

        assert result is False


class TestPollForJob:
    """Tests for poll_for_job() function."""

    def test_sends_to_job_endpoint(self, mock_session):
        """poll_for_job() GETs /api/runners/{id}/job."""
        mock_session.get.return_value.status_code = 200
        mock_session.get.return_value.json.return_value = {"job": None}

        poll_for_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        call_url = mock_session.get.call_args[0][0]
        assert "/api/runners/runner-123/job" in call_url

    def test_returns_job_dict_when_available(self, mock_session):
        """poll_for_job() returns job dict when job is available."""
        job_data = {
            "id": "job-456",
            "card_id": "card-789",
            "card_title": "Fix bug",
            "card_description": "Fix the login bug",
            "step_type": "agent",
        }
        mock_session.get.return_value.status_code = 200
        mock_session.get.return_value.json.return_value = {"job": job_data}

        result = poll_for_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        assert result == job_data

    def test_returns_none_when_no_job(self, mock_session):
        """poll_for_job() returns None when no job available."""
        mock_session.get.return_value.status_code = 200
        mock_session.get.return_value.json.return_value = {"job": None}

        result = poll_for_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        assert result is None

    def test_returns_none_on_404(self, mock_session):
        """poll_for_job() returns None when runner not found."""
        mock_session.get.return_value.status_code = 404

        result = poll_for_job(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
            session=mock_session,
        )

        assert result is None


class TestJobHelpersClass:
    """Tests for JobHelpers class (stateful wrapper)."""

    def test_initialization(self):
        """JobHelpers initializes with backend URL and runner ID."""
        helpers = JobHelpers(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
        )

        assert helpers.backend_url == "http://localhost:8000"
        assert helpers.runner_id == "runner-123"

    def test_has_session(self):
        """JobHelpers creates a requests session."""
        helpers = JobHelpers(
            backend_url="http://localhost:8000",
            runner_id="runner-123",
        )

        assert helpers.session is not None

    def test_log_method(self, mock_session):
        """JobHelpers.log() logs to console and sends to backend."""
        with patch("runner_common.job_helpers.requests.Session", return_value=mock_session):
            mock_session.post.return_value.status_code = 200

            helpers = JobHelpers(
                backend_url="http://localhost:8000",
                runner_id="runner-123",
            )
            helpers.log("Test message")

            # Should have sent to backend
            mock_session.post.assert_called()

    def test_heartbeat_method(self, mock_session):
        """JobHelpers.heartbeat() sends heartbeat."""
        with patch("runner_common.job_helpers.requests.Session", return_value=mock_session):
            mock_session.post.return_value.status_code = 200

            helpers = JobHelpers(
                backend_url="http://localhost:8000",
                runner_id="runner-123",
            )
            result = helpers.heartbeat()

            assert result is True

    def test_complete_method(self, mock_session):
        """JobHelpers.complete() completes job."""
        with patch("runner_common.job_helpers.requests.Session", return_value=mock_session):
            mock_session.post.return_value.status_code = 200

            helpers = JobHelpers(
                backend_url="http://localhost:8000",
                runner_id="runner-123",
            )
            helpers.complete(success=True)

            call_url = mock_session.post.call_args[0][0]
            assert "complete" in call_url
