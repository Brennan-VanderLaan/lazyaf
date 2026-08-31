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
    PlaygroundCapabilities,
    PlaygroundTestRequest,
    PlaygroundTestResponse,
    PlaygroundStatus,
    PlaygroundResult,
    PlaygroundSessionSummary,
    attachment_refusal,
    playground_capabilities,
)
from app.services.playground_service import PlaygroundCancelError, playground_service
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

    # Attachments, BEFORE anything that costs money.
    #
    # The UI already greys the attach control and names the reason, but the
    # button is not the contract - this is, exactly as it is for the blank
    # prompt above. A refusal here is a 422 with the whole story in it, never
    # a 200 that quietly drops the files: a run that SUCCEEDS having lost half
    # its input is the worst failure shape on this page, because nothing looks
    # wrong. See the Attachments section of schemas/playground.py.
    refusal = attachment_refusal(request.attachments)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)

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

    # Start the test as an ad-hoc agent run (12.5): an ephemeral hidden
    # Pipeline + a real PipelineRun with one agent step. Nothing is enqueued
    # for a polling runner any more.
    session_id = await playground_service.start_test(
        db,
        repo,
        branch=request.branch,
        runner_type=request.runner_type,
        model=request.model,
        task_override=task_description,
        save_branch=request.save_to_branch,
        prompt_template=prompt_template,
        agent_file_ids=agent_file_ids,
    )

    session = playground_service.get_session(session_id)
    status = session.status if session else "queued"
    return PlaygroundTestResponse(
        session_id=session_id,
        status=status,
        message=(
            "Test failed to start"
            if status == "failed"
            else "Test running in an ephemeral agent container"
        ),
    )


@router.get("/sessions", response_model=list[PlaygroundSessionSummary])
async def list_sessions(
    repo_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Recent playground runs for a repo, newest first.

    This is the history the playground never had, and it is a READ of records
    the platform already writes - not a new store. Every playground run leaves
    a PipelineRun (``trigger_type='playground'``, ``trigger_ref=<session_id>``)
    whose StepRun holds the transcript and whose hidden ad-hoc Pipeline holds
    the prompt. See the block comment on
    ``PlaygroundService.list_runs`` for why there is no playground table.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=422, detail="limit must be between 1 and 100"
        )

    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    rows = await playground_service.list_runs(db, repo_id, limit=limit)
    return [PlaygroundSessionSummary(**row) for row in rows]


# Session endpoints


@session_router.get("/capabilities", response_model=PlaygroundCapabilities)
async def get_capabilities():
    """What the playground itself can carry, and the caps it enforces.

    Declared BEFORE the `/{session_id}/...` routes so a literal path can never
    be shadowed by the parameterised ones (it would not be, at two segments
    against one, but ordering is cheaper than remembering why).

    This exists so the UI RENDERS the limits instead of re-spelling them. A
    "max 5 MiB" written into a Svelte template beside a `5 * 1024 * 1024` in a
    validator is two sources of truth for one contract (R3), and the half that
    drifts is always the sentence a human reads. It also carries the reason
    each modality is or is not attachable, so a greyed control on that page is
    never greyed for a reason nobody wrote down.

    Static, cheap and unauthenticated-by-the-same-rules-as-the-rest: it names
    no session, reads no database and reveals nothing but this build's own
    limits.
    """
    return playground_capabilities()


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
async def get_status(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get current status of a playground session.

    Falls back to the durable run when the in-memory session has been swept
    (30-minute TTL, or a backend restart). A page reload lands here first: it
    is how the client decides whether to re-open the SSE stream or just show
    the finished transcript.
    """
    session = playground_service.get_session(session_id)
    if session:
        return PlaygroundStatus(
            session_id=session.id,
            status=session.status,
            started_at=session.started_at,
            completed_at=session.completed_at,
            source="session",
        )

    from_run = await playground_service.get_status_from_run(db, session_id)
    if from_run is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return PlaygroundStatus(**from_run)


@session_router.post("/{session_id}/cancel")
async def cancel_test(session_id: str, db: AsyncSession = Depends(get_db)):
    """Cancel a running test.

    12.5: cancelling the session also cancels the ad-hoc run behind it, which
    kills the agent container and reclaims its workspace volume - the db
    session is threaded through for that.

    A run that could not be cancelled is a 503, not a 200: the container is
    what costs money, and answering "cancelled" while the agent keeps working
    hides the one thing the user asked to stop. The session is left running
    so the call can be retried.
    """
    try:
        success = await playground_service.cancel_test(session_id, db)
    except PlaygroundCancelError as e:
        logger.error("Playground cancel failed for session %s: %s", session_id, e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel session")

    return {"status": "cancelled", "session_id": session_id}


@session_router.get("/{session_id}/result", response_model=PlaygroundResult)
async def get_result(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get transcript, diff and completion status.

    This used to 404 the moment the 30-minute in-memory session was swept -
    about data that was still sitting in the database. It now falls back to
    the durable run record, which carries the whole transcript.

    What it CANNOT carry is the diff: the ``playground/<id>`` branch is
    deleted once the diff has been computed, so a result read from the run
    says ``source="run"`` and the client renders "the diff was not retained"
    rather than the indistinguishable "no changes were made" (R1).
    """
    result = playground_service.get_result(session_id)
    if result:
        result["source"] = "session"
        return PlaygroundResult(**result)

    from_run = await playground_service.get_result_from_run(db, session_id)
    if from_run is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return PlaygroundResult(**from_run)


# Internal endpoints for runners.
#
# LEGACY-ONLY since 12.5: the default playground path is an ad-hoc agent run,
# whose logs arrive via POST /api/steps/{id}/logs and whose diff is computed
# server-side from the internal git server. These routes stay for the
# `executor: legacy` escape hatch (R2) and are named in the 12.6 deletion
# list; nothing on the default path calls them.


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
