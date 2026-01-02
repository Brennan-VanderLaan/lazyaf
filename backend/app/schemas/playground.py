"""
Playground schemas for ephemeral agent testing.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class PlaygroundTestRequest(BaseModel):
    """Request to start a playground test."""

    agent_id: str | None = None  # Platform agent file ID
    repo_agent_name: str | None = None  # OR repo-defined agent name
    runner_type: str = "claude-code"  # claude-code | gemini
    model: str | None = None  # Specific model (e.g., claude-sonnet-4-20250514, gemini-2.5-pro)
    branch: str  # Branch to test against
    task_override: str | None = None  # Optional task description override
    save_to_branch: str | None = None  # If set, save changes to this branch


class PlaygroundTestResponse(BaseModel):
    """Response from starting a playground test."""

    session_id: str
    status: str  # "queued" | "running"
    message: str


class PlaygroundStatus(BaseModel):
    """Current status of a playground session."""

    session_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PlaygroundResult(BaseModel):
    """Result of a completed playground test."""

    session_id: str
    status: str
    diff: str | None = None  # Git diff output
    files_changed: list[str] = []
    branch_saved: str | None = None  # Branch name if saved
    error: str | None = None
    logs: str = ""
    duration_seconds: float | None = None


class PlaygroundLogEvent(BaseModel):
    """SSE event for log streaming."""

    type: str  # "log" | "tool" | "status" | "complete" | "error" | "ping"
    data: str
    timestamp: datetime
