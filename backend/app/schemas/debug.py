"""Pydantic schemas for the debug re-run API - Phase 12.7.

Adapted from failure_01's `schemas/debug.py`. Four contract changes, each
with a reason the salvage audit or the wiring design named:

- **`breakpoints` are step KEYS (strings), not indices.** A graph (v2) step
  has a stable `step_id` and no meaningful index; a legacy (v1) step has an
  index and no id. One resolver (`debug_state.debug_step_key`) covers both,
  and the create endpoint 400s on a key the pipeline does not define rather
  than accepting a breakpoint that would silently never fire.
- **`DebugSessionInfo` carries NO token.** failure_01 returned the session's
  long-lived secret from a GET the UI polls - an oracle that sprays it
  through logs, caches and browser history. The join credential is minted on
  demand by `POST /api/debug/{id}/join-token` and is short-lived.
- **`attach_available` + `attach_unavailable_reason`.** 12.7 ships LOCAL
  terminal attach only. A remote-step pause is still a real pause (resume,
  abort, extend, logs all work), and the refusal states its reason at every
  surface rather than silently degrading (R1).
- **`breakpoints_hit` / `breakpoints_pending` / `end_reason`.** A breakpoint
  that never fired because its step's upstream failed is a visible fact.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DebugRerunRequest(BaseModel):
    """Request to create a debug re-run from an existing pipeline run."""

    breakpoints: list[str] = Field(
        default_factory=list,
        description=(
            "Step keys to pause BEFORE. A graph step's key is its step_id; a "
            "legacy step's key is its stringified index. Unknown keys are "
            "rejected with 400."
        ),
    )
    use_original_commit: bool = Field(
        default=True,
        description="Re-run at the same commit/branch as the original run",
    )
    commit_sha: Optional[str] = Field(
        default=None,
        description="Specific commit SHA (used when use_original_commit=False)",
    )
    branch: Optional[str] = Field(
        default=None,
        description="Branch name (used when use_original_commit=False)",
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        ge=60,
        description=(
            "How long a breakpoint pause may wait. Clamped to the session's "
            "max_timeout_seconds (4h)."
        ),
    )


class DebugRerunResponse(BaseModel):
    """Response after creating a debug re-run.

    Deliberately WITHOUT a token: see the module docstring.
    """

    run_id: str = Field(description="ID of the new (debug) pipeline run")
    debug_session_id: str = Field(description="ID of the debug session")
    join_command: str = Field(description="CLI command that attaches to the session")


class DebugStepInfo(BaseModel):
    """The step a session is paused before."""

    key: str = Field(description="Breakpoint key (step_id, or stringified index)")
    name: str = Field(description="Step name")
    index: int = Field(description="Step index (0-based)")
    type: str = Field(default="", description="Step type (script, docker, agent)")


class DebugCommitInfo(BaseModel):
    """The commit the debug re-run is executing."""

    sha: str = Field(default="", description="Commit SHA, empty when the run tracks a branch head")
    message: str = Field(default="", description="Commit message, when known")
    branch: str = Field(default="", description="Branch the re-run was started on")


class DebugRuntimeInfo(BaseModel):
    """Where the paused step would run."""

    host: str = Field(default="local", description="'local' or the runner id")
    orchestrator: str = Field(default="docker", description="Orchestrator type")
    image: str = Field(default="", description="Container image, when known")
    image_sha: Optional[str] = Field(default=None, description="Resolved image ID")


class DebugSessionInfo(BaseModel):
    """Full debug session state for the UI and the CLI."""

    id: str
    pipeline_run_id: str
    original_run_id: Optional[str] = None
    status: str = Field(
        description="pending | waiting_at_bp | connected | timeout | ended"
    )
    current_step: Optional[DebugStepInfo] = None
    commit: DebugCommitInfo = Field(default_factory=DebugCommitInfo)
    runtime: DebugRuntimeInfo = Field(default_factory=DebugRuntimeInfo)
    logs: str = Field(default="", description="Paused step's logs so far")
    join_command: str = Field(description="CLI command that attaches to the session")
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    breakpoints: list[str] = Field(default_factory=list)
    breakpoints_hit: list[str] = Field(default_factory=list)
    breakpoints_pending: list[str] = Field(default_factory=list)
    attach_available: bool = Field(
        default=False,
        description="True only while a LOCAL step is held at a breakpoint",
    )
    attach_unavailable_reason: Optional[str] = Field(
        default=None,
        description="Why attach is refused. Always set when attach_available is False.",
    )
    connection_mode: Optional[str] = None
    end_reason: Optional[str] = None

    class Config:
        from_attributes = True


class DebugJoinTokenResponse(BaseModel):
    """A freshly minted, short-lived terminal credential."""

    token: str = Field(description="JWT bounding the terminal socket")
    expires_at: datetime = Field(description="Token expiry (<= session expires_at)")
    join_command: str = Field(description="CLI command carrying the token")


class DebugResumeRequest(BaseModel):
    """Request to resume a paused session."""

    clear_remaining: bool = Field(
        default=False,
        description=(
            "False: continue to the next breakpoint. True: drop the remaining "
            "breakpoints and run to completion."
        ),
    )


class DebugResumeResponse(BaseModel):
    """Response after resuming. The session goes to PENDING, never ENDED."""

    status: str = Field(description="New session status (pending)")
    next_breakpoint: Optional[str] = Field(
        default=None, description="Next un-hit breakpoint key, if any"
    )


class DebugExtendRequest(BaseModel):
    """Request to extend the pause deadline."""

    additional_minutes: int = Field(
        default=30, ge=1, le=180, description="Minutes to add (1-180)"
    )


class DebugExtendResponse(BaseModel):
    """Response after extending the deadline."""

    expires_at: datetime = Field(description="New expiration time")
    clamped: bool = Field(
        default=False,
        description="True when the request was clamped to max_timeout_seconds",
    )


class DebugAbortResponse(BaseModel):
    """Response after aborting a session (and cancelling its run)."""

    status: str = Field(description="New session status (ended)")
    end_reason: str = Field(description="Why the session ended")


__all__ = [
    "DebugRerunRequest",
    "DebugRerunResponse",
    "DebugStepInfo",
    "DebugCommitInfo",
    "DebugRuntimeInfo",
    "DebugSessionInfo",
    "DebugJoinTokenResponse",
    "DebugResumeRequest",
    "DebugResumeResponse",
    "DebugExtendRequest",
    "DebugExtendResponse",
    "DebugAbortResponse",
]
