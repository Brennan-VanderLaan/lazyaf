"""
Pydantic schemas for debug session API endpoints.

These schemas define the request/response format for:
- Creating debug re-runs
- Getting session info
- Resume/abort/extend operations
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class DebugRerunRequest(BaseModel):
    """Request to create a debug re-run from a failed pipeline."""
    breakpoints: list[int] = Field(
        default_factory=list,
        description="Step indices to pause before (0-based)",
    )
    use_original_commit: bool = Field(
        default=True,
        description="Use the same commit as the original failed run",
    )
    commit_sha: Optional[str] = Field(
        default=None,
        description="Specific commit SHA (if use_original_commit=False)",
    )
    branch: Optional[str] = Field(
        default=None,
        description="Branch name (if use_original_commit=False)",
    )


class DebugRerunResponse(BaseModel):
    """Response after creating a debug re-run."""
    run_id: str = Field(description="ID of the new pipeline run")
    debug_session_id: str = Field(description="ID of the debug session")
    token: str = Field(description="Auth token for CLI connection")


class DebugStepInfo(BaseModel):
    """Information about the current step at breakpoint."""
    name: str = Field(description="Step name")
    index: int = Field(description="Step index (0-based)")
    type: str = Field(description="Step type (script, docker, agent)")


class DebugCommitInfo(BaseModel):
    """Information about the commit being debugged."""
    sha: str = Field(description="Full commit SHA")
    message: str = Field(description="Commit message")


class DebugRuntimeInfo(BaseModel):
    """Runtime environment information."""
    host: str = Field(description="Host running the step")
    orchestrator: str = Field(description="Orchestrator type (docker, native)")
    image: str = Field(description="Container image being used")
    image_sha: Optional[str] = Field(
        default=None,
        description="Container image SHA",
    )


class DebugSessionInfo(BaseModel):
    """Full debug session information for UI display."""
    id: str = Field(description="Debug session ID")
    status: str = Field(description="Session status (pending, waiting_at_bp, connected, timeout, ended)")
    current_step: Optional[DebugStepInfo] = Field(
        default=None,
        description="Current step info (when at breakpoint)",
    )
    commit: DebugCommitInfo = Field(description="Commit being debugged")
    runtime: DebugRuntimeInfo = Field(description="Runtime environment info")
    logs: str = Field(description="Logs up to current point")
    join_command: str = Field(description="CLI command to connect")
    token: str = Field(description="Auth token for CLI")
    expires_at: Optional[datetime] = Field(
        default=None,
        description="When the session expires",
    )

    class Config:
        from_attributes = True


class DebugResumeResponse(BaseModel):
    """Response after resuming a debug session."""
    status: str = Field(description="New status (resumed)")
    next_breakpoint: Optional[int] = Field(
        default=None,
        description="Next breakpoint index (if any)",
    )


class DebugExtendRequest(BaseModel):
    """Request to extend session timeout."""
    additional_minutes: int = Field(
        default=30,
        ge=1,
        le=180,
        description="Minutes to add (1-180)",
    )


class DebugExtendResponse(BaseModel):
    """Response after extending timeout."""
    expires_at: datetime = Field(description="New expiration time")


class DebugAbortResponse(BaseModel):
    """Response after aborting a debug session."""
    status: str = Field(description="New status (aborted)")
