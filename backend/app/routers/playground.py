"""
Playground API endpoints for ephemeral agent testing.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.models import Repo, AgentFile
from app.schemas.playground import (
    PlaygroundTestRequest,
    PlaygroundTestResponse,
    PlaygroundStatus,
    PlaygroundResult,
)
from app.services.playground_service import playground_service
from app.services.agent_resolver import agent_resolver

logger = logging.getLogger(__name__)

# Router for repo-scoped endpoints
router = APIRouter(prefix="/api/repos/{repo_id}/playground", tags=["playground"])

# Router for session endpoints (no repo_id prefix)
session_router = APIRouter(prefix="/api/playground", tags=["playground"])


@router.post("/test", response_model=PlaygroundTestResponse)
async def start_test(
    repo_id: str,
    request: PlaygroundTestRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Start a playground test.

    Returns session_id for SSE streaming.
    """
    # Validate repo
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repository not ingested")

    # Resolve agent configuration
    prompt_template = None
    agent_file_ids = []

    if request.agent_id:
        # Platform agent by ID
        result = await db.execute(
            select(AgentFile).where(AgentFile.id == request.agent_id)
        )
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent file not found")
        agent_file_ids = [agent.id]
        prompt_template = agent.content
    elif request.repo_agent_name:
        # Repo-defined agent by name
        agent_data = await agent_resolver.resolve_agent(
            db, repo_id, request.branch, request.repo_agent_name
        )
        if not agent_data:
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{request.repo_agent_name}' not found",
            )
        prompt_template = agent_data.get("prompt_template")

    # Apply task override
    if request.task_override:
        if prompt_template and "{{description}}" in prompt_template:
            # Replace the placeholder with task override
            prompt_template = prompt_template.replace(
                "{{description}}", request.task_override
            )
        elif not prompt_template:
            # Use task override as the full description (no template)
            prompt_template = None  # Let the runner use default prompt with task_override

    # Build the task description for the job
    task_description = request.task_override or "Test agent behavior on this branch"

    # Start the test
    session_id = await playground_service.start_test(
        repo_id=repo.id,
        branch=request.branch,
        runner_type=request.runner_type,
        model=request.model,
        task_override=task_description,
        save_branch=request.save_to_branch,
        prompt_template=prompt_template,
        agent_file_ids=agent_file_ids,
    )

    return PlaygroundTestResponse(
        session_id=session_id,
        status="queued",
        message="Test queued, waiting for runner",
    )


# Session endpoints


@session_router.get("/{session_id}/stream")
async def stream_logs(session_id: str):
    """
    SSE endpoint streaming runner logs.

    Event types:
    - log: Log line from runner
    - status: Status change (running, completed, etc.)
    - complete: Session completed
    - error: Error occurred
    - ping: Keepalive
    """
    session = playground_service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        async for event in playground_service.stream_logs(session_id):
            yield {
                "event": event["type"],
                "data": json.dumps(event),
            }

    return EventSourceResponse(event_generator())


@session_router.get("/{session_id}/status", response_model=PlaygroundStatus)
async def get_status(session_id: str):
    """Get current status of a playground session."""
    session = playground_service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return PlaygroundStatus(
        session_id=session.id,
        status=session.status,
        started_at=session.started_at,
        completed_at=session.completed_at,
    )


@session_router.post("/{session_id}/cancel")
async def cancel_test(session_id: str):
    """Cancel a running test."""
    success = await playground_service.cancel_test(session_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel session")

    return {"status": "cancelled", "session_id": session_id}


@session_router.get("/{session_id}/result", response_model=PlaygroundResult)
async def get_result(session_id: str):
    """Get diff and completion status."""
    result = playground_service.get_result(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")

    return PlaygroundResult(**result)


# Internal endpoints for runners


class InternalStatusUpdate(BaseModel):
    status: str
    error: str | None = None


class InternalResultUpdate(BaseModel):
    status: str
    diff: str | None = None
    files_changed: list[str] = []
    branch_saved: str | None = None
    error: str | None = None


class InternalLogUpdate(BaseModel):
    lines: list[str]


@session_router.post("/{session_id}/internal/status")
async def internal_update_status(session_id: str, data: InternalStatusUpdate):
    """Internal endpoint for runners to update session status."""
    await playground_service.update_status(session_id, data.status, data.error)
    return {"ok": True}


@session_router.post("/{session_id}/internal/result")
async def internal_set_result(session_id: str, data: InternalResultUpdate):
    """Internal endpoint for runners to report results."""
    await playground_service.update_status(session_id, data.status, data.error)
    await playground_service.set_result(
        session_id,
        diff=data.diff,
        files_changed=data.files_changed,
        branch_saved=data.branch_saved,
    )
    return {"ok": True}


@session_router.post("/{session_id}/internal/log")
async def internal_append_log(session_id: str, data: InternalLogUpdate):
    """Internal endpoint for runners to append logs."""
    await playground_service.append_logs(session_id, data.lines)
    return {"ok": True}


@session_router.post("/{session_id}/internal/runner")
async def internal_set_runner(session_id: str, runner_id: str):
    """Internal endpoint for runners to register themselves with a session."""
    await playground_service.set_runner(session_id, runner_id)
    return {"ok": True}
