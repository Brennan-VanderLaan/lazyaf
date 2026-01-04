"""
Tests for Runner WebSocket Protocol (Phase 12.6).

These tests DEFINE the WebSocket message protocol between runners and backend.
Write tests first, then implement to make them pass.

Protocol Messages:
Runner -> Backend:
  - register: {type: "register", runner_id, name, runner_type, labels}
  - ack: {type: "ack", step_id}
  - heartbeat: {type: "heartbeat"}
  - log: {type: "log", step_id, lines}
  - step_complete: {type: "step_complete", step_id, exit_code, error}

Backend -> Runner:
  - registered: {type: "registered", runner_id}
  - execute_step: {type: "execute_step", step_id, execution_key, config}
  - pong: {type: "pong"}
  - error: {type: "error", message}

Timeouts:
  - Registration: 10s (runner must send register within 10s of connect)
  - ACK: 5s (runner must ACK job within 5s of assignment)
  - Heartbeat interval: 10s (runner sends heartbeat every 10s)
  - Death: 30s (mark runner dead if no heartbeat for 30s)
"""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.runner_protocol import (
        RunnerMessage,
        BackendMessage,
        RegisterMessage,
        AckMessage,
        HeartbeatMessage,
        LogMessage,
        StepCompleteMessage,
        RegisteredMessage,
        ExecuteStepMessage,
        PongMessage,
        ErrorMessage,
        parse_runner_message,
        create_backend_message,
        validate_runner_message,
        REGISTRATION_TIMEOUT,
        ACK_TIMEOUT,
        HEARTBEAT_INTERVAL,
        DEATH_TIMEOUT,
    )
    PROTOCOL_MODULE_AVAILABLE = True
except ImportError:
    PROTOCOL_MODULE_AVAILABLE = False
    # Define placeholders
    RunnerMessage = None
    BackendMessage = None
    RegisterMessage = None
    AckMessage = None
    HeartbeatMessage = None
    LogMessage = None
    StepCompleteMessage = None
    RegisteredMessage = None
    ExecuteStepMessage = None
    PongMessage = None
    ErrorMessage = None
    parse_runner_message = None
    create_backend_message = None
    validate_runner_message = None
    REGISTRATION_TIMEOUT = 10
    ACK_TIMEOUT = 5
    HEARTBEAT_INTERVAL = 10
    DEATH_TIMEOUT = 30


pytestmark = pytest.mark.skipif(
    not PROTOCOL_MODULE_AVAILABLE,
    reason="runner_protocol module not yet implemented"
)


class TestTimeoutConstants:
    """Tests for protocol timeout constants."""

    def test_registration_timeout_is_10_seconds(self):
        """Registration timeout is 10 seconds."""
        assert REGISTRATION_TIMEOUT == 10

    def test_ack_timeout_is_5_seconds(self):
        """ACK timeout is 5 seconds."""
        assert ACK_TIMEOUT == 5

    def test_heartbeat_interval_is_10_seconds(self):
        """Heartbeat interval is 10 seconds."""
        assert HEARTBEAT_INTERVAL == 10

    def test_death_timeout_is_30_seconds(self):
        """Death timeout is 30 seconds."""
        assert DEATH_TIMEOUT == 30


class TestRegisterMessage:
    """Tests for register message format."""

    def test_register_message_type(self):
        """Register message has correct type field."""
        msg = RegisterMessage(
            runner_id="runner-1",
            name="My Runner",
            runner_type="claude-code",
            labels={}
        )
        assert msg.type == "register"

    def test_register_message_includes_runner_id(self):
        """Register message includes runner_id."""
        msg = RegisterMessage(
            runner_id="runner-123",
            name="My Runner",
            runner_type="claude-code",
            labels={}
        )
        assert msg.runner_id == "runner-123"

    def test_register_message_includes_name(self):
        """Register message includes name."""
        msg = RegisterMessage(
            runner_id="runner-1",
            name="Workshop Pi",
            runner_type="claude-code",
            labels={}
        )
        assert msg.name == "Workshop Pi"

    def test_register_message_includes_runner_type(self):
        """Register message includes runner_type."""
        msg = RegisterMessage(
            runner_id="runner-1",
            name="My Runner",
            runner_type="gemini",
            labels={}
        )
        assert msg.runner_type == "gemini"

    def test_register_message_includes_labels(self):
        """Register message includes labels dict."""
        labels = {"arch": "arm64", "has": ["gpio", "camera"]}
        msg = RegisterMessage(
            runner_id="runner-1",
            name="My Runner",
            runner_type="claude-code",
            labels=labels
        )
        assert msg.labels == labels

    def test_register_message_to_dict(self):
        """Register message serializes to dict."""
        msg = RegisterMessage(
            runner_id="runner-1",
            name="My Runner",
            runner_type="claude-code",
            labels={"arch": "amd64"}
        )
        data = msg.to_dict()

        assert data["type"] == "register"
        assert data["runner_id"] == "runner-1"
        assert data["name"] == "My Runner"
        assert data["runner_type"] == "claude-code"
        assert data["labels"] == {"arch": "amd64"}


class TestAckMessage:
    """Tests for ACK message format."""

    def test_ack_message_type(self):
        """ACK message has correct type field."""
        msg = AckMessage(step_id="step-123")
        assert msg.type == "ack"

    def test_ack_message_includes_step_id(self):
        """ACK message includes step_id."""
        msg = AckMessage(step_id="step-456")
        assert msg.step_id == "step-456"

    def test_ack_message_to_dict(self):
        """ACK message serializes to dict."""
        msg = AckMessage(step_id="step-123")
        data = msg.to_dict()

        assert data["type"] == "ack"
        assert data["step_id"] == "step-123"


class TestHeartbeatMessage:
    """Tests for heartbeat message format."""

    def test_heartbeat_message_type(self):
        """Heartbeat message has correct type field."""
        msg = HeartbeatMessage()
        assert msg.type == "heartbeat"

    def test_heartbeat_message_to_dict(self):
        """Heartbeat message serializes to dict."""
        msg = HeartbeatMessage()
        data = msg.to_dict()

        assert data["type"] == "heartbeat"


class TestLogMessage:
    """Tests for log message format."""

    def test_log_message_type(self):
        """Log message has correct type field."""
        msg = LogMessage(step_id="step-123", lines=["line 1"])
        assert msg.type == "log"

    def test_log_message_includes_step_id(self):
        """Log message includes step_id."""
        msg = LogMessage(step_id="step-456", lines=["line 1"])
        assert msg.step_id == "step-456"

    def test_log_message_includes_lines(self):
        """Log message includes log lines."""
        lines = ["Starting job...", "Processing...", "Done!"]
        msg = LogMessage(step_id="step-123", lines=lines)
        assert msg.lines == lines

    def test_log_message_to_dict(self):
        """Log message serializes to dict."""
        msg = LogMessage(step_id="step-123", lines=["line 1", "line 2"])
        data = msg.to_dict()

        assert data["type"] == "log"
        assert data["step_id"] == "step-123"
        assert data["lines"] == ["line 1", "line 2"]


class TestStepCompleteMessage:
    """Tests for step_complete message format."""

    def test_step_complete_message_type(self):
        """Step complete message has correct type field."""
        msg = StepCompleteMessage(step_id="step-123", exit_code=0, error=None)
        assert msg.type == "step_complete"

    def test_step_complete_message_includes_step_id(self):
        """Step complete message includes step_id."""
        msg = StepCompleteMessage(step_id="step-456", exit_code=0, error=None)
        assert msg.step_id == "step-456"

    def test_step_complete_message_includes_exit_code(self):
        """Step complete message includes exit_code."""
        msg = StepCompleteMessage(step_id="step-123", exit_code=1, error=None)
        assert msg.exit_code == 1

    def test_step_complete_message_includes_error(self):
        """Step complete message can include error message."""
        msg = StepCompleteMessage(
            step_id="step-123",
            exit_code=1,
            error="Command failed"
        )
        assert msg.error == "Command failed"

    def test_step_complete_message_to_dict(self):
        """Step complete message serializes to dict."""
        msg = StepCompleteMessage(
            step_id="step-123",
            exit_code=0,
            error=None
        )
        data = msg.to_dict()

        assert data["type"] == "step_complete"
        assert data["step_id"] == "step-123"
        assert data["exit_code"] == 0
        assert data["error"] is None


class TestRegisteredMessage:
    """Tests for registered (backend->runner) message format."""

    def test_registered_message_type(self):
        """Registered message has correct type field."""
        msg = RegisteredMessage(runner_id="runner-123")
        assert msg.type == "registered"

    def test_registered_message_includes_runner_id(self):
        """Registered message includes runner_id."""
        msg = RegisteredMessage(runner_id="runner-456")
        assert msg.runner_id == "runner-456"

    def test_registered_message_to_dict(self):
        """Registered message serializes to dict."""
        msg = RegisteredMessage(runner_id="runner-123")
        data = msg.to_dict()

        assert data["type"] == "registered"
        assert data["runner_id"] == "runner-123"


class TestExecuteStepMessage:
    """Tests for execute_step (backend->runner) message format."""

    def test_execute_step_message_type(self):
        """Execute step message has correct type field."""
        msg = ExecuteStepMessage(
            step_id="step-123",
            execution_key="run-1:0:1",
            config={}
        )
        assert msg.type == "execute_step"

    def test_execute_step_message_includes_step_id(self):
        """Execute step message includes step_id."""
        msg = ExecuteStepMessage(
            step_id="step-456",
            execution_key="run-1:0:1",
            config={}
        )
        assert msg.step_id == "step-456"

    def test_execute_step_message_includes_execution_key(self):
        """Execute step message includes execution_key for idempotency."""
        msg = ExecuteStepMessage(
            step_id="step-123",
            execution_key="run-abc:2:1",
            config={}
        )
        assert msg.execution_key == "run-abc:2:1"

    def test_execute_step_message_includes_config(self):
        """Execute step message includes full config."""
        config = {
            "command": "pytest -v",
            "image": "python:3.12",
            "timeout": 300
        }
        msg = ExecuteStepMessage(
            step_id="step-123",
            execution_key="run-1:0:1",
            config=config
        )
        assert msg.config == config

    def test_execute_step_message_to_dict(self):
        """Execute step message serializes to dict."""
        config = {"command": "echo hello"}
        msg = ExecuteStepMessage(
            step_id="step-123",
            execution_key="run-1:0:1",
            config=config
        )
        data = msg.to_dict()

        assert data["type"] == "execute_step"
        assert data["step_id"] == "step-123"
        assert data["execution_key"] == "run-1:0:1"
        assert data["config"] == config


class TestErrorMessage:
    """Tests for error (backend->runner) message format."""

    def test_error_message_type(self):
        """Error message has correct type field."""
        msg = ErrorMessage(message="Something went wrong")
        assert msg.type == "error"

    def test_error_message_includes_message(self):
        """Error message includes error message."""
        msg = ErrorMessage(message="Invalid step ID")
        assert msg.message == "Invalid step ID"

    def test_error_message_to_dict(self):
        """Error message serializes to dict."""
        msg = ErrorMessage(message="Connection refused")
        data = msg.to_dict()

        assert data["type"] == "error"
        assert data["message"] == "Connection refused"


class TestParseRunnerMessage:
    """Tests for parsing incoming runner messages."""

    def test_parse_register_message(self):
        """Parses register message correctly."""
        data = {
            "type": "register",
            "runner_id": "runner-1",
            "name": "My Runner",
            "runner_type": "claude-code",
            "labels": {"arch": "amd64"}
        }
        msg = parse_runner_message(data)

        assert isinstance(msg, RegisterMessage)
        assert msg.runner_id == "runner-1"
        assert msg.labels == {"arch": "amd64"}

    def test_parse_ack_message(self):
        """Parses ACK message correctly."""
        data = {"type": "ack", "step_id": "step-123"}
        msg = parse_runner_message(data)

        assert isinstance(msg, AckMessage)
        assert msg.step_id == "step-123"

    def test_parse_heartbeat_message(self):
        """Parses heartbeat message correctly."""
        data = {"type": "heartbeat"}
        msg = parse_runner_message(data)

        assert isinstance(msg, HeartbeatMessage)

    def test_parse_log_message(self):
        """Parses log message correctly."""
        data = {
            "type": "log",
            "step_id": "step-123",
            "lines": ["line 1", "line 2"]
        }
        msg = parse_runner_message(data)

        assert isinstance(msg, LogMessage)
        assert msg.step_id == "step-123"
        assert msg.lines == ["line 1", "line 2"]

    def test_parse_step_complete_message(self):
        """Parses step_complete message correctly."""
        data = {
            "type": "step_complete",
            "step_id": "step-123",
            "exit_code": 0,
            "error": None
        }
        msg = parse_runner_message(data)

        assert isinstance(msg, StepCompleteMessage)
        assert msg.step_id == "step-123"
        assert msg.exit_code == 0

    def test_parse_unknown_type_raises(self):
        """Parsing unknown message type raises ValueError."""
        data = {"type": "unknown_type"}

        with pytest.raises(ValueError, match="Unknown message type"):
            parse_runner_message(data)

    def test_parse_missing_type_raises(self):
        """Parsing message without type raises ValueError."""
        data = {"runner_id": "runner-1"}

        with pytest.raises(ValueError, match="Missing message type"):
            parse_runner_message(data)


class TestValidateRunnerMessage:
    """Tests for message validation."""

    def test_validate_register_requires_runner_id(self):
        """Register message requires runner_id."""
        data = {
            "type": "register",
            "name": "My Runner",
            "runner_type": "claude-code",
            "labels": {}
        }

        errors = validate_runner_message(data)
        assert "runner_id" in str(errors)

    def test_validate_register_requires_runner_type(self):
        """Register message requires runner_type."""
        data = {
            "type": "register",
            "runner_id": "runner-1",
            "name": "My Runner",
            "labels": {}
        }

        errors = validate_runner_message(data)
        assert "runner_type" in str(errors)

    def test_validate_ack_requires_step_id(self):
        """ACK message requires step_id."""
        data = {"type": "ack"}

        errors = validate_runner_message(data)
        assert "step_id" in str(errors)

    def test_validate_log_requires_step_id(self):
        """Log message requires step_id."""
        data = {"type": "log", "lines": ["test"]}

        errors = validate_runner_message(data)
        assert "step_id" in str(errors)

    def test_validate_log_requires_lines(self):
        """Log message requires lines."""
        data = {"type": "log", "step_id": "step-123"}

        errors = validate_runner_message(data)
        assert "lines" in str(errors)

    def test_validate_step_complete_requires_step_id(self):
        """Step complete message requires step_id."""
        data = {"type": "step_complete", "exit_code": 0}

        errors = validate_runner_message(data)
        assert "step_id" in str(errors)

    def test_validate_step_complete_requires_exit_code(self):
        """Step complete message requires exit_code."""
        data = {"type": "step_complete", "step_id": "step-123"}

        errors = validate_runner_message(data)
        assert "exit_code" in str(errors)

    def test_validate_valid_message_returns_empty(self):
        """Valid message returns no errors."""
        data = {
            "type": "register",
            "runner_id": "runner-1",
            "name": "My Runner",
            "runner_type": "claude-code",
            "labels": {}
        }

        errors = validate_runner_message(data)
        assert len(errors) == 0


class TestCreateBackendMessage:
    """Tests for creating backend->runner messages."""

    def test_create_registered_message(self):
        """Creates registered message correctly."""
        msg = create_backend_message("registered", runner_id="runner-123")

        assert isinstance(msg, RegisteredMessage)
        assert msg.runner_id == "runner-123"

    def test_create_execute_step_message(self):
        """Creates execute_step message correctly."""
        config = {"command": "echo hello"}
        msg = create_backend_message(
            "execute_step",
            step_id="step-123",
            execution_key="run-1:0:1",
            config=config
        )

        assert isinstance(msg, ExecuteStepMessage)
        assert msg.step_id == "step-123"
        assert msg.config == config

    def test_create_pong_message(self):
        """Creates pong message correctly."""
        msg = create_backend_message("pong")

        assert isinstance(msg, PongMessage)
        assert msg.type == "pong"

    def test_create_error_message(self):
        """Creates error message correctly."""
        msg = create_backend_message("error", message="Something failed")

        assert isinstance(msg, ErrorMessage)
        assert msg.message == "Something failed"

    def test_create_unknown_type_raises(self):
        """Creating unknown message type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown message type"):
            create_backend_message("unknown_type")
