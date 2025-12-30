from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job, Card
from app.schemas import JobRead
from app.services.runner_pool import runner_pool
from app.services.websocket import manager

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

    return JobLogsResponse(
        logs=job.logs or "",
        job_id=job.id,
        status=job.status,
    )


@router.post("/{job_id}/cancel", response_model=JobRead)
async def cancel_job(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")

    # TODO: Actually cancel the job on the runner
    job.status = "failed"
    job.error = "Cancelled by user"
    await db.commit()
    await db.refresh(job)
    return job


class JobCallback(BaseModel):
    status: str  # "running", "completed", "failed"
    error: str | None = None
    pr_url: str | None = None


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

    # Update the associated card
    result = await db.execute(select(Card).where(Card.id == job.card_id))
    card = result.scalar_one_or_none()
    if card:
        if callback.status == "completed":
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
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
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
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
        })

    # Mark runner as idle
    if callback.status in ("completed", "failed"):
        runner_pool.mark_runner_idle(job_id)

    return {"status": "ok"}
