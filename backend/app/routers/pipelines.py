import json
from typing import Optional
from urllib.parse import quote

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
from app.schemas.pipeline import (
    ADHOC_TRIGGER_TYPES,
    DEBUG_TRIGGER_TYPES,
    ArrayConversionError,
    PipelineGraphModel,
    PipelineStepConfig,
    array_to_graph,
)
from app.services.agent_run import ADHOC_PREFIX
from app.services.websocket import manager

# Usage channel (Phase 12.5) — separate import lines on purpose: the run
# rollup below is the only thing in this module that needs them.
from app.models import StepUsage
from app.schemas.usage import RunUsageRollup

router = APIRouter(tags=["pipelines"])

# A run in one of these states still owns a step container and is still being
# steered by the executor. Nothing that would delete the run row out from under
# it may proceed while it is in flight.
IN_FLIGHT_RUN_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


def live_run_refusal(subject: str, runs: list) -> str:
    """Human-readable 409 body naming the live run(s) and how to proceed.

    Shared with the repo delete guard (repos.py) so both refusals read the
    same and there is one place that knows the wording (R3). This string is
    what a person reads on screen when a tidy-up collides with a running
    pipeline, so it names the run and the exact next call rather than just
    saying "conflict".
    """
    listed = ", ".join(f"{r.id} ({r.status})" for r in runs[:3])
    if len(runs) > 3:
        listed += f", and {len(runs) - 3} more"
    first = runs[0].id
    return (
        f"{subject} still has a live run and was not deleted. "
        f"In-flight: {listed}. Deleting now would erase the run "
        f"mid-flight and leave its step container behind. Cancel it first "
        f"(POST /api/pipeline-runs/{first}/cancel), or wait for it to finish, "
        f"then delete again."
    )



def parse_steps(steps_str: str | None) -> list:
    """Parse the LEGACY v1 steps array from its JSON string.

    12.8: the only two readers left are the run gate and the export fallback
    below, and both exist solely for rows written BEFORE this phase - nothing
    writes the column any more (`create_pipeline` / `update_pipeline` /
    `upsert_materialized_pipeline` all write `steps_graph`). Both readers, and
    this function, go with the column at P6.
    """
    if not steps_str:
        return []
    try:
        return json.loads(steps_str)
    except (json.JSONDecodeError, TypeError):
        return []


def parse_steps_graph(steps_graph_str: str | None) -> dict | None:
    """Parse a stored steps_graph JSON string to a dict, or None."""
    if not steps_graph_str:
        return None
    try:
        parsed = json.loads(steps_graph_str)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed or None


def graph_from_request(
    steps: list[PipelineStepConfig] | None,
    steps_graph: PipelineGraphModel | None,
) -> PipelineGraphModel | None:
    """The graph an API write means, or None when it authors no definition.

    The API door (12.8 §4.4). One definition reaches the column, always the
    graph, so the executor never has to ask which of two fields the caller
    meant:

      * `steps` non-empty -> converted here, once, by the same
        `array_to_graph` the YAML door uses (R3).
      * `steps_graph` -> used as given.
      * neither, or an EMPTY `steps` -> None. An empty array is NOT "the
        caller supplied a definition": the editor's create-then-author flow
        posts `{"name": ..., "steps": []}` first and authors the graph in a
        follow-up PATCH, and 11 test call sites do the same. The pipeline is
        simply unrunnable until it has one, which `POST /run` already says.

    BOTH non-empty is refused by `_refuse_both_dialects` on `PipelineCreate` /
    `PipelineUpdate`, which is the single owner of that rule (R3) and runs
    during body parsing - i.e. before this function is ever reached. It is
    not re-checked here: a second copy of a refusal is a second thing to keep
    in step with the first.

    Raises:
        HTTPException: 422 with the reasons, if the array cannot be held
            faithfully by a graph.
    """
    if steps_graph is not None:
        return steps_graph
    if not steps:
        return None
    try:
        return array_to_graph(steps)
    except ArrayConversionError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "`steps` cannot be expressed as a pipeline graph: "
                + "; ".join(exc.reasons)
            ),
        )


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
    """Convert a Pipeline model to a dict for websocket broadcast.

    12.8: carries `steps_graph`, not `steps`. The frame REPLACES the store's
    row on the client, so shipping the retired array meant a create or an edit
    handed the UI a pipeline with no definition at all - the card had nothing
    to count and nothing to render. `definition_error` rides along for the
    same reason: it is a property of the row, and a badge that only appears
    after a page reload is a badge nobody sees.
    """
    return {
        "id": pipeline.id,
        "repo_id": pipeline.repo_id,
        "name": pipeline.name,
        "description": pipeline.description,
        "steps_graph": parse_steps_graph(pipeline.steps_graph),
        "definition_error": pipeline.definition_error,
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

    # ONE definition reaches the row, and it is always the graph (12.8 §4.4).
    # `steps` is not passed at all: the column keeps its python-side "[]"
    # default until it is dropped, so nothing new is ever written into it.
    graph = graph_from_request(pipeline.steps, pipeline.steps_graph)
    triggers_json = serialize_triggers(pipeline.triggers)

    db_pipeline = Pipeline(
        repo_id=repo_id,
        name=pipeline.name,
        description=pipeline.description,
        steps_graph=graph.model_dump_json() if graph is not None else None,
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

    # The definition, if this PATCH touches it at all. Both keys land on the
    # ONE column (12.8 §4.4); `steps` is converted here and `steps_graph` is
    # taken as given, and sending both is a 422 rather than a write whose
    # loser is silently discarded. An explicit `steps_graph: null`, or a
    # `steps: []`, clears the definition - which is exactly what writing "[]"
    # into the array used to mean.
    touches_definition = bool(
        {"steps", "steps_graph"} & set(update_data)
    )
    update_data.pop("steps", None)
    update_data.pop("steps_graph", None)

    if touches_definition:
        graph = graph_from_request(update.steps, update.steps_graph)
        pipeline.steps_graph = (
            graph.model_dump_json() if graph is not None else None
        )

    for key, value in update_data.items():
        if key == "triggers" and value is not None:
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

    # A pipeline delete CASCADES its runs (Pipeline.runs is delete-orphan), so
    # deleting one mid-run does not stop the run - it erases the row the
    # executor and /cancel are both steering by. The run 404s instantly,
    # /cancel can no longer reach it, and its step container is left behind
    # exited instead of being removed. Refuse loudly at the edge (R1) and name
    # the way out rather than half-deleting a live thing.
    live = (
        await db.execute(
            select(PipelineRun)
            .where(PipelineRun.pipeline_id == pipeline_id)
            .where(PipelineRun.status.in_(IN_FLIGHT_RUN_STATUSES))
            .order_by(PipelineRun.created_at)
        )
    ).scalars().all()
    if live:
        raise HTTPException(status_code=409, detail=live_run_refusal(f"Pipeline '{pipeline.name}'", live))

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

    # A definition that REFUSED to materialize must not run (12.8 §1.7). The
    # row keeps whatever graph it had before, so without this guard a broken
    # push would quietly re-run yesterday's definition under today's name and
    # report green - the Y5 dark channel.
    #
    # Checked BEFORE the repo lookup on purpose: this is a fact about the
    # thing the caller asked to run, and "repo not ingested" would answer a
    # question they did not ask about a pipeline that could not run either
    # way. No existing behaviour moves - until this phase no row could carry
    # the field at all.
    if pipeline.definition_error:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Pipeline '{pipeline.name}' has no runnable definition: "
                f"{pipeline.definition_error}. Fix the definition and sync it "
                f"again; running now would run the definition this one "
                f"replaced."
            ),
        )

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
        # Rows written BEFORE the graph cutover, which migration 0014 has not
        # backfilled yet. Nothing writes the array any more; this branch and
        # the executor's array fork die together at P5/P6.
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

def _content_disposition(pipeline_name: str) -> str:
    """RFC 6266 Content-Disposition for the exported YAML.

    The name is user data and used to go into the header RAW: a non-Latin-1
    name (any accent, any CJK) 500s on header encoding, and a name carrying
    CR, LF or NUL makes h11 refuse the whole response - uvicorn drops the
    connection and the browser hangs with no response at all. So: drop
    non-printable characters, keep an ASCII `filename=` fallback for old
    clients, and carry the real name in `filename*` as UTF-8 percent-encoding.
    """
    unsafe = '"' + chr(92)  # quote and backslash would break the quoted form
    cleaned = "".join(
        ch for ch in (pipeline_name or "")
        if ch.isprintable() and ch not in unsafe
    ).strip().replace(" ", "_")
    if not cleaned:
        cleaned = "pipeline"
    utf8_name = f"{cleaned}.yaml"

    ascii_name = utf8_name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(utf8_name, safe='')}"
    )


#: The step keys the export writes, in this order. Pinned deliberately: this
#: is `PipelineStepYaml`'s field set, and dropping any of them makes the
#: round trip lossy in a way nothing would notice - `id` renames every node
#: on re-import (and node ids are context-directory names and debug
#: breakpoint keys), `timeout` silently resets every step to 300s, and
#: `continue_in_context` silently loses workspace continuation.
EXPORT_STEP_KEYS = (
    "id", "name", "type", "config",
    "on_success", "on_failure", "timeout", "continue_in_context",
)


class GraphNotExportable(Exception):
    """A graph using a construct the v1 authoring array cannot say.

    Carries the CONSTRUCT, not just "cannot export": the whole point of
    refusing instead of flattening is that the author is told which edge or
    action is the problem and can go and change it.
    """

    def __init__(self, construct: str):
        self.construct = construct
        super().__init__(construct)


def _outgoing_by_condition(edges: list) -> dict[str, dict[str, list[str]]]:
    """`{from_step: {condition: [to_step, ...]}}`, refusing on `always`."""
    outgoing: dict[str, dict[str, list[str]]] = {}
    for edge in edges:
        condition = edge.get("condition", "success")
        source = edge.get("from_step")
        target = edge.get("to_step")
        if condition == "always":
            raise GraphNotExportable(
                f"an 'always' edge ({source} -> {target}): the array format "
                f"routes on success and on failure, and has no word for both"
            )
        if condition not in ("success", "failure"):
            raise GraphNotExportable(
                f"an edge condition {condition!r} ({source} -> {target}) the "
                f"array format has no word for"
            )
        outgoing.setdefault(source, {}).setdefault(condition, []).append(target)
    return outgoing


def _condition_action(
    step_id: str,
    condition: str,
    fired: list[str],
    targets: list[str],
    next_id: str | None,
) -> str:
    """The single `on_{condition}` word for one node, or a refusal.

    v1 carried FLOW and EFFECT in one string, so this is the inverse of
    `array_to_graph`'s split: an edge to the next step is `next`, no edge is
    `stop`, and an action is the action itself (which in v1 ALWAYS continued
    afterwards, hence the `does not continue` refusal below).
    """
    continues = next_id is not None and next_id in targets

    if len(fired) > 1:
        raise GraphNotExportable(
            f"step {step_id!r} firing {len(fired)} actions on {condition} "
            f"({', '.join(fired)}): the array format carries one action per "
            f"outcome, which is why they became a list in the first place"
        )
    if fired:
        if next_id is not None and not continues:
            raise GraphNotExportable(
                f"step {step_id!r} firing {fired[0]!r} on {condition} without "
                f"continuing to {next_id!r}: in the array format an action "
                f"always runs the following step afterwards"
            )
        return fired[0]
    return "next" if continues else "stop"


def graph_to_yaml_steps(graph: dict) -> list[dict]:
    """The v1 authoring array for a graph, or a refusal naming the construct.

    Export emits the ARRAY dialect (12.8 §4.10) because that is what
    `.lazyaf/pipelines/*.yaml` is: the point of exporting is to commit the
    file and have LazyAF read it back. Until 12.8 this endpoint emitted a
    THIRD dialect - `steps` as a mapping keyed by step id, with edge TARGETS
    written into `on_success` - which `PipelineYaml` cannot validate at all,
    so LazyAF could not import its own export.

    The down-conversion is total for a linear graph and REFUSES for
    everything else. It does not flatten: silently dropping the second branch
    of a fan-out would be the same class of defect on the way out that
    `array_to_graph` exists to prevent on the way in (R1).

    Raises:
        GraphNotExportable: naming the construct that cannot be written.
    """
    steps = graph.get("steps") or {}
    if not isinstance(steps, dict) or not steps:
        raise GraphNotExportable("a graph with no steps")

    entry_points = list(graph.get("entry_points") or [])
    if len(entry_points) != 1:
        raise GraphNotExportable(
            f"{len(entry_points)} entry points"
            + (f" ({', '.join(entry_points)})" if entry_points else "")
            + ": the array format has exactly one, its first step"
        )

    outgoing = _outgoing_by_condition(graph.get("edges") or [])

    incoming: dict[str, set[str]] = {}
    for source, by_condition in outgoing.items():
        for targets in by_condition.values():
            for target in targets:
                incoming.setdefault(target, set()).add(source)
    for target, sources in incoming.items():
        if len(sources) > 1:
            raise GraphNotExportable(
                f"fan-in: step {target!r} reached from {len(sources)} "
                f"different steps ({', '.join(sorted(sources))}), and the "
                f"array format reaches every step from exactly the one "
                f"before it"
            )

    # Walk the chain from the single entry point. In the array format step
    # i+1 is reachable ONLY from step i, so the walk both derives the export
    # order and proves the graph is a chain.
    order: list[str] = []
    seen: set[str] = set()
    current = entry_points[0]
    while True:
        if current in seen:
            raise GraphNotExportable(f"a cycle back through step {current!r}")
        seen.add(current)
        order.append(current)
        targets = {
            target
            for condition_targets in outgoing.get(current, {}).values()
            for target in condition_targets
        }
        if not targets:
            break
        if len(targets) > 1:
            raise GraphNotExportable(
                f"fan-out: step {current!r} continuing to {len(targets)} "
                f"different steps ({', '.join(sorted(targets))}), and the "
                f"array format continues to exactly one"
            )
        current = next(iter(targets))

    stranded = [step_id for step_id in steps if step_id not in seen]
    if stranded:
        raise GraphNotExportable(
            f"step(s) {', '.join(repr(s) for s in stranded)} that the path "
            f"from entry point {entry_points[0]!r} never reaches"
        )

    exported: list[dict] = []
    for index, step_id in enumerate(order):
        node = steps[step_id] or {}
        actions = node.get("actions") or {}
        if actions.get("always"):
            raise GraphNotExportable(
                f"step {step_id!r} firing {len(actions['always'])} 'always' "
                f"action(s) ({', '.join(actions['always'])}): the array "
                f"format keys an action on success or on failure, never both"
            )
        next_id = order[index + 1] if index + 1 < len(order) else None

        record = {
            "id": step_id,
            "name": node.get("name", step_id),
            "type": node.get("type", "script"),
            "config": node.get("config") or {},
            "timeout": node.get("timeout", 300),
            "continue_in_context": bool(node.get("continue_in_context", False)),
        }
        for condition in ("success", "failure"):
            record[f"on_{condition}"] = _condition_action(
                step_id,
                condition,
                list(actions.get(condition) or []),
                outgoing.get(step_id, {}).get(condition, []),
                next_id,
            )
        exported.append({key: record[key] for key in EXPORT_STEP_KEYS})

    return exported


@router.get("/api/pipelines/{pipeline_id}/export/yaml")
async def export_pipeline_yaml(pipeline_id: str, db: AsyncSession = Depends(get_db)):
    """Export a pipeline as a `.lazyaf/pipelines/*.yaml` file.

    The output is the AUTHORING dialect - the same array shape `PipelineYaml`
    reads - so an exported file can be committed to `.lazyaf/pipelines/` and
    synced straight back in. A graph the array cannot express is a 409 naming
    the construct, never a quiet flatten.
    """
    result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    export_data: dict = {"name": pipeline.name}
    if pipeline.description:
        export_data["description"] = pipeline.description

    triggers = parse_triggers(pipeline.triggers)
    if triggers:
        export_data["triggers"] = triggers

    graph = parse_steps_graph(pipeline.steps_graph)
    if graph is not None:
        try:
            export_data["steps"] = graph_to_yaml_steps(graph)
        except GraphNotExportable as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Pipeline '{pipeline.name}' cannot be exported as a "
                    f"`.lazyaf/pipelines` yaml file: it uses {exc.construct}. "
                    f"The export is the authoring array, so flattening this "
                    f"would hand you a file that re-imports as a DIFFERENT "
                    f"pipeline."
                ),
            )
    else:
        # Rows written before the graph cutover that migration 0014 has not
        # backfilled yet. They are already the authoring dialect; fill in the
        # keys the array left optional so the export has one shape either way.
        export_data["steps"] = [
            {
                "id": step.get("id") or f"step_{index}",
                "name": step.get("name", f"step_{index}"),
                "type": step.get("type", "script"),
                "config": step.get("config") or {},
                "on_success": step.get("on_success", "next"),
                "on_failure": step.get("on_failure", "stop"),
                "timeout": step.get("timeout", 300),
                "continue_in_context": bool(step.get("continue_in_context", False)),
            }
            for index, step in enumerate(parse_steps(pipeline.steps))
        ]

    # Generate YAML with nice formatting
    yaml_content = yaml.dump(export_data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return Response(
        content=yaml_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": _content_disposition(pipeline.name)},
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
