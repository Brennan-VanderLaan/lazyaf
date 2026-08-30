import json
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
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
from app.schemas.pipeline import ADHOC_TRIGGER_TYPES, DEBUG_TRIGGER_TYPES
from app.services.agent_run import ADHOC_PREFIX
from app.services.websocket import manager

# Usage channel (Phase 12.5) — separate import lines on purpose: the run
# rollup below is the only thing in this module that needs them.
from app.models import StepUsage
from app.schemas.usage import RunUsageRollup

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

def _visible_pipelines(query):
    """Hide the ephemeral pipelines behind ad-hoc agent runs (12.5).

    Starting a card or a playground test creates a one-step Pipeline row so
    the work gets a real PipelineRun (workspace, StepRun, control mode, usage
    - see app/services/agent_run.py). Those rows are plumbing, one per card
    start, and they would bury a repo's real pipelines within a day. Their
    RUNS stay listed and readable: that is the point of using a run at all.

    Filtering is by the writer's own prefix helper, so the hide rule and the
    name rule cannot drift apart.
    """
    return query.where(~Pipeline.name.startswith(ADHOC_PREFIX))


@router.get("/api/pipelines", response_model=list[PipelineRead])
async def list_all_pipelines(
    repo_id: Optional[str] = Query(None, description="Filter by repo ID"),
    db: AsyncSession = Depends(get_db)
):
    """List all pipelines, optionally filtered by repo_id.

    Ad-hoc agent-run pipelines (12.5) are never listed - see
    _visible_pipelines.
    """
    query = select(Pipeline)
    if repo_id:
        query = query.where(Pipeline.repo_id == repo_id)
    result = await db.execute(_visible_pipelines(query))
    return result.scalars().all()


@router.get("/api/repos/{repo_id}/pipelines", response_model=list[PipelineRead])
async def list_pipelines_for_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    """List all pipelines for a specific repo (ad-hoc runs excluded)."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    result = await db.execute(
        _visible_pipelines(select(Pipeline).where(Pipeline.repo_id == repo_id))
    )
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
        elif key == "steps_graph" and value is not None:
            # Serialize steps_graph dict to JSON string
            value = json.dumps(value)
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

    # trigger_type is a ROUTING KEY, not a label (12.5): a run stamped
    # `card_work` makes `agent_run.on_run_complete` write the Card named by
    # trigger_ref - status, Job row, and the card_complete triggers that fire
    # off it. Accepting it here would let any caller drive an arbitrary card
    # to in_review/failed by starting a pipeline. Only the internal ad-hoc
    # path may stamp these; the schema validator rejects everything outside
    # the known vocabulary, and this rejects the ad-hoc subset of it.
    #
    # Checked FIRST, before any lookup or write, so a refused request cannot
    # have touched anything.
    if request.trigger_type in ADHOC_TRIGGER_TYPES + DEBUG_TRIGGER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"trigger_type {request.trigger_type!r} is reserved for "
                "internal ad-hoc agent runs (card work / playground / "
                "experiment) and debug re-runs, and cannot be set on this "
                "endpoint"
            ),
        )

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
        .options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))
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
        .options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))
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
    query = select(PipelineRun).options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))

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
        .options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))
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
        .options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))
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
        .options(selectinload(PipelineRun.step_runs).selectinload(StepRun.executions))
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


# ============================================================================
# Pipeline Export
# ============================================================================

@router.get("/api/pipelines/{pipeline_id}/export/yaml")
async def export_pipeline_yaml(pipeline_id: str, db: AsyncSession = Depends(get_db)):
    """Export a pipeline to YAML format."""
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    # Build the YAML structure
    export_data = {
        "name": pipeline.name,
        "description": pipeline.description,
        "version": 2,
    }

    # Use steps_graph if available, otherwise fall back to legacy steps
    if pipeline.steps_graph:
        graph = json.loads(pipeline.steps_graph)
        steps = graph.get("steps", {})
        edges = graph.get("edges", [])
        entry_points = graph.get("entry_points", [])

        # Convert graph to YAML-friendly format
        export_data["entry_points"] = entry_points
        export_data["steps"] = {}

        for step_id, step in steps.items():
            step_export = {
                "name": step.get("name", step_id),
                "type": step.get("type", "script"),
            }

            # Add config if present
            if step.get("config"):
                step_export["config"] = step["config"]

            # Find outgoing edges for this step
            success_targets = []
            failure_targets = []
            always_targets = []

            for edge in edges:
                if edge.get("from_step") == step_id:
                    target = edge.get("to_step")
                    condition = edge.get("condition", "success")
                    if condition == "success":
                        success_targets.append(target)
                    elif condition == "failure":
                        failure_targets.append(target)
                    elif condition == "always":
                        always_targets.append(target)

            if success_targets:
                step_export["on_success"] = success_targets if len(success_targets) > 1 else success_targets[0]
            if failure_targets:
                step_export["on_failure"] = failure_targets if len(failure_targets) > 1 else failure_targets[0]
            if always_targets:
                step_export["on_always"] = always_targets if len(always_targets) > 1 else always_targets[0]

            export_data["steps"][step_id] = step_export
    else:
        # Legacy format - convert steps array to YAML
        steps = parse_steps(pipeline.steps)
        export_data["steps"] = []
        for step in steps:
            step_export = {
                "name": step.get("name", "Unnamed"),
                "type": step.get("type", "script"),
            }
            if step.get("config"):
                step_export["config"] = step["config"]
            export_data["steps"].append(step_export)

    # Generate YAML with nice formatting
    yaml_content = yaml.dump(export_data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return Response(
        content=yaml_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f"attachment; filename={pipeline.name.replace(' ', '_')}.yaml"}
    )


# -----------------------------------------------------------------------------
# Usage rollup (Phase 12.5, api-surface 2.7) — appended at the bottom of this
# module by design: the ad-hoc list filter above and this read endpoint are
# the two 12.5 edits to this file and they must not collide.
# -----------------------------------------------------------------------------

@router.get("/api/pipeline-runs/{run_id}/usage", response_model=RunUsageRollup)
async def get_pipeline_run_usage(run_id: str, db: AsyncSession = Depends(get_db)):
    """
    Cost/token rollup for one pipeline run, grouped by role.

    [read-heavy] — served by ix_step_usages_pipeline_run_id_role: the
    pipeline_run_id is DENORMALIZED onto StepUsage precisely so this scan
    never has to join back to reach the run.

    The per-step listing is what makes a dropped usage channel VISIBLE: the
    dogfood gate compares it against the run's StepRuns, so a step that
    silently reported nothing fails the push instead of quietly lowering the
    median. A NULL role aggregates under "unattributed" and is never dropped
    from the total; `cost_coverage` below 1.0 means some rows carry
    cost_source="unknown" — "we could not price this", which is a different
    fact from "this was free".

    An unknown run id is a 404, not an empty rollup (api-surface 0).
    """
    run = (
        await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    # COLUMNS, not entities. `RunUsageRollup.build` reads exactly the
    # attributes named here; selecting the whole StepUsage also dragged back
    # `raw` (capped at 8 KiB) and `determinism` per row, which this response
    # never looks at — a run with 30 steps was fetching a quarter of a
    # megabyte of TEXT to render numbers. A SQLAlchemy Row answers attribute
    # access by label, so `from_model` needs no change.
    rows = (
        await db.execute(
            select(
                StepUsage.id,
                StepUsage.step_execution_id,
                StepUsage.step_run_id,
                StepUsage.provider,
                StepUsage.model,
                StepUsage.role,
                StepUsage.input_tokens,
                StepUsage.output_tokens,
                StepUsage.cache_read_tokens,
                StepUsage.cache_write_tokens,
                StepUsage.cost_usd,
                StepUsage.cost_source,
                StepUsage.wall_clock_ms,
                StepUsage.container_seconds,
                StepRun.step_index,
                StepRun.step_name,
            )
            .outerjoin(StepRun, StepRun.id == StepUsage.step_run_id)
            .where(StepUsage.pipeline_run_id == run_id)
            .order_by(StepRun.step_index, StepUsage.created_at)
        )
    ).all()

    return RunUsageRollup.build(
        run_id, [(row, row.step_index, row.step_name) for row in rows]
    )
