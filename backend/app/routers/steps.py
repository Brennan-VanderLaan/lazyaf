"""
Step API Endpoints - Phase 12.3

Endpoints for container-to-backend communication during step execution:
- POST /api/steps/{step_id}/status - Update step status
- POST /api/steps/{step_id}/logs - Append logs
- POST /api/steps/{step_id}/heartbeat - Extend timeout
"""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import StepExecution, StepRun, StepExecutionStatus
from app.services.control_layer.auth import validate_step_token


router = APIRouter(prefix="/api/steps", tags=["steps"])


# -----------------------------------------------------------------------------
# Request/Response Models
# -----------------------------------------------------------------------------

class StatusUpdateRequest(BaseModel):
    """Request to update step status."""
    status: str
    exit_code: Optional[int] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None


class StatusUpdateResponse(BaseModel):
    """Response from status update."""
    status: str
    exit_code: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class LogLine(BaseModel):
    """A single log line."""
    content: str
    stream: str = "stdout"
    timestamp: Optional[str] = None


class LogsRequest(BaseModel):
    """Request to append logs."""
    content: Optional[str] = None
    stream: str = "stdout"
    timestamp: Optional[str] = None
    lines: Optional[List[LogLine]] = None


class LogsResponse(BaseModel):
    """Response from logs append."""
    lines_appended: int


class HeartbeatRequest(BaseModel):
    """Request to send heartbeat."""
    extend_seconds: Optional[int] = None
    progress: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None


class HeartbeatResponse(BaseModel):
    """Response from heartbeat."""
    timeout_extended: bool
    last_seen: str
    progress_updated: bool = False


class StepExecutionResponse(BaseModel):
    """Response with step execution details."""
    id: str
    status: str
    exit_code: Optional[int] = None
    error: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


# -----------------------------------------------------------------------------
# Auth Dependency
# -----------------------------------------------------------------------------

async def verify_step_auth(
    step_id: str,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> StepExecution:
    """
    Verify auth token and return step execution.

    Raises:
        HTTPException 401: Missing auth header
        HTTPException 403: Invalid token
        HTTPException 404: Step not found
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    # Parse Bearer token
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization format")

    token = authorization[7:]  # Remove "Bearer " prefix

    # Find step execution
    result = await db.execute(
        select(StepExecution).where(StepExecution.id == step_id)
    )
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Step execution not found")

    # Validate token
    if not validate_step_token(token, step_id):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    return execution


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@router.post("/{step_id}/status", response_model=StatusUpdateResponse)
async def update_step_status(
    step_id: str,
    request: StatusUpdateRequest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> StatusUpdateResponse:
    """
    Update step execution status.

    Called by control layer to report step progress.
    """
    execution = await verify_step_auth(step_id, authorization, db)

    now = datetime.utcnow()

    # Update status
    execution.status = request.status

    # Handle status-specific updates
    if request.status == "running" and not execution.started_at:
        execution.started_at = now

    if request.status in ("completed", "failed", "cancelled", "timeout"):
        execution.completed_at = now

    if request.exit_code is not None:
        execution.exit_code = request.exit_code

    if request.error:
        execution.error = request.error

    await db.commit()
    await db.refresh(execution)

    # Update the parent step run status too
    result = await db.execute(
        select(StepRun).where(StepRun.id == execution.step_run_id)
    )
    step_run = result.scalar_one_or_none()
    if step_run:
        step_run.status = request.status
        if request.status == "running" and not step_run.started_at:
            step_run.started_at = now
        if request.status in ("completed", "failed", "cancelled", "timeout"):
            step_run.completed_at = now
        if request.error:
            step_run.error = request.error
        await db.commit()

    return StatusUpdateResponse(
        status=execution.status,
        exit_code=execution.exit_code,
        error=execution.error,
        started_at=execution.started_at.isoformat() if execution.started_at else None,
        completed_at=execution.completed_at.isoformat() if execution.completed_at else None,
    )


@router.post("/{step_id}/logs", response_model=LogsResponse)
async def append_step_logs(
    step_id: str,
    request: LogsRequest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> LogsResponse:
    """
    Append logs to step execution.

    Called by control layer to stream output.
    """
    execution = await verify_step_auth(step_id, authorization, db)

    # Get step run to append logs
    result = await db.execute(
        select(StepRun).where(StepRun.id == execution.step_run_id)
    )
    step_run = result.scalar_one_or_none()

    if not step_run:
        raise HTTPException(status_code=404, detail="Step run not found")

    lines_appended = 0

    # Handle batch logs
    if request.lines:
        for line in request.lines:
            step_run.logs = (step_run.logs or "") + line.content
            lines_appended += 1
    # Handle single log
    elif request.content:
        step_run.logs = (step_run.logs or "") + request.content
        lines_appended = 1

    await db.commit()

    return LogsResponse(lines_appended=lines_appended)


@router.post("/{step_id}/heartbeat", response_model=HeartbeatResponse)
async def send_heartbeat(
    step_id: str,
    request: HeartbeatRequest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    """
    Send heartbeat to extend step timeout.

    Called periodically by control layer during execution.
    """
    execution = await verify_step_auth(step_id, authorization, db)

    now = datetime.utcnow()

    # Update last heartbeat
    execution.last_heartbeat = now

    # Extend timeout if requested
    timeout_extended = False
    if request.extend_seconds:
        execution.timeout_at = now + timedelta(seconds=request.extend_seconds)
        timeout_extended = True

    # Update progress if provided
    progress_updated = False
    if request.progress:
        execution.progress = json.dumps(request.progress)
        progress_updated = True

    await db.commit()

    return HeartbeatResponse(
        timeout_extended=timeout_extended,
        last_seen=now.isoformat(),
        progress_updated=progress_updated,
    )


@router.get("/{step_id}", response_model=StepExecutionResponse)
async def get_step_execution(
    step_id: str,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> StepExecutionResponse:
    """
    Get step execution details.

    Used to check current status.
    """
    execution = await verify_step_auth(step_id, authorization, db)

    progress = None
    if execution.progress:
        try:
            progress = json.loads(execution.progress)
        except json.JSONDecodeError:
            pass

    return StepExecutionResponse(
        id=execution.id,
        status=execution.status,
        exit_code=execution.exit_code,
        error=execution.error,
        progress=progress,
        started_at=execution.started_at.isoformat() if execution.started_at else None,
        completed_at=execution.completed_at.isoformat() if execution.completed_at else None,
    )
