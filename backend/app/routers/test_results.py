"""
Test tie-back APIs (Phase 12.2.6, pinned contract #6).

- GET  /api/test-refs — the registered test set, filterable by repo,
  criterion and status. Repo-scoped by identity (contract #1: a TestRef is
  identified by the PAIR (repo_id, lazyaf_test_id)), so results are ordered
  by that pair and every row carries its repo_id.
- GET  /api/criteria/{criterion_id}/history — the TestRun series for a
  criterion, joined via TestRef.criterion_id, newest first.
- POST /api/test-refs/reconcile — repo-scoped upsert of the declared test
  set: listed refs become/stay active (file_path refreshed), previously
  active refs for that repo that are absent from the list flip to orphan.

The step-token-authenticated ingestion endpoint lives in routers/steps.py
(it shares the step auth/terminal-409 machinery); this module is the
operator/UI surface.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AcceptanceCriterion, Repo, TestRef, TestRefStatus, TestRun
from app.services.test_ingestion import normalize_repo_relative_path
from app.schemas.testref import (
    CriterionHistoryEntry,
    TestRefRead,
    ReconcileRequest,
    ReconcileResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["test-tieback"])


@router.get("/api/test-refs", response_model=list[TestRefRead])
async def list_test_refs(
    repo_id: str | None = Query(None, description="Filter to one repo"),
    criterion_id: str | None = Query(
        None, description="Filter to refs linked to this acceptance criterion"
    ),
    status: str | None = Query(None, description="Filter by status: active | orphan"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List registered TestRefs.

    The read side of the tie-back registry — what the MCP `list_test_refs`
    tool and the UI call to answer "which tests does LazyAF know about, and
    which criterion does each one measure?".

    Repo scoping (pinned contract #1): a TestRef's identity is the pair
    (repo_id, lazyaf_test_id), so `repo_id` is the primary filter and the
    ordering is (repo_id, lazyaf_test_id) — stable, and the same marker
    string appearing under two repos lists as two distinct rows rather than
    looking like a duplicate.

    An unknown `repo_id` returns 404 (a silent empty list would read as "this
    repo declares no tests", which is a very different fact); an unknown
    `status` value is a 422-shaped 400 naming the vocabulary.
    """
    if status is not None and status not in [s.value for s in TestRefStatus]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid status {status!r}: valid values are "
                + ", ".join(s.value for s in TestRefStatus)
            ),
        )

    if repo_id is not None:
        repo = (
            await db.execute(select(Repo.id).where(Repo.id == repo_id))
        ).scalar_one_or_none()
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")

    query = select(TestRef)
    if repo_id is not None:
        query = query.where(TestRef.repo_id == repo_id)
    if criterion_id is not None:
        query = query.where(TestRef.criterion_id == criterion_id)
    if status is not None:
        query = query.where(TestRef.status == status)

    query = (
        query.order_by(TestRef.repo_id.asc(), TestRef.lazyaf_test_id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list((await db.execute(query)).scalars())


@router.get(
    "/api/criteria/{criterion_id}/history",
    response_model=list[CriterionHistoryEntry],
)
async def criterion_history(
    criterion_id: str,
    limit: int = Query(50, ge=1, le=500),
    branch: str | None = Query(None, description="Filter runs by branch"),
    db: AsyncSession = Depends(get_db),
):
    """TestRun series for a criterion, newest first.

    Joined TestRun -> TestRef on the indexed criterion_id/test_ref_id
    columns; ordered by created_at (id tiebreak for determinism).
    """
    criterion = (
        await db.execute(
            select(AcceptanceCriterion.id).where(AcceptanceCriterion.id == criterion_id)
        )
    ).scalar_one_or_none()
    if criterion is None:
        raise HTTPException(status_code=404, detail="Criterion not found")

    query = (
        select(TestRun, TestRef.lazyaf_test_id)
        .join(TestRef, TestRef.id == TestRun.test_ref_id)
        .where(TestRef.criterion_id == criterion_id)
        .order_by(TestRun.created_at.desc(), TestRun.id.desc())
        .limit(limit)
    )
    if branch is not None:
        query = query.where(TestRun.branch == branch)

    rows = (await db.execute(query)).all()
    return [
        CriterionHistoryEntry(
            id=run.id,
            test_ref_id=run.test_ref_id,
            lazyaf_test_id=lazyaf_test_id,
            pipeline_run_id=run.pipeline_run_id,
            step_run_id=run.step_run_id,
            commit_sha=run.commit_sha,
            branch=run.branch,
            status=run.status,
            duration_ms=run.duration_ms,
            model=run.model,
            prompt_template_id=run.prompt_template_id,
            created_at=run.created_at,
        )
        for run, lazyaf_test_id in rows
    ]


@router.post("/api/test-refs/reconcile", response_model=ReconcileResponse)
async def reconcile_test_refs(
    request: ReconcileRequest,
    db: AsyncSession = Depends(get_db),
):
    """Reconcile a repo's declared test set against the registry.

    - Listed refs are upserted to ACTIVE with the given file_path.
    - TestRefs previously ACTIVE for this repo but absent from the list flip
      to ORPHAN.

    Repo-scoped by identity (contract #1): lookups filter on
    (repo_id, lazyaf_test_id), so another repo declaring the same marker
    string is neither read nor re-homed here — a listed id that exists only
    under a different repo is CREATED fresh under this one. This is the whole
    point of the composite identity: reconciling repo A must never move,
    relabel or orphan repo B's registrations.

    file_path follows the repo-root-relative convention (contract #3);
    anything else is refused rather than written over a good path.
    """
    repo = (
        await db.execute(select(Repo.id).where(Repo.id == request.repo_id))
    ).scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    now = datetime.utcnow()
    counts = ReconcileResponse(created=0, updated=0, orphaned=0)

    listed_ids = {item.lazyaf_test_id for item in request.refs}

    # This repo's refs, and only this repo's: the set the orphan flip walks
    # AND the set the upsert resolves against (contract #1).
    repo_refs = {
        ref.lazyaf_test_id: ref
        for ref in (
            await db.execute(select(TestRef).where(TestRef.repo_id == request.repo_id))
        ).scalars()
    }

    for item in request.refs:
        file_path = normalize_repo_relative_path(item.file_path)
        if item.file_path and file_path is None:
            logger.warning(
                "Ignoring non-repo-root-relative file_path %r for lazyaf_test_id %r "
                "(contract #3: paths are repo-root-relative)",
                item.file_path,
                item.lazyaf_test_id,
            )

        ref = repo_refs.get(item.lazyaf_test_id)
        if ref is None:
            # Staged in repo_refs too: a list that repeats an id must not
            # try to insert it twice against the composite unique.
            ref = TestRef(
                lazyaf_test_id=item.lazyaf_test_id,
                repo_id=request.repo_id,
                file_path=file_path,
                status=TestRefStatus.ACTIVE.value,
                updated_at=now,
            )
            db.add(ref)
            repo_refs[item.lazyaf_test_id] = ref
            counts.created += 1
        else:
            if file_path is not None:
                ref.file_path = file_path
            ref.status = TestRefStatus.ACTIVE.value
            ref.updated_at = now
            counts.updated += 1

    for lazyaf_test_id, ref in repo_refs.items():
        if lazyaf_test_id in listed_ids:
            continue
        if ref.status == TestRefStatus.ACTIVE.value:
            ref.status = TestRefStatus.ORPHAN.value
            ref.updated_at = now
            counts.orphaned += 1
            logger.info(
                "TestRef %r orphaned by reconcile (absent from repo %s's declared set)",
                lazyaf_test_id,
                request.repo_id,
            )

    await db.commit()
    return counts
