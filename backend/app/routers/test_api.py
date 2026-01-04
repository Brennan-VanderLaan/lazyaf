"""
Test API Router - Endpoints for E2E testing infrastructure.

These endpoints are only mounted when LAZYAF_TEST_MODE=true.
They provide:
- Database reset functionality
- Test data seeding
- AI mock configuration

SECURITY: Never enable in production!
"""

import os
import shutil
from typing import Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, Base, engine
from app.models import (
    Repo, Card, Job, Pipeline, PipelineRun, StepRun,
    AgentFile, Runner, StepExecution, Workspace, DebugSession
)
from app.models.card import CardStatus, RunnerType, StepType
from app.models.pipeline import RunStatus


router = APIRouter(prefix="/api/test", tags=["test"])

settings = get_settings()


# ==============================================================================
# Request/Response Models
# ==============================================================================

class ResetResponse(BaseModel):
    success: bool
    message: str
    tables_cleared: list[str]


class SeedRequest(BaseModel):
    """Request to seed test data."""
    scenario: str = "basic"  # basic, card_workflow, pipeline_workflow
    options: dict[str, Any] = {}


class SeedResponse(BaseModel):
    success: bool
    message: str
    created: dict[str, Any]


class TestRepoCreate(BaseModel):
    """Create a test repo (simplified, no real git)."""
    name: str = "test-repo"
    default_branch: str = "main"


class TestCardCreate(BaseModel):
    """Create a test card."""
    repo_id: str
    title: str = "Test Card"
    description: str = "Test card description"
    step_type: str = "script"  # script, docker (not agent - to avoid AI)
    step_config: dict[str, Any] | None = None


class TestPipelineCreate(BaseModel):
    """Create a test pipeline."""
    repo_id: str
    name: str = "Test Pipeline"
    description: str = "Test pipeline for E2E tests"
    steps: list[dict[str, Any]] = []


class MockAIConfig(BaseModel):
    """Configuration for AI mocking behavior."""
    enabled: bool = True
    response_delay_ms: int = 100
    default_response: str = "Mocked AI response for testing"
    should_fail: bool = False
    fail_message: str = "Mocked AI failure"


# Global mock AI state (in-memory, reset with database)
_mock_ai_config = MockAIConfig()


# ==============================================================================
# Database Reset
# ==============================================================================

@router.post("/reset", response_model=ResetResponse)
async def reset_database(db: AsyncSession = Depends(get_db)):
    """
    Reset the test database by clearing all tables.

    This endpoint:
    1. Deletes all data from all tables
    2. Clears any git repo storage
    3. Resets mock AI configuration

    Use before each test run for isolation.
    """
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    tables_cleared = []

    try:
        # Clear tables in dependency order (children before parents)
        # Using raw SQL for efficiency and to handle foreign keys

        # Step-level data
        await db.execute(text("DELETE FROM step_run"))
        tables_cleared.append("step_run")

        await db.execute(text("DELETE FROM step_execution"))
        tables_cleared.append("step_execution")

        await db.execute(text("DELETE FROM debug_session"))
        tables_cleared.append("debug_session")

        # Run-level data
        await db.execute(text("DELETE FROM pipeline_run"))
        tables_cleared.append("pipeline_run")

        await db.execute(text("DELETE FROM job"))
        tables_cleared.append("job")

        # Entity data
        await db.execute(text("DELETE FROM card"))
        tables_cleared.append("card")

        await db.execute(text("DELETE FROM pipeline"))
        tables_cleared.append("pipeline")

        await db.execute(text("DELETE FROM agent_file"))
        tables_cleared.append("agent_file")

        await db.execute(text("DELETE FROM runner"))
        tables_cleared.append("runner")

        await db.execute(text("DELETE FROM workspace"))
        tables_cleared.append("workspace")

        # Root data
        await db.execute(text("DELETE FROM repo"))
        tables_cleared.append("repo")

        await db.commit()

        # Clear git repo storage if it exists
        git_storage_path = os.path.join(os.getcwd(), "git_repos")
        if os.path.exists(git_storage_path):
            shutil.rmtree(git_storage_path, ignore_errors=True)
            os.makedirs(git_storage_path, exist_ok=True)

        # Reset mock AI configuration
        global _mock_ai_config
        _mock_ai_config = MockAIConfig()

        return ResetResponse(
            success=True,
            message="Database reset complete",
            tables_cleared=tables_cleared
        )

    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Reset failed: {str(e)}"
        )


# ==============================================================================
# Test Data Seeding
# ==============================================================================

@router.post("/seed", response_model=SeedResponse)
async def seed_test_data(
    request: SeedRequest = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Seed the database with test data for a given scenario.

    Scenarios:
    - basic: Single repo, no cards
    - card_workflow: Repo with cards in various states
    - pipeline_workflow: Repo with pipeline and step definitions
    """
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    if request is None:
        request = SeedRequest()

    created = {}

    try:
        if request.scenario == "basic":
            created = await _seed_basic(db, request.options)
        elif request.scenario == "card_workflow":
            created = await _seed_card_workflow(db, request.options)
        elif request.scenario == "pipeline_workflow":
            created = await _seed_pipeline_workflow(db, request.options)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown scenario: {request.scenario}"
            )

        await db.commit()

        return SeedResponse(
            success=True,
            message=f"Seeded scenario: {request.scenario}",
            created=created
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Seeding failed: {str(e)}"
        )


async def _seed_basic(db: AsyncSession, options: dict) -> dict:
    """Seed basic scenario: just a repo."""
    repo = Repo(
        name=options.get("repo_name", "test-repo"),
        default_branch="main",
        is_ingested=True,  # Pretend it's ingested for testing
        internal_git_url="git://localhost/test-repo.git"
    )
    db.add(repo)
    await db.flush()

    return {
        "repo": {"id": repo.id, "name": repo.name}
    }


async def _seed_card_workflow(db: AsyncSession, options: dict) -> dict:
    """Seed card workflow scenario: repo with cards in various states."""
    # Create repo
    repo = Repo(
        name=options.get("repo_name", "card-workflow-repo"),
        default_branch="main",
        is_ingested=True,
        internal_git_url="git://localhost/card-workflow-repo.git"
    )
    db.add(repo)
    await db.flush()

    # Create cards in different states
    cards = []

    # Todo card (ready to start)
    card_todo = Card(
        repo_id=repo.id,
        title="Todo Task",
        description="This card is ready to be started",
        status=CardStatus.TODO.value,
        step_type=StepType.SCRIPT.value,
        step_config='{"command": "echo Hello World"}'
    )
    db.add(card_todo)
    cards.append({"title": "Todo Task", "status": "todo"})

    # In-progress card (with a job)
    card_in_progress = Card(
        repo_id=repo.id,
        title="In Progress Task",
        description="This card is currently running",
        status=CardStatus.IN_PROGRESS.value,
        step_type=StepType.SCRIPT.value,
        step_config='{"command": "sleep 10"}'
    )
    db.add(card_in_progress)
    await db.flush()

    job = Job(
        card_id=card_in_progress.id,
        status="running",
        step_type=StepType.SCRIPT.value,
        step_config='{"command": "sleep 10"}'
    )
    db.add(job)
    await db.flush()
    card_in_progress.job_id = job.id
    cards.append({"title": "In Progress Task", "status": "in_progress", "job_id": job.id})

    # In-review card (completed, awaiting approval)
    card_review = Card(
        repo_id=repo.id,
        title="Review Task",
        description="This card is ready for review",
        status=CardStatus.IN_REVIEW.value,
        step_type=StepType.SCRIPT.value,
        step_config='{"command": "echo Done"}',
        branch_name="lazyaf/test-review"
    )
    db.add(card_review)
    cards.append({"title": "Review Task", "status": "in_review"})

    # Done card
    card_done = Card(
        repo_id=repo.id,
        title="Completed Task",
        description="This card has been approved and merged",
        status=CardStatus.DONE.value,
        step_type=StepType.SCRIPT.value
    )
    db.add(card_done)
    cards.append({"title": "Completed Task", "status": "done"})

    await db.flush()

    return {
        "repo": {"id": repo.id, "name": repo.name},
        "cards": cards,
        "card_ids": {
            "todo": card_todo.id,
            "in_progress": card_in_progress.id,
            "in_review": card_review.id,
            "done": card_done.id
        }
    }


async def _seed_pipeline_workflow(db: AsyncSession, options: dict) -> dict:
    """Seed pipeline workflow scenario: repo with pipeline definition."""
    # Create repo
    repo = Repo(
        name=options.get("repo_name", "pipeline-workflow-repo"),
        default_branch="main",
        is_ingested=True,
        internal_git_url="git://localhost/pipeline-workflow-repo.git"
    )
    db.add(repo)
    await db.flush()

    # Create a pipeline with script steps (no AI)
    import json
    steps = [
        {
            "name": "Setup",
            "type": "script",
            "config": {"command": "echo 'Setting up...'"},
            "on_success": "next",
            "on_failure": "stop",
            "timeout": 60
        },
        {
            "name": "Build",
            "type": "script",
            "config": {"command": "echo 'Building...'"},
            "on_success": "next",
            "on_failure": "stop",
            "timeout": 120
        },
        {
            "name": "Test",
            "type": "script",
            "config": {"command": "echo 'Testing...' && exit 0"},
            "on_success": "next",
            "on_failure": "stop",
            "timeout": 300
        }
    ]

    pipeline = Pipeline(
        repo_id=repo.id,
        name=options.get("pipeline_name", "Test Pipeline"),
        description="A test pipeline with script steps",
        steps=json.dumps(steps),
        triggers="[]",
        is_template=False
    )
    db.add(pipeline)
    await db.flush()

    return {
        "repo": {"id": repo.id, "name": repo.name},
        "pipeline": {
            "id": pipeline.id,
            "name": pipeline.name,
            "steps_count": len(steps)
        }
    }


# ==============================================================================
# Individual Entity Creation (for fine-grained test control)
# ==============================================================================

@router.post("/repos", response_model=dict)
async def create_test_repo(
    request: TestRepoCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a test repo directly (bypassing git ingestion)."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    repo = Repo(
        name=request.name,
        default_branch=request.default_branch,
        is_ingested=True,  # Mark as ingested for testing
        internal_git_url=f"git://localhost/{request.name}.git"
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return {
        "id": repo.id,
        "name": repo.name,
        "default_branch": repo.default_branch,
        "is_ingested": repo.is_ingested
    }


@router.post("/cards", response_model=dict)
async def create_test_card(
    request: TestCardCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a test card with specified configuration."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    import json

    # Verify repo exists
    from sqlalchemy import select
    result = await db.execute(select(Repo).where(Repo.id == request.repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    # Default step config for script type
    step_config = request.step_config
    if step_config is None and request.step_type == "script":
        step_config = {"command": "echo 'Test card executed'"}

    card = Card(
        repo_id=request.repo_id,
        title=request.title,
        description=request.description,
        status=CardStatus.TODO.value,
        step_type=request.step_type,
        step_config=json.dumps(step_config) if step_config else None
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)

    return {
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "status": card.status,
        "step_type": card.step_type
    }


@router.post("/pipelines", response_model=dict)
async def create_test_pipeline(
    request: TestPipelineCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a test pipeline with specified steps."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    import json

    # Verify repo exists
    from sqlalchemy import select
    result = await db.execute(select(Repo).where(Repo.id == request.repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    # Default steps if none provided
    steps = request.steps
    if not steps:
        steps = [
            {
                "name": "Default Step",
                "type": "script",
                "config": {"command": "echo 'Pipeline step executed'"},
                "on_success": "next",
                "on_failure": "stop",
                "timeout": 60
            }
        ]

    pipeline = Pipeline(
        repo_id=request.repo_id,
        name=request.name,
        description=request.description,
        steps=json.dumps(steps),
        triggers="[]",
        is_template=False
    )
    db.add(pipeline)
    await db.commit()
    await db.refresh(pipeline)

    return {
        "id": pipeline.id,
        "repo_id": pipeline.repo_id,
        "name": pipeline.name,
        "steps_count": len(steps)
    }


# ==============================================================================
# Mock AI Configuration
# ==============================================================================

@router.get("/mock-ai/config", response_model=MockAIConfig)
async def get_mock_ai_config():
    """Get current mock AI configuration."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    return _mock_ai_config


@router.post("/mock-ai/config", response_model=MockAIConfig)
async def set_mock_ai_config(config: MockAIConfig):
    """Update mock AI configuration."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    global _mock_ai_config
    _mock_ai_config = config
    return _mock_ai_config


def get_mock_ai_response() -> dict:
    """
    Get the current mock AI response configuration.

    Called by AI service implementations when mock_ai is enabled.
    Returns dict with response info or raises if should_fail is True.
    """
    if _mock_ai_config.should_fail:
        raise Exception(_mock_ai_config.fail_message)

    return {
        "response": _mock_ai_config.default_response,
        "delay_ms": _mock_ai_config.response_delay_ms,
        "mocked": True
    }


# ==============================================================================
# Test Utilities
# ==============================================================================

@router.get("/health")
async def test_health():
    """Health check for test API."""
    return {
        "status": "ok",
        "test_mode": settings.test_mode,
        "mock_ai": settings.mock_ai,
        "mock_ai_config": _mock_ai_config.model_dump()
    }


@router.get("/state")
async def get_test_state(db: AsyncSession = Depends(get_db)):
    """Get current database state (counts of entities)."""
    if not settings.test_mode:
        raise HTTPException(
            status_code=403,
            detail="Test endpoints are disabled. Set LAZYAF_TEST_MODE=true"
        )

    from sqlalchemy import func, select

    repo_count = await db.scalar(select(func.count()).select_from(Repo))
    card_count = await db.scalar(select(func.count()).select_from(Card))
    job_count = await db.scalar(select(func.count()).select_from(Job))
    pipeline_count = await db.scalar(select(func.count()).select_from(Pipeline))
    pipeline_run_count = await db.scalar(select(func.count()).select_from(PipelineRun))

    return {
        "repos": repo_count,
        "cards": card_count,
        "jobs": job_count,
        "pipelines": pipeline_count,
        "pipeline_runs": pipeline_run_count
    }
