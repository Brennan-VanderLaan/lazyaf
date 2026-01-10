"""
Unit tests for Step Execution Idempotency Keys.

These tests define the contract for idempotent step execution.
Each step execution has a unique execution_key that prevents duplicate execution:

    execution_key = "{pipeline_run_id}:{step_index}:{attempt}"

Idempotency guarantees:
- Same execution_key always returns the same StepExecution
- Different attempt numbers create new executions (for retries)
- Duplicate requests are safely ignored

Tests are written FIRST to define the contract, then implementation makes them pass.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Execution Key Format
# -----------------------------------------------------------------------------

class TestExecutionKeyFormat:
    """Tests that define the execution key format."""

    def test_execution_key_format_basic(self):
        """Execution key follows format: {pipeline_run_id}:{step_index}:{attempt}."""
        from app.services.execution.idempotency import generate_execution_key

        key = generate_execution_key(
            pipeline_run_id="run-123",
            step_index=0,
            attempt=1
        )
        assert key == "run-123:0:1"

    def test_execution_key_format_with_uuid(self):
        """Execution key works with UUID pipeline_run_id."""
        from app.services.execution.idempotency import generate_execution_key

        run_id = str(uuid4())
        key = generate_execution_key(
            pipeline_run_id=run_id,
            step_index=2,
            attempt=3
        )
        assert key == f"{run_id}:2:3"

    def test_execution_key_format_step_index_zero(self):
        """Execution key handles step index 0 correctly."""
        from app.services.execution.idempotency import generate_execution_key

        key = generate_execution_key(
            pipeline_run_id="run-abc",
            step_index=0,
            attempt=1
        )
        assert key == "run-abc:0:1"

    def test_execution_key_format_high_step_index(self):
        """Execution key handles high step indices."""
        from app.services.execution.idempotency import generate_execution_key

        key = generate_execution_key(
            pipeline_run_id="run-xyz",
            step_index=99,
            attempt=1
        )
        assert key == "run-xyz:99:1"

    def test_execution_key_format_retry_attempt(self):
        """Execution key increments attempt for retries."""
        from app.services.execution.idempotency import generate_execution_key

        key1 = generate_execution_key("run-1", step_index=0, attempt=1)
        key2 = generate_execution_key("run-1", step_index=0, attempt=2)
        key3 = generate_execution_key("run-1", step_index=0, attempt=3)

        assert key1 == "run-1:0:1"
        assert key2 == "run-1:0:2"
        assert key3 == "run-1:0:3"
        assert key1 != key2 != key3

    def test_parse_execution_key(self):
        """Can parse execution key back to components."""
        from app.services.execution.idempotency import parse_execution_key

        run_id, step_index, attempt = parse_execution_key("run-123:5:2")
        assert run_id == "run-123"
        assert step_index == 5
        assert attempt == 2

    def test_parse_execution_key_with_colons_in_run_id(self):
        """Parse handles run_id containing colons (edge case)."""
        from app.services.execution.idempotency import parse_execution_key

        # UUIDs don't have colons, but be defensive
        key = "abc:def:ghi:2:3"  # run_id="abc:def:ghi", step=2, attempt=3
        run_id, step_index, attempt = parse_execution_key(key)
        # Should take last two as step_index and attempt
        assert step_index == 2
        assert attempt == 3


# -----------------------------------------------------------------------------
# Contract: Idempotent Execution Creation
# -----------------------------------------------------------------------------

class TestIdempotentCreation:
    """Tests that define idempotent step execution creation."""

    @pytest_asyncio.fixture
    async def execution_service(self, db_session):
        """Create an ExecutionService for testing."""
        from app.services.execution.idempotency import ExecutionService
        return ExecutionService(db_session)

    @pytest_asyncio.fixture
    async def step_run(self, db_session):
        """Create a StepRun for testing."""
        from app.models.pipeline import StepRun, PipelineRun, Pipeline
        from app.models.repo import Repo

        # Create required parent objects
        repo = Repo(id=str(uuid4()), name="test-repo")
        db_session.add(repo)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="test-pipeline",
            steps="[]"
        )
        db_session.add(pipeline)

        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running"
        )
        db_session.add(pipeline_run)

        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=0,
            step_name="test-step",
            status="pending"
        )
        db_session.add(step_run)
        await db_session.commit()
        await db_session.refresh(step_run)
        return step_run

    async def test_create_execution_returns_new(self, execution_service, step_run):
        """First call with execution_key creates new StepExecution."""
        execution = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=f"{step_run.pipeline_run_id}:0:1"
        )
        assert execution is not None
        assert execution.execution_key == f"{step_run.pipeline_run_id}:0:1"
        assert execution.step_run_id == step_run.id

    async def test_same_key_returns_existing(self, execution_service, step_run):
        """Second call with same execution_key returns existing StepExecution."""
        key = f"{step_run.pipeline_run_id}:0:1"

        exec1 = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key
        )
        exec2 = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key
        )

        assert exec1.id == exec2.id  # Same execution returned

    async def test_different_attempt_creates_new(self, execution_service, step_run):
        """Different attempt number creates new StepExecution."""
        key1 = f"{step_run.pipeline_run_id}:0:1"
        key2 = f"{step_run.pipeline_run_id}:0:2"

        exec1 = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key1
        )
        exec2 = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key2
        )

        assert exec1.id != exec2.id  # Different executions

    async def test_execution_has_status_pending(self, execution_service, step_run):
        """New StepExecution starts with pending status."""
        execution = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=f"{step_run.pipeline_run_id}:0:1"
        )
        assert execution.status == "pending"


# -----------------------------------------------------------------------------
# Contract: Execution Lookup
# -----------------------------------------------------------------------------

class TestExecutionLookup:
    """Tests for looking up executions by key."""

    @pytest_asyncio.fixture
    async def execution_service(self, db_session):
        """Create an ExecutionService for testing."""
        from app.services.execution.idempotency import ExecutionService
        return ExecutionService(db_session)

    @pytest_asyncio.fixture
    async def step_run(self, db_session):
        """Create a StepRun for testing."""
        from app.models.pipeline import StepRun, PipelineRun, Pipeline
        from app.models.repo import Repo

        repo = Repo(id=str(uuid4()), name="test-repo")
        db_session.add(repo)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="test-pipeline",
            steps="[]"
        )
        db_session.add(pipeline)

        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running"
        )
        db_session.add(pipeline_run)

        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=0,
            step_name="test-step",
            status="pending"
        )
        db_session.add(step_run)
        await db_session.commit()
        await db_session.refresh(step_run)
        return step_run

    async def test_get_by_key_returns_execution(self, execution_service, step_run):
        """get_by_key returns execution for known key."""
        key = f"{step_run.pipeline_run_id}:0:1"
        created = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key
        )

        found = await execution_service.get_by_key(key)
        assert found is not None
        assert found.id == created.id

    async def test_get_by_key_returns_none_for_unknown(self, execution_service):
        """get_by_key returns None for unknown key."""
        found = await execution_service.get_by_key("nonexistent:0:1")
        assert found is None

    async def test_get_by_id_returns_execution(self, execution_service, step_run):
        """get_by_id returns execution for known ID."""
        key = f"{step_run.pipeline_run_id}:0:1"
        created = await execution_service.get_or_create_execution(
            step_run_id=step_run.id,
            execution_key=key
        )

        found = await execution_service.get_by_id(created.id)
        assert found is not None
        assert found.execution_key == key


# -----------------------------------------------------------------------------
# Contract: Concurrent Creation Safety
# -----------------------------------------------------------------------------

class TestConcurrentCreation:
    """Tests for concurrent creation handling."""

    @pytest_asyncio.fixture
    async def execution_service(self, db_session):
        """Create an ExecutionService for testing."""
        from app.services.execution.idempotency import ExecutionService
        return ExecutionService(db_session)

    @pytest_asyncio.fixture
    async def step_run(self, db_session):
        """Create a StepRun for testing."""
        from app.models.pipeline import StepRun, PipelineRun, Pipeline
        from app.models.repo import Repo

        repo = Repo(id=str(uuid4()), name="test-repo")
        db_session.add(repo)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="test-pipeline",
            steps="[]"
        )
        db_session.add(pipeline)

        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running"
        )
        db_session.add(pipeline_run)

        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=0,
            step_name="test-step",
            status="pending"
        )
        db_session.add(step_run)
        await db_session.commit()
        await db_session.refresh(step_run)
        return step_run

    async def test_sequential_creates_same_execution(self, execution_service, step_run):
        """Sequential creation attempts with same key yield same execution.

        Note: True concurrent testing requires multiple database sessions/connections
        which is not feasible with SQLite in-memory. This test verifies the
        idempotency logic works for sequential calls, which exercises the same
        code path (check-then-create with uniqueness constraint).
        """
        key = f"{step_run.pipeline_run_id}:0:1"

        # Multiple sequential calls with same key
        exec1 = await execution_service.get_or_create_execution(step_run.id, key)
        exec2 = await execution_service.get_or_create_execution(step_run.id, key)
        exec3 = await execution_service.get_or_create_execution(step_run.id, key)

        # All should return the same execution
        assert exec1.id == exec2.id == exec3.id


# -----------------------------------------------------------------------------
# Contract: Execution Key Uniqueness
# -----------------------------------------------------------------------------

class TestExecutionKeyUniqueness:
    """Tests for execution key uniqueness constraints."""

    @pytest_asyncio.fixture
    async def execution_service(self, db_session):
        """Create an ExecutionService for testing."""
        from app.services.execution.idempotency import ExecutionService
        return ExecutionService(db_session)

    @pytest_asyncio.fixture
    async def step_run(self, db_session):
        """Create a StepRun for testing."""
        from app.models.pipeline import StepRun, PipelineRun, Pipeline
        from app.models.repo import Repo

        repo = Repo(id=str(uuid4()), name="test-repo")
        db_session.add(repo)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="test-pipeline",
            steps="[]"
        )
        db_session.add(pipeline)

        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status="running"
        )
        db_session.add(pipeline_run)

        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=0,
            step_name="test-step",
            status="pending"
        )
        db_session.add(step_run)
        await db_session.commit()
        await db_session.refresh(step_run)
        return step_run

    async def test_different_steps_different_keys(self, execution_service, step_run, db_session):
        """Different step indices produce different keys."""
        from app.models.pipeline import StepRun

        # Create another step run
        step_run_2 = StepRun(
            id=str(uuid4()),
            pipeline_run_id=step_run.pipeline_run_id,
            step_index=1,
            step_name="test-step-2",
            status="pending"
        )
        db_session.add(step_run_2)
        await db_session.commit()

        key1 = f"{step_run.pipeline_run_id}:0:1"
        key2 = f"{step_run.pipeline_run_id}:1:1"

        exec1 = await execution_service.get_or_create_execution(step_run.id, key1)
        exec2 = await execution_service.get_or_create_execution(step_run_2.id, key2)

        assert exec1.id != exec2.id
        assert exec1.execution_key != exec2.execution_key

    async def test_different_runs_different_keys(self, db_session):
        """Different pipeline runs produce different keys."""
        from app.services.execution.idempotency import ExecutionService, generate_execution_key

        service = ExecutionService(db_session)

        key1 = generate_execution_key("run-aaa", step_index=0, attempt=1)
        key2 = generate_execution_key("run-bbb", step_index=0, attempt=1)

        assert key1 != key2
        assert key1 == "run-aaa:0:1"
        assert key2 == "run-bbb:0:1"
