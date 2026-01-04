"""
Runner WebSocket Protocol for Phase 12.6.

Defines the message format for communication between backend and remote runners.

Protocol Messages:
Runner -> Backend:
  - register: Runner registration with labels
  - ack: Acknowledge job assignment
  - heartbeat: Keep-alive signal
  - log: Stream log output
  - step_complete: Report step completion

Backend -> Runner:
  - registered: Confirm registration
  - execute_step: Push job to runner
  - pong: Heartbeat response
  - error: Error notification
"""

from dataclasses import dataclass, field
from typing import Any, Optional, Union


# Timeout constants (seconds)
REGISTRATION_TIMEOUT = 10  # Runner must send register within 10s of connect
ACK_TIMEOUT = 5            # Runner must ACK job within 5s of assignment
HEARTBEAT_INTERVAL = 10    # Runner sends heartbeat every 10s
DEATH_TIMEOUT = 30         # Mark runner dead if no heartbeat for 30s


# ============================================================================
# Runner -> Backend Messages
# ============================================================================

@dataclass
class RegisterMessage:
    """Runner registration message."""
    runner_id: str
    name: str
    runner_type: str
    labels: dict = field(default_factory=dict)
    type: str = field(default="register", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "runner_id": self.runner_id,
            "name": self.name,
            "runner_type": self.runner_type,
            "labels": self.labels,
        }


@dataclass
class AckMessage:
    """Acknowledge job assignment."""
    step_id: str
    type: str = field(default="ack", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "step_id": self.step_id,
        }


@dataclass
class HeartbeatMessage:
    """Keep-alive heartbeat."""
    type: str = field(default="heartbeat", init=False)

    def to_dict(self) -> dict:
        return {"type": self.type}


@dataclass
class LogMessage:
    """Stream log output."""
    step_id: str
    lines: list[str]
    type: str = field(default="log", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "step_id": self.step_id,
            "lines": self.lines,
        }


@dataclass
class StepCompleteMessage:
    """Report step completion."""
    step_id: str
    exit_code: int
    error: Optional[str] = None
    type: str = field(default="step_complete", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "step_id": self.step_id,
            "exit_code": self.exit_code,
            "error": self.error,
        }


# Type alias for all runner messages
RunnerMessage = Union[
    RegisterMessage,
    AckMessage,
    HeartbeatMessage,
    LogMessage,
    StepCompleteMessage,
]


# ============================================================================
# Backend -> Runner Messages
# ============================================================================

@dataclass
class RegisteredMessage:
    """Confirm runner registration."""
    runner_id: str
    type: str = field(default="registered", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "runner_id": self.runner_id,
        }


@dataclass
class ExecuteStepMessage:
    """Push job to runner."""
    step_id: str
    execution_key: str
    config: dict
    type: str = field(default="execute_step", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "step_id": self.step_id,
            "execution_key": self.execution_key,
            "config": self.config,
        }


@dataclass
class PongMessage:
    """Heartbeat response."""
    type: str = field(default="pong", init=False)

    def to_dict(self) -> dict:
        return {"type": self.type}


@dataclass
class ErrorMessage:
    """Error notification."""
    message: str
    type: str = field(default="error", init=False)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "message": self.message,
        }


# Type alias for all backend messages
BackendMessage = Union[
    RegisteredMessage,
    ExecuteStepMessage,
    PongMessage,
    ErrorMessage,
]


# ============================================================================
# Message Parsing
# ============================================================================

def parse_runner_message(data: dict) -> RunnerMessage:
    """
    Parse a message from a runner.

    Args:
        data: JSON data from WebSocket

    Returns:
        Parsed message object

    Raises:
        ValueError: If message type is unknown or missing
    """
    if "type" not in data:
        raise ValueError("Missing message type")

    msg_type = data["type"]

    if msg_type == "register":
        return RegisterMessage(
            runner_id=data["runner_id"],
            name=data.get("name", ""),
            runner_type=data["runner_type"],
            labels=data.get("labels", {}),
        )
    elif msg_type == "ack":
        return AckMessage(step_id=data["step_id"])
    elif msg_type == "heartbeat":
        return HeartbeatMessage()
    elif msg_type == "log":
        return LogMessage(
            step_id=data["step_id"],
            lines=data["lines"],
        )
    elif msg_type == "step_complete":
        return StepCompleteMessage(
            step_id=data["step_id"],
            exit_code=data["exit_code"],
            error=data.get("error"),
        )
    else:
        raise ValueError(f"Unknown message type: {msg_type}")


def validate_runner_message(data: dict) -> list[str]:
    """
    Validate a runner message for required fields.

    Args:
        data: JSON data from WebSocket

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    if "type" not in data:
        errors.append("Missing 'type' field")
        return errors

    msg_type = data["type"]

    if msg_type == "register":
        if "runner_id" not in data:
            errors.append("Missing 'runner_id' field")
        if "runner_type" not in data:
            errors.append("Missing 'runner_type' field")
    elif msg_type == "ack":
        if "step_id" not in data:
            errors.append("Missing 'step_id' field")
    elif msg_type == "heartbeat":
        pass  # No required fields
    elif msg_type == "log":
        if "step_id" not in data:
            errors.append("Missing 'step_id' field")
        if "lines" not in data:
            errors.append("Missing 'lines' field")
    elif msg_type == "step_complete":
        if "step_id" not in data:
            errors.append("Missing 'step_id' field")
        if "exit_code" not in data:
            errors.append("Missing 'exit_code' field")
    else:
        errors.append(f"Unknown message type: {msg_type}")

    return errors


def create_backend_message(msg_type: str, **kwargs) -> BackendMessage:
    """
    Create a backend -> runner message.

    Args:
        msg_type: Message type
        **kwargs: Message-specific arguments

    Returns:
        Message object

    Raises:
        ValueError: If message type is unknown
    """
    if msg_type == "registered":
        return RegisteredMessage(runner_id=kwargs["runner_id"])
    elif msg_type == "execute_step":
        return ExecuteStepMessage(
            step_id=kwargs["step_id"],
            execution_key=kwargs["execution_key"],
            config=kwargs["config"],
        )
    elif msg_type == "pong":
        return PongMessage()
    elif msg_type == "error":
        return ErrorMessage(message=kwargs["message"])
    else:
        raise ValueError(f"Unknown message type: {msg_type}")
