"""
Test-result manifest ingestion (Phase 12.2.6).

Turns a step's test-results manifest (pinned contract #1, shipped by the
control runtime per contract #2) into TestRef/TestRun rows joined to the
StepExecution -> StepRun -> PipelineRun -> Pipeline chain: repo_id comes
from the pipeline, commit/branch from PipelineRun.trigger_context.

Repo scoping (contract #1): TestRef identity is the PAIR
(repo_id, lazyaf_test_id). Every lookup here filters by the run's repo, so
two repos declaring the same marker string keep independent refs, runs and
criterion links — one repo's green can never satisfy another repo's gate.

Duplicate ids: a manifest may legitimately carry the same lazyaf_test_id
more than once (several parametrized cases sharing one marker, a rerun
inside the same step). Those entries are AGGREGATED, never last-wins:
failed beats error beats passed beats skipped, so a red case can never be
papered over by a green sibling arriving later in the file.

Idempotency: one TestRun per (step_run_id, test_ref_id). Re-POSTing the same
step's manifest (control-runtime retry, duplicate ship) UPDATES the existing
row instead of duplicating it. New refs are collected and flushed ONCE; a
concurrent manifest racing the same (repo_id, lazyaf_test_id) insert is
absorbed with the codebase's rollback/re-select idiom
(app/services/execution/idempotency.py) instead of 500ing away a whole
manifest.

Unknown lazyaf_test_ids auto-create ORPHAN TestRefs (logged): results are
never dropped on the floor, and `POST /api/test-refs/reconcile` (or a later
criterion link) promotes the ref to active.

Writes are content-addressed: a ref whose stored content still matches the
manifest is NOT re-stamped (no updated_at churn, no UPDATE per result row).
updated_at therefore means "last time this registration changed" — the
declared-set freshness signal is reconcile's job, not ingestion's.

file_path (contract #3) is REPO-ROOT-relative. A manifest path that is not
(absolute, drive-lettered, parent-escaping) is refused and logged rather
than overwriting a seeded repo-root-relative path with a worse one.

Experiment context (Phase 12.6.5) is DERIVED HERE, never trusted from the
wire. When the run's PERSISTED trigger says `experiment`, the cell row it
points at is the authority for which variant produced these results, and its
coordinates (experiment_run_id / model / prompt_template_id / prompt_version)
are stamped onto every TestRun of the step. The manifest schema
(`TestResultsManifest`) is UNCHANGED — the frozen control-layer protocol
stays frozen, and a container cannot mislabel which variant it was. This is
the same wire-vs-server split `usage_ingestion` states in its own docstring
("step_run_id, pipeline_run_id | HERE — never trusted from the wire").
Non-experiment runs stamp NULL, which is the true value, not a hole.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, PipelineRun, StepExecution, StepRun, TestRef, TestRefStatus, TestRun
# TRIGGER_EXPERIMENT lives on models.experiment, the leaf module both this
# path and the experiment service import, so the two sides of the cell -> run
# link cannot fork (R3).
from app.models.experiment import TRIGGER_EXPERIMENT, ExperimentRun
from app.schemas.testref import TestResultEntry, TestResultsManifest

logger = logging.getLogger(__name__)

# Aggregation precedence for duplicate ids: LOWEST rank wins. "error" is not
# in today's manifest vocabulary (the schema pins passed|failed|skipped) but
# is ranked here so widening the vocabulary cannot silently turn an error
# into a pass.
_STATUS_RANK = {"failed": 0, "error": 1, "passed": 2, "skipped": 3}
_UNKNOWN_STATUS_RANK = 1  # an unrecognised status is at least as bad as error

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


@dataclass
class IngestCounts:
    results_received: int = 0
    # Distinct lazyaf_test_ids after duplicate aggregation (<= received).
    results_aggregated: int = 0
    test_runs_created: int = 0
    test_runs_updated: int = 0
    orphan_refs_created: int = 0


@dataclass
class _RunContext:
    step_run_id: str
    pipeline_run_id: str
    repo_id: str
    commit_sha: str
    branch: str | None
    # Experiment coordinates (12.6.5). All four are NULL unless the run's
    # PERSISTED trigger says this run IS an experiment cell — see the module
    # docstring on why they are derived here and not read off the manifest.
    experiment_run_id: str | None = None
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None


@dataclass
class _AggregatedResult:
    """All manifest entries for one lazyaf_test_id, collapsed."""
    lazyaf_test_id: str
    status: str
    duration_ms: int | None = None
    file_path: str | None = None
    entries: int = 1


def normalize_repo_relative_path(path: str | None) -> str | None:
    """Contract #3: file paths are repo-root-relative POSIX paths.

    Returns the normalized path, or None when the input is absent or not
    repo-root-relative (absolute, Windows-drive-rooted, or escaping the repo
    root with '..'). Returning None is what keeps a seeded repo-root-relative
    path from being overwritten by a differently-rooted one.
    """
    if not path:
        return None
    candidate = path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or _DRIVE_PREFIX.match(candidate):
        return None
    parts = [part for part in candidate.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _aggregate_results(results: list[TestResultEntry]) -> list[_AggregatedResult]:
    """Collapse duplicate lazyaf_test_ids deterministically (R4 fake green).

    Status is the WORST of the entries (failed > error > passed > skipped);
    durations sum (total time attributed to the marker); the file_path is the
    first repo-root-relative one seen. First-appearance order is preserved so
    the ingestion order is stable for a given manifest.
    """
    aggregated: dict[str, _AggregatedResult] = {}
    for entry in results:
        file_path = normalize_repo_relative_path(entry.file_path)
        if entry.file_path and file_path is None:
            logger.warning(
                "Ignoring non-repo-root-relative file_path %r for lazyaf_test_id %r "
                "(contract #3: paths are repo-root-relative)",
                entry.file_path,
                entry.lazyaf_test_id,
            )
        current = aggregated.get(entry.lazyaf_test_id)
        if current is None:
            aggregated[entry.lazyaf_test_id] = _AggregatedResult(
                lazyaf_test_id=entry.lazyaf_test_id,
                status=entry.status,
                duration_ms=entry.duration_ms,
                file_path=file_path,
            )
            continue

        current.entries += 1
        if _rank(entry.status) < _rank(current.status):
            current.status = entry.status
        if entry.duration_ms is not None:
            current.duration_ms = (current.duration_ms or 0) + entry.duration_ms
        if current.file_path is None:
            current.file_path = file_path

    return list(aggregated.values())


def _rank(status: str) -> int:
    return _STATUS_RANK.get(status, _UNKNOWN_STATUS_RANK)


async def _resolve_run_context(db: AsyncSession, execution: StepExecution) -> _RunContext:
    """Walk StepExecution -> StepRun -> PipelineRun -> Pipeline for provenance."""
    row = (
        await db.execute(
            select(
                StepRun.id,
                StepRun.pipeline_run_id,
                Pipeline.repo_id,
                PipelineRun.trigger_context,
                # The DURABLE cell link (12.6.5): trigger_type/trigger_ref are
                # written at run CREATION, so they are already true even when
                # a step finishes in under 100 ms.
                PipelineRun.trigger_type,
                PipelineRun.trigger_ref,
            )
            .join(PipelineRun, PipelineRun.id == StepRun.pipeline_run_id)
            .join(Pipeline, Pipeline.id == PipelineRun.pipeline_id)
            .where(StepRun.id == execution.step_run_id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError(f"Step run {execution.step_run_id} not found for execution {execution.id}")

    context: dict = {}
    if row.trigger_context:
        try:
            context = json.loads(row.trigger_context) or {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Unparseable trigger_context on pipeline run %s", row.pipeline_run_id)

    ctx = _RunContext(
        step_run_id=row.id,
        pipeline_run_id=row.pipeline_run_id,
        repo_id=row.repo_id,
        commit_sha=context.get("commit_sha") or context.get("sha") or "",
        branch=context.get("branch"),
    )

    # Experiment coordinates, from the CELL ROW (one indexed PK read), not
    # from the manifest. A cell that is gone leaves them NULL and logs — the
    # results are still ingested, because dropping measurements over a
    # missing label would be the worse failure.
    if row.trigger_type == TRIGGER_EXPERIMENT and row.trigger_ref:
        cell = await db.get(ExperimentRun, row.trigger_ref)
        if cell is None:
            logger.warning(
                "Run %s claims trigger_type=%r but experiment cell %s is gone "
                "— ingesting without experiment coordinates",
                row.pipeline_run_id,
                TRIGGER_EXPERIMENT,
                row.trigger_ref,
            )
        else:
            ctx.experiment_run_id = cell.id
            ctx.model = cell.model
            ctx.prompt_template_id = cell.prompt_template_id
            ctx.prompt_version = cell.prompt_version

    return ctx


async def _select_refs(
    db: AsyncSession, repo_id: str, test_ids: list[str]
) -> dict[str, TestRef]:
    """This repo's refs for the given ids, keyed by lazyaf_test_id.

    Repo-scoped by identity (contract #1): another repo's ref with the same
    marker string is invisible here and is never re-homed.
    """
    return {
        ref.lazyaf_test_id: ref
        for ref in (
            await db.execute(
                select(TestRef).where(
                    TestRef.repo_id == repo_id,
                    TestRef.lazyaf_test_id.in_(test_ids),
                )
            )
        ).scalars()
    }


def _add_orphan_refs(
    db: AsyncSession, ctx: _RunContext, items: list[_AggregatedResult]
) -> list[TestRef]:
    """Stage ORPHAN refs for unknown ids (no flush — the caller batches)."""
    created = []
    for item in items:
        ref = TestRef(
            lazyaf_test_id=item.lazyaf_test_id,
            repo_id=ctx.repo_id,
            file_path=item.file_path,
            status=TestRefStatus.ORPHAN.value,
        )
        db.add(ref)
        created.append(ref)
        logger.warning(
            "Auto-created ORPHAN TestRef for unknown lazyaf_test_id %r "
            "(repo %s, step run %s) — register it via /api/test-refs/reconcile",
            item.lazyaf_test_id,
            ctx.repo_id,
            ctx.step_run_id,
        )
    return created


async def _resolve_refs(
    db: AsyncSession,
    ctx: _RunContext,
    aggregated: list[_AggregatedResult],
    counts: IngestCounts,
) -> dict[str, TestRef]:
    """Repo-scoped get-or-create for every id in the manifest.

    ONE flush for the whole batch. A concurrent manifest that inserted the
    same (repo_id, lazyaf_test_id) between our SELECT and the flush raises
    IntegrityError; that is absorbed with the rollback/re-select idiom from
    app/services/execution/idempotency.py so the race costs a retry, never
    the manifest.
    """
    test_ids = [item.lazyaf_test_id for item in aggregated]
    refs_by_id = await _select_refs(db, ctx.repo_id, test_ids)

    missing = [item for item in aggregated if item.lazyaf_test_id not in refs_by_id]
    if not missing:
        return refs_by_id

    created = _add_orphan_refs(db, ctx, missing)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        logger.info(
            "Concurrent TestRef insert for repo %s (step run %s) — re-resolving %d refs",
            ctx.repo_id,
            ctx.step_run_id,
            len(missing),
        )
        refs_by_id = await _select_refs(db, ctx.repo_id, test_ids)
        created = _add_orphan_refs(
            db, ctx, [item for item in missing if item.lazyaf_test_id not in refs_by_id]
        )
        if created:
            await db.flush()

    counts.orphan_refs_created = len(created)
    for ref in created:
        refs_by_id[ref.lazyaf_test_id] = ref
    return refs_by_id


async def ingest_manifest(
    db: AsyncSession,
    execution: StepExecution,
    manifest: TestResultsManifest,
) -> IngestCounts:
    """Ingest one step's manifest. Commits once; safe to re-run (idempotent
    per (step_run, test_ref) — see module docstring)."""
    counts = IngestCounts(results_received=len(manifest.results))
    if not manifest.results:
        return counts

    ctx = await _resolve_run_context(db, execution)

    aggregated = _aggregate_results(manifest.results)
    counts.results_aggregated = len(aggregated)
    if counts.results_aggregated != counts.results_received:
        logger.info(
            "Manifest for step run %s carried %d results for %d distinct "
            "lazyaf_test_ids — duplicates aggregated (worst status wins)",
            ctx.step_run_id,
            counts.results_received,
            counts.results_aggregated,
        )

    refs_by_id = await _resolve_refs(db, ctx, aggregated, counts)

    # One query for this step run's existing TestRuns (idempotency key).
    existing_runs = {
        run.test_ref_id: run
        for run in (
            await db.execute(select(TestRun).where(TestRun.step_run_id == ctx.step_run_id))
        ).scalars()
    }

    for item in aggregated:
        ref = refs_by_id[item.lazyaf_test_id]

        # Only a valid repo-root-relative path, only when it actually moved:
        # unchanged refs are left alone (no updated_at churn).
        if item.file_path is not None and ref.file_path != item.file_path:
            ref.file_path = item.file_path

        run = existing_runs.get(ref.id)
        if run is not None:
            run.status = item.status
            run.duration_ms = item.duration_ms
            run.commit_sha = ctx.commit_sha
            run.branch = ctx.branch
            run.experiment_run_id = ctx.experiment_run_id
            run.model = ctx.model
            run.prompt_template_id = ctx.prompt_template_id
            run.prompt_version = ctx.prompt_version
            counts.test_runs_updated += 1
        else:
            run = TestRun(
                test_ref_id=ref.id,
                pipeline_run_id=ctx.pipeline_run_id,
                step_run_id=ctx.step_run_id,
                commit_sha=ctx.commit_sha,
                branch=ctx.branch,
                status=item.status,
                duration_ms=item.duration_ms,
                experiment_run_id=ctx.experiment_run_id,
                model=ctx.model,
                prompt_template_id=ctx.prompt_template_id,
                prompt_version=ctx.prompt_version,
            )
            db.add(run)
            existing_runs[ref.id] = run
            counts.test_runs_created += 1

    await db.commit()
    return counts
