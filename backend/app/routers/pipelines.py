import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Repo, Pipeline, PipelineRun, StepRun, RunStatus
from app.schemas import (
    PipelineCreate,
    PipelineRead,
    PipelineUpdate,
    PipelineRunRead,
    PipelineRunCreate,
    StepRunRead,
)
from app.services.websocket import manager

router = APIRouter(tags=["pipelines"])


def parse_steps(steps_str: str | None) -> list:
    """Parse steps from JSON string to list."""
    if not steps_str:
        return []
    try:
        return json.loads(steps_str)
    except (json.JSONDecodeError, TypeError):
        return []


def serialize_steps(steps: list | None) -> str:
    """Serialize steps from list to JSON string."""
    if not steps:
        return "[]"
    return json.dumps([s.model_dump() if hasattr(s, 'model_dump') else s for s in steps])


def parse_triggers(triggers_str: str | None) -> list:
    """Parse triggers from JSON string to list."""
    if not triggers_str:
        return []
    try:
        return json.loads(triggers_str)
    except (json.JSONDecodeError, TypeError):
        return []


def serialize_triggers(triggers: list | None) -> str:
    """Serialize triggers from list to JSON string."""
    if not triggers:
        return "[]"
    return json.dumps([t.model_dump() if hasattr(t, 'model_dump') else t for t in triggers])


def pipeline_to_ws_dict(pipeline: Pipeline) -> dict:
    """Convert a Pipeline model to a dict for websocket broadcast."""
    return {
        "id": pipeline.id,
        "repo_id": pipeline.repo_id,
        "name": pipeline.name,
        "description": pipeline.description,
        "steps": parse_steps(pipeline.steps),
        "triggers": parse_triggers(pipeline.triggers),
        "is_template": pipeline.is_template,
        "created_at": pipeline.created_at.isoformat() if pipeline.created_at else None,
        "updated_at": pipeline.updated_at.isoformat() if pipeline.updated_at else None,
    }


def parse_trigger_context(context_str: str | None) -> dict | None:
    """Parse trigger_context from JSON string to dict."""
    if not context_str:
        return None
    try:
        return json.loads(context_str)
    except (json.JSONDecodeError, TypeError):
        return None


def pipeline_run_to_ws_dict(run: PipelineRun) -> dict:
    """Convert a PipelineRun model to a dict for websocket broadcast."""
    return {
        "id": run.id,
        "pipeline_id": run.pipeline_id,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "trigger_context": parse_trigger_context(run.trigger_context),
        "current_step": run.current_step,
        "steps_completed": run.steps_completed,
        "steps_total": run.steps_total,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def step_run_to_ws_dict(step_run: StepRun) -> dict:
    """Convert a StepRun model to a dict for websocket broadcast."""
    return {
        "id": step_run.id,
        "pipeline_run_id": step_run.pipeline_run_id,
        "step_index": step_run.step_index,
        "step_name": step_run.step_name,
        "status": step_run.status,
        "job_id": step_run.job_id,
        "logs": step_run.logs,
        "error": step_run.error,
        "started_at": step_run.started_at.isoformat() if step_run.started_at else None,
        "completed_at": step_run.completed_at.isoformat() if step_run.completed_at else None,
    }


# ============================================================================
# Pipeline CRUD
# ============================================================================

@router.get("/api/pipelines", response_model=list[PipelineRead])
async def list_all_pipelines(
    repo_id: Optional[str] = Query(None, description="Filter by repo ID"),
    db: AsyncSession = Depends(get_db)
):
    """List all pipelines, optionally filtered by repo_id."""
    if repo_id:
        result = await db.execute(select(Pipeline).where(Pipeline.repo_id == repo_id))
    else:
        result = await db.execute(select(Pipeline))
    return result.scalars().all()


@router.get("/api/repos/{repo_id}/pipelines", response_model=list[PipelineRead])
async def list_pipelines_for_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    """List all pipelines for a specific repo."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    result = await db.execute(select(Pipeline).where(Pipeline.repo_id == repo_id))
    return result.scalars().all()


@router.post("/api/repos/{repo_id}/pipelines", response_model=PipelineRead, status_code=201)
async def create_pipeline(repo_id: str, pipeline: PipelineCreate, db: AsyncSession = Depends(get_db)):
    """Create a new pipeline for a repo."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    # Serialize steps, steps_graph, and triggers to JSON
    steps_json = serialize_steps(pipeline.steps)
    triggers_json = serialize_triggers(pipeline.triggers)
    steps_graph_json = pipeline.steps_graph.model_dump_json() if pipeline.steps_graph else None

    db_pipeline = Pipeline(
        repo_id=repo_id,
        name=pipeline.name,
        description=pipeline.description,
        steps=steps_json,
        steps_graph=steps_graph_json,
        triggers=triggers_json,
        is_template=pipeline.is_template,
    )
    db.add(db_pipeline)
    await db.commit()
    await db.refresh(db_pipeline)

    # Broadcast pipeline creation via WebSocket
    await manager.send_pipeline_updated(pipeline_to_ws_dict(db_pipeline))

    return db_pipeline


@router.get("/api/pipelines/{pipeline_id}", response_model=PipelineRead)
async def get_pipeline(pipeline_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific pipeline by ID."""
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return pipeline


@router.patch("/api/pipelines/{pipeline_id}", response_model=PipelineRead)
async def update_pipeline(pipeline_id: str, update: PipelineUpdate, db: AsyncSession = Depends(get_db)):
    """Update a pipeline."""
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key == "steps" and value is not None:
            value = serialize_steps(value)
        elif key == "triggers" and value is not None:
            value = serialize_triggers(value)
        setattr(pipeline, key, value)

    await db.commit()
    await db.refresh(pipeline)

    # Broadcast pipeline update via WebSocket
    await manager.send_pipeline_updated(pipeline_to_ws_dict(pipeline))

    return pipeline


@router.delete("/api/pipelines/{pipeline_id}", status_code=204)
async def delete_pipeline(pipeline_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a pipeline and all its runs."""
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    await db.delete(pipeline)
    await db.commit()

    # Broadcast pipeline deletion via WebSocket
    await manager.send_pipeline_deleted(pipeline_id)


# ============================================================================
# Pipeline Execution
# ============================================================================

@router.post("/api/pipelines/{pipeline_id}/run", response_model=PipelineRunRead)
async def run_pipeline(
    pipeline_id: str,
    request: PipelineRunCreate = None,
    db: AsyncSession = Depends(get_db)
):
    """Start a new run of a pipeline."""
    if request is None:
        request = PipelineRunCreate()

    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    # Get repo to check if it's ready
    result = await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(
            status_code=400,
            detail="Repo must be ingested before running pipelines"
        )

    # Parse steps from either steps_graph (new) or steps (legacy)
    steps = []
    if pipeline.steps_graph:
        try:
            graph = json.loads(pipeline.steps_graph)
            steps = list(graph.get("steps", {}).values())
            entry_points = graph.get("entry_points", [])
            if not entry_points:
                raise HTTPException(status_code=400, detail="Pipeline must have at least one entry point")
        except (json.JSONDecodeError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid steps_graph: {e}")
    else:
        steps = parse_steps(pipeline.steps)

    if not steps:
        raise HTTPException(status_code=400, detail="Pipeline has no steps defined")

    # Import executor here to avoid circular imports
    from app.services.pipeline_executor import pipeline_executor

    # Start the pipeline run
    pipeline_run = await pipeline_executor.start_pipeline(
        db=db,
        pipeline=pipeline,
        repo=repo,
        trigger_type=request.trigger_type,
        trigger_ref=request.trigger_ref,
        trigger_context=request.trigger_context,
        params=request.params,
    )

    # Re-fetch with eager loading to avoid lazy-load issues during serialization
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.id == pipeline_run.id)
        .options(selectinload(PipelineRun.step_runs))
    )
    pipeline_run = result.scalar_one()

    return pipeline_run


@router.get("/api/pipelines/{pipeline_id}/runs", response_model=list[PipelineRunRead])
async def list_pipeline_runs(
    pipeline_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    """List runs for a specific pipeline."""
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Pipeline not found")

    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.pipeline_id == pipeline_id)
        .options(selectinload(PipelineRun.step_runs))
        .order_by(PipelineRun.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/api/pipeline-runs", response_model=list[PipelineRunRead])
async def list_all_pipeline_runs(
    pipeline_id: Optional[str] = Query(None, description="Filter by pipeline ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    """List all pipeline runs with optional filters."""
    query = select(PipelineRun).options(selectinload(PipelineRun.step_runs))

    if pipeline_id:
        query = query.where(PipelineRun.pipeline_id == pipeline_id)
    if status:
        query = query.where(PipelineRun.status == status)

    query = query.order_by(PipelineRun.created_at.desc()).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/api/pipeline-runs/{run_id}", response_model=PipelineRunRead)
async def get_pipeline_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific pipeline run with its step runs."""
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.id == run_id)
        .options(selectinload(PipelineRun.step_runs))
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return run


@router.post("/api/pipeline-runs/{run_id}/cancel", response_model=PipelineRunRead)
async def cancel_pipeline_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Cancel a running pipeline."""
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.id == run_id)
        .options(selectinload(PipelineRun.step_runs))
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    if run.status not in (RunStatus.PENDING.value, RunStatus.RUNNING.value):
        raise HTTPException(status_code=400, detail="Pipeline run cannot be cancelled")

    # Import executor here to avoid circular imports
    from app.services.pipeline_executor import pipeline_executor

    run = await pipeline_executor.cancel_run(db, run)

    # Re-fetch with eager loading to avoid lazy-load issues during serialization
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.id == run.id)
        .options(selectinload(PipelineRun.step_runs))
    )
    run = result.scalar_one()

    return run


@router.get("/api/step-runs/{step_run_id}", response_model=StepRunRead)
async def get_step_run(step_run_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific step run by ID."""
    result = await db.execute(
        select(StepRun).where(StepRun.id == step_run_id)
    )
    step_run = result.scalar_one_or_none()
    if not step_run:
        raise HTTPException(status_code=404, detail="Step run not found")
    return step_run


@router.get("/api/pipeline-runs/{run_id}/steps/{step_index}/logs")
async def get_step_logs(run_id: str, step_index: int, db: AsyncSession = Depends(get_db)):
    """Get logs for a specific step in a pipeline run."""
    result = await db.execute(
        select(StepRun)
        .where(StepRun.pipeline_run_id == run_id)
        .where(StepRun.step_index == step_index)
    )
    step_run = result.scalar_one_or_none()
    if not step_run:
        raise HTTPException(status_code=404, detail="Step run not found")

    # If step has a job, get logs from the job
    if step_run.job_id:
        from app.models import Job
        result = await db.execute(select(Job).where(Job.id == step_run.job_id))
        job = result.scalar_one_or_none()
        if job:
            return {
                "step_index": step_index,
                "step_name": step_run.step_name,
                "logs": job.logs or step_run.logs,
                "error": job.error or step_run.error,
                "status": step_run.status,
            }

    return {
        "step_index": step_index,
        "step_name": step_run.step_name,
        "logs": step_run.logs,
        "error": step_run.error,
        "status": step_run.status,
    }
