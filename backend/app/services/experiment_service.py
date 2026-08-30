"""
Experiment engine: matrix expansion, fan-out, budget enforcement, recording
(Phase 12.6.5).

ONE CELL = ONE AD-HOC AGENT RUN
-------------------------------
The ad-hoc run mechanism (``app/services/agent_run.py``) already creates a
hidden ephemeral ``Pipeline`` plus a real, visible ``PipelineRun`` and calls
``pipeline_executor.start_pipeline`` verbatim, which buys the workspace
volume, StepRun, StepExecution + step JWT, control mode, streamed logs,
``/test-results``, ``/usage``, the watchdog and cancellation. An experiment
cell is one of those runs, so this module builds NO execution machinery.

It reuses ``agent_run``'s BUILDERS (``build_agent_step_config``,
``adhoc_pipeline_name``) rather than its ENTRY POINT
(``start_adhoc_agent_run``), for one reason: that entry point builds exactly
one step, and a cell may need two (agent + verify). Reusing the builders
keeps the step-config vocabulary single-sourced — which is the part that must
not fork — while letting the cell own its own step list. ``agent_run.py`` and
``pipeline_executor.py`` are IMPORTED here and never edited.

THE CELL -> RUN LINK, AND THE SYNCHRONOUS-COMPLETION TRAP
---------------------------------------------------------
``start_pipeline`` can complete a run SYNCHRONOUSLY, before it returns: an
image-preflight failure, an empty step list and a graph with no entry points
all call ``_complete_pipeline`` inline, which calls ``on_run_complete``
inline, which lands here. So the link is ``trigger_ref = ExperimentRun.id``,
written as part of the run row at CREATION — never a column set afterwards.
Everything downstream resolves the cell from it:

- ``on_cell_complete`` looks the cell up by ``trigger_ref``, so a run that
  died inside ``start_pipeline`` still lands its cell correctly.
- ``test_ingestion`` stamps coordinates by ``trigger_type``/``trigger_ref``
  too, which removes the race entirely: a mock step can finish in under
  100 ms, well before the pump could have written ``pipeline_run_id``.

``experiment_runs.pipeline_run_id`` is a convenience mirror for the UI's
"open this run" link and is load-bearing for nothing.

THE PUMP
--------
There is no polling loop. Dispatch is driven by launch and by cell
completion, guarded by a per-experiment ``asyncio.Lock`` plus a re-pump flag:
a caller that finds the lock held raises the flag and returns, and the holder
loops until the flag is clear. That is what stops
``pump -> dispatch -> synchronous failure -> on_cell_complete -> pump`` from
recursing two hundred frames deep on a bad image.

Cells are claimed with a compare-and-set UPDATE, never read-then-write, so
two concurrent pumps cannot both take the same cell. The live-cell count is
read from the DATABASE, never from memory: a backend restart must not lose
it.

RESTART DURABILITY (R1: nothing dark)
-------------------------------------
The pump is in-process, so a restart with cells still ``pending`` leaves the
matrix stalled. That state is REPORTED, never hidden: the detail endpoint
returns ``stalled: true`` and ``POST /api/experiments/{id}/resume`` re-pumps.
``resume_stalled_experiments()`` is the optional lifespan sweep that makes
restarts self-heal; the endpoint is the guaranteed path either way.

THE CAP
-------
Enforced HARD, before EVERY dispatch, off OBSERVED spend recomputed from
``step_usages`` — not off the estimate. Three properties, stated plainly
because a cap that is quietly approximate is worse than none:

1. The cap bounds DISPATCH, not in-flight spend. Maximum overshoot is
   whatever ``max_concurrency`` cells were already running when it tripped,
   and that overshoot is WRITTEN to ``Experiment.budget_overrun_usd`` rather
   than absorbed.
2. ``cost_source="unknown"`` rows count as ZERO against the cap — which is
   exactly why ``cost_coverage`` is surfaced on every cell and variant.
3. No pricing history does not disable the cap. The estimate is advisory;
   enforcement runs off observed ``StepUsage``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcceptanceCriterion,
    Card,
    Feature,
    Pipeline,
    PipelineRun,
    PromptTemplate,
    Repo,
    RunStatus,
    StepUsage,
    TestRef,
    TestRun,
    UserStory,
)
from app.models.experiment import (
    LIVE_CELL_STATUSES,
    MEASURED_CELL_STATUSES,
    TERMINAL_CELL_STATUSES,
    TERMINAL_EXPERIMENT_STATUSES,
    EstimateBasis,
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
    PromptVersion,
    TRIGGER_EXPERIMENT,
)
from app.models.usage import UsageCostSource
from app.schemas.experiment import (
    EstimateResponse,
    MatrixSpec,
    VariantEstimate,
    VerifySpec,
    money,
)
from app.services import experiment_metrics as metrics
from app.services.websocket import manager

logger = logging.getLogger(__name__)

# ``TRIGGER_EXPERIMENT`` is imported above from ``models.experiment``, its
# single home, and re-exported here so callers can keep writing
# ``experiment_service.TRIGGER_EXPERIMENT``. It must equal the constant the
# integrator adds to ``agent_run`` (registration section 12.4).

#: How many recent priced usage rows the estimate's median is taken over.
ESTIMATE_HISTORY_LIMIT = 50

_ZERO = Decimal("0")

# Per-experiment dispatch serialization. PROCESS-LOCAL by construction: the
# pump is in-process, which is exactly why `stalled` is reported and `resume`
# exists rather than this pretending to be durable.
#
# The dict is never pruned. That is deliberate, not an oversight: dropping a
# lock while a coroutine is waiting on it would let a second pump build a
# fresh one and run concurrently, and the only thing standing between that and
# a double dispatch would be the CAS. One asyncio.Lock per experiment id, for
# user-created experiments, is a bounded and tiny cost; correctness is not.
_pump_locks: dict[str, asyncio.Lock] = {}
_repump: set[str] = set()


# =============================================================================
# Matrix expansion
# =============================================================================

@dataclass(frozen=True)
class CellCoordinates:
    """The frozen coordinates of one matrix cell."""

    cell_index: int
    variant_index: int
    repeat_index: int
    agent: str
    model: str | None
    prompt_template_id: str | None
    label: str
    step_config: dict[str, Any]


def variant_label(model_label: str | None, prompt_label: str | None,
                  agent: str, model: str | None,
                  prompt_template_id: str | None) -> str:
    """Human name for a variant. Falls back to the coordinates themselves."""
    left = model_label or f"{agent}/{model or 'default'}"
    right = prompt_label or (
        f"tpl:{prompt_template_id[:8]}" if prompt_template_id else "platform default"
    )
    return f"{left} / {right}"


def expand_matrix(matrix: MatrixSpec) -> list[CellCoordinates]:
    """N models x M prompts x R repeats -> cells, in a DETERMINISTIC order.

    ``cell_index = ((model_i * n_prompts) + prompt_i) * repeat + repeat_i``
    and ``variant_index = cell_index // repeat``. Both are part of the API
    contract: the matrix grid renders straight off them, and repeats of one
    variant sharing a ``variant_index`` is what turns the leaderboard's
    grouping into an integer comparison.

    Per-axis ``step_config`` overlays are merged models-first, prompts-second,
    so a prompt entry can refine what its model entry set. Reserved keys were
    already refused at validation (schemas/experiment.py) — an overlay that
    could silently rewrite the axis it is varying is the definition of dark.
    """
    cells: list[CellCoordinates] = []
    n_prompts = len(matrix.prompts)
    repeat = matrix.repeat

    for model_i, model_entry in enumerate(matrix.models):
        for prompt_i, prompt_entry in enumerate(matrix.prompts):
            variant_index = (model_i * n_prompts) + prompt_i
            overlay: dict[str, Any] = {}
            overlay.update(model_entry.step_config or {})
            overlay.update(prompt_entry.step_config or {})
            label = variant_label(
                model_entry.label,
                prompt_entry.label,
                model_entry.agent,
                model_entry.model,
                prompt_entry.prompt_template_id,
            )
            for repeat_i in range(repeat):
                cells.append(
                    CellCoordinates(
                        cell_index=variant_index * repeat + repeat_i,
                        variant_index=variant_index,
                        repeat_index=repeat_i,
                        agent=model_entry.agent,
                        model=model_entry.model,
                        prompt_template_id=prompt_entry.prompt_template_id,
                        label=label,
                        step_config=overlay,
                    )
                )
    return cells


def parse_matrix(raw: str | None) -> MatrixSpec | None:
    """Parse a persisted matrix column. Never raises on stored data."""
    if not raw:
        return None
    try:
        return MatrixSpec.model_validate(json.loads(raw))
    except Exception:
        logger.warning("Unparseable experiment matrix column: %r", (raw or "")[:200])
        return None


def parse_verify(raw: str | None) -> VerifySpec | None:
    if not raw:
        return None
    try:
        return VerifySpec.model_validate(json.loads(raw))
    except Exception:
        logger.warning("Unparseable experiment verify column: %r", (raw or "")[:200])
        return None


# =============================================================================
# Prompt version freezing
# =============================================================================

def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def freeze_prompt_versions(
    db: AsyncSession, template_ids: list[str]
) -> dict[str, PromptVersion]:
    """Get-or-create an immutable ``PromptVersion`` per template.

    Called ONCE for the whole matrix, BEFORE any cell dispatches, so a
    template edited mid-experiment cannot split one variant across two prompt
    bodies.

    Identity is ``(template_id, content_hash)``: relaunching an unchanged
    template reuses version N; editing it and relaunching yields N+1. The
    ``(template_id, version)`` unique index makes a concurrent insert an
    ``IntegrityError``, absorbed with the codebase's rollback/re-select idiom
    (``app/services/execution/idempotency.py``).
    """
    wanted = [tid for tid in dict.fromkeys(template_ids) if tid]
    if not wanted:
        return {}

    templates = {
        row.id: row
        for row in (
            await db.execute(
                select(PromptTemplate).where(PromptTemplate.id.in_(wanted))
            )
        ).scalars()
    }
    missing = [tid for tid in wanted if tid not in templates]
    if missing:
        raise LookupError(
            "unknown prompt_template_id(s): " + ", ".join(sorted(missing))
        )

    resolved: dict[str, PromptVersion] = {}
    for template_id in wanted:
        template = templates[template_id]
        digest = content_hash(template.content or "")
        resolved[template_id] = await _get_or_create_version(
            db, template_id, template.content or "", digest
        )
    return resolved


async def _get_or_create_version(
    db: AsyncSession, template_id: str, body: str, digest: str
) -> PromptVersion:
    existing = (
        await db.execute(
            select(PromptVersion).where(
                PromptVersion.template_id == template_id,
                PromptVersion.content_hash == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    next_version = (
        await db.execute(
            select(func.coalesce(func.max(PromptVersion.version), 0)).where(
                PromptVersion.template_id == template_id
            )
        )
    ).scalar_one() + 1

    version = PromptVersion(
        id=str(uuid4()),
        template_id=template_id,
        version=next_version,
        body=body,
        content_hash=digest,
    )
    db.add(version)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent launch inserted the same body (or claimed the same
        # version number) between our SELECT and this flush. Roll back and
        # re-select: the race costs a retry, never the launch. Nothing is
        # read off a live ORM row across the rollback.
        await db.rollback()
        logger.info(
            "Concurrent PromptVersion insert for template %s - re-resolving",
            template_id[:8],
        )
        again = (
            await db.execute(
                select(PromptVersion).where(
                    PromptVersion.template_id == template_id,
                    PromptVersion.content_hash == digest,
                )
            )
        ).scalar_one_or_none()
        if again is not None:
            return again
        raise
    return version


# =============================================================================
# Target resolution
# =============================================================================

@dataclass
class ExperimentTarget:
    repo_id: str
    title: str
    description: str
    card_id: str | None = None


class TargetError(ValueError):
    """A target that cannot be resolved. Carries a message naming the value."""


async def resolve_target(
    db: AsyncSession,
    target_type: str,
    target_id: str,
    repo_id: str | None = None,
) -> ExperimentTarget:
    """Resolve a card / user story into the task text and repo a cell needs.

    ``target_type="feature"`` is REFUSED, loudly: a feature spans repos and
    has no single task text, so running one would mean silently picking a
    story and a repo on the user's behalf. Milestone 13.2's orchestrator owns
    that shape.
    """
    if target_type == "feature":
        raise TargetError(
            "target_type 'feature' is not supported in Phase 12.6.5: a feature "
            "spans repos and has no single task text, so a cell would have to "
            "guess both. Target a user_story or a card. Cross-repo, "
            "multi-story experiments arrive with Milestone 13.2."
        )
    if target_type == "card":
        card = await db.get(Card, target_id)
        if card is None:
            raise TargetError(f"card {target_id!r} not found")
        if repo_id and repo_id != card.repo_id:
            # Refused, not silently honoured: cloning a different repo for a
            # card's task would run the agent against code the card does not
            # describe, and every result would be mislabelled.
            raise TargetError(
                f"repo_id {repo_id!r} does not match card {target_id!r}'s repo "
                f"{card.repo_id!r}; a card experiment runs in the card's repo"
            )
        return ExperimentTarget(
            repo_id=card.repo_id,
            title=card.title,
            description=card.description or "",
            card_id=card.id,
        )
    if target_type == "user_story":
        story = await db.get(UserStory, target_id)
        if story is None:
            raise TargetError(f"user_story {target_id!r} not found")
        feature = await db.get(Feature, story.feature_id)
        repo_ids: list[str] = []
        if feature is not None:
            try:
                repo_ids = json.loads(feature.repo_ids or "[]") or []
            except (json.JSONDecodeError, TypeError):
                repo_ids = []
        if not repo_id:
            raise TargetError(
                "a user_story target requires an explicit repo_id: a story "
                "belongs to a feature that may span repos "
                f"({repo_ids or 'none declared'}), and guessing one is a "
                "silent choice about where the agent commits"
            )
        if repo_ids and repo_id not in repo_ids:
            raise TargetError(
                f"repo_id {repo_id!r} is not one of feature "
                f"{story.feature_id[:8]}'s repo_ids ({', '.join(repo_ids)})"
            )
        criteria = list(
            (
                await db.execute(
                    select(AcceptanceCriterion)
                    .where(AcceptanceCriterion.user_story_id == story.id)
                    .order_by(AcceptanceCriterion.created_at.asc())
                )
            ).scalars()
        )
        lines = [story.narrative or ""]
        if criteria:
            lines.append("")
            lines.append("Acceptance criteria:")
            lines.extend(
                f"- {'(required) ' if c.required else ''}{c.text}" for c in criteria
            )
        return ExperimentTarget(
            repo_id=repo_id,
            title=story.title,
            description="\n".join(lines).strip(),
        )
    raise TargetError(
        f"unknown target_type {target_type!r}: valid values are card, user_story"
    )


# =============================================================================
# Estimation (the dry run)
# =============================================================================

async def _model_cost_history(
    db: AsyncSession, model: str | None
) -> tuple[Decimal | None, int]:
    """Median cost of the most recent priced usage rows for one model.

    There is no price table by design (owner decision 2026-08-29: while the
    CLIs report cost, a second pricing table is a second source of truth that
    will drift). With ``model=None`` — "the CLI's own default" — there is
    nothing to key history on, so the variant is honestly unpriced rather
    than approximated from an unrelated model's rows.
    """
    if not model:
        return None, 0
    rows = list(
        (
            await db.execute(
                select(StepUsage.cost_usd)
                .where(
                    StepUsage.model == model,
                    StepUsage.cost_usd.is_not(None),
                    StepUsage.cost_source != UsageCostSource.UNKNOWN.value,
                )
                .order_by(StepUsage.created_at.desc())
                .limit(ESTIMATE_HISTORY_LIMIT)
            )
        ).scalars()
    )
    values = [Decimal(str(value)) for value in rows if value is not None]
    if not values:
        return None, 0
    return metrics.median_decimal(values), len(values)


async def estimate_matrix(
    db: AsyncSession,
    matrix: MatrixSpec,
    budget_usd: Decimal,
    *,
    repo_id: str | None = None,
    push_branches: bool = False,
) -> EstimateResponse:
    """Price a matrix from HISTORY. Creates nothing.

    An unpriced variant contributes NOTHING to the total and is NAMED in the
    warnings, and ``estimate_basis`` degrades to ``partial`` / ``no-history``
    so the number is explicitly a LOWER BOUND. A missing estimate must never
    silently read as ``$0.00``.
    """
    cells = expand_matrix(matrix)
    per_variant: list[VariantEstimate] = []
    warnings: list[str] = []
    total = _ZERO
    priced = unpriced = 0

    by_variant: dict[int, list[CellCoordinates]] = {}
    for cell in cells:
        by_variant.setdefault(cell.variant_index, []).append(cell)

    for variant_index in sorted(by_variant):
        group = by_variant[variant_index]
        head = group[0]
        median, samples = await _model_cost_history(db, head.model)
        if median is None:
            unpriced += 1
            basis = EstimateBasis.NO_HISTORY
            estimate = _ZERO
            warnings.append(
                f"variant {head.label!r}: no priced history for model "
                f"{head.model or '(CLI default)'} - its cost is NOT in this "
                "estimate, which is therefore a LOWER BOUND"
            )
        else:
            priced += 1
            basis = EstimateBasis.HISTORICAL_MEDIAN
            estimate = median * len(group)
            total += estimate
        per_variant.append(
            VariantEstimate(
                variant_index=variant_index,
                label=head.label,
                agent=head.agent,
                model=head.model,
                prompt_template_id=head.prompt_template_id,
                runs=len(group),
                estimate_usd=money(estimate) or "0.000000",
                basis=basis,
                samples=samples,
            )
        )

    if unpriced == 0:
        overall = EstimateBasis.HISTORICAL_MEDIAN
    elif priced == 0:
        overall = EstimateBasis.NO_HISTORY
    else:
        overall = EstimateBasis.PARTIAL

    if push_branches and repo_id:
        warnings.extend(await push_trigger_warnings(db, repo_id, len(cells)))

    return EstimateResponse(
        cells=len(cells),
        models=len(matrix.models),
        prompts=len(matrix.prompts),
        repeat=matrix.repeat,
        runs=len(cells),
        estimated_cost_usd=money(total) or "0.000000",
        estimate_basis=overall,
        per_variant=per_variant,
        budget_usd=money(budget_usd) or "0.000000",
        within_budget=total <= budget_usd,
        warnings=warnings,
    )


async def push_trigger_warnings(
    db: AsyncSession, repo_id: str, cells: int
) -> list[str]:
    """Name every push-triggered pipeline a pushed cell branch would fire.

    A push trigger with no ``branches:`` pattern matches EVERY branch
    (``trigger_service.on_push``), so ``push_branches=true`` on a 20-cell
    matrix starts 20 CI runs that this experiment's cap neither covers nor
    estimated. Stated, never silent.
    """
    warnings: list[str] = []
    pipelines = list(
        (
            await db.execute(
                select(Pipeline).where(
                    Pipeline.repo_id == repo_id,
                    Pipeline.is_template.is_(False),
                )
            )
        ).scalars()
    )
    for pipeline in pipelines:
        try:
            triggers = json.loads(pipeline.triggers or "[]") or []
        except (json.JSONDecodeError, TypeError):
            continue
        for trigger in triggers:
            if not isinstance(trigger, dict):
                continue
            if trigger.get("type") != "push" or not trigger.get("enabled", True):
                continue
            patterns = (trigger.get("config") or {}).get("branches") or []
            scope = (
                "triggers on every branch"
                if not patterns
                else f"triggers on branches matching {', '.join(patterns)}"
            )
            warnings.append(
                f"push_branches=true: pipeline {pipeline.name!r} {scope} and "
                f"will start up to {cells} additional runs not covered by this "
                "experiment's cap"
            )
            break
    return warnings


# =============================================================================
# Launch
# =============================================================================

async def launch(db: AsyncSession, experiment: Experiment) -> tuple[int, int]:
    """Freeze prompt versions, create every cell, then pump.

    Returns ``(cells_created, dispatched)``. Warnings belong to the ESTIMATE
    (one producer), and the router merges them into the launch response.

    Prompt versions are frozen for the WHOLE matrix here, before a single
    cell dispatches: a template edited between cell 3 and cell 4 must not
    split one variant across two prompt bodies.
    """
    matrix = parse_matrix(experiment.matrix)
    if matrix is None:
        raise ValueError("experiment matrix is missing or unparseable")

    coordinates = expand_matrix(matrix)
    versions = await freeze_prompt_versions(
        db, [c.prompt_template_id for c in coordinates if c.prompt_template_id]
    )

    now = datetime.utcnow()
    for coord in coordinates:
        version = versions.get(coord.prompt_template_id or "")
        db.add(
            ExperimentRun(
                id=str(uuid4()),
                experiment_id=experiment.id,
                cell_index=coord.cell_index,
                variant_index=coord.variant_index,
                agent=coord.agent,
                model=coord.model,
                prompt_template_id=coord.prompt_template_id,
                prompt_version_id=version.id if version else None,
                prompt_version=version.version if version else None,
                label=coord.label,
                repeat_index=coord.repeat_index,
                status=ExperimentRunStatus.PENDING.value,
                created_at=now,
            )
        )

    experiment.status = ExperimentStatus.RUNNING.value
    experiment.launched_at = now
    await db.commit()

    await broadcast_experiment(db, experiment.id)
    dispatched = await pump(db, experiment.id)
    return len(coordinates), dispatched


# =============================================================================
# The pump
# =============================================================================

async def pump(db: AsyncSession, experiment_id: str) -> int:
    """Dispatch as many pending cells as concurrency and budget allow.

    Returns the number dispatched. NEVER raises: it is called from cell
    completion, and an exception here would abandon the matrix.

    Re-entrancy: a caller that finds the lock held raises the re-pump flag
    and returns 0; the holder loops until the flag is clear. This is what
    stops a synchronously-failing dispatch from recursing through
    ``on_cell_complete`` back into ``pump``.
    """
    lock = _pump_locks.setdefault(experiment_id, asyncio.Lock())
    if lock.locked():
        _repump.add(experiment_id)
        return 0

    dispatched = 0
    try:
        async with lock:
            while True:
                _repump.discard(experiment_id)
                dispatched += await _pump_once(db, experiment_id)
                if experiment_id not in _repump:
                    break
    except Exception:
        logger.exception(
            "Experiment pump failed for %s; the matrix is left in a reported "
            "state and POST /api/experiments/{id}/resume can restart it",
            experiment_id[:8],
        )
    finally:
        _repump.discard(experiment_id)
    return dispatched


async def _pump_once(db: AsyncSession, experiment_id: str) -> int:
    experiment = await db.get(Experiment, experiment_id)
    if experiment is None:
        return 0
    if experiment.status in TERMINAL_EXPERIMENT_STATUSES:
        # An ABORTED experiment still has live cells finishing (abort cancels
        # pending work and lets running work land). No further dispatch
        # happens, but the LAST completion still has to close the row —
        # completed_at and budget_overrun_usd — or an aborted matrix reads as
        # permanently in-flight.
        if experiment.completed_at is None:
            await _maybe_finalize(db, experiment)
        return 0

    dispatched = 0
    while True:
        live = await _count_cells(db, experiment_id, LIVE_CELL_STATUSES)
        if live >= experiment.max_concurrency:
            break

        pending = await _next_pending(db, experiment_id)
        if pending is None:
            break

        # The cap, recomputed from OBSERVED spend before EVERY dispatch.
        spend = metrics.observed_spend(await fetch_usage_rows(db, experiment_id))
        if spend >= experiment.budget_usd:
            await _exhaust_budget(db, experiment, spend)
            break

        claimed = await _claim(db, pending.id)
        if not claimed:
            continue  # someone else took it; try the next one

        dispatched += 1
        await _dispatch_cell(db, experiment, pending.id)

    await _maybe_finalize(db, experiment)
    return dispatched


async def _count_cells(
    db: AsyncSession, experiment_id: str, statuses: frozenset[str]
) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment_id,
                ExperimentRun.status.in_(tuple(statuses)),
            )
        )
    ).scalar_one()


async def _next_pending(db: AsyncSession, experiment_id: str) -> ExperimentRun | None:
    return (
        await db.execute(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment_id,
                ExperimentRun.status == ExperimentRunStatus.PENDING.value,
            )
            .order_by(ExperimentRun.cell_index.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _claim(db: AsyncSession, cell_id: str) -> bool:
    """Compare-and-set, never read-then-write: exactly one pump wins a cell."""
    result = await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.id == cell_id,
            ExperimentRun.status == ExperimentRunStatus.PENDING.value,
        )
        .values(
            status=ExperimentRunStatus.DISPATCHING.value,
            started_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return result.rowcount == 1


async def _dispatch_cell(
    db: AsyncSession, experiment: Experiment, cell_id: str
) -> None:
    """Build and start one cell's run. One bad cell never kills a matrix."""
    cell = await db.get(ExperimentRun, cell_id)
    if cell is None:
        return
    try:
        run = await start_cell_run(db, experiment, cell)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        logger.exception(
            "Experiment %s cell %s failed to start", experiment.id[:8], cell.cell_index
        )
        await _fail_cell(db, cell_id, f"{type(exc).__name__}: {exc}")
        return

    # The run may ALREADY be terminal: start_pipeline can complete a run
    # synchronously, and on_cell_complete will have landed this cell. Only
    # promote a cell that is still dispatching, and only mirror the run id.
    await db.execute(
        update(ExperimentRun)
        .where(ExperimentRun.id == cell_id)
        .values(pipeline_run_id=run.id)
    )
    await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.id == cell_id,
            ExperimentRun.status == ExperimentRunStatus.DISPATCHING.value,
        )
        .values(status=ExperimentRunStatus.RUNNING.value)
    )
    await db.commit()
    await broadcast_cell(db, cell_id)


async def _fail_cell(db: AsyncSession, cell_id: str, error: str) -> None:
    await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.id == cell_id,
            ExperimentRun.status.not_in(tuple(TERMINAL_CELL_STATUSES)),
        )
        .values(
            status=ExperimentRunStatus.ERROR.value,
            error=error[:4000],
            completed_at=datetime.utcnow(),
        )
    )
    await db.commit()
    await broadcast_cell(db, cell_id)


def cell_branch(experiment_id: str, cell_index: int) -> str:
    return f"lazyaf/exp/{experiment_id[:8]}/{cell_index:03d}"


async def start_cell_run(
    db: AsyncSession, experiment: Experiment, cell: ExperimentRun
) -> PipelineRun:
    """Create the cell's ephemeral pipeline and start it.

    ``build_agent_step_config`` and ``adhoc_pipeline_name`` are imported from
    ``agent_run`` read-only, so the hidden-pipeline filter that keeps ad-hoc
    pipelines out of ``GET /api/pipelines`` keeps hiding these too, for free.
    """
    from app.services.agent_run import (
        adhoc_pipeline_name,
        build_agent_step_config,
    )
    from app.services.pipeline_executor import pipeline_executor

    repo = await db.get(Repo, experiment.repo_id)
    if repo is None:
        raise LookupError(f"repo {experiment.repo_id} not found")

    target = await resolve_target(
        db, experiment.target_type, experiment.target_id, experiment.repo_id
    )
    matrix = parse_matrix(experiment.matrix)
    overlay: dict[str, Any] = {}
    if matrix is not None:
        for coord in expand_matrix(matrix):
            if coord.cell_index == cell.cell_index:
                overlay = dict(coord.step_config)
                break

    # `mock_config` is a named parameter of build_agent_step_config (and one
    # of ITS reserved keys), so it must be passed through explicitly rather
    # than as an `extra` — which would drop it silently.
    mock_config = overlay.pop("mock_config", None)

    prompt_body: str | None = None
    if cell.prompt_version_id:
        version = await db.get(PromptVersion, cell.prompt_version_id)
        prompt_body = version.body if version else None

    base_branch = repo.default_branch
    work_branch = cell_branch(experiment.id, cell.cell_index)

    step_config = build_agent_step_config(
        agent=cell.agent,
        model=cell.model,
        task=target.description or target.title,
        title=target.title,
        description=target.description,
        prompt_template=prompt_body,
        base_branch=base_branch,
        branch=work_branch,
        commit_enabled=True,
        # Cells commit inside their ephemeral workspace and do NOT push by
        # default: 20 pushed branches would start 20 uncosted CI runs.
        push_branch=bool(experiment.push_branches),
        card_id=target.card_id,
        mock_config=mock_config,
        extra=overlay,
    )

    steps: list[dict[str, Any]] = [
        {
            "id": "agent",
            "name": f"{cell.label or 'cell'} #{cell.repeat_index}",
            "type": "agent",
            "config": step_config,
            "timeout": experiment.cell_timeout,
            "on_success": "next",
            # A crashed agent produced no measurement, so verify must not run
            # and paper a 0% over it. That is the `error` classification.
            "on_failure": "stop",
        }
    ]
    verify = parse_verify(experiment.verify)
    if verify is not None:
        steps.append(
            {
                "id": "verify",
                "name": "Verify",
                "type": "script",
                "config": {"image": verify.image, "command": verify.command},
                "timeout": verify.timeout,
                "on_success": "next",
                "on_failure": "stop",
            }
        )

    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name=adhoc_pipeline_name(TRIGGER_EXPERIMENT, cell.id),
        description=(
            "Ephemeral pipeline for experiment "
            f"{experiment.id[:8]} cell {cell.cell_index}. Hidden from "
            "GET /api/pipelines; its RUN is visible."
        ),
        steps=json.dumps(steps),
        steps_graph=None,
        triggers="[]",
        is_template=False,
    )
    db.add(pipeline)
    await db.commit()
    await db.refresh(pipeline)

    return await pipeline_executor.start_pipeline(
        db=db,
        pipeline=pipeline,
        repo=repo,
        # trigger_ref IS the link, written at run creation - see the module
        # docstring's synchronous-completion note.
        trigger_type=TRIGGER_EXPERIMENT,
        trigger_ref=cell.id,
        trigger_context={
            "branch": base_branch,
            "base_branch": base_branch,
            "work_branch": work_branch,
            "repo_id": repo.id,
            "adhoc": True,
            "experiment_id": experiment.id,
            "experiment_run_id": cell.id,
            "cell_index": cell.cell_index,
        },
    )


async def _exhaust_budget(
    db: AsyncSession, experiment: Experiment, spend: Decimal
) -> None:
    """The cap tripped: refuse every remaining pending cell IN ONE PASS.

    All of them, not one per completion — a matrix that dribbles out
    ``skipped_budget`` cells one at a time reads like progress.
    """
    result = await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.experiment_id == experiment.id,
            ExperimentRun.status == ExperimentRunStatus.PENDING.value,
        )
        .values(
            status=ExperimentRunStatus.SKIPPED_BUDGET.value,
            completed_at=datetime.utcnow(),
            error=(
                f"budget cap reached: observed spend {money(spend)} >= cap "
                f"{money(experiment.budget_usd)} before this cell was dispatched"
            ),
        )
    )
    await db.commit()
    logger.warning(
        "Experiment %s hit its $%s cap at $%s observed - %s pending cell(s) "
        "refused (skipped_budget)",
        experiment.id[:8],
        money(experiment.budget_usd),
        money(spend),
        result.rowcount,
    )


async def _maybe_finalize(db: AsyncSession, experiment: Experiment) -> None:
    """Finalize when no cell is pending and none is live.

    Guarded on ``completed_at``, not on the status: an ABORTED experiment is
    already 'terminal' by status while its running cells are still landing,
    and it still needs closing exactly once.
    """
    if experiment.completed_at is not None:
        return
    remaining = await _count_cells(
        db,
        experiment.id,
        frozenset({ExperimentRunStatus.PENDING.value}) | LIVE_CELL_STATUSES,
    )
    if remaining:
        await broadcast_experiment(db, experiment.id)
        return

    skipped = await _count_cells(
        db, experiment.id, frozenset({ExperimentRunStatus.SKIPPED_BUDGET.value})
    )
    if experiment.status == ExperimentStatus.ABORTED.value:
        final = ExperimentStatus.ABORTED.value
    elif skipped:
        final = ExperimentStatus.BUDGET_EXHAUSTED.value
    else:
        final = ExperimentStatus.COMPLETE.value

    spend = metrics.observed_spend(await fetch_usage_rows(db, experiment.id))
    overrun = spend - Decimal(str(experiment.budget_usd))
    experiment.status = final
    experiment.completed_at = datetime.utcnow()
    experiment.budget_overrun_usd = overrun if overrun > 0 else _ZERO
    await db.commit()
    if experiment.budget_overrun_usd > 0:
        logger.warning(
            "Experiment %s finished $%s OVER its cap: the cap bounds dispatch, "
            "and %s cell(s) were already in flight when it tripped",
            experiment.id[:8],
            money(experiment.budget_overrun_usd),
            experiment.max_concurrency,
        )
    await broadcast_experiment(db, experiment.id)


# =============================================================================
# Completion
# =============================================================================

async def on_cell_complete(
    db: AsyncSession, pipeline_run: PipelineRun, success: bool
) -> None:
    """Land one cell from its finished run, then pump the next.

    Called from ``agent_run.on_run_complete``'s dispatch (integrator
    registration 12.4). NEVER raises: an exception here would abandon the
    pump and stall the matrix, and the run itself is already terminal.
    Idempotent — a cell that is already terminal is left alone.
    """
    try:
        cell_id = getattr(pipeline_run, "trigger_ref", None)
        if not cell_id:
            return
        cell = await db.get(ExperimentRun, cell_id)
        if cell is None or cell.status in TERMINAL_CELL_STATUSES:
            return

        cell.pipeline_run_id = cell.pipeline_run_id or pipeline_run.id
        cell.status = await classify_cell(db, pipeline_run, success)
        cell.completed_at = datetime.utcnow()
        if cell.status == ExperimentRunStatus.ERROR.value and not cell.error:
            cell.error = (
                "the run failed with no test result tied back to it - nothing "
                "was measured, so this cell is an error, not a 0% score"
            )
        await db.commit()
        await broadcast_cell(db, cell.id)
        await pump(db, cell.experiment_id)
    except Exception:
        logger.exception(
            "Experiment cell completion handling failed for run %s; the run "
            "itself is already terminal",
            getattr(pipeline_run, "id", "?")[:8],
        )


async def classify_cell(
    db: AsyncSession, pipeline_run: PipelineRun, success: bool
) -> str:
    """Cell outcome, from PERSISTED evidence only.

    | run outcome | TestRun rows | cell     |
    |-------------|--------------|----------|
    | passed      | any / none   | passed   |
    | failed      | >= 1         | failed   |
    | failed      | zero         | error    |
    | cancelled   | any          | cancelled|

    No string matching on error messages, no heuristics. "The suite was red"
    and "nothing was ever measured" are different facts, and only the first
    belongs in a pass-rate denominator.
    """
    if getattr(pipeline_run, "status", None) == RunStatus.CANCELLED.value:
        return ExperimentRunStatus.CANCELLED.value
    if success:
        return ExperimentRunStatus.PASSED.value
    measured = (
        await db.execute(
            select(func.count())
            .select_from(TestRun)
            .where(TestRun.pipeline_run_id == pipeline_run.id)
        )
    ).scalar_one()
    return (
        ExperimentRunStatus.FAILED.value
        if measured
        else ExperimentRunStatus.ERROR.value
    )


# =============================================================================
# Abort / resume
# =============================================================================

async def abort(db: AsyncSession, experiment: Experiment) -> tuple[int, int]:
    """Cancel every pending cell; LEAVE RUNNING CELLS TO FINISH.

    Their results still land and still count — work already paid for is
    measurement, and throwing it away would be the expensive kind of tidy.
    The last completion finalizes the experiment.
    """
    experiment.status = ExperimentStatus.ABORTED.value
    result = await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.experiment_id == experiment.id,
            ExperimentRun.status == ExperimentRunStatus.PENDING.value,
        )
        .values(
            status=ExperimentRunStatus.CANCELLED.value,
            completed_at=datetime.utcnow(),
        )
    )
    await db.commit()
    still_running = await _count_cells(db, experiment.id, LIVE_CELL_STATUSES)
    if not still_running:
        await _maybe_finalize(db, experiment)
    await broadcast_experiment(db, experiment.id)
    return result.rowcount, still_running


async def is_stalled(db: AsyncSession, experiment: Experiment) -> bool:
    """Running, nothing live, work left: the pump died with a restart."""
    if experiment.status != ExperimentStatus.RUNNING.value:
        return False
    live = await _count_cells(db, experiment.id, LIVE_CELL_STATUSES)
    if live:
        return False
    pending = await _count_cells(
        db, experiment.id, frozenset({ExperimentRunStatus.PENDING.value})
    )
    return pending > 0


async def resume(db: AsyncSession, experiment: Experiment) -> tuple[int, int]:
    """Reconcile orphaned cells and re-pump. Returns ``(dispatched, reset)``.

    A cell left ``dispatching`` with no run never started (the run row would
    exist otherwise), so it goes back to ``pending``. A live cell whose run
    is already terminal is classified from that run — the completion hook
    fired into a process that is gone.
    """
    reset = (
        await db.execute(
            update(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.status == ExperimentRunStatus.DISPATCHING.value,
                ExperimentRun.pipeline_run_id.is_(None),
            )
            .values(status=ExperimentRunStatus.PENDING.value, started_at=None)
        )
    ).rowcount
    await db.commit()

    orphans = list(
        (
            await db.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status.in_(tuple(LIVE_CELL_STATUSES)),
                    ExperimentRun.pipeline_run_id.is_not(None),
                )
            )
        ).scalars()
    )
    for cell in orphans:
        run = await db.get(PipelineRun, cell.pipeline_run_id)
        if run is None or run.status in (
            RunStatus.PENDING.value,
            RunStatus.RUNNING.value,
        ):
            continue
        cell.status = await classify_cell(
            db, run, run.status == RunStatus.PASSED.value
        )
        cell.completed_at = cell.completed_at or datetime.utcnow()
    if orphans:
        await db.commit()

    dispatched = await pump(db, experiment.id)
    return dispatched, reset


async def resume_stalled_experiments() -> int:
    """Optional lifespan sweep: re-pump every experiment a restart stalled.

    Opens its own session (it runs before request handling). The
    ``/resume`` endpoint is the guaranteed path; this only makes restarts
    self-heal. Never raises — a failed sweep must not stop the app booting.
    """
    resumed = 0
    try:
        from app.database import async_session

        async with async_session() as db:
            running = list(
                (
                    await db.execute(
                        select(Experiment).where(
                            Experiment.status == ExperimentStatus.RUNNING.value
                        )
                    )
                ).scalars()
            )
            for experiment in running:
                dispatched, reset = await resume(db, experiment)
                logger.info(
                    "Resumed experiment %s after restart: %s cell(s) reset, "
                    "%s dispatched",
                    experiment.id[:8],
                    reset,
                    dispatched,
                )
                resumed += 1
    except Exception:
        logger.exception("resume_stalled_experiments failed; /resume still works")
    return resumed


# =============================================================================
# Aggregation reads
# =============================================================================

async def fetch_cell_rows(
    db: AsyncSession, experiment_id: str
) -> list[metrics.CellRow]:
    rows = (
        await db.execute(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.cell_index.asc())
        )
    ).scalars()
    return [
        metrics.CellRow(
            id=row.id,
            variant_index=row.variant_index,
            status=row.status,
            agent=row.agent,
            model=row.model,
            prompt_template_id=row.prompt_template_id,
            prompt_version=row.prompt_version,
            label=row.label,
        )
        for row in rows
    ]


async def fetch_outcome_rows(
    db: AsyncSession, experiment_id: str
) -> list[metrics.OutcomeRow]:
    """TestRuns of MEASURED cells, joined to their criterion.

    ``error`` / ``cancelled`` / ``skipped_budget`` cells are excluded HERE, in
    the query, so no downstream caller can accidentally count a cell that
    measured nothing.
    """
    rows = (
        await db.execute(
            select(
                ExperimentRun.variant_index,
                TestRef.criterion_id,
                TestRun.status,
                AcceptanceCriterion.text,
            )
            .select_from(TestRun)
            .join(ExperimentRun, ExperimentRun.id == TestRun.experiment_run_id)
            .join(TestRef, TestRef.id == TestRun.test_ref_id)
            .join(
                AcceptanceCriterion,
                AcceptanceCriterion.id == TestRef.criterion_id,
                isouter=True,
            )
            .where(
                ExperimentRun.experiment_id == experiment_id,
                ExperimentRun.status.in_(tuple(MEASURED_CELL_STATUSES)),
            )
        )
    ).all()
    return [
        metrics.OutcomeRow(
            variant_index=row[0],
            criterion_id=row[1],
            status=row[2],
            criterion_text=row[3],
        )
        for row in rows
    ]


async def fetch_usage_rows(
    db: AsyncSession, experiment_id: str
) -> list[metrics.UsageRow]:
    """StepUsage rows of every cell's run.

    Joined on ``step_usages.pipeline_run_id``, whose index already exists
    (``ix_step_usages_pipeline_run_id_role``, leading column). Dollars are
    summed by the caller in Python over ``Decimal`` — never with SQL
    ``SUM()``, which SQLite returns as a float.
    """
    rows = (
        await db.execute(
            select(
                ExperimentRun.id,
                ExperimentRun.variant_index,
                StepUsage.cost_usd,
                StepUsage.cost_source,
                StepUsage.wall_clock_ms,
                StepUsage.input_tokens,
                StepUsage.output_tokens,
            )
            .select_from(ExperimentRun)
            .join(StepUsage, StepUsage.pipeline_run_id == ExperimentRun.pipeline_run_id)
            .where(ExperimentRun.experiment_id == experiment_id)
        )
    ).all()
    return [
        metrics.UsageRow(
            cell_id=row[0],
            variant_index=row[1],
            cost_usd=Decimal(str(row[2])) if row[2] is not None else None,
            cost_source=row[3],
            wall_clock_ms=row[4],
            input_tokens=row[5],
            output_tokens=row[6],
        )
        for row in rows
    ]


async def fetch_cell_test_counts(
    db: AsyncSession, experiment_id: str
) -> dict[str, dict[str, int]]:
    """Per-cell passed/failed/skipped counts, for the matrix view."""
    rows = (
        await db.execute(
            select(TestRun.experiment_run_id, TestRun.status, func.count())
            .select_from(TestRun)
            .join(ExperimentRun, ExperimentRun.id == TestRun.experiment_run_id)
            .where(ExperimentRun.experiment_id == experiment_id)
            .group_by(TestRun.experiment_run_id, TestRun.status)
        )
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for cell_id, status, count in rows:
        bucket = counts.setdefault(
            cell_id, {"passed": 0, "failed": 0, "skipped": 0}
        )
        if status in bucket:
            bucket[status] += count
    return counts


async def experiment_progress(
    db: AsyncSession, experiment_id: str
) -> tuple[dict[str, int], Decimal, float | None]:
    """``(by_status, spend, cost_coverage)`` — computed, never materialized."""
    rows = (
        await db.execute(
            select(ExperimentRun.status, func.count())
            .where(ExperimentRun.experiment_id == experiment_id)
            .group_by(ExperimentRun.status)
        )
    ).all()
    by_status = {status: count for status, count in rows}
    usages = await fetch_usage_rows(db, experiment_id)
    return by_status, metrics.observed_spend(usages), metrics.cost_coverage(usages)


# =============================================================================
# WS frames (broadcast directly - no edit to websocket.py)
# =============================================================================

async def broadcast_experiment(db: AsyncSession, experiment_id: str) -> None:
    try:
        experiment = await db.get(Experiment, experiment_id)
        if experiment is None:
            return
        by_status, spend, coverage = await experiment_progress(db, experiment_id)
        await manager.broadcast(
            "experiment_status",
            {
                "id": experiment.id,
                "name": experiment.name,
                "status": experiment.status,
                "cells_total": sum(by_status.values()),
                "by_status": by_status,
                "spend_usd": money(spend) or "0.000000",
                "budget_usd": money(experiment.budget_usd) or "0.000000",
                "cost_coverage": coverage,
                "stalled": await is_stalled(db, experiment),
            },
        )
    except Exception:
        logger.exception("experiment_status broadcast failed for %s", experiment_id[:8])


async def broadcast_cell(db: AsyncSession, cell_id: str) -> None:
    try:
        cell = await db.get(ExperimentRun, cell_id)
        if cell is None:
            return
        await manager.broadcast(
            "experiment_cell_status",
            {
                "id": cell.id,
                "experiment_id": cell.experiment_id,
                "cell_index": cell.cell_index,
                "variant_index": cell.variant_index,
                "status": cell.status,
                "pipeline_run_id": cell.pipeline_run_id,
                "label": cell.label,
                "agent": cell.agent,
                "model": cell.model,
                "prompt_template_id": cell.prompt_template_id,
                "prompt_version": cell.prompt_version,
            },
        )
    except Exception:
        logger.exception("experiment_cell_status broadcast failed for %s", cell_id[:8])
