"""
Experiment REST surface (Phase 12.6.5).

NOTE (integrator): register with `app.include_router(experiments.router)` in
main.py, after `app.include_router(test_results.router)`.

The wire shapes are owned by `app/schemas/experiment.py` (R3); nothing here
invents a field. Every metric comes from `app/services/experiment_metrics.py`
as a pure function over fetched rows — no aggregation is computed inline in
an endpoint, which is what lets 13.4's `bench_metrics.py` absorb these later
without archaeology.

Refusals name the offending value. A 422 that says "invalid matrix" is a 422
nobody can act on, and a guardrail whose message you cannot act on is a
guardrail people route around.
"""
import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AcceptanceCriterion, Repo, TestRef, TestRun, UserStory
from app.models.experiment import (
    TERMINAL_EXPERIMENT_STATUSES,
    EstimateBasis,
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.schemas.experiment import (
    AbortResponse,
    EstimateResponse,
    ExperimentCellRead,
    ExperimentCreate,
    ExperimentDetail,
    ExperimentRead,
    ExperimentUpdate,
    LaunchResponse,
    LeaderboardResponse,
    ResumeResponse,
    money,
)
from app.services import experiment_metrics as metrics
from app.services import experiment_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["experiments"])


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

async def _get_or_404(db: AsyncSession, experiment_id: str) -> Experiment:
    experiment = await db.get(Experiment, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


async def _read_model(db: AsyncSession, experiment: Experiment) -> ExperimentRead:
    by_status, spend, coverage = await svc.experiment_progress(db, experiment.id)
    return ExperimentRead(
        id=experiment.id,
        name=experiment.name,
        description=experiment.description,
        target_type=experiment.target_type,
        target_id=experiment.target_id,
        repo_id=experiment.repo_id,
        matrix=svc.parse_matrix(experiment.matrix),
        verify=svc.parse_verify(experiment.verify),
        budget_usd=money(experiment.budget_usd) or "0.000000",
        max_concurrency=experiment.max_concurrency,
        cell_timeout=experiment.cell_timeout,
        push_branches=bool(experiment.push_branches),
        status=ExperimentStatus(experiment.status),
        estimated_cost_usd=money(experiment.estimated_cost_usd),
        estimate_basis=(
            EstimateBasis(experiment.estimate_basis)
            if experiment.estimate_basis
            else None
        ),
        budget_overrun_usd=money(experiment.budget_overrun_usd) or "0.000000",
        created_by=experiment.created_by,
        created_at=experiment.created_at,
        updated_at=experiment.updated_at,
        launched_at=experiment.launched_at,
        completed_at=experiment.completed_at,
        cells_total=sum(by_status.values()),
        by_status=by_status,
        spend_usd=money(spend) or "0.000000",
        cost_coverage=coverage,
        stalled=await svc.is_stalled(db, experiment),
    )


async def _validate_prompt_templates(db: AsyncSession, matrix) -> None:
    """Every prompt_template_id must resolve, by id, before anything runs."""
    from app.models.spec import PromptTemplate

    wanted = sorted(
        {p.prompt_template_id for p in matrix.prompts if p.prompt_template_id}
    )
    if not wanted:
        return
    found = {
        row
        for row in (
            await db.execute(
                select(PromptTemplate.id).where(PromptTemplate.id.in_(wanted))
            )
        ).scalars()
    }
    missing = [tid for tid in wanted if tid not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"unknown prompt_template_id(s): {', '.join(missing)}",
        )


# -----------------------------------------------------------------------------
# Create / dry run
# -----------------------------------------------------------------------------

@router.post("/api/experiments", status_code=201)
async def create_experiment(
    payload: ExperimentCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create an experiment, or price one with `"dry_run": true`.

    A dry run returns `200` and CREATES NOTHING. That is what makes the
    UI able to gate its Launch button behind a real estimate rather than a
    guess.

    Launch-time budget refusal: if the estimate is a real
    `historical-median` number and it exceeds the cap, the create is refused
    with the estimate in the message — raise the cap or shrink the matrix.
    Under `partial` / `no-history` we cannot prove it, so the create
    proceeds and the response echoes `budget_enforced_at_dispatch`, which is
    the honest position: enforcement runs off observed spend either way.
    """
    try:
        target = await svc.resolve_target(
            db, payload.target_type, payload.target_id, payload.repo_id
        )
    except svc.TargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    repo = await db.get(Repo, target.repo_id)
    if repo is None:
        raise HTTPException(
            status_code=422, detail=f"repo {target.repo_id!r} not found"
        )

    await _validate_prompt_templates(db, payload.matrix)

    estimate = await svc.estimate_matrix(
        db,
        payload.matrix,
        payload.budget_usd,
        repo_id=target.repo_id,
        push_branches=payload.push_branches,
    )

    if payload.dry_run:
        # 200, and nothing written. The shape is the same one
        # GET /api/experiments/{id}/estimate returns for a saved draft, and
        # the 200-vs-201 split is what tells a client nothing was created.
        response.status_code = 200
        return estimate

    if (
        estimate.estimate_basis == EstimateBasis.HISTORICAL_MEDIAN
        and not estimate.within_budget
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"estimated cost {estimate.estimated_cost_usd} exceeds "
                f"budget_usd {estimate.budget_usd} for {estimate.cells} cells "
                "(priced from historical medians of real usage rows). Raise "
                "the cap or shrink the matrix."
            ),
        )

    experiment = Experiment(
        name=payload.name,
        description=payload.description,
        target_type=payload.target_type,
        target_id=payload.target_id,
        repo_id=target.repo_id,
        matrix=payload.matrix.model_dump_json(),
        verify=payload.verify.model_dump_json() if payload.verify else None,
        budget_usd=payload.budget_usd,
        max_concurrency=payload.max_concurrency,
        cell_timeout=payload.cell_timeout,
        push_branches=payload.push_branches,
        status=ExperimentStatus.DRAFT.value,
        estimated_cost_usd=Decimal(estimate.estimated_cost_usd),
        estimate_basis=estimate.estimate_basis.value,
        created_by=payload.created_by,
    )
    db.add(experiment)
    await db.commit()
    await db.refresh(experiment)
    return await _read_model(db, experiment)


@router.get("/api/experiments", response_model=list[ExperimentRead])
async def list_experiments(
    status: str | None = Query(None, description="Filter by experiment status"),
    target_id: str | None = Query(None),
    repo_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. An unknown `status` is a 400 naming the vocabulary — an
    empty list would read as "no experiments", a very different fact.

    Cost note, stated rather than discovered later: progress and spend are
    COMPUTED per row (R3 — there is no materialized copy to read), so this is
    two extra indexed queries per experiment. Bounded by `limit`, and the
    alternative is a denormalized counter with a second writer."""
    if status is not None and status not in [s.value for s in ExperimentStatus]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid status {status!r}: valid values are "
                + ", ".join(s.value for s in ExperimentStatus)
            ),
        )
    query = select(Experiment)
    if status is not None:
        query = query.where(Experiment.status == status)
    if target_id is not None:
        query = query.where(Experiment.target_id == target_id)
    if repo_id is not None:
        query = query.where(Experiment.repo_id == repo_id)
    query = query.order_by(Experiment.created_at.desc()).offset(offset).limit(limit)
    rows = list((await db.execute(query)).scalars())
    return [await _read_model(db, row) for row in rows]


@router.get("/api/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    experiment = await _get_or_404(db, experiment_id)
    base = await _read_model(db, experiment)
    return ExperimentDetail(**base.model_dump(), cells=await _cells(db, experiment_id))


@router.patch("/api/experiments/{experiment_id}", response_model=ExperimentRead)
async def update_experiment(
    experiment_id: str,
    payload: ExperimentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Name/description/budget/concurrency are editable at any time; the
    MATRIX only while the experiment is a draft.

    A launched matrix is the frozen record of what ran — editing it would
    relabel results after the fact, which is the one thing a measurement
    platform may not do."""
    experiment = await _get_or_404(db, experiment_id)
    data = payload.model_dump(exclude_unset=True)

    if ("matrix" in data or "verify" in data) and experiment.status != (
        ExperimentStatus.DRAFT.value
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"experiment {experiment_id} is {experiment.status}: the matrix "
                "is frozen once launched, because it is the record of what ran"
            ),
        )
    if "matrix" in data and payload.matrix is not None:
        await _validate_prompt_templates(db, payload.matrix)
        experiment.matrix = payload.matrix.model_dump_json()
    if "verify" in data:
        experiment.verify = (
            payload.verify.model_dump_json() if payload.verify else None
        )
    for field in ("name", "description", "max_concurrency", "cell_timeout",
                  "push_branches", "budget_usd"):
        if field in data and data[field] is not None:
            setattr(experiment, field, data[field])
    experiment.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(experiment)
    return await _read_model(db, experiment)


@router.delete("/api/experiments/{experiment_id}", status_code=204)
async def delete_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """Drafts and terminal experiments only. Deleting a RUNNING experiment
    would orphan live containers whose completion hook then has nothing to
    land — a 422 saying so beats a silent leak."""
    experiment = await _get_or_404(db, experiment_id)
    if experiment.status == ExperimentStatus.RUNNING.value:
        raise HTTPException(
            status_code=422,
            detail=(
                "cannot delete a running experiment: abort it first "
                f"(POST /api/experiments/{experiment_id}/abort), then delete"
            ),
        )
    await db.delete(experiment)
    await db.commit()
    return None


@router.get("/api/experiments/{experiment_id}/estimate", response_model=EstimateResponse)
async def estimate_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """The dry run, for a saved draft."""
    experiment = await _get_or_404(db, experiment_id)
    matrix = svc.parse_matrix(experiment.matrix)
    if matrix is None:
        raise HTTPException(status_code=422, detail="experiment has no valid matrix")
    return await svc.estimate_matrix(
        db,
        matrix,
        Decimal(str(experiment.budget_usd)),
        repo_id=experiment.repo_id,
        push_branches=bool(experiment.push_branches),
    )


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------

@router.post(
    "/api/experiments/{experiment_id}/launch",
    response_model=LaunchResponse,
    status_code=202,
)
async def launch_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """Freeze prompt versions, create every cell, dispatch what fits."""
    experiment = await _get_or_404(db, experiment_id)
    if experiment.status != ExperimentStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"experiment {experiment_id} is already {experiment.status}; "
                "only a draft can be launched"
            ),
        )
    matrix = svc.parse_matrix(experiment.matrix)
    if matrix is None:
        raise HTTPException(status_code=422, detail="experiment has no valid matrix")

    estimate = await svc.estimate_matrix(
        db,
        matrix,
        Decimal(str(experiment.budget_usd)),
        repo_id=experiment.repo_id,
        push_branches=bool(experiment.push_branches),
    )
    if (
        estimate.estimate_basis == EstimateBasis.HISTORICAL_MEDIAN
        and not estimate.within_budget
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"estimated cost {estimate.estimated_cost_usd} exceeds "
                f"budget_usd {estimate.budget_usd} for {estimate.cells} cells. "
                "Raise the cap or shrink the matrix."
            ),
        )
    experiment.estimated_cost_usd = Decimal(estimate.estimated_cost_usd)
    experiment.estimate_basis = estimate.estimate_basis.value

    try:
        created, dispatched = await svc.launch(db, experiment)
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    await db.refresh(experiment)
    return LaunchResponse(
        id=experiment.id,
        status=ExperimentStatus(experiment.status),
        cells_created=created,
        dispatched=dispatched,
        estimated_cost_usd=estimate.estimated_cost_usd,
        estimate_basis=estimate.estimate_basis,
        warnings=estimate.warnings,
    )


@router.post("/api/experiments/{experiment_id}/abort", response_model=AbortResponse)
async def abort_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """Cancel pending cells; running cells are LEFT TO FINISH and still count."""
    experiment = await _get_or_404(db, experiment_id)
    if experiment.status in TERMINAL_EXPERIMENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"experiment {experiment_id} is already {experiment.status}; "
                "there is nothing left to abort"
            ),
        )
    cancelled, still_running = await svc.abort(db, experiment)
    await db.refresh(experiment)
    return AbortResponse(
        id=experiment.id,
        status=ExperimentStatus(experiment.status),
        cancelled=cancelled,
        still_running=still_running,
    )


@router.post("/api/experiments/{experiment_id}/resume", response_model=ResumeResponse)
async def resume_experiment(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """Re-pump an experiment a backend restart left stalled.

    The pump is in-process. That limit is REPORTED (`stalled` on the detail
    endpoint), and this is the guaranteed way out of it."""
    experiment = await _get_or_404(db, experiment_id)
    if experiment.status in TERMINAL_EXPERIMENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"experiment {experiment_id} is {experiment.status}; a terminal "
                "experiment has no cells left to dispatch"
            ),
        )
    dispatched, reset = await svc.resume(db, experiment)
    await db.refresh(experiment)
    return ResumeResponse(
        id=experiment.id,
        status=ExperimentStatus(experiment.status),
        dispatched=dispatched,
        reset_dispatching=reset,
    )


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------

async def _cells(db: AsyncSession, experiment_id: str) -> list[ExperimentCellRead]:
    usage_rows = await svc.fetch_usage_rows(db, experiment_id)
    test_counts = await svc.fetch_cell_test_counts(db, experiment_id)

    by_cell: dict[str, list] = {}
    for row in usage_rows:
        by_cell.setdefault(row.cell_id, []).append(row)

    rows = (
        await db.execute(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.cell_index.asc())
        )
    ).scalars()

    out: list[ExperimentCellRead] = []
    for cell in rows:
        usages = by_cell.get(cell.id, [])
        counts = test_counts.get(cell.id, {})
        wall = [u.wall_clock_ms for u in usages if u.wall_clock_ms is not None]
        out.append(
            ExperimentCellRead(
                id=cell.id,
                experiment_id=cell.experiment_id,
                cell_index=cell.cell_index,
                variant_index=cell.variant_index,
                agent=cell.agent,
                model=cell.model,
                prompt_template_id=cell.prompt_template_id,
                prompt_version=cell.prompt_version,
                label=cell.label,
                repeat_index=cell.repeat_index,
                pipeline_run_id=cell.pipeline_run_id,
                status=ExperimentRunStatus(cell.status),
                error=cell.error,
                started_at=cell.started_at,
                completed_at=cell.completed_at,
                created_at=cell.created_at,
                cost_usd=money(metrics.observed_spend(usages)) if usages else None,
                cost_coverage=metrics.cost_coverage(usages),
                wall_clock_ms=sum(wall) if wall else None,
                # `if usages`, not `or None`: a genuine zero-token step is a
                # fact, and collapsing it to "unknown" would hide it.
                input_tokens=(
                    sum(u.input_tokens or 0 for u in usages) if usages else None
                ),
                output_tokens=(
                    sum(u.output_tokens or 0 for u in usages) if usages else None
                ),
                tests_passed=counts.get("passed", 0),
                tests_failed=counts.get("failed", 0),
                tests_skipped=counts.get("skipped", 0),
            )
        )
    return out


@router.get(
    "/api/experiments/{experiment_id}/results",
    response_model=list[ExperimentCellRead],
)
async def experiment_results(experiment_id: str, db: AsyncSession = Depends(get_db)):
    """Per-CELL rows: coordinates, status, cost, test counts. The matrix view."""
    await _get_or_404(db, experiment_id)
    return await _cells(db, experiment_id)


@router.get(
    "/api/experiments/{experiment_id}/leaderboard",
    response_model=LeaderboardResponse,
)
async def experiment_leaderboard(
    experiment_id: str, db: AsyncSession = Depends(get_db)
):
    """Per-VARIANT aggregation.

    `ranked` is ALWAYS false and the note says why. 12.6.5 reports; ranking
    needs the paired cluster bootstrap and the separability rule that
    Milestone 13.4 owns. Sorting this table is a client convenience and is
    labelled as one.
    """
    await _get_or_404(db, experiment_id)
    variants = metrics.build_leaderboard(
        await svc.fetch_cell_rows(db, experiment_id),
        await svc.fetch_outcome_rows(db, experiment_id),
        await svc.fetch_usage_rows(db, experiment_id),
    )
    return LeaderboardResponse(
        experiment_id=experiment_id,
        variants=variants,
        cost_coverage=metrics.board_coverage(variants),
        warnings=metrics.board_warnings(variants),
    )


@router.get(
    "/api/leaderboards/feature/{feature_id}",
    response_model=LeaderboardResponse,
)
async def feature_leaderboard(
    feature_id: str,
    experiment_id: list[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Cross-experiment board over every criterion under one feature.

    Also emits one extra row with `variant_index: -1`, labelled
    "non-experiment runs", covering TestRuns with `experiment_run_id IS NULL`
    for those criteria — the repo's ordinary CI baseline. It is free, it is
    the number every variant should be compared against, and labelling it
    honestly is cheaper than letting someone mistake it for a variant.

    STATED LIMIT (not a silent one): this board is built from TEST OUTCOMES,
    so a variant's `cells_total` here counts only the cells that produced a
    tied-back result for one of this feature's criteria — it is not the
    matrix's cell count, and no cost is joined. Per-experiment cost, error
    rates and skipped-budget counts live on
    `GET /api/experiments/{id}/leaderboard`, which sees the whole matrix.
    """
    criterion_ids = list(
        (
            await db.execute(
                select(AcceptanceCriterion.id)
                .join(UserStory, UserStory.id == AcceptanceCriterion.user_story_id)
                .where(UserStory.feature_id == feature_id)
            )
        ).scalars()
    )
    if not criterion_ids:
        raise HTTPException(
            status_code=404,
            detail=(
                f"feature {feature_id} has no acceptance criteria - there is "
                "nothing to build a leaderboard from"
            ),
        )

    query = (
        select(
            ExperimentRun,
            TestRef.criterion_id,
            TestRun.status,
            AcceptanceCriterion.text,
        )
        .select_from(TestRun)
        .join(ExperimentRun, ExperimentRun.id == TestRun.experiment_run_id)
        .join(TestRef, TestRef.id == TestRun.test_ref_id)
        .join(AcceptanceCriterion, AcceptanceCriterion.id == TestRef.criterion_id)
        .where(
            TestRef.criterion_id.in_(criterion_ids),
            ExperimentRun.status.in_(
                (ExperimentRunStatus.PASSED.value, ExperimentRunStatus.FAILED.value)
            ),
        )
    )
    if experiment_id:
        query = query.where(ExperimentRun.experiment_id.in_(experiment_id))

    rows = (await db.execute(query)).all()

    # Variant identity is cross-experiment here: two experiments running the
    # same (agent, model, template, version) are the SAME variant, so the
    # local variant_index cannot be the key.
    keyed: dict[tuple, int] = {}
    cells: dict[str, metrics.CellRow] = {}
    outcomes: list[metrics.OutcomeRow] = []
    for cell, criterion_id, status, text in rows:
        key = (cell.agent, cell.model, cell.prompt_template_id, cell.prompt_version)
        index = keyed.setdefault(key, len(keyed))
        cells.setdefault(
            cell.id,
            metrics.CellRow(
                id=cell.id,
                variant_index=index,
                status=cell.status,
                agent=cell.agent,
                model=cell.model,
                prompt_template_id=cell.prompt_template_id,
                prompt_version=cell.prompt_version,
                label=cell.label,
            ),
        )
        outcomes.append(
            metrics.OutcomeRow(
                variant_index=index,
                criterion_id=criterion_id,
                status=status,
                criterion_text=text,
            )
        )

    variants = metrics.build_leaderboard(list(cells.values()), outcomes, [])

    baseline = (
        await db.execute(
            select(TestRef.criterion_id, TestRun.status, AcceptanceCriterion.text)
            .select_from(TestRun)
            .join(TestRef, TestRef.id == TestRun.test_ref_id)
            .join(AcceptanceCriterion, AcceptanceCriterion.id == TestRef.criterion_id)
            .where(
                TestRef.criterion_id.in_(criterion_ids),
                TestRun.experiment_run_id.is_(None),
            )
        )
    ).all()
    if baseline:
        baseline_cell = metrics.CellRow(
            id="baseline",
            variant_index=-1,
            status=ExperimentRunStatus.PASSED.value,
            agent="",
            label="non-experiment runs",
        )
        baseline_rows = metrics.build_leaderboard(
            [baseline_cell],
            [
                metrics.OutcomeRow(
                    variant_index=-1,
                    criterion_id=criterion_id,
                    status=status,
                    criterion_text=text,
                )
                for criterion_id, status, text in baseline
            ],
            [],
        )
        variants = baseline_rows + variants

    return LeaderboardResponse(
        feature_id=feature_id,
        variants=variants,
        cost_coverage=metrics.board_coverage(variants),
        warnings=metrics.board_warnings(variants),
    )
