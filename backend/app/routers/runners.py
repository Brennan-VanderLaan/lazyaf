from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Runner, Job, Card
from app.schemas import RunnerRead
from app.services.runner_pool import runner_pool, RunnerPool
from app.services.job_queue import job_queue
from app.services.websocket import manager

router = APIRouter(prefix="/api/runners", tags=["runners"])


# === Models ===

class PoolStatus(BaseModel):
    total_runners: int
    idle_runners: int
    busy_runners: int
    offline_runners: int
    queued_jobs: int
    pending_jobs: int


class RegisterRequest(BaseModel):
    name: str | None = None


class RegisterResponse(BaseModel):
    runner_id: str
    name: str


class JobResponse(BaseModel):
    id: str
    card_id: str
    repo_id: str
    repo_url: str
    repo_path: str | None = None  # Deprecated, not used with internal git
    base_branch: str
    branch_name: str
    card_title: str
    card_description: str
    use_internal_git: bool = False  # When True, runner clones from internal git server


class CompleteRequest(BaseModel):
    success: bool
    error: str | None = None
    pr_url: str | None = None


class LogRequest(BaseModel):
    lines: list[str]


class DockerCommand(BaseModel):
    command: str
    command_with_secrets: str
    image: str
    env_vars: dict[str, str]


# === Endpoints ===

@router.get("", response_model=list[dict])
async def list_runners():
    """Get status of all runners in the pool."""
    return runner_pool.get_runners()


@router.get("/status", response_model=PoolStatus)
async def pool_status():
    """Get overall pool status."""
    return PoolStatus(
        total_runners=runner_pool.runner_count,
        idle_runners=runner_pool.idle_count,
        busy_runners=runner_pool.busy_count,
        offline_runners=runner_pool.offline_count,
        queued_jobs=job_queue.queue_size,
        pending_jobs=job_queue.pending_count,
    )


@router.post("/register", response_model=RegisterResponse)
async def register_runner(request: RegisterRequest):
    """Register a new runner with the pool."""
    runner = runner_pool.register(name=request.name)
    return RegisterResponse(runner_id=runner.id, name=runner.name)


@router.post("/{runner_id}/heartbeat")
async def runner_heartbeat(runner_id: str):
    """Send a heartbeat to keep the runner alive."""
    if not runner_pool.heartbeat(runner_id):
        raise HTTPException(status_code=404, detail="Runner not found")
    return {"status": "ok"}


@router.get("/{runner_id}/job")
async def get_runner_job(runner_id: str):
    """Poll for a job. Returns null if no job available."""
    runner = runner_pool.get_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    # Also acts as heartbeat
    runner_pool.heartbeat(runner_id)

    job = await runner_pool.get_job(runner_id)
    if not job:
        return {"job": None}

    return {
        "job": JobResponse(
            id=job.id,
            card_id=job.card_id,
            repo_id=job.repo_id,
            repo_url=job.repo_url,
            repo_path=job.repo_path,
            base_branch=job.base_branch,
            branch_name=f"lazyaf/{job.id[:8]}",
            card_title=job.card_title,
            card_description=job.card_description,
            use_internal_git=job.use_internal_git,
        )
    }


@router.post("/{runner_id}/complete")
async def complete_job(runner_id: str, request: CompleteRequest, db: AsyncSession = Depends(get_db)):
    """Mark the current job as complete."""
    from datetime import datetime

    runner = runner_pool.get_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    # Get runner logs before completing (they get cleared)
    runner_logs = runner_pool.get_logs(runner_id)

    job_data = runner_pool.complete_job(runner_id, request.success, request.error)
    if not job_data:
        raise HTTPException(status_code=400, detail="No job to complete")

    # Update job in database
    result = await db.execute(select(Job).where(Job.id == job_data.id))
    job = result.scalar_one_or_none()
    card = None
    if job:
        job.status = "completed" if request.success else "failed"
        job.completed_at = datetime.utcnow()
        if request.error:
            job.error = request.error

        # Sync runner logs to job
        if runner_logs:
            job.logs = "\n".join(runner_logs)

        # Update card status
        result = await db.execute(select(Card).where(Card.id == job.card_id))
        card = result.scalar_one_or_none()
        if card:
            if request.success:
                card.status = "in_review"
                if request.pr_url:
                    card.pr_url = request.pr_url
            else:
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

        # Broadcast card update via WebSocket
        if card:
            await db.refresh(card)
            await manager.send_card_updated({
                "id": card.id,
                "repo_id": card.repo_id,
                "title": card.title,
                "description": card.description,
                "status": card.status,
                "branch_name": card.branch_name,
                "pr_url": card.pr_url,
                "job_id": card.job_id,
            })

    return {"status": "ok"}


@router.post("/{runner_id}/logs")
async def append_logs(runner_id: str, request: LogRequest, db: AsyncSession = Depends(get_db)):
    """Append log lines for a runner and sync to job."""
    runner = runner_pool.get_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    for line in request.lines:
        runner_pool.append_log(runner_id, line)

    # Sync logs to Job model if runner has an active job
    if runner.current_job:
        result = await db.execute(select(Job).where(Job.id == runner.current_job.id))
        job = result.scalar_one_or_none()
        if job:
            job.logs = "\n".join(runner_pool.get_logs(runner_id))
            await db.commit()

    return {"status": "ok", "total_lines": len(runner_pool.get_logs(runner_id))}


@router.get("/{runner_id}/logs")
async def get_logs(runner_id: str, offset: int = Query(0)):
    """Get logs for a runner."""
    runner = runner_pool.get_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner not found")

    logs = runner_pool.get_logs(runner_id)
    return {"logs": logs[offset:], "total": len(logs)}


@router.delete("/{runner_id}")
async def unregister_runner(runner_id: str):
    """Unregister a runner."""
    if not runner_pool.unregister(runner_id):
        raise HTTPException(status_code=404, detail="Runner not found")
    return {"status": "ok"}


@router.get("/docker-command", response_model=DockerCommand)
async def get_docker_command(with_secrets: bool = Query(False)):
    """Get the docker run command for starting a runner."""
    settings = get_settings()
    image = RunnerPool.RUNNER_IMAGE

    # Environment variables with placeholders
    env_vars = {
        "BACKEND_URL": "http://host.docker.internal:8000",
        "ANTHROPIC_API_KEY": "<YOUR_ANTHROPIC_API_KEY>",
        "GITHUB_TOKEN": "<YOUR_GITHUB_TOKEN>",
    }

    # Build command with placeholders
    env_flags = " ".join(f'-e {k}="{v}"' for k, v in env_vars.items())
    command = f"docker run --rm {env_flags} {image}"

    # Build command with actual secrets if requested
    secret_env_vars = {
        "BACKEND_URL": "http://host.docker.internal:8000",
        "ANTHROPIC_API_KEY": settings.anthropic_api_key or "<YOUR_ANTHROPIC_API_KEY>",
        "GITHUB_TOKEN": "<YOUR_GITHUB_TOKEN>",
    }
    secret_env_flags = " ".join(f'-e {k}="{v}"' for k, v in secret_env_vars.items())
    command_with_secrets = f"docker run --rm {secret_env_flags} {image}"

    return DockerCommand(
        command=command,
        command_with_secrets=command_with_secrets if with_secrets else command,
        image=image,
        env_vars=env_vars if not with_secrets else secret_env_vars,
    )
