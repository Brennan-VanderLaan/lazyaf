"""Debug re-run API - Phase 12.7.

failure_01's `routers/debug.py` is DISCARDED wholesale: its WebSocket command
loop lived inside an `except ImportError` block and was dead code from the
commit that introduced it. This is a rebuild, and it holds three properties
that one could not.

**Auth, honestly described.** The backend has NO authentication system today:
every other endpoint here is open. The terminal socket is bounded by a
15-minute JWT minted through `POST /api/debug/{id}/join-token` - the same
machinery `generate_step_token` uses - and that token bounds the TERMINAL,
not the API. Saying otherwise would be the gate lying about what it protects
(R1). Two consequences follow, and both are deliberate:

- `GET /api/debug/{id}` NEVER returns a token. failure_01 put the session's
  long-lived secret in a response the UI polls; a secret sprayed through
  logs, caches and browser history is not a secret.
- The token is re-mintable, NOT one-time. Single use is incompatible with
  reconnecting a dropped terminal, and a one-shot secret that has to survive
  a copy-paste into a shell is worse than a short-lived one. Revocation is
  free anyway: the WS upgrade re-reads the session row and refuses a terminal
  session whatever the JWT says (contract C14).

**Upgrade-time refusal.** The terminal socket authenticates BEFORE
`accept()`, mirroring `ws_runners.py`, so a refusal is visible in the
handshake rather than one frame later. The tradeoff, stated: a WebSocket
cannot carry an application frame before it is accepted, so the reason
travels in the close REASON.

**Remote steps pause; they do not attach.** A remote step's workspace is a
volume on the runner host that this backend's docker client cannot see, and
adding terminal frames to the runner protocol is a version bump with a new
agent-side auth surface. 12.7 refuses with close code 4403 and a sentence
naming the reason, at every surface - never a silent fallback onto the wrong
volume (contract C16).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.pipeline import Pipeline, PipelineRun, StepRun
from app.models.repo import Repo
from app.schemas.debug import (
    DebugAbortResponse,
    DebugExtendRequest,
    DebugExtendResponse,
    DebugJoinTokenResponse,
    DebugRerunRequest,
    DebugRerunResponse,
    DebugResumeRequest,
    DebugResumeResponse,
    DebugSessionInfo,
)
from app.services.execution import debug_terminal as terminal
from app.services.execution.debug_session_service import (
    DebugSessionError,
    debug_session_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["debug"])

#: The join credential's lifetime. Short on purpose, and re-mintable.
JOIN_TOKEN_TTL_SECONDS = 900
_JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    """Sign with the step-auth secret.

    One secret, not a new one: `settings.step_auth_secret` is already the
    house's "dev constant a real deployment overrides", and inventing a
    second key to configure would be a second thing to get wrong.
    """
    from app.config import get_settings

    return get_settings().step_auth_secret


def mint_join_token(session_id: str, expires_at: datetime | None) -> tuple[str, datetime]:
    """Mint a terminal credential bounded by BOTH the TTL and the pause.

    A token that outlives the pause it was minted for is a capability with no
    subject; `min(now + TTL, session.expires_at)` is what makes it one.
    """
    now = datetime.utcnow()
    exp = now + timedelta(seconds=JOIN_TOKEN_TTL_SECONDS)
    if expires_at is not None and expires_at < exp:
        exp = expires_at
    token = jwt.encode(
        {"debug_session_id": session_id, "iat": now, "exp": exp},
        _jwt_secret(),
        algorithm=_JWT_ALGORITHM,
    )
    return token, exp


def read_join_token(token: str | None) -> str | None:
    """The session id a token proves, or None if it proves nothing."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    session_id = payload.get("debug_session_id")
    return str(session_id) if session_id else None


def _join_command(session_id: str, token: str | None = None) -> str:
    if token:
        return f"lazyaf debug attach {session_id} --token {token}"
    return f"lazyaf debug attach {session_id}"


async def _session_info(
    db: AsyncSession, session, *, include_logs: bool = True
) -> DebugSessionInfo:
    """Project a session row through the SERVICE's one projection (R3).

    The GET response, the WS `debug_session_status` frame and the CLI all
    read the same dict, so the UI and the API cannot drift into two
    vocabularies the way failure_01's fixtures and enum did in one commit.
    """
    logs = ""
    if include_logs and session.current_step_key is not None:
        stmt = select(StepRun.logs).where(
            StepRun.pipeline_run_id == session.pipeline_run_id
        )
        if session.current_step_index is not None:
            stmt = stmt.where(StepRun.step_index == session.current_step_index)
        row = (await db.execute(stmt)).first()
        if row is not None:
            logs = row[0] or ""
    payload = debug_session_service.to_dict(session, logs=logs)
    payload["join_command"] = _join_command(session.id)
    # Commit/branch context, read from the run rather than duplicated on the
    # session row (one source of truth for what the run is executing).
    run = (
        await db.execute(
            select(PipelineRun).where(PipelineRun.id == session.pipeline_run_id)
        )
    ).scalar_one_or_none()
    if run is not None and run.trigger_context:
        import json

        try:
            context = json.loads(run.trigger_context) or {}
        except (json.JSONDecodeError, TypeError):
            context = {}
        payload["commit"] = {
            "sha": context.get("commit_sha") or "",
            "message": "",
            "branch": context.get("branch") or "",
        }
    return DebugSessionInfo(**payload)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@router.post(
    "/api/pipeline-runs/{run_id}/debug-rerun", response_model=DebugRerunResponse
)
async def create_debug_rerun(
    run_id: str, request: DebugRerunRequest, db: AsyncSession = Depends(get_db)
):
    """Re-run a pipeline run with breakpoints.

    The re-run is an ORDINARY `start_pipeline` call, which is why commit
    selection needs no new machinery: `workspace_service.get_or_create`
    already takes `(branch, commit_sha)` off `trigger_context`.

    That `trigger_context` is REBUILT, not copied (contract C10): only
    `branch` and `commit_sha` carry over, so a debug re-run of a `card_work`
    run with `on_pass: merge` can neither merge the branch nor walk the card.
    """
    original = (
        await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))
    ).scalar_one_or_none()
    if original is None:
        raise HTTPException(status_code=404, detail=f"Pipeline run {run_id} not found")
    pipeline = (
        await db.execute(
            select(Pipeline).where(Pipeline.id == original.pipeline_id)
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(
            status_code=404, detail=f"Pipeline {original.pipeline_id} not found"
        )
    repo = (
        await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
    ).scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail=f"Repo {pipeline.repo_id} not found")

    try:
        session, new_run = await debug_session_service.create(
            db,
            original_run=original,
            pipeline=pipeline,
            repo=repo,
            breakpoints=request.breakpoints,
            use_original_commit=request.use_original_commit,
            commit_sha=request.commit_sha,
            branch=request.branch,
            timeout_seconds=request.timeout_seconds,
        )
    except DebugSessionError as exc:
        # An unknown breakpoint key is a 400, never an accepted breakpoint
        # that silently never fires (contract C2).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DebugRerunResponse(
        run_id=new_run.id,
        debug_session_id=session.id,
        join_command=_join_command(session.id),
    )


@router.get("/api/debug", response_model=list[DebugSessionInfo])
async def list_debug_sessions(db: AsyncSession = Depends(get_db)):
    """Every non-terminal debug session, newest first."""
    sessions = await debug_session_service.list_active(db)
    return [await _session_info(db, s, include_logs=False) for s in sessions]


@router.get("/api/debug/{session_id}", response_model=DebugSessionInfo)
async def get_debug_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Full session state. Carries NO token - see the module docstring."""
    session = await debug_session_service.get(db, session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"Debug session {session_id} not found"
        )
    return await _session_info(db, session)


@router.post(
    "/api/debug/{session_id}/join-token", response_model=DebugJoinTokenResponse
)
async def create_join_token(session_id: str, db: AsyncSession = Depends(get_db)):
    """Mint a short-lived terminal credential on demand."""
    session = await debug_session_service.get(db, session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"Debug session {session_id} not found"
        )
    if session.is_terminal():
        raise HTTPException(
            status_code=409,
            detail=(
                f"debug session {session_id} has ended "
                f"({session.end_reason or session.status}); there is nothing "
                "to attach to"
            ),
        )
    token, exp = mint_join_token(session_id, session.expires_at)
    return DebugJoinTokenResponse(
        token=token, expires_at=exp, join_command=_join_command(session_id, token)
    )


@router.post("/api/debug/{session_id}/resume", response_model=DebugResumeResponse)
async def resume_debug_session(
    session_id: str,
    request: DebugResumeRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Release the paused step. The session goes to PENDING, never ENDED."""
    body = request or DebugResumeRequest()
    try:
        session, next_bp = await debug_session_service.resume(
            db, session_id, clear_remaining=body.clear_remaining
        )
    except DebugSessionError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=str(exc)) from exc
    return DebugResumeResponse(status=session.status, next_breakpoint=next_bp)


@router.post("/api/debug/{session_id}/abort", response_model=DebugAbortResponse)
async def abort_debug_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """End the session AND cancel its run."""
    try:
        session = await debug_session_service.abort(db, session_id)
    except DebugSessionError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=str(exc)) from exc
    return DebugAbortResponse(
        status=session.status, end_reason=session.end_reason or ""
    )


@router.post("/api/debug/{session_id}/extend", response_model=DebugExtendResponse)
async def extend_debug_session(
    session_id: str,
    request: DebugExtendRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Move the pause deadline, clamped to `max_timeout_seconds`."""
    body = request or DebugExtendRequest()
    try:
        session, clamped = await debug_session_service.extend(
            db, session_id, body.additional_minutes
        )
    except DebugSessionError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=str(exc)) from exc
    return DebugExtendResponse(expires_at=session.expires_at, clamped=clamped)


def _status_for(exc: DebugSessionError) -> int:
    """404 for "no such session", 409 for "wrong state". Never a bare 400."""
    message = str(exc)
    if "not found" in message:
        return 404
    return 409


# ---------------------------------------------------------------------------
# Terminal WebSocket
# ---------------------------------------------------------------------------


def get_debug_session_factory():
    """The factory each terminal handler opens its OWN short session from.

    A dependency rather than an import so tests can bind the endpoint to
    their own engine, and a FACTORY rather than a session because a terminal
    connection can last hours and must never pin one.
    """
    from app.database import async_session

    return async_session


@router.websocket("/api/debug/{session_id}/terminal")
async def debug_terminal_socket(
    websocket: WebSocket,
    session_id: str,
    mode: str = Query(terminal.CONNECTION_MODE_SIDECAR),
    token: str | None = Query(None),
    session_factory=Depends(get_debug_session_factory),
) -> None:
    """Attach a terminal to the session's sidecar.

    Every refusal below happens BEFORE `accept()` and names its reason.
    `--shell` is refused rather than downgraded (contract C17): a breakpoint
    is a PRE-step gate, so the step container does not exist yet.
    """
    header = websocket.headers.get("authorization") or ""
    presented = token
    if header.lower().startswith("bearer "):
        presented = header[7:].strip() or token

    if read_join_token(presented) != session_id:
        await websocket.close(
            code=terminal.CLOSE_BAD_TOKEN,
            reason="missing or invalid join token (POST /api/debug/{id}/join-token)",
        )
        return

    if mode != terminal.CONNECTION_MODE_SIDECAR:
        await websocket.close(
            code=terminal.CLOSE_NOT_ATTACHABLE, reason=terminal.SHELL_REFUSED_REASON
        )
        return

    db = session_factory()
    try:
        session = await debug_session_service.get(db, session_id)
        if session is None:
            await websocket.close(
                code=terminal.CLOSE_UNKNOWN_SESSION, reason="unknown debug session"
            )
            return
        # Re-read, so revocation is free: a session that ended since the
        # token was minted is refused whatever the JWT says (contract C14).
        attachable, reason = debug_session_service.attachability(session)
        if not attachable:
            await websocket.close(
                code=terminal.CLOSE_NOT_ATTACHABLE, reason=(reason or "")[:120]
            )
            return
        run_id = session.pipeline_run_id
        existing_container = session.sidecar_container_id
    finally:
        await db.close()

    if terminal.debug_terminal_service.has_terminal(session_id):
        await websocket.close(
            code=terminal.CLOSE_DUPLICATE_TERMINAL,
            reason="a terminal is already attached to this debug session",
        )
        return

    await websocket.accept()
    stream = None
    try:
        container_id = await terminal.debug_terminal_service.ensure_sidecar(
            session_id, run_id, existing_container
        )
        stream = await terminal.debug_terminal_service.attach(session_id, container_id)
        db = session_factory()
        try:
            await debug_session_service.mark_connected(
                db, session_id, container_id, terminal.CONNECTION_MODE_SIDECAR
            )
        finally:
            await db.close()
        await websocket.send_text(
            terminal.encode_frame(
                terminal.TYPE_READY,
                mode=terminal.CONNECTION_MODE_SIDECAR,
                container_id=container_id,
            )
        )
        await websocket.send_text(
            terminal.encode_frame(
                terminal.TYPE_NOTICE,
                text=(
                    "/workspace is mounted READ-WRITE: edits here are seen by "
                    "the resumed step. Ctrl-] for @resume/@abort/@status/@help."
                ),
            )
        )
        await _pump_terminal(websocket, stream, session_id, session_factory)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Debug terminal for session %s crashed", session_id[:8])
        try:
            await websocket.close(
                code=terminal.CLOSE_BOUND_EXCEEDED, reason="terminal error"
            )
        except Exception:
            pass
    finally:
        await terminal.debug_terminal_service.detach(session_id)
        db = session_factory()
        try:
            await debug_session_service.mark_disconnected(db, session_id)
        except Exception:
            logger.warning(
                "Could not mark debug session %s disconnected", session_id[:8],
                exc_info=True,
            )
        finally:
            await db.close()


async def _pump_terminal(websocket, stream, session_id: str, session_factory) -> None:
    """Bridge the socket and the container until either side ends.

    Every bound closes the socket with a reason and nothing is silently
    dropped or truncated (contract C15).
    """
    import asyncio

    async def _outbound() -> None:
        while True:
            kind, payload = await stream.queue.get()
            if kind == "eof":
                await websocket.send_text(
                    terminal.encode_frame(
                        terminal.TYPE_CLOSED, reason="the sidecar shell exited"
                    )
                )
                return
            if kind == "overflow":
                await websocket.close(
                    code=terminal.CLOSE_BOUND_EXCEEDED,
                    reason=(
                        f"output backpressure: more than "
                        f"{terminal.MAX_OUTBOUND_QUEUE} pending chunks"
                    ),
                )
                return
            if kind == "error":
                await websocket.send_text(
                    terminal.encode_frame(
                        terminal.TYPE_CLOSED, reason=f"sidecar stream error: {payload}"
                    )
                )
                return
            await websocket.send_text(
                terminal.encode_frame(
                    terminal.TYPE_STDOUT, data=terminal.encode_bytes(payload)
                )
            )

    outbound = asyncio.create_task(_outbound())
    window_start = time.monotonic()
    window_count = 0
    try:
        while True:
            if outbound.done():
                return
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8", "ignore")) > terminal.MAX_FRAME_BYTES:
                await websocket.close(
                    code=terminal.CLOSE_BOUND_EXCEEDED,
                    reason=f"frame exceeds {terminal.MAX_FRAME_BYTES} bytes",
                )
                return
            now = time.monotonic()
            if now - window_start > terminal.RATE_WINDOW_SECONDS:
                window_start, window_count = now, 0
            window_count += 1
            if window_count > terminal.RATE_MAX_FRAMES_PER_WINDOW:
                await websocket.close(
                    code=terminal.CLOSE_BOUND_EXCEEDED,
                    reason=(
                        f"more than {terminal.RATE_MAX_FRAMES_PER_WINDOW} frames "
                        f"in {terminal.RATE_WINDOW_SECONDS}s"
                    ),
                )
                return
            try:
                frame = terminal.decode_frame(raw)
            except terminal.DebugProtocolError as exc:
                await websocket.send_text(
                    terminal.encode_frame(terminal.TYPE_NOTICE, text=str(exc))
                )
                continue
            kind = frame["type"]
            if kind == terminal.TYPE_STDIN:
                await stream.write(terminal.decode_bytes(frame["data"]))
            elif kind == terminal.TYPE_PING:
                await websocket.send_text(terminal.encode_frame(terminal.TYPE_PONG))
            elif kind == terminal.TYPE_RESIZE:
                # Best effort: the exec TTY is resized through the docker API
                # by the terminal service in a later phase; the frame is
                # accepted now so the CLI protocol does not change later.
                pass
            elif kind == terminal.TYPE_COMMAND:
                done = await _handle_command(
                    websocket, session_id, frame["command"], session_factory
                )
                if done:
                    return
    finally:
        outbound.cancel()


async def _handle_command(
    websocket, session_id: str, command: str, session_factory
) -> bool:
    """Run one `@`-verb. Returns True when the socket should close.

    Commands are their OWN frame type, never sniffed out of stdin: scanning
    the byte stream for a leading `@` corrupts any program that legitimately
    reads `@...`. Every verb here is also a plain HTTP subcommand, so
    controlling a session never depends on having a TTY.
    """
    db = session_factory()
    try:
        if command == "@help":
            await websocket.send_text(
                terminal.encode_frame(
                    terminal.TYPE_NOTICE,
                    text="@resume continue · @abort cancel the run · @status · @help",
                )
            )
            return False
        if command == "@status":
            session = await debug_session_service.get(db, session_id)
            text = (
                "session is gone"
                if session is None
                else (
                    f"{session.status} at {session.current_step_key} "
                    f"(expires {session.expires_at})"
                )
            )
            await websocket.send_text(
                terminal.encode_frame(terminal.TYPE_NOTICE, text=text)
            )
            return False
        if command == "@resume":
            await debug_session_service.resume(db, session_id)
            await websocket.send_text(
                terminal.encode_frame(terminal.TYPE_CLOSED, reason="resumed")
            )
            await websocket.close(code=terminal.CLOSE_NORMAL, reason="resumed")
            return True
        if command == "@abort":
            await debug_session_service.abort(db, session_id)
            await websocket.send_text(
                terminal.encode_frame(terminal.TYPE_CLOSED, reason="aborted")
            )
            await websocket.close(code=terminal.CLOSE_NORMAL, reason="aborted")
            return True
    except DebugSessionError as exc:
        await websocket.send_text(
            terminal.encode_frame(terminal.TYPE_NOTICE, text=str(exc))
        )
        return False
    finally:
        await db.close()
    return False


__all__ = ["router", "mint_join_token", "read_join_token"]
