"""
Unit tests for Step Execution Recovery Service.

These tests verify crash recovery behavior:
- On startup, orphaned executions are found and marked failed
- Terminal executions are not affected
- Recovery includes proper error messages and timestamps
"""
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime

import pytest
import pytest_asyncio

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def setup_test_data(db_session):
    """Create test data with various execution states."""
    from app.models.pipeline import (
        StepRun, StepExecution, PipelineRun, Pipeline,
        StepExecutionStatus
    )
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
        status="running"
    )
    db_session.add(step_run)
    await db_session.commit()

    return {
        "repo": repo,
        "pipeline": pipeline,
        "pipeline_run": pipeline_run,
        "step_run": step_run,
    }


# -----------------------------------------------------------------------------
# Contract: Orphaned Execution Recovery
# -----------------------------------------------------------------------------

class TestOrphanedExecutionRecovery:
    """Tests for recovering orphaned executions."""

    async def test_pending_execution_marked_failed(self, db_session, setup_test_data):
        """Pending executions are marked as failed."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        # Create a pending execution
        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.PENDING.value,
        )
        db_session.add(execution)
        await db_session.commit()

        # Run recovery
        recovered = await recover_orphaned_executions(db_session)

        # Verify execution was recovered
        assert execution.id in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.FAILED.value
        assert "restart" in execution.error.lower()

    async def test_running_execution_marked_failed(self, db_session, setup_test_data):
        """Running executions are marked as failed."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        # Create a running execution
        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.RUNNING.value,
            started_at=datetime.utcnow(),
        )
        db_session.add(execution)
        await db_session.commit()

        # Run recovery
        recovered = await recover_orphaned_executions(db_session)

        # Verify execution was recovered
        assert execution.id in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.FAILED.value

    async def test_preparing_execution_marked_failed(self, db_session, setup_test_data):
        """Preparing executions are marked as failed."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.PREPARING.value,
        )
        db_session.add(execution)
        await db_session.commit()

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.FAILED.value

    async def test_completed_execution_not_affected(self, db_session, setup_test_data):
        """Completed executions are not modified."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.COMPLETED.value,
            exit_code=0,
            completed_at=datetime.utcnow(),
        )
        db_session.add(execution)
        await db_session.commit()

        recovered = await recover_orphaned_executions(db_session)

        # Should not be recovered
        assert execution.id not in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.COMPLETED.value

    async def test_failed_execution_not_affected(self, db_session, setup_test_data):
        """Already failed executions are not modified."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.FAILED.value,
            exit_code=1,
            error="Original error",
            completed_at=datetime.utcnow(),
        )
        db_session.add(execution)
        await db_session.commit()

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id not in recovered
        await db_session.refresh(execution)
        assert execution.error == "Original error"  # Not overwritten

    async def test_recovery_sets_completed_at(self, db_session, setup_test_data):
        """Recovery sets completed_at timestamp."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]

        execution = StepExecution(
            id=str(uuid4()),
            step_run_id=step_run.id,
            execution_key=f"{setup_test_data['pipeline_run'].id}:0:1",
            status=StepExecutionStatus.RUNNING.value,
        )
        db_session.add(execution)
        await db_session.commit()

        assert execution.completed_at is None

        before = datetime.utcnow()
        await recover_orphaned_executions(db_session)
        after = datetime.utcnow()

        await db_session.refresh(execution)
        assert execution.completed_at is not None
        assert before <= execution.completed_at <= after

    async def test_no_orphans_returns_empty(self, db_session, setup_test_data):
        """Returns empty list when no orphans exist."""
        from app.services.execution.recovery import recover_orphaned_executions

        # No executions created
        recovered = await recover_orphaned_executions(db_session)
        assert recovered == []

    async def test_multiple_orphans_all_recovered(self, db_session, setup_test_data):
        """Multiple orphaned executions are all recovered."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import recover_orphaned_executions

        step_run = setup_test_data["step_run"]
        pipeline_run_id = setup_test_data["pipeline_run"].id

        # Create multiple orphaned executions in different states
        executions = []
        for i, status in enumerate([
            StepExecutionStatus.PENDING,
            StepExecutionStatus.ASSIGNED,
            StepExecutionStatus.PREPARING,
            StepExecutionStatus.RUNNING,
            StepExecutionStatus.COMPLETING,
        ]):
            execution = StepExecution(
                id=str(uuid4()),
                step_run_id=step_run.id,
                execution_key=f"{pipeline_run_id}:{i}:1",
                status=status.value,
            )
            db_session.add(execution)
            executions.append(execution)

        await db_session.commit()

        recovered = await recover_orphaned_executions(db_session)

        # All 5 should be recovered
        assert len(recovered) == 5
        for execution in executions:
            assert execution.id in recovered
            await db_session.refresh(execution)
            assert execution.status == StepExecutionStatus.FAILED.value


# -----------------------------------------------------------------------------
# Contract: Get Orphaned Count
# -----------------------------------------------------------------------------

class TestGetOrphanedCount:
    """Tests for counting orphaned executions."""

    async def test_count_with_no_orphans(self, db_session, setup_test_data):
        """Returns 0 when no orphans exist."""
        from app.services.execution.recovery import get_orphaned_execution_count

        count = await get_orphaned_execution_count(db_session)
        assert count == 0

    async def test_count_with_orphans(self, db_session, setup_test_data):
        """Returns correct count of orphans."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import get_orphaned_execution_count

        step_run = setup_test_data["step_run"]
        pipeline_run_id = setup_test_data["pipeline_run"].id

        # Create 3 orphaned executions
        for i in range(3):
            execution = StepExecution(
                id=str(uuid4()),
                step_run_id=step_run.id,
                execution_key=f"{pipeline_run_id}:{i}:1",
                status=StepExecutionStatus.RUNNING.value,
            )
            db_session.add(execution)

        await db_session.commit()

        count = await get_orphaned_execution_count(db_session)
        assert count == 3

    async def test_count_excludes_terminal_states(self, db_session, setup_test_data):
        """Count excludes terminal state executions."""
        from app.models.pipeline import StepExecution, StepExecutionStatus
        from app.services.execution.recovery import get_orphaned_execution_count

        step_run = setup_test_data["step_run"]
        pipeline_run_id = setup_test_data["pipeline_run"].id

        # Create mix of terminal and non-terminal
        statuses = [
            StepExecutionStatus.RUNNING,      # orphan
            StepExecutionStatus.COMPLETED,    # not orphan
            StepExecutionStatus.PREPARING,    # orphan
            StepExecutionStatus.FAILED,       # not orphan
        ]

        for i, status in enumerate(statuses):
            execution = StepExecution(
                id=str(uuid4()),
                step_run_id=step_run.id,
                execution_key=f"{pipeline_run_id}:{i}:1",
                status=status.value,
            )
            db_session.add(execution)

        await db_session.commit()

        count = await get_orphaned_execution_count(db_session)
        assert count == 2  # Only RUNNING and PREPARING
