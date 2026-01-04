"""
Tests for Job Recovery Service (Phase 12.6).

These tests DEFINE the recovery behavior when runners fail or disconnect.
Write tests first, then implement to make them pass.

Recovery Scenarios:
1. Runner dies mid-job (heartbeat timeout) -> requeue step
2. Runner disconnects mid-job (WebSocket closes) -> requeue step
3. Runner reconnects after death -> check if step was reassigned
4. Runner reconnects after reassignment -> abort local work
5. Backend restarts -> find and recover orphaned steps

Key Principles:
- Database is source of truth
- Idempotency: duplicate recovery is safe
- Step can only be assigned to one runner at a time
- Orphaned steps (no runner) return to pending
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.job_recovery import (
        JobRecoveryService,
        get_job_recovery_service,
    )
    from app.services.execution.runner_state import RunnerState
    from app.models.runner import Runner
    from app.models.step_execution import StepExecution, ExecutionStatus
    JOB_RECOVERY_MODULE_AVAILABLE = True
except ImportError:
    JOB_RECOVERY_MODULE_AVAILABLE = False
    # Define placeholders
    JobRecoveryService = None
    get_job_recovery_service = None
    RunnerState = None
    Runner = None
    StepExecution = None
    ExecutionStatus = None


pytestmark = pytest.mark.skipif(
    not JOB_RECOVERY_MODULE_AVAILABLE,
    reason="job_recovery module not yet implemented"
)


class TestJobRecoveryServiceSingleton:
    """Tests for JobRecoveryService singleton pattern."""

    def test_get_job_recovery_service_returns_instance(self):
        """get_job_recovery_service() returns a JobRecoveryService."""
        service = get_job_recovery_service()
        assert isinstance(service, JobRecoveryService)

    def test_get_job_recovery_service_returns_same_instance(self):
        """get_job_recovery_service() returns the same instance."""
        service1 = get_job_recovery_service()
        service2 = get_job_recovery_service()
        assert service1 is service2


class TestRunnerDeath:
    """Tests for handling runner death (heartbeat timeout)."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def mock_runner_with_step(self):
        """Create a mock runner that was executing a step."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "busy"
        runner.current_step_execution_id = "step-123"
        return runner

    @pytest.fixture
    def mock_step_execution(self):
        """Create a mock step execution."""
        step = MagicMock()
        step.id = "step-123"
        step.status = "running"
        step.runner_id = "runner-1"
        return step

    def _mock_execute_result(self, step):
        """Create a mock execute result that returns step via scalar_one_or_none()."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = step
        return mock_result

    @pytest.mark.asyncio
    async def test_on_runner_death_requeues_step(
        self, service, mock_db, mock_runner_with_step, mock_step_execution
    ):
        """on_runner_death() requeues the step for another runner."""
        mock_db.execute.return_value = self._mock_execute_result(mock_step_execution)

        await service.on_runner_death(mock_db, mock_runner_with_step)

        # Step should be marked pending
        assert mock_step_execution.status == "pending"
        assert mock_step_execution.runner_id is None
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_on_runner_death_updates_runner_status(
        self, service, mock_db, mock_runner_with_step, mock_step_execution
    ):
        """on_runner_death() updates runner status to dead."""
        mock_db.execute.return_value = self._mock_execute_result(mock_step_execution)

        await service.on_runner_death(mock_db, mock_runner_with_step)

        assert mock_runner_with_step.status == "dead"

    @pytest.mark.asyncio
    async def test_on_runner_death_handles_no_current_step(self, service, mock_db):
        """on_runner_death() handles runner with no current step."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "idle"
        runner.current_step_execution_id = None

        # Should not raise
        await service.on_runner_death(mock_db, runner)

    @pytest.mark.asyncio
    async def test_on_runner_death_skips_completed_step(
        self, service, mock_db, mock_runner_with_step
    ):
        """on_runner_death() doesn't requeue already completed step."""
        completed_step = MagicMock()
        completed_step.id = "step-123"
        completed_step.status = "completed"  # Already done
        completed_step.runner_id = "runner-1"

        mock_db.execute.return_value = self._mock_execute_result(completed_step)

        await service.on_runner_death(mock_db, mock_runner_with_step)

        # Status should not change
        assert completed_step.status == "completed"


class TestRunnerDisconnect:
    """Tests for handling runner disconnection (WebSocket closes)."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock()
        return db

    def _mock_execute_result(self, step):
        """Create a mock execute result that returns step via scalar_one_or_none()."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = step
        return mock_result

    @pytest.mark.asyncio
    async def test_on_runner_disconnect_requeues_step(self, service, mock_db):
        """on_runner_disconnect() requeues step if runner was busy."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "busy"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.status = "running"
        step.runner_id = "runner-1"

        mock_db.execute.return_value = self._mock_execute_result(step)

        await service.on_runner_disconnect(mock_db, runner)

        assert step.status == "pending"
        assert step.runner_id is None

    @pytest.mark.asyncio
    async def test_on_runner_disconnect_idle_runner_no_requeue(self, service, mock_db):
        """on_runner_disconnect() does nothing for idle runner."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "idle"
        runner.current_step_execution_id = None

        await service.on_runner_disconnect(mock_db, runner)

        # Should complete without error, no step to requeue
        mock_db.execute.assert_not_called()


class TestRunnerReconnect:
    """Tests for handling runner reconnection."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock()
        return db

    def _mock_execute_result(self, step):
        """Create a mock execute result that returns step via scalar_one_or_none()."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = step
        return mock_result

    @pytest.mark.asyncio
    async def test_on_runner_reconnect_returns_continue_if_step_still_assigned(
        self, service, mock_db
    ):
        """on_runner_reconnect() returns 'continue' if step still assigned."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.runner_id = "runner-1"  # Still assigned to this runner

        mock_db.execute.return_value = self._mock_execute_result(step)

        result = await service.on_runner_reconnect(mock_db, runner)

        assert result["action"] == "continue"
        assert result.get("step_id") == "step-123"

    @pytest.mark.asyncio
    async def test_on_runner_reconnect_returns_abort_if_step_reassigned(
        self, service, mock_db
    ):
        """on_runner_reconnect() returns 'abort' if step was reassigned."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.runner_id = "runner-2"  # Reassigned to different runner

        mock_db.execute.return_value = self._mock_execute_result(step)

        result = await service.on_runner_reconnect(mock_db, runner)

        assert result["action"] == "abort"
        assert result.get("step_id") == "step-123"

    @pytest.mark.asyncio
    async def test_on_runner_reconnect_clears_stale_step_reference(
        self, service, mock_db
    ):
        """on_runner_reconnect() clears runner's step reference if reassigned."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.runner_id = "runner-2"  # Reassigned

        mock_db.execute.return_value = self._mock_execute_result(step)

        await service.on_runner_reconnect(mock_db, runner)

        # Runner should no longer reference the step
        assert runner.current_step_execution_id is None

    @pytest.mark.asyncio
    async def test_on_runner_reconnect_returns_idle_if_no_step(
        self, service, mock_db
    ):
        """on_runner_reconnect() returns 'idle' if runner had no step."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.current_step_execution_id = None

        result = await service.on_runner_reconnect(mock_db, runner)

        assert result["action"] == "idle"


class TestBackendStartupRecovery:
    """Tests for recovering orphaned steps on backend startup."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_recover_orphaned_steps_finds_running_with_dead_runner(
        self, service, mock_db
    ):
        """recover_orphaned_steps() finds steps with dead/disconnected runners."""
        orphaned_step = MagicMock()
        orphaned_step.id = "step-123"
        orphaned_step.status = "running"
        orphaned_step.runner_id = "dead-runner"

        # Mock query to return orphaned step
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [orphaned_step]
        mock_db.execute = AsyncMock(return_value=mock_result)

        recovered = await service.recover_orphaned_steps(mock_db)

        assert len(recovered) == 1
        assert recovered[0].id == "step-123"

    @pytest.mark.asyncio
    async def test_recover_orphaned_steps_marks_pending(self, service, mock_db):
        """recover_orphaned_steps() marks orphaned steps as pending."""
        orphaned_step = MagicMock()
        orphaned_step.id = "step-123"
        orphaned_step.status = "running"
        orphaned_step.runner_id = "dead-runner"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [orphaned_step]
        mock_db.execute = AsyncMock(return_value=mock_result)

        await service.recover_orphaned_steps(mock_db)

        assert orphaned_step.status == "pending"
        assert orphaned_step.runner_id is None

    @pytest.mark.asyncio
    async def test_recover_orphaned_steps_handles_preparing_state(
        self, service, mock_db
    ):
        """recover_orphaned_steps() also recovers steps in 'preparing' state."""
        preparing_step = MagicMock()
        preparing_step.id = "step-456"
        preparing_step.status = "preparing"
        preparing_step.runner_id = "disconnected-runner"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [preparing_step]
        mock_db.execute = AsyncMock(return_value=mock_result)

        recovered = await service.recover_orphaned_steps(mock_db)

        assert len(recovered) == 1
        assert preparing_step.status == "pending"

    @pytest.mark.asyncio
    async def test_recover_orphaned_steps_skips_terminal_states(
        self, service, mock_db
    ):
        """recover_orphaned_steps() skips completed/failed/cancelled steps."""
        # These should not be returned by the query in the first place
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        recovered = await service.recover_orphaned_steps(mock_db)

        assert len(recovered) == 0


class TestIdempotency:
    """Tests for idempotent recovery operations."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock()
        return db

    def _mock_execute_result(self, step):
        """Create a mock execute result that returns step via scalar_one_or_none()."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = step
        return mock_result

    @pytest.mark.asyncio
    async def test_double_death_handling_is_safe(self, service, mock_db):
        """Calling on_runner_death twice is safe (idempotent)."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "busy"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.status = "running"
        step.runner_id = "runner-1"

        mock_db.execute.return_value = self._mock_execute_result(step)

        # First call
        await service.on_runner_death(mock_db, runner)

        # Step is now pending
        step.status = "pending"
        step.runner_id = None
        runner.status = "dead"

        # Second call should be safe
        await service.on_runner_death(mock_db, runner)

        # Status should still be pending
        assert step.status == "pending"

    @pytest.mark.asyncio
    async def test_recovery_of_already_recovered_step_is_safe(
        self, service, mock_db
    ):
        """Recovering an already-recovered step is safe."""
        step = MagicMock()
        step.id = "step-123"
        step.status = "pending"  # Already recovered
        step.runner_id = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [step]
        mock_db.execute = AsyncMock(return_value=mock_result)

        # Should not raise or cause issues
        await service.recover_orphaned_steps(mock_db)


class TestConcurrentAccess:
    """Tests for concurrent recovery scenarios."""

    @pytest.fixture
    def service(self):
        """Create a JobRecoveryService."""
        return JobRecoveryService()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock()
        return db

    def _mock_execute_result(self, step):
        """Create a mock execute result that returns step via scalar_one_or_none()."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = step
        return mock_result

    @pytest.mark.asyncio
    async def test_concurrent_death_and_reconnect_handled(self, service, mock_db):
        """Concurrent death detection and reconnection is handled correctly."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.current_step_execution_id = "step-123"

        step = MagicMock()
        step.id = "step-123"
        step.status = "running"
        step.runner_id = "runner-1"

        mock_db.execute.return_value = self._mock_execute_result(step)

        # Simulate concurrent operations
        async def death_handler():
            runner.status = "busy"
            await service.on_runner_death(mock_db, runner)

        async def reconnect_handler():
            await asyncio.sleep(0.01)  # Slight delay
            result = await service.on_runner_reconnect(mock_db, runner)
            return result

        # Run concurrently
        death_task = asyncio.create_task(death_handler())
        reconnect_task = asyncio.create_task(reconnect_handler())

        await death_task
        result = await reconnect_task

        # One of these outcomes is valid:
        # 1. Death processed first -> step requeued -> reconnect returns abort/idle
        # 2. Reconnect processed first -> returns continue -> death marks dead
        assert result["action"] in ("abort", "idle", "continue")
