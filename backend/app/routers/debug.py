"""
Debug session API endpoints (Phase 12.7).

Endpoints for creating and managing debug re-run sessions.
"""

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.debug import (
    DebugRerunRequest,
    DebugRerunResponse,
    DebugSessionInfo,
    DebugStepInfo,
    DebugCommitInfo,
    DebugRuntimeInfo,
    DebugResumeResponse,
    DebugExtendResponse,
    DebugAbortResponse,
)
from app.services.execution.debug_session_service import get_debug_session_service


router = APIRouter(tags=["debug"])


@router.post("/api/pipeline-runs/{run_id}/debug-rerun", response_model=DebugRerunResponse)
async def create_debug_rerun(
    run_id: str,
    request: DebugRerunRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Start a debug re-run from a failed/cancelled pipeline.

    Args:
        run_id: ID of the original (failed) pipeline run
        request: Debug re-run configuration

    Returns:
        Debug session info including ID and auth token
    """
    service = get_debug_session_service()

    try:
        session, new_run = await service.create_debug_rerun(
            db=db,
            pipeline_run_id=run_id,
            breakpoints=request.breakpoints,
            use_original_commit=request.use_original_commit,
            commit_sha=request.commit_sha,
            branch=request.branch,
        )
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    return DebugRerunResponse(
        run_id=new_run.id,
        debug_session_id=session.id,
        token=session.token,
    )


@router.get("/api/debug/{session_id}", response_model=DebugSessionInfo)
async def get_debug_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Get debug session info for UI display.

    Args:
        session_id: Debug session ID

    Returns:
        Full session info including current step, logs, join command
    """
    service = get_debug_session_service()

    try:
        info = await service.get_session_info(db, session_id)
    except ValueError as e:
        if "expired" in str(e).lower():
            raise HTTPException(status_code=410, detail="Session expired")
        raise HTTPException(status_code=500, detail=str(e))

    if not info:
        raise HTTPException(status_code=404, detail="Debug session not found")

    # Convert dict to Pydantic model
    return DebugSessionInfo(
        id=info["id"],
        status=info["status"],
        current_step=DebugStepInfo(**info["current_step"]) if info["current_step"] else None,
        commit=DebugCommitInfo(**info["commit"]),
        runtime=DebugRuntimeInfo(**info["runtime"]),
        logs=info["logs"],
        join_command=info["join_command"],
        token=info["token"],
        expires_at=info["expires_at"],
    )


@router.post("/api/debug/{session_id}/resume")
async def resume_debug_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Resume pipeline execution from breakpoint.

    Args:
        session_id: Debug session ID

    Returns:
        Status confirmation
    """
    service = get_debug_session_service()

    try:
        await service.resume(db, session_id)
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "resumed"}


@router.post("/api/debug/{session_id}/abort")
async def abort_debug_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Abort debug session and cancel pipeline.

    Args:
        session_id: Debug session ID

    Returns:
        Status confirmation
    """
    service = get_debug_session_service()

    try:
        await service.abort(db, session_id)
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "aborted"}


@router.post("/api/debug/{session_id}/extend", response_model=DebugExtendResponse)
async def extend_debug_session(
    session_id: str,
    additional_minutes: int = Query(default=30, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
):
    """
    Extend session timeout.

    Args:
        session_id: Debug session ID
        additional_minutes: Minutes to add (1-180)

    Returns:
        New expiration time
    """
    service = get_debug_session_service()

    try:
        new_expiry = await service.extend_timeout(
            db, session_id, additional_minutes * 60
        )
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    return DebugExtendResponse(expires_at=new_expiry)


@router.websocket("/api/debug/{session_id}/terminal")
async def debug_terminal(
    session_id: str,
    websocket: WebSocket,
    mode: str = Query(default="sidecar"),
    token: str = Query(default=None),
):
    """
    WebSocket endpoint for terminal connection.

    Args:
        session_id: Debug session ID
        mode: Connection mode ("sidecar" or "shell")
        token: Auth token for validation

    Protocol:
    1. Client connects with session_id and token
    2. Server validates token
    3. Server spawns sidecar or exec into container
    4. Bidirectional terminal I/O
    5. Handle special commands (@resume, @abort, @status)
    """
    # Import here to avoid circular imports
    from app.database import async_session_maker

    await websocket.accept()

    async with async_session_maker() as db:
        service = get_debug_session_service()

        # Get session
        session = await service.get_session(db, session_id)
        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return

        # Validate token
        if not token or not service.validate_token(session, token):
            await websocket.close(code=4001, reason="Invalid token")
            return

        # Check session state
        if session.status not in {"waiting_at_bp", "connected"}:
            await websocket.close(code=4002, reason=f"Cannot connect: session is {session.status}")
            return

        try:
            # Mark as connected
            await service.on_connect(db, session_id, mode)

            # Import terminal service
            try:
                from app.services.execution.debug_terminal import get_debug_terminal_service
                terminal_service = get_debug_terminal_service()
            except ImportError:
                # Terminal service not yet implemented
                await websocket.send_json({
                    "type": "info",
                    "message": f"Connected to debug session {session_id} in {mode} mode",
                })
                await websocket.send_json({
                    "type": "info",
                    "message": "Terminal service not yet implemented. Use @resume, @abort, or @status.",
                })

                # Simple command loop for now
                while True:
                    data = await websocket.receive_text()

                    if data.startswith("@"):
                        # Handle special commands
                        response = await _handle_special_command(data, session, db)
                        await websocket.send_json({
                            "type": "response",
                            "message": response,
                        })

                        # Check if we should close
                        if data.strip().lower() in {"@resume", "@abort"}:
                            await websocket.close(code=1000, reason=f"Session {data.strip()}")
                            return
                    else:
                        await websocket.send_json({
                            "type": "info",
                            "message": "Terminal not connected. Use @resume, @abort, @status, or @help.",
                        })

        except WebSocketDisconnect:
            # Client disconnected - allow reconnection
            pass
        except Exception as e:
            await websocket.close(code=4000, reason=str(e))


async def _handle_special_command(command: str, session, db) -> str:
    """Handle special @ commands."""
    from app.services.execution.debug_session_service import get_debug_session_service

    cmd = command.strip().lower()
    service = get_debug_session_service()

    if cmd == "@help":
        return """Available commands:
  @resume  - Continue pipeline execution
  @abort   - Cancel debug session and pipeline
  @status  - Show session status
  @help    - Show this help"""

    if cmd == "@status":
        return f"""Debug Session: {session.id}
Status: {session.status}
Step: {session.current_step_name or 'N/A'} (index {session.current_step_index})
Expires: {session.expires_at or 'N/A'}"""

    if cmd == "@resume":
        await service.resume(db, session.id)
        return "Resuming pipeline execution..."

    if cmd == "@abort":
        await service.abort(db, session.id)
        return "Aborting debug session..."

    return f"Unknown command: {command}. Type @help for available commands."
