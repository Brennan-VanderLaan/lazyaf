import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job, Card, StepRun
from app.schemas import JobRead
from app.schemas._datetime import utc_isoformat
from app.services import agent_run
from app.services.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class JobLogsResponse(BaseModel):
    logs: str
    job_id: str
    status: str


@router.get("/{job_id}/logs", response_model=JobLogsResponse)
async def get_job_logs(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # 12.5: card work runs as an ad-hoc agent run, whose log lines are
    # written to StepRun.logs by POST /api/steps/{id}/logs. agent_run mirrors
    # them onto Job.logs when the run COMPLETES; while it is still running
    # this is where the card modal's 3-second poll gets live output from.
    # Job.logs wins when it has any, so a completed job never depends on a
    # StepRun row surviving.
    logs = job.logs or ""
    if not logs and job.step_run_id:
        result = await db.execute(
            select(StepRun.logs).where(StepRun.id == job.step_run_id)
        )
        logs = result.scalar_one_or_none() or ""

    return JobLogsResponse(
        logs=logs,
        job_id=job.id,
        status=job.status,
    )


async def _cancel_work_behind_job(db: AsyncSession, job: Job) -> None:
    """Stop the agent run behind a card job, and land the Job row.

    The body of this used to live here; it now lives in
    ``agent_run.cancel_card_work`` because POST /api/cards/{id}/reject needs
    exactly the same thing (QA finding T2: reject unwound the card and left
    the agent running). One implementation, two callers - what is left here
    is the HTTP mapping of its one failure mode.

    Nothing is committed here: the caller owns the transaction, so a failed
    cancel leaves the Job untouched and nobody is told work stopped that did
    not.
    """
    try:
        await agent_run.cancel_card_work(db, job=job, error="Cancelled by user")
    except agent_run.CancelRunFailed as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"could not cancel the agent run {e.run_id[:8]} behind this job "
                f"({e.cause}); the container may still be running"
            ),
        ) from e


@router.post("/{job_id}/cancel", response_model=JobRead)
async def cancel_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Cancel a card job and the agent run behind it.

    Order matters: the RUN is cancelled first. If that fails the endpoint
    503s with the Job untouched, so a retry is possible and nobody is told
    work stopped that did not. Once the run is CANCELLED, the executor's own
    status guard stops the straggler step task from completing the pipeline,
    so ``agent_run.on_run_complete`` never fires - the card cannot walk into
    ``in_review``, the Job cannot be rewritten to ``completed``, and no
    ``card_complete`` trigger runs. The card is landed ``failed`` here
    instead, which is the one status a user can retry from.
    """
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")

    # Kill the work before rewriting the bookkeeping (raises 503 on failure).
    # This also lands the Job row itself - status/error/completed_at - so the
    # cancel and the bookkeeping cannot drift apart between the two callers.
    await _cancel_work_behind_job(db, job)

    # A cancelled card must not sit in in_progress forever: the run is gone,
    # so nothing else will ever land it. `failed` is the status /retry
    # accepts.
    result = await db.execute(select(Card).where(Card.id == job.card_id))
    card = result.scalar_one_or_none()
    if card is not None and card.status == "in_progress":
        card.status = "failed"

    await db.commit()
    await db.refresh(job)

    await manager.send_job_status({
        "id": job.id,
        "card_id": job.card_id,
        "status": job.status,
        "error": job.error,
        "started_at": utc_isoformat(job.started_at),
        "completed_at": utc_isoformat(job.completed_at),
    })
    if card is not None:
        await db.refresh(card)
        from app.routers.cards import card_to_ws_dict

        await manager.send_card_updated(card_to_ws_dict(card))

    return job


class TestResults(BaseModel):
    """Test execution results from runner."""
    tests_run: bool = False
    tests_passed: bool | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    skip_count: int | None = None
    output: str | None = None


class JobCallback(BaseModel):
    status: str  # "running", "completed", "failed"
    error: str | None = None
    pr_url: str | None = None
    test_results: TestResults | None = None


@router.post("/{job_id}/callback")
async def job_callback(job_id: str, callback: JobCallback, db: AsyncSession = Depends(get_db)):
    """Callback endpoint for runners to report job status."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Update job status
    job.status = callback.status
    if callback.error:
        job.error = callback.error
    if callback.status == "running" and not job.started_at:
        job.started_at = datetime.utcnow()
    if callback.status in ("completed", "failed"):
        job.completed_at = datetime.utcnow()

    # Update test results if provided
    if callback.test_results:
        job.tests_run = callback.test_results.tests_run
        job.tests_passed = callback.test_results.tests_passed
        job.test_pass_count = callback.test_results.pass_count
        job.test_fail_count = callback.test_results.fail_count
        job.test_skip_count = callback.test_results.skip_count
        job.test_output = callback.test_results.output

    # Update the associated card
    result = await db.execute(select(Card).where(Card.id == job.card_id))
    card = result.scalar_one_or_none()
    if card:
        if callback.status == "completed":
            # If tests were run and failed, mark card as failed instead of in_review
            if callback.test_results and callback.test_results.tests_run and not callback.test_results.tests_passed:
                card.status = "failed"
            else:
                card.status = "in_review"
            if callback.pr_url:
                card.pr_url = callback.pr_url
        elif callback.status == "failed":
            card.status = "failed"

    await db.commit()
    await db.refresh(job)

    # Broadcast job status update via WebSocket
    await manager.send_job_status({
        "id": job.id,
        "card_id": job.card_id,
        "status": job.status,
        "error": job.error,
        "started_at": utc_isoformat(job.started_at),
        "completed_at": utc_isoformat(job.completed_at),
        "tests_run": job.tests_run,
        "tests_passed": job.tests_passed,
        "test_pass_count": job.test_pass_count,
        "test_fail_count": job.test_fail_count,
        "test_skip_count": job.test_skip_count,
    })

    # Broadcast card update via WebSocket if card was modified
    if card and callback.status in ("completed", "failed"):
        await db.refresh(card)
        await manager.send_card_updated({
            "id": card.id,
            "repo_id": card.repo_id,
            "title": card.title,
            "description": card.description,
            "status": card.status,
            "runner_type": card.runner_type,
            "branch_name": card.branch_name,
            "pr_url": card.pr_url,
            "job_id": card.job_id,
            "completed_runner_type": card.completed_runner_type,
            "created_at": utc_isoformat(card.created_at),
            "updated_at": utc_isoformat(card.updated_at),
        })

        # Check for pipeline triggers on card status change (only for non-pipeline cards)
        # Pipeline step cards shouldn't trigger additional pipelines
        if not job.step_run_id:
            from app.services.trigger_service import trigger_service
            await trigger_service.on_card_status_change(
                db, card, "in_progress", card.status
            )

    # 12.6: nothing to mark idle here any more. A `Job` row is now
    # written only by the ad-hoc agent path (app/services/agent_run.py),
    # which never involved a pooled runner; the runner lifecycle lives
    # entirely on the runner socket and the registry owns it.

    # Notify pipeline executor if this job is part of a pipeline (Phase 9)
    if callback.status in ("completed", "failed") and job.step_run_id:
        from app.services.pipeline_executor import pipeline_executor
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"[GRAPH] Job callback - calling on_step_complete for step_run_id={job.step_run_id}, job_status={callback.status}")
        # Note: runner_id not available in legacy callback, affinity won't work
        await pipeline_executor.on_step_complete(db, job.step_run_id, job, runner_id=None)

    return {"status": "ok"}
