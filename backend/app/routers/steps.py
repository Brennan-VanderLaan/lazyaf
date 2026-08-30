"""
Step API Endpoints - Phase 12.3

Endpoints for container-to-backend communication during step execution:
- POST /api/steps/{step_id}/status - Update step status
- POST /api/steps/{step_id}/logs - Append logs
- POST /api/steps/{step_id}/heartbeat - Extend timeout
- POST /api/steps/{step_id}/test-results - Ingest a test-results manifest
- POST /api/steps/{step_id}/usage - Ingest resource accounting (Phase 12.5)

Reporting-path ownership (wave2-123-wiring design, R3): in control mode this
router is the SOLE writer of StepRun.logs and the step-log WS frames (one
`step_log_batch` frame per POST), and broadcasts the intermediate `running`
transition as a `step_update` frame. Terminal StepRun state (status/
completed_at/error, continuation, the step_run_status / pipeline_run_status
frames) is owned by the pipeline executor's `_finish_local_step` - the
container exit code observed by the executor is ground truth - so this
router NEVER writes StepRun.status. StepExecution telemetry (status
vocabulary: preparing/running/completed/failed/cancelled/timeout, heartbeat,
timeout_at) lives here.

Zombie-token hardening (12.3 adversarial review): once a StepExecution is
terminal - reported by the runtime OR reconciled by _finish_local_step -
every write endpoint here answers 409. A leaked token from a finished step
can no longer smear logs/telemetry onto a later attempt's rows.

Wire-shape note: LogLine used to carry `stream`/`timestamp` fields that were
accepted and then dropped on the floor (never persisted, never broadcast).
They are DELETED from the schema (decision: drop, not persist); the control
runtime may keep sending them - pydantic ignores unknown keys - but `content`
is the only datum this endpoint transports.
"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import StepExecution, StepRun, StepExecutionStatus, StepUsage
from app.schemas.testref import TestIngestResponse, TestResultsManifest
from app.schemas.usage import StepUsageRead, UsageIngestResponse, UsageManifest
from app.services.control_layer.auth import validate_step_token
from app.services.test_ingestion import ingest_manifest
from app.services.usage_ingestion import ingest_usage
from app.services.websocket import manager


router = APIRouter(prefix="/api/steps", tags=["steps"])

# StepExecution statuses that accept no further writes (409): the runtime
# reported terminal, or _finish_local_step reconciled the row terminal.
TERMINAL_EXECUTION_STATUSES = frozenset({
    StepExecutionStatus.COMPLETED.value,
    StepExecutionStatus.FAILED.value,
    StepExecutionStatus.CANCELLED.value,
    StepExecutionStatus.TIMEOUT.value,
})


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
    """A single log line (content only - see module docstring)."""
    content: str


class LogsRequest(BaseModel):
    """Request to append logs."""
    content: Optional[str] = None
    lines: Optional[List[LogLine]] = None


class LogsResponse(BaseModel):
    """Response from logs append."""
    lines_appended: int


class HeartbeatRequest(BaseModel):
    """Request to send heartbeat."""
    extend_seconds: Optional[int] = None
    timestamp: Optional[str] = None


class HeartbeatResponse(BaseModel):
    """Response from heartbeat."""
    timeout_extended: bool
    last_seen: str


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


def _reject_terminal_writes(execution: StepExecution) -> None:
    """409 any write to a terminal StepExecution (zombie-token hardening)."""
    if execution.status in TERMINAL_EXECUTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"step execution is terminal ({execution.status}); "
                "writes are rejected"
            ),
        )


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
    _reject_terminal_writes(execution)

    now = datetime.utcnow()

    # Update status
    execution.status = request.status

    # Handle status-specific updates
    if request.status == "running" and not execution.started_at:
        execution.started_at = now

    if request.status in TERMINAL_EXECUTION_STATUSES:
        execution.completed_at = now

    if request.exit_code is not None:
        execution.exit_code = request.exit_code

    if request.error:
        execution.error = request.error

    await db.commit()
    await db.refresh(execution)

    # StepRun terminal state is OWNED by the pipeline executor's
    # _finish_local_step (RunStatus vocabulary) - never mirrored here (the
    # old mirror wrote this endpoint's "completed" vocabulary onto StepRun,
    # diverging from RunStatus.PASSED). Only the harmless started_at
    # timestamp is set on `running`, and the `running` transition is
    # broadcast as the step_update frame the frontend already consumes.
    result = await db.execute(
        select(StepRun).where(StepRun.id == execution.step_run_id)
    )
    step_run = result.scalar_one_or_none()
    if step_run and request.status == "running":
        if not step_run.started_at:
            step_run.started_at = now
            await db.commit()
        await manager.publish_step_update(
            step_run.pipeline_run_id, step_run.step_index, "running"
        )

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

    Called by control layer to stream output. Content is appended VERBATIM
    (no newline added) - the control runtime sends lines WITH their trailing
    newline (wire contract with images/base/control/backend_client.py).

    Efficiency (12.3 adversarial review): ONE string join per POST, appended
    with a targeted SQL expression (logs = COALESCE(logs,'') || :chunk) so
    the existing log blob is never read-modify-written, ONE commit, and ONE
    step_log_batch WS frame carrying the whole batch.
    """
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)

    # Address the StepRun without loading its (potentially large) log blob.
    result = await db.execute(
        select(StepRun.pipeline_run_id, StepRun.step_index).where(
            StepRun.id == execution.step_run_id
        )
    )
    step_run_row = result.one_or_none()

    if step_run_row is None:
        raise HTTPException(status_code=404, detail="Step run not found")

    if request.lines:
        contents = [line.content for line in request.lines]
    elif request.content:
        contents = [request.content]
    else:
        contents = []

    lines_appended = len(contents)
    if contents:
        chunk = "".join(contents)
        await db.execute(
            update(StepRun)
            .where(StepRun.id == execution.step_run_id)
            .values(logs=func.coalesce(StepRun.logs, "") + chunk)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

        # Sole writer of the step-log frames in control mode (R3): one
        # step_log_batch frame per POST, lines rstripped of their trailing
        # newline to match what the frontend renders per line.
        await manager.publish_step_log_batch(
            step_run_row.pipeline_run_id,
            step_run_row.step_index,
            [content.rstrip("\n") for content in contents],
        )

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

    Called periodically by control layer during execution. Extensions only
    ever move timeout_at FORWARD: timeout_at = max(current, now +
    extend_seconds) - a late/short heartbeat must never shrink a deadline a
    previous heartbeat already earned.
    """
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)

    now = datetime.utcnow()

    # Update last heartbeat
    execution.last_heartbeat = now

    # Extend timeout if requested (never shrink)
    timeout_extended = False
    if request.extend_seconds:
        candidate = now + timedelta(seconds=request.extend_seconds)
        if execution.timeout_at is None or candidate > execution.timeout_at:
            execution.timeout_at = candidate
            timeout_extended = True

    await db.commit()

    return HeartbeatResponse(
        timeout_extended=timeout_extended,
        last_seen=now.isoformat(),
    )


@router.post("/{step_id}/test-results", response_model=TestIngestResponse)
async def ingest_step_test_results(
    step_id: str,
    request: TestResultsManifest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> TestIngestResponse:
    """
    Ingest a test-results manifest for this step (Phase 12.2.6, contract #3).

    Called by the control runtime after the command exits, when the pytest
    plugin wrote a manifest to LAZYAF_TEST_RESULTS_PATH. Same Bearer step
    token as /logs; terminal StepExecutions answer 409 like every other
    write endpoint (zombie-token hardening). The server derives pipeline
    run / repo / commit / branch from the StepExecution -> StepRun ->
    PipelineRun chain; unknown lazyaf_test_ids auto-create ORPHAN TestRefs.
    Idempotent per (step_run, test_ref): a re-POST updates rather than
    duplicates.
    """
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)

    counts = await ingest_manifest(db, execution, request)
    return TestIngestResponse(
        results_received=counts.results_received,
        test_runs_created=counts.test_runs_created,
        test_runs_updated=counts.test_runs_updated,
        orphan_refs_created=counts.orphan_refs_created,
    )


@router.post("/{step_id}/usage", response_model=UsageIngestResponse)
async def ingest_step_usage(
    step_id: str,
    request: UsageManifest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> UsageIngestResponse:
    """
    Ingest one step's resource accounting (Phase 12.5, contract #3).

    Called by the control runtime after the command exits, from
    LAZYAF_USAGE_PATH. Same Bearer step token as /logs; terminal
    StepExecutions answer 409. Idempotent per step_execution: a re-POST
    updates rather than duplicates. When cost_usd is absent and the step
    ran on a registered GPU node, the SERVER prices it (node rate x
    occupancy) and stamps cost_source="gpu-node".

    Ordering vs /status: the runtime POSTs usage BEFORE its terminal
    /status. A usage POST arriving after terminal is a 409 and is dropped
    by the runtime with a WARN - telemetry never fails a step, and the
    step's exit code is ground truth about the work either way.

    EVERY control-mode step produces a row, script steps included: theirs
    carry cost_source="unknown", which is the recorded fact that nobody
    reported a cost, not a gap. A dropped usage channel must be visible as
    a missing row, and it can only be visible if the rows are complete.
    """
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)

    usage = await ingest_usage(db, execution, request)
    return UsageIngestResponse(
        usage_id=usage.id,
        cost_usd=str(usage.cost_usd) if usage.cost_usd is not None else None,
        cost_source=usage.cost_source,
    )


@router.get("/{step_id}/usage", response_model=StepUsageRead)
async def get_step_usage(
    step_id: str,
    db: AsyncSession = Depends(get_db),
) -> StepUsageRead:
    """
    Read one step's usage row (api-surface 2.7).

    Operator/UI read: unauthenticated like the rest of the operator API.
    The step token gates WRITES from the container, not operator reads of
    what was written.

    404s distinguish "no such execution" from "that execution reported no
    usage" (api-surface 0: an unknown parent id is a 404, and "no rows" and
    "no such thing" are different facts).
    """
    execution = (
        await db.execute(select(StepExecution).where(StepExecution.id == step_id))
    ).scalar_one_or_none()
    if execution is None:
        raise HTTPException(status_code=404, detail="Step execution not found")

    usage = (
        await db.execute(
            select(StepUsage).where(StepUsage.step_execution_id == step_id)
        )
    ).scalar_one_or_none()
    if usage is None:
        raise HTTPException(
            status_code=404,
            detail=f"No usage recorded for step execution {step_id}",
        )

    return StepUsageRead.from_model(usage)
