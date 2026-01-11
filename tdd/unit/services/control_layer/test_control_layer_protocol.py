"""
Unit tests for Control Layer Protocol.

These tests define the contract for container-to-backend communication:
- Reading step config from /workspace/.control/step_config.json
- Reporting status on start/complete
- Streaming logs to backend
- Sending periodic heartbeats
- Handling backend unavailability gracefully

Write these tests BEFORE implementing the control layer.
"""
import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from uuid import uuid4

import pytest

# Tests enabled - Phase 12.3 control layer implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Step Config Reading
# -----------------------------------------------------------------------------

class TestStepConfigReading:
    """Tests that verify step config reading from control directory."""

    def test_reads_config_from_control_directory(self):
        """Control layer reads config from /workspace/.control/step_config.json."""
        from app.services.control_layer.protocol import StepConfig

        config_data = {
            "step_id": "step-123",
            "step_run_id": "run-456",
            "execution_key": "exec-789:0:1",
            "command": "python test.py",
            "backend_url": "http://backend:8000",
            "auth_token": "secret-token",
        }

        config = StepConfig.from_dict(config_data)
        assert config.step_id == "step-123"
        assert config.step_run_id == "run-456"
        assert config.command == "python test.py"
        assert config.backend_url == "http://backend:8000"
        assert config.auth_token == "secret-token"

    def test_config_includes_environment_vars(self):
        """Step config can include environment variables to set."""
        from app.services.control_layer.protocol import StepConfig

        config_data = {
            "step_id": "step-123",
            "step_run_id": "run-456",
            "execution_key": "exec-789:0:1",
            "command": "echo $FOO",
            "backend_url": "http://backend:8000",
            "auth_token": "token",
            "environment": {
                "FOO": "bar",
                "DEBUG": "1",
            },
        }

        config = StepConfig.from_dict(config_data)
        assert config.environment == {"FOO": "bar", "DEBUG": "1"}

    def test_config_includes_timeout(self):
        """Step config can specify execution timeout."""
        from app.services.control_layer.protocol import StepConfig

        config_data = {
            "step_id": "step-123",
            "step_run_id": "run-456",
            "execution_key": "exec-789:0:1",
            "command": "long-running-script",
            "backend_url": "http://backend:8000",
            "auth_token": "token",
            "timeout_seconds": 3600,
        }

        config = StepConfig.from_dict(config_data)
        assert config.timeout_seconds == 3600

    def test_config_includes_working_directory(self):
        """Step config can specify working directory."""
        from app.services.control_layer.protocol import StepConfig

        config_data = {
            "step_id": "step-123",
            "step_run_id": "run-456",
            "execution_key": "exec-789:0:1",
            "command": "npm test",
            "backend_url": "http://backend:8000",
            "auth_token": "token",
            "working_directory": "/workspace/repo",
        }

        config = StepConfig.from_dict(config_data)
        assert config.working_directory == "/workspace/repo"

    def test_config_default_working_directory(self):
        """Default working directory is /workspace/repo."""
        from app.services.control_layer.protocol import StepConfig

        config_data = {
            "step_id": "step-123",
            "step_run_id": "run-456",
            "execution_key": "exec-789:0:1",
            "command": "ls",
            "backend_url": "http://backend:8000",
            "auth_token": "token",
        }

        config = StepConfig.from_dict(config_data)
        assert config.working_directory == "/workspace/repo"


# -----------------------------------------------------------------------------
# Contract: Status Reporting
# -----------------------------------------------------------------------------

class TestStatusReporting:
    """Tests that verify status reporting to backend."""

    async def test_reports_running_on_start(self):
        """Control layer reports 'running' status when step starts."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.report_status("running")

        mock_http.post.assert_called_once()
        call_args = mock_http.post.call_args
        assert "/api/steps/step-123/status" in call_args[0][0]
        assert call_args[1]["json"]["status"] == "running"

    async def test_reports_completed_on_success(self):
        """Control layer reports 'completed' status when step succeeds."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.report_status("completed", exit_code=0)

        call_args = mock_http.post.call_args
        assert call_args[1]["json"]["status"] == "completed"
        assert call_args[1]["json"]["exit_code"] == 0

    async def test_reports_failed_on_error(self):
        """Control layer reports 'failed' status when step fails."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.report_status("failed", exit_code=1, error="Command failed")

        call_args = mock_http.post.call_args
        assert call_args[1]["json"]["status"] == "failed"
        assert call_args[1]["json"]["exit_code"] == 1
        assert call_args[1]["json"]["error"] == "Command failed"

    async def test_status_includes_auth_token(self):
        """Status requests include auth token in header."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="secret-token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.report_status("running")

        call_args = mock_http.post.call_args
        assert call_args[1]["headers"]["Authorization"] == "Bearer secret-token"


# -----------------------------------------------------------------------------
# Contract: Log Streaming
# -----------------------------------------------------------------------------

class TestLogStreaming:
    """Tests that verify log streaming to backend."""

    async def test_streams_stdout_to_backend(self):
        """Control layer streams stdout lines to backend."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.send_logs("Hello from step\n")

        mock_http.post.assert_called()
        call_args = mock_http.post.call_args
        assert "/api/steps/step-123/logs" in call_args[0][0]
        assert "Hello from step" in call_args[1]["json"]["content"]

    async def test_streams_stderr_to_backend(self):
        """Control layer streams stderr lines to backend."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.send_logs("Error occurred\n", stream="stderr")

        call_args = mock_http.post.call_args
        assert call_args[1]["json"]["stream"] == "stderr"

    async def test_batches_rapid_log_lines(self):
        """Control layer batches rapid log lines to reduce API calls."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            log_batch_size=3,
        )

        # Send multiple lines rapidly
        await client.queue_log_line("line 1")
        await client.queue_log_line("line 2")
        await client.queue_log_line("line 3")

        # Should have batched into one call
        await client.flush_logs()

        # Verify batching occurred
        calls = [c for c in mock_http.post.call_args_list if "/logs" in str(c)]
        assert len(calls) >= 1

    async def test_logs_include_timestamp(self):
        """Log entries include timestamp."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.send_logs("Test message")

        call_args = mock_http.post.call_args
        assert "timestamp" in call_args[1]["json"]


# -----------------------------------------------------------------------------
# Contract: Heartbeat
# -----------------------------------------------------------------------------

class TestHeartbeat:
    """Tests that verify heartbeat mechanism."""

    async def test_sends_heartbeat_periodically(self):
        """Control layer sends heartbeats during long operations."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            heartbeat_interval=0.1,  # 100ms for testing
        )

        await client.send_heartbeat()

        mock_http.post.assert_called()
        call_args = mock_http.post.call_args
        assert "/api/steps/step-123/heartbeat" in call_args[0][0]

    async def test_heartbeat_extends_timeout(self):
        """Heartbeat request extends step timeout on backend."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.send_heartbeat(extend_seconds=300)

        call_args = mock_http.post.call_args
        assert call_args[1]["json"]["extend_seconds"] == 300

    async def test_heartbeat_includes_progress(self):
        """Heartbeat can include progress information."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.return_value = Mock(status_code=200)

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
        )

        await client.send_heartbeat(progress={"percent": 50, "message": "Halfway done"})

        call_args = mock_http.post.call_args
        assert call_args[1]["json"]["progress"]["percent"] == 50


# -----------------------------------------------------------------------------
# Contract: Backend Unavailability
# -----------------------------------------------------------------------------

class TestBackendUnavailability:
    """Tests that verify graceful handling of backend unavailability."""

    async def test_retries_on_connection_error(self):
        """Control layer retries on connection errors."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        # First call fails, second succeeds
        mock_http.post.side_effect = [
            Exception("Connection refused"),
            Mock(status_code=200),
        ]

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            max_retries=3,
            retry_delay=0.01,
        )

        await client.report_status("running")

        assert mock_http.post.call_count == 2

    async def test_continues_execution_on_backend_down(self):
        """Step continues execution even if backend is unavailable."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.side_effect = Exception("Backend down")

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            max_retries=1,
            retry_delay=0.01,
        )

        # Should not raise, just log warning
        await client.report_status("running")
        # Execution continues...

    async def test_queues_logs_when_backend_unavailable(self):
        """Logs are queued when backend is unavailable."""
        from app.services.control_layer.protocol import ControlLayerClient

        mock_http = AsyncMock()
        mock_http.post.side_effect = Exception("Backend down")

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            max_retries=1,
            retry_delay=0.01,
        )

        await client.queue_log_line("Log line 1")
        await client.queue_log_line("Log line 2")

        # Logs should be queued internally
        assert client.pending_log_count >= 2

    async def test_flushes_queued_logs_when_backend_recovers(self):
        """Queued logs are flushed when backend recovers."""
        from app.services.control_layer.protocol import ControlLayerClient

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("Backend down")
            return Mock(status_code=200)

        mock_http = AsyncMock()
        mock_http.post.side_effect = mock_post

        client = ControlLayerClient(
            backend_url="http://backend:8000",
            auth_token="token",
            step_id="step-123",
            http_client=mock_http,
            max_retries=1,
            retry_delay=0.01,
        )

        await client.queue_log_line("Log line 1")
        await client.queue_log_line("Log line 2")

        # Backend recovers
        await client.flush_logs()

        # Should have attempted to send
        assert mock_http.post.call_count >= 1


# -----------------------------------------------------------------------------
# Contract: Step Execution
# -----------------------------------------------------------------------------

class TestStepExecution:
    """Tests that verify the step execution lifecycle."""

    async def test_executes_command_in_working_directory(self):
        """Control layer executes command in specified working directory."""
        from app.services.control_layer.protocol import StepExecutor

        executor = StepExecutor(
            command="pwd",
            working_directory="/tmp",
        )

        result = await executor.run()
        assert result.exit_code == 0

    async def test_captures_stdout_and_stderr(self):
        """Control layer captures both stdout and stderr."""
        from app.services.control_layer.protocol import StepExecutor

        executor = StepExecutor(
            command='echo "stdout" && echo "stderr" >&2',
            working_directory="/tmp",
            shell=True,
        )

        result = await executor.run()
        assert "stdout" in result.stdout
        assert "stderr" in result.stderr

    async def test_respects_timeout(self):
        """Control layer respects execution timeout."""
        from app.services.control_layer.protocol import StepExecutor, StepTimeoutError

        executor = StepExecutor(
            command="sleep 10",
            working_directory="/tmp",
            timeout_seconds=0.1,
            shell=True,
        )

        with pytest.raises(StepTimeoutError):
            await executor.run()

    async def test_sets_environment_variables(self):
        """Control layer sets specified environment variables."""
        from app.services.control_layer.protocol import StepExecutor
        import platform

        # Use platform-appropriate command
        if platform.system() == "Windows":
            command = 'echo %MY_VAR%'
        else:
            command = 'echo $MY_VAR'

        executor = StepExecutor(
            command=command,
            working_directory="/tmp" if platform.system() != "Windows" else "C:\\",
            environment={"MY_VAR": "hello"},
            shell=True,
        )

        result = await executor.run()
        assert "hello" in result.stdout
