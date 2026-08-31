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
from app.services.execution.runner_dispatcher import runner_dispatcher
from app.services.execution.runner_registry import runner_registry
from app.services.git_server import git_repo_manager
from app.services.pipeline_executor import pipeline_executor
from app.services.playground_service import playground_service
from app.services.trigger_service import reset_trigger_dedup
from app.services.websocket import manager
from app.services.workspace_service import workspace_service

logger = logging.getLogger(__name__)

# The branch the in-review seed card points at. One name, two users: the
# git ref created by _init_seed_git_repo and the card row that claims it.
# They drifted once (the ref was never created) and the seeded demo's first
# Approve was a red toast - so they share a constant now.
SEED_REVIEW_BRANCH = "lazyaf/seed-review"


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


async def _reset_trigger_dedup() -> None:
    reset_trigger_dedup()


async def _reset_workspace_service() -> None:
    workspace_service.reset()


# 12.6: the polling pool and its queue are gone; the registry and the
# dispatcher are what hold runner state in memory now, and BOTH must be wiped
# with the DB. The registry closes every live socket (a runner reconnects and
# re-enrols against the clean database), and the dispatcher drops its waiters,
# its in-flight assignments and its loop-bound wake event - a reset that left
# any of those behind would hand the next test an assignment for a step row
# that no longer exists.
register_resettable("runner_registry", runner_registry.reset)
register_resettable("runner_dispatcher", runner_dispatcher.reset)
register_resettable("websocket_manager", manager.reset)
register_resettable("playground_sessions", playground_service.reset)
register_resettable("trigger_dedup", _reset_trigger_dedup)
# Phase 12.2-INT: per-run task registry/state machines/locks + LocalExecutor
# idempotency cache, and the workspace lock manager - all point at rows the
# DB reset deletes.
register_resettable("pipeline_executor", pipeline_executor.reset)
register_resettable("workspace_service", _reset_workspace_service)
# 12.7: a paused debug gate is an in-process future and a sidecar is a live
# container - both outlive the DB reset unless they are wiped with it.
from app.services.execution.debug_session_service import debug_session_service
from app.services.execution.debug_terminal import debug_terminal_service

register_resettable("debug_sessions", debug_session_service.reset)
register_resettable("debug_terminals", debug_terminal_service.reset)

# M14: the capability probe keeps one asyncio.Lock per endpoint id so two
# probes of one endpoint cannot race. Those locks are LOOP-BOUND and keyed by
# rows the DB reset deletes - the failure_01 shape exactly - so they are wiped
# with the database like every other singleton.
from app.services.model_endpoints import probe as _endpoint_probe


async def _reset_model_endpoint_probes() -> None:
    # REQUESTED EDIT (owner: Agent A): `probe.py` should expose a public
    # `reset_probe_state()` and this should call it, the way
    # `runner_registry.reset` and `debug_terminal_service.reset` do. Reaching
    # into the module's private dict is the honest stopgap, not the shape this
    # should keep.
    _endpoint_probe._probe_locks.clear()


register_resettable("model_endpoint_probes", _reset_model_endpoint_probes)


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


class SeededEndpoint(BaseModel):
    id: str
    name: str
    base_url: str
    model: str
    probe_status: str


class SeedResponse(BaseModel):
    success: bool
    repo: SeededRepo
    pipeline: SeededPipeline
    cards: list[SeededCard]
    #: M14. Empty only if the ModelEndpoint table is not present.
    model_endpoints: list[SeededEndpoint] = []


# -----------------------------------------------------------------------------
# M14: the two mock model endpoints (wave8 s8.2)
#
# CI must not need a GPU, so the e2e lane gets the same two endpoints the
# dogfood pipeline uses, pointed at the `mock-endpoint` compose service. Both
# carry a real `rate_usd_hour`, which is what puts the 12.5 `gpu-node` pricing
# branch on a lane that runs continuously.
#
# The probe is BEST EFFORT here and deliberately so: a seed call must not fail
# because a mock server is not up yet, and an unprobed endpoint is not a silent
# downgrade - dispatch REFUSES it with a message naming the probe endpoint.
# `probe_status` is returned so the caller can see which it got.
# -----------------------------------------------------------------------------

DEFAULT_MOCK_ENDPOINT_URL = "http://mock-endpoint:8099"

SEED_MODEL_ENDPOINTS = [
    {
        "name": "dogfood-mock",
        "description": "M14 seed: tool-calling mock OpenAI server (happy_tools).",
        "scenario": "happy_tools",
        "model": "mock-model",
    },
    {
        "name": "dogfood-mock-notools",
        "description": (
            "M14 seed: a model that CANNOT tool-call (happy_text). Probes "
            "`degraded`, which is USABLE - it routes the fallback protocol."
        ),
        "scenario": "happy_text",
        "model": "mock-model-notools",
    },
]


def _apply_seed_endpoint_spec(endpoint, spec, mock_url, default_gpu_node_id) -> None:
    """Stamp a seed spec onto an endpoint row, new or existing.

    One definition of what a seeded endpoint IS, so an update and an insert
    cannot drift apart (R3).
    """
    from decimal import Decimal

    endpoint.description = spec["description"]
    endpoint.base_url = f"{mock_url}/{spec['scenario']}/v1"
    endpoint.model = spec["model"]
    endpoint.server_kind = "vllm"
    endpoint.auth_style = "none"
    endpoint.reach = "direct"
    endpoint.rate_usd_hour = Decimal("0.010000")
    endpoint.gpu_node_id = default_gpu_node_id(spec["name"])
    endpoint.max_concurrency = 1
    endpoint.request_timeout_seconds = 60
    endpoint.probe_status = "unprobed"
    endpoint.probe_detail = "{}"
    endpoint.consecutive_failures = 0
    endpoint.enabled = True


async def _seed_model_endpoints(db: AsyncSession) -> list[SeededEndpoint]:
    """Register (and best-effort probe) the two mock endpoints."""
    import os
    from decimal import Decimal

    from app.models.model_endpoint import ModelEndpoint, default_gpu_node_id
    from app.services.model_endpoints.probe import probe_endpoint

    mock_url = os.environ.get(
        "LAZYAF_MOCK_ENDPOINT_URL", DEFAULT_MOCK_ENDPOINT_URL
    ).rstrip("/")

    # Idempotent by name. ModelEndpoint carries a UNIQUE index on `name`, and
    # every other row this seeder writes is keyed by uuid4() - so a second
    # /seed used to succeed for the repo, the pipeline and both cards and then
    # explode on this one table. "Produce this known state" is a naturally
    # idempotent job; matches upsert_materialized_pipeline's shape elsewhere.
    existing = {
        row.name: row
        for row in (
            await db.execute(
                select(ModelEndpoint).where(
                    ModelEndpoint.name.in_([e["name"] for e in SEED_MODEL_ENDPOINTS])
                )
            )
        ).scalars()
    }

    rows = []
    for spec in SEED_MODEL_ENDPOINTS:
        endpoint = existing.get(spec["name"]) or ModelEndpoint(name=spec["name"])
        _apply_seed_endpoint_spec(endpoint, spec, mock_url, default_gpu_node_id)
        if endpoint.name not in existing:
            db.add(endpoint)
        rows.append(endpoint)
    await db.commit()

    seeded = []
    for endpoint in rows:
        await db.refresh(endpoint)
        try:
            await probe_endpoint(db, endpoint, force=True)
            await db.refresh(endpoint)
        except Exception as e:  # noqa: BLE001 - a probe never fails a seed
            logger.warning(
                "Seed probe of model endpoint %s failed: %s", endpoint.name, e
            )
        seeded.append(
            SeededEndpoint(
                id=endpoint.id,
                name=endpoint.name,
                base_url=endpoint.base_url,
                model=endpoint.model,
                probe_status=endpoint.probe_status,
            )
        )
    return seeded


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
            git_repo_manager.create_bare_repo(repo_id, default_branch)

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

        # The in-review seed card claims a branch. Give it a REAL one, with a
        # real commit on top of the default branch, or the very first thing a
        # demo does on the seeded board - press Approve - is a red toast,
        # because approve MERGES and there was nothing to merge (T16). One
        # extra commit buys a working merge AND a diff worth looking at on the
        # review screen.
        review_blob = Blob.from_string(
            b"# Seed Repository\n\nCreated by the test-mode API.\n\n"
            b"Reviewed change: this line came from the seeded agent branch.\n"
        )
        dulwich_repo.object_store.add_object(review_blob)

        review_tree = Tree()
        review_tree.add(b"README.md", 0o100644, review_blob.id)
        dulwich_repo.object_store.add_object(review_tree)

        review_commit = Commit()
        review_commit.tree = review_tree.id
        review_commit.parents = [commit.id]
        review_commit.author = review_commit.committer = b"LazyAF Test <test@lazyaf.local>"
        review_commit.author_time = review_commit.commit_time = 0
        review_commit.author_timezone = review_commit.commit_timezone = 0
        review_commit.encoding = b"UTF-8"
        review_commit.message = b"Seed review commit"
        dulwich_repo.object_store.add_object(review_commit)

        dulwich_repo.refs[f"refs/heads/{SEED_REVIEW_BRANCH}".encode()] = review_commit.id
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
        # AGENT, not SCRIPT. 12.4 removed docker/script execution from the
        # runners, so a seeded script card was a card whose only possible
        # outcome was a red toast on Start - the seed handed a demo a broken
        # button. Seeded through the ORM, this row bypasses create_card's
        # refusal, so the seed has to hold the rule itself.
        step_type=StepType.AGENT.value,
        runner_type="mock",
        step_config=json.dumps({"task": "Write a short greeting to README.md"}),
    )
    card_review = Card(
        id=str(uuid4()),
        repo_id=repo.id,
        title="Seed card (in review)",
        description="Deterministic seed card awaiting review",
        status=CardStatus.IN_REVIEW.value,
        # AGENT for the same reason as the todo card above: Retry is offered
        # from in_review, and a deprecated step type would refuse there too.
        step_type=StepType.AGENT.value,
        runner_type="mock",
        step_config=json.dumps({"task": "Write a short greeting to README.md"}),
        branch_name=SEED_REVIEW_BRANCH,
    )
    db.add(card_todo)
    db.add(card_review)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Seeding failed: {e}")

    # Read every value the response needs BEFORE touching model endpoints.
    #
    # The rescue below exists so endpoint seeding can never fail a seed, and it
    # was doing the opposite: `rollback()` EXPIRES the ORM rows committed above,
    # so building the response then lazy-loaded `repo.id` outside a greenlet and
    # raised MissingGreenlet - a 500 from the handler whose whole job was to
    # swallow the failure. Plain values cannot be expired.
    seeded_repo = SeededRepo(
        id=repo.id,
        name=repo.name,
        default_branch=repo.default_branch,
        git_initialized=git_initialized,
    )
    seeded_pipeline = SeededPipeline(id=pipeline.id, name=pipeline.name)
    seeded_cards = [
        SeededCard(id=card_todo.id, title=card_todo.title, status=card_todo.status),
        SeededCard(id=card_review.id, title=card_review.title, status=card_review.status),
    ]

    try:
        model_endpoints = await _seed_model_endpoints(db)
    except Exception as e:  # noqa: BLE001 - endpoint seeding never fails a seed
        await db.rollback()
        logger.warning("Model endpoint seeding skipped: %s", e)
        model_endpoints = []

    return SeedResponse(
        success=True,
        model_endpoints=model_endpoints,
        repo=seeded_repo,
        pipeline=seeded_pipeline,
        cards=seeded_cards,
    )
