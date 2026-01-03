"""
TDD Tests for Control Layer Protocol (Phase 12.3).

These tests DEFINE the contract for the control layer that runs inside containers.
The control layer is responsible for:
- Reading step configuration from /workspace/.control/step_config.json
- Reporting status to backend (running, completed, failed)
- Streaming logs to backend (batched)
- Sending heartbeats during execution
- Executing the actual command

Write these tests FIRST, then implement to make them pass.
"""
import sys
import json
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, call
from datetime import datetime

import pytest

# Add images/base/control to path for imports
images_path = Path(__file__).parent.parent.parent.parent / "images" / "base"
sys.path.insert(0, str(images_path))


# -----------------------------------------------------------------------------
# Config Module Tests
# -----------------------------------------------------------------------------

class TestConfigReading:
    """Tests for step config reading."""

    def test_reads_config_from_control_dir(self, tmp_path):
        """Config at /workspace/.control/step_config.json is read."""
        config_dir = tmp_path / ".control"
        config_dir.mkdir()
        config_file = config_dir / "step_config.json"
        config_file.write_text(json.dumps({
            "step_id": "test-step-123",
            "backend_url": "http://localhost:8000",
            "token": "test-token-abc",
            "command": ["echo", "hello"],
            "working_dir": "/workspace/repo",
            "environment": {"CI": "true"},
            "timeout_seconds": 3600,
            "heartbeat_interval": 10,
            "log_batch_size": 100,
            "log_batch_interval": 1.0,
        }))

        from control.config import load_step_config

        config = load_step_config(config_file)

        assert config is not None
        assert config.step_id == "test-step-123"
        assert config.backend_url == "http://localhost:8000"
        assert config.token == "test-token-abc"
        assert config.command == ["echo", "hello"]
        assert config.working_dir == "/workspace/repo"
        assert config.environment == {"CI": "true"}
        assert config.timeout_seconds == 3600
        assert config.heartbeat_interval == 10

    def test_missing_config_returns_none(self, tmp_path):
        """Missing config file returns None."""
        from control.config import load_step_config

        config = load_step_config(tmp_path / "nonexistent.json")

        assert config is None

    def test_invalid_json_returns_none(self, tmp_path):
        """Invalid JSON returns None."""
        config_file = tmp_path / "step_config.json"
        config_file.write_text("not valid json {{{")

        from control.config import load_step_config

        config = load_step_config(config_file)

        assert config is None

    def test_config_with_defaults(self, tmp_path):
        """Config with only required fields uses defaults."""
        config_dir = tmp_path / ".control"
        config_dir.mkdir()
        config_file = config_dir / "step_config.json"
        config_file.write_text(json.dumps({
            "step_id": "test-step",
            "backend_url": "http://localhost:8000",
            "token": "token",
            "command": ["ls"],
        }))

        from control.config import load_step_config

        config = load_step_config(config_file)

        assert config is not None
        assert config.working_dir == "/workspace/repo"  # default
        assert config.timeout_seconds == 3600  # default
        assert config.heartbeat_interval == 10  # default
        assert config.log_batch_size == 100  # default
        assert config.log_batch_interval == 1.0  # default


# -----------------------------------------------------------------------------
# Backend Client Tests
# -----------------------------------------------------------------------------

class TestBackendClient:
    """Tests for backend HTTP client."""

    def test_reports_status_running(self):
        """POST to /api/steps/{id}/status with 'running' on start."""
        from control.backend_client import BackendClient

        # Create a mock response
        mock_response = Mock()
        mock_response.status_code = 200

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            result = client.report_status("running")

            assert result is True
            # Verify the request was made correctly
            mock_session.request.assert_called_once()
            call_args = mock_session.request.call_args
            assert call_args[0][0] == "POST"  # method
            assert "status" in call_args[0][1]  # URL contains status
            assert call_args[1]["json"]["status"] == "running"

    def test_reports_status_completed_with_exit_code(self):
        """POST with 'completed' and exit code on success."""
        from control.backend_client import BackendClient

        mock_response = Mock()
        mock_response.status_code = 200

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            result = client.report_status("completed", exit_code=0)

            assert result is True
            call_args = mock_session.request.call_args
            assert call_args[1]["json"]["status"] == "completed"
            assert call_args[1]["json"]["exit_code"] == 0

    def test_reports_status_failed_with_error(self):
        """POST with 'failed' and error message on failure."""
        from control.backend_client import BackendClient

        mock_response = Mock()
        mock_response.status_code = 200

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            result = client.report_status("failed", exit_code=1, error="Command failed")

            assert result is True
            call_args = mock_session.request.call_args
            assert call_args[1]["json"]["status"] == "failed"
            assert call_args[1]["json"]["exit_code"] == 1
            assert call_args[1]["json"]["error"] == "Command failed"

    def test_sends_logs_batch(self):
        """POST to /api/steps/{id}/logs with batched lines."""
        from control.backend_client import BackendClient

        mock_response = Mock()
        mock_response.status_code = 200

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            lines = ["line 1", "line 2", "line 3"]
            result = client.send_logs(lines)

            assert result is True
            call_args = mock_session.request.call_args
            assert call_args[1]["json"]["lines"] == lines

    def test_sends_heartbeat(self):
        """POST to /api/steps/{id}/heartbeat."""
        from control.backend_client import BackendClient

        mock_response = Mock()
        mock_response.status_code = 200

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            result = client.heartbeat()

            assert result is True


class TestBackendClientRetry:
    """Tests for retry behavior on network errors."""

    def test_retries_on_connection_error(self):
        """Retries on connection errors with backoff."""
        from control.backend_client import BackendClient

        call_count = [0]

        def mock_request(*args, **kwargs):
            call_count[0] += 1
            response = Mock()
            if call_count[0] < 3:
                response.status_code = 500
            else:
                response.status_code = 200
            return response

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.side_effect = mock_request
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            # Override retry settings for faster test
            client.MAX_RETRIES = 5
            client.BASE_BACKOFF = 0.01
            client.TOTAL_TIMEOUT = 10

            result = client.report_status("running")

            assert result is True
            assert call_count[0] == 3  # Failed twice, succeeded on third

    def test_returns_false_after_max_retries(self):
        """Returns False after exhausting retries."""
        from control.backend_client import BackendClient

        mock_response = Mock()
        mock_response.status_code = 500

        with patch("control.backend_client.requests.Session") as mock_session_class:
            mock_session = MagicMock()
            mock_session.request.return_value = mock_response
            mock_session_class.return_value = mock_session

            client = BackendClient("http://localhost:8000", "test-step", "test-token")
            # Override for faster test
            client.MAX_RETRIES = 2
            client.BASE_BACKOFF = 0.01
            client.TOTAL_TIMEOUT = 1

            result = client.report_status("running")

            assert result is False


# -----------------------------------------------------------------------------
# Heartbeat Manager Tests
# -----------------------------------------------------------------------------

class TestHeartbeatManager:
    """Tests for background heartbeat thread."""

    def test_starts_and_stops(self):
        """Heartbeat manager can start and stop."""
        from control.heartbeat import HeartbeatManager

        mock_client = Mock()
        mock_client.heartbeat.return_value = True

        manager = HeartbeatManager(mock_client, interval=0.1)
        manager.start()

        # Wait a bit for at least one heartbeat
        time.sleep(0.15)

        manager.stop()

        # Heartbeat should have been called at least once
        assert mock_client.heartbeat.called

    def test_sends_periodic_heartbeats(self):
        """Heartbeat is sent at configured interval."""
        from control.heartbeat import HeartbeatManager

        mock_client = Mock()
        mock_client.heartbeat.return_value = True

        manager = HeartbeatManager(mock_client, interval=0.05)
        manager.start()

        # Wait for multiple heartbeats
        time.sleep(0.2)

        manager.stop()

        # Should have sent multiple heartbeats
        assert mock_client.heartbeat.call_count >= 2

    def test_stops_cleanly_on_stop(self):
        """Manager stops without hanging."""
        from control.heartbeat import HeartbeatManager

        mock_client = Mock()
        mock_client.heartbeat.return_value = True

        manager = HeartbeatManager(mock_client, interval=1.0)
        manager.start()

        # Stop should be quick even with long interval
        start = time.time()
        manager.stop()
        elapsed = time.time() - start

        assert elapsed < 1.0  # Should not wait for next interval


# -----------------------------------------------------------------------------
# Command Execution Tests
# -----------------------------------------------------------------------------

class TestCommandExecution:
    """Tests for command execution and output capture."""

    def test_executes_command_and_returns_exit_code(self, tmp_path):
        """Command is executed and exit code returned."""
        from control.executor import execute_command

        class MockConfig:
            command = ["python", "-c", "print('hello')"]
            working_dir = str(tmp_path)
            environment = {}
            timeout_seconds = 60
            log_batch_size = 100
            log_batch_interval = 1.0

        mock_client = Mock()
        mock_client.send_logs.return_value = True

        exit_code = execute_command(MockConfig(), mock_client)

        assert exit_code == 0

    def test_captures_stdout(self, tmp_path):
        """Command stdout is captured and sent to backend."""
        from control.executor import execute_command

        class MockConfig:
            command = ["python", "-c", "print('hello world')"]
            working_dir = str(tmp_path)
            environment = {}
            timeout_seconds = 60
            log_batch_size = 100
            log_batch_interval = 1.0

        mock_client = Mock()
        mock_client.send_logs.return_value = True

        execute_command(MockConfig(), mock_client)

        # Verify logs were sent
        assert mock_client.send_logs.called
        # Find the call that contains "hello world"
        all_lines = []
        for call in mock_client.send_logs.call_args_list:
            all_lines.extend(call[0][0])
        assert any("hello world" in line for line in all_lines)

    def test_returns_nonzero_exit_code_on_failure(self, tmp_path):
        """Failed command returns non-zero exit code."""
        from control.executor import execute_command

        class MockConfig:
            command = ["python", "-c", "import sys; sys.exit(42)"]
            working_dir = str(tmp_path)
            environment = {}
            timeout_seconds = 60
            log_batch_size = 100
            log_batch_interval = 1.0

        mock_client = Mock()
        mock_client.send_logs.return_value = True

        exit_code = execute_command(MockConfig(), mock_client)

        assert exit_code == 42

    def test_uses_environment_variables(self, tmp_path):
        """Environment variables are passed to command."""
        from control.executor import execute_command

        class MockConfig:
            command = ["python", "-c", "import os; print(os.environ.get('MY_VAR', 'not set'))"]
            working_dir = str(tmp_path)
            environment = {"MY_VAR": "test_value"}
            timeout_seconds = 60
            log_batch_size = 100
            log_batch_interval = 1.0

        mock_client = Mock()
        mock_client.send_logs.return_value = True

        execute_command(MockConfig(), mock_client)

        # Check that the environment variable was used
        all_lines = []
        for call_args in mock_client.send_logs.call_args_list:
            all_lines.extend(call_args[0][0])
        assert any("test_value" in line for line in all_lines)
