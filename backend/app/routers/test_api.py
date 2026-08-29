"""
Test-mode API - env-gated reset/seed endpoints for deterministic e2e state.

Only mounted when LAZYAF_TEST_MODE is truthy (see main.py); every request
additionally re-checks the flag as defense in depth, so a stray registration
cannot expose these endpoints.

Reset clears the database AND the in-memory singletons (job queue, runner
pool, WS connections, playground sessions, trigger-dedup cache). The
failure_01 post-mortem: a DB-only reset leaves in-memory state pointing at
deleted rows.

SECURITY: never enable in production.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import Base, get_db
from app.models import Card, Pipeline, Repo
from app.models.card import CardStatus, StepType
from app.services.git_server import git_repo_manager
from app.services.job_queue import job_queue
from app.services.pipeline_executor import pipeline_executor
from app.services.playground_service import playground_service
from app.services.runner_pool import runner_pool
from app.services.trigger_service import reset_trigger_dedup
from app.services.websocket import manager
from app.services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Resettable singleton registry
#
# Every in-memory singleton that must be wiped alongside the DB registers an
# async reset hook here. The reset endpoint iterates the registry, so the
# response's memory_reset list derives from the registered names - there is
# no hand-maintained list to drift out of sync (the failure_01 decay mode).
# -----------------------------------------------------------------------------

_RESETTABLES: dict[str, Callable[[], Awaitable[object]]] = {}


def register_resettable(name: str, async_fn: Callable[[], Awaitable[object]]) -> None:
    """Register an async callable that resets one in-memory singleton.

    Called at import for the known singletons below; any new singleton with
    process-local state MUST register here (or leak stale state into e2e runs).
    """
    _RESETTABLES[name] = async_fn


async def _reset_runner_pool() -> None:
    runner_pool.reset()


async def _reset_trigger_dedup() -> None:
    reset_trigger_dedup()


async def _reset_workspace_service() -> None:
    workspace_service.reset()


register_resettable("job_queue", job_queue.clear)
register_resettable("runner_pool", _reset_runner_pool)
register_resettable("websocket_manager", manager.reset)
register_resettable("playground_sessions", playground_service.reset)
register_resettable("trigger_dedup", _reset_trigger_dedup)
# Phase 12.2-INT: per-run task registry/state machines/locks + LocalExecutor
# idempotency cache, and the workspace lock manager - all point at rows the
# DB reset deletes.
register_resettable("pipeline_executor", pipeline_executor.reset)
register_resettable("workspace_service", _reset_workspace_service)


def require_test_mode():
    if not get_settings().test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true",
        )


router = APIRouter(
    prefix="/api/test",
    tags=["test"],
    dependencies=[Depends(require_test_mode)],
)


class ResetResponse(BaseModel):
    success: bool
    tables_cleared: list[str]
    memory_reset: list[str]


class SeededRepo(BaseModel):
    id: str
    name: str
    default_branch: str
    git_initialized: bool


class SeededPipeline(BaseModel):
    id: str
    name: str


class SeededCard(BaseModel):
    id: str
    title: str
    status: str


class SeedResponse(BaseModel):
    success: bool
    repo: SeededRepo
    pipeline: SeededPipeline
    cards: list[SeededCard]


def _delete_git_storage(repo_ids: list[str]) -> None:
    """Blocking rmtree loop - run via run_in_threadpool, never on the loop."""
    for repo_id in repo_ids:
        try:
            git_repo_manager.delete_repo(repo_id)
        except Exception as e:
            logger.warning(f"Could not delete git storage for repo {repo_id}: {e}")


def _clear_all_tables(sync_session: Session) -> list[str]:
    """Delete every row from every table in one sync batch.

    sorted_tables is dependency-ordered (parents first); delete children
    first. Schema growth is picked up automatically - no hardcoded list.
    Running the whole loop inside one run_sync bridge issues the deletes
    as a single batch instead of a per-table async round trip each.
    """
    tables_cleared = []
    for table in reversed(Base.metadata.sorted_tables):
        sync_session.execute(table.delete())
        tables_cleared.append(table.name)
    return tables_cleared


@router.post("/reset", response_model=ResetResponse)
async def reset_state(db: AsyncSession = Depends(get_db)):
    """
    Wipe all DB tables (order-aware) and reset every in-memory singleton.

    Call between e2e tests for a deterministic starting state.
    """
    # Remove internal git storage for known repos before their rows go away.
    # rmtree is blocking I/O - keep it off the event loop.
    repo_ids = (await db.execute(select(Repo.id))).scalars().all()
    await run_in_threadpool(_delete_git_storage, list(repo_ids))

    try:
        tables_cleared = await db.run_sync(_clear_all_tables)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")

    # In-memory singletons - MUST stay in sync with the emptied DB. The
    # registry is the single source: memory_reset derives from its keys.
    memory_reset = []
    for name, reset_fn in _RESETTABLES.items():
        await reset_fn()
        memory_reset.append(name)

    return ResetResponse(
        success=True,
        tables_cleared=tables_cleared,
        memory_reset=memory_reset,
    )


def _init_seed_git_repo(repo_id: str, default_branch: str) -> bool:
    """
    Create the internal bare repo with one deterministic initial commit
    (fixed author, epoch timestamps - the commit sha is stable across seeds).
    In-process via dulwich, so it is cheap; failure is non-fatal (seed data
    remains usable for non-git flows).
    """
    try:
        from dulwich.objects import Blob, Commit, Tree
        from dulwich.repo import Repo as DulwichRepo

        if not git_repo_manager.repo_exists(repo_id):
            git_repo_manager.create_bare_repo(repo_id)

        dulwich_repo = DulwichRepo(str(git_repo_manager.get_repo_path(repo_id)))

        blob = Blob.from_string(b"# Seed Repository\n\nCreated by the test-mode API.\n")
        dulwich_repo.object_store.add_object(blob)

        tree = Tree()
        tree.add(b"README.md", 0o100644, blob.id)
        dulwich_repo.object_store.add_object(tree)

        commit = Commit()
        commit.tree = tree.id
        commit.author = commit.committer = b"LazyAF Test <test@lazyaf.local>"
        commit.author_time = commit.commit_time = 0
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = b"Seed commit"
        dulwich_repo.object_store.add_object(commit)

        branch_ref = f"refs/heads/{default_branch}".encode()
        dulwich_repo.refs[branch_ref] = commit.id
        dulwich_repo.refs.set_symbolic_ref(b"HEAD", branch_ref)
        return True
    except Exception as e:
        logger.warning(f"Seed git init failed for repo {repo_id}: {e}")
        return False


@router.post("/seed", response_model=SeedResponse)
async def seed_state(db: AsyncSession = Depends(get_db)):
    """
    Create minimal deterministic fixtures: one ingested repo (with an
    initialized internal git repo), one script pipeline, two cards.
    Returns the created ids. Call after /reset.
    """
    repo = Repo(
        id=str(uuid4()),
        name="e2e-seed-repo",
        default_branch="main",
        is_ingested=True,
    )
    db.add(repo)

    git_initialized = _init_seed_git_repo(repo.id, repo.default_branch)

    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name="e2e-seed-pipeline",
        description="Seed pipeline (test-mode API)",
        steps=json.dumps([
            {
                "name": "Echo",
                "type": "script",
                "config": {"command": "echo seed-pipeline-ran"},
            }
        ]),
        triggers="[]",
        is_template=False,
    )
    db.add(pipeline)

    card_todo = Card(
        id=str(uuid4()),
        repo_id=repo.id,
        title="Seed card (todo)",
        description="Deterministic seed card ready to start",
        status=CardStatus.TODO.value,
        step_type=StepType.SCRIPT.value,
        step_config=json.dumps({"command": "echo seed-card-ran"}),
    )
    card_review = Card(
        id=str(uuid4()),
        repo_id=repo.id,
        title="Seed card (in review)",
        description="Deterministic seed card awaiting review",
        status=CardStatus.IN_REVIEW.value,
        step_type=StepType.SCRIPT.value,
        step_config=json.dumps({"command": "echo seed-card-ran"}),
        branch_name="lazyaf/seed-review",
    )
    db.add(card_todo)
    db.add(card_review)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Seeding failed: {e}")

    return SeedResponse(
        success=True,
        repo=SeededRepo(
            id=repo.id,
            name=repo.name,
            default_branch=repo.default_branch,
            git_initialized=git_initialized,
        ),
        pipeline=SeededPipeline(id=pipeline.id, name=pipeline.name),
        cards=[
            SeededCard(id=card_todo.id, title=card_todo.title, status=card_todo.status),
            SeededCard(id=card_review.id, title=card_review.title, status=card_review.status),
        ],
    )
