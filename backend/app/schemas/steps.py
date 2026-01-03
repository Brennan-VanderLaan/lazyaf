"""
Pydantic schemas for Step API endpoints (Phase 12.3).

These schemas define the request/response format for control layer communication.
The control layer inside containers uses these endpoints to report status, logs,
and heartbeats back to the backend.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    """Valid step status values reported by control layer."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# -----------------------------------------------------------------------------
# Request Schemas
# -----------------------------------------------------------------------------

class StatusUpdate(BaseModel):
    """Request body for status update from control layer.

    Sent when:
    - Step starts running
    - Step completes successfully (exit_code = 0)
    - Step fails (exit_code != 0 or error)
    """
    status: StepStatus
    exit_code: Optional[int] = Field(
        default=None,
        description="Process exit code (required for completed/failed status)",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if step failed",
    )


class LogsUpdate(BaseModel):
    """Request body for log lines from control layer.

    Control layer batches log lines and sends them periodically
    (typically every 1 second or 100 lines).
    """
    lines: list[str] = Field(
        default_factory=list,
        description="List of log lines to append",
    )


class HeartbeatRequest(BaseModel):
    """Request body for heartbeat from control layer.

    Empty body - presence of the request is sufficient.
    Heartbeats are sent periodically (default: every 10 seconds)
    to prove the step is still alive.
    """
    pass


# -----------------------------------------------------------------------------
# Response Schemas
# -----------------------------------------------------------------------------

class StatusResponse(BaseModel):
    """Response to status update."""
    status: str = "ok"


class LogsResponse(BaseModel):
    """Response to logs update."""
    status: str = "ok"
    total_lines: int = Field(
        default=0,
        description="Total number of log lines for this step after append",
    )


class HeartbeatResponse(BaseModel):
    """Response to heartbeat."""
    status: str = "ok"
