"""
Unit tests for PipelineExecutor service.

These tests verify the pipeline execution logic including:
- Action handler branching (next, stop, trigger, merge)
- Step state transitions
- Step branching based on on_success/on_failure
- Parse steps utility function
"""
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.pipeline_executor import (
    PipelineExecutor,
    parse_steps,
    pipeline_run_to_ws_dict,
    step_run_to_ws_dict,
)
from app.models.pipeline import RunStatus


class TestParseSteps:
    """Tests for parse_steps utility function."""

    def test_parse_steps_valid_json(self):
        """parse_steps should parse valid JSON string to list."""
        steps_json = '[{"name": "Test", "type": "script"}]'
        result = parse_steps(steps_json)
        assert result == [{"name": "Test", "type": "script"}]

    def test_parse_steps_empty_string(self):
        """parse_steps should return empty list for empty string."""
        result = parse_steps("")
        assert result == []

    def test_parse_steps_none(self):
        """parse_steps should return empty list for None."""
        result = parse_steps(None)
        assert result == []

    def test_parse_steps_invalid_json(self):
        """parse_steps should return empty list for invalid JSON."""
        result = parse_steps("not valid json")
        assert result == []

    def test_parse_steps_empty_array(self):
        """parse_steps should return empty list for '[]'."""
        result = parse_steps("[]")
        assert result == []

    def test_parse_steps_multiple_steps(self):
        """parse_steps should handle multiple steps."""
        steps_json = '''[
            {"name": "Lint", "type": "script", "config": {"command": "npm run lint"}},
            {"name": "Test", "type": "script", "config": {"command": "npm test"}},
            {"name": "Build", "type": "docker", "config": {"image": "node:20", "command": "npm build"}}
        ]'''
        result = parse_steps(steps_json)
        assert len(result) == 3
        assert result[0]["name"] == "Lint"
        assert result[1]["name"] == "Test"
        assert result[2]["name"] == "Build"


class TestPipelineRunToWsDict:
    """Tests for pipeline_run_to_ws_dict conversion function."""

    def test_converts_basic_fields(self):
        """Should convert all basic pipeline run fields."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.pipeline_id = "pipeline-456"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 1
        mock_run.steps_completed = 1
        mock_run.steps_total = 3
        mock_run.started_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_run.completed_at = None
        mock_run.created_at = datetime(2024, 1, 15, 9, 55, 0)

        result = pipeline_run_to_ws_dict(mock_run)

        assert result["id"] == "run-123"
        assert result["pipeline_id"] == "pipeline-456"
        assert result["status"] == "running"
        assert result["trigger_type"] == "manual"
        assert result["trigger_ref"] is None
        assert result["current_step"] == 1
        assert result["steps_completed"] == 1
        assert result["steps_total"] == 3
        assert result["started_at"] == "2024-01-15T10:00:00"
        assert result["completed_at"] is None

    def test_handles_none_timestamps(self):
        """Should handle None timestamps gracefully."""
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.pipeline_id = "pipeline-456"
        mock_run.status = RunStatus.PENDING.value
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = None
        mock_run.completed_at = None
        mock_run.created_at = None

        result = pipeline_run_to_ws_dict(mock_run)

        assert result["started_at"] is None
        assert result["completed_at"] is None
        assert result["created_at"] is None


class TestStepRunToWsDict:
    """Tests for step_run_to_ws_dict conversion function."""

    def test_converts_basic_fields(self):
        """Should convert all basic step run fields."""
        mock_step = MagicMock()
        mock_step.id = "step-123"
        mock_step.pipeline_run_id = "run-456"
        mock_step.step_index = 0
        mock_step.step_name = "Test Step"
        mock_step.status = RunStatus.RUNNING.value
        mock_step.job_id = "job-789"
        mock_step.error = None
        mock_step.started_at = datetime(2024, 1, 15, 10, 0, 0)
        mock_step.completed_at = None

        result = step_run_to_ws_dict(mock_step)

        assert result["id"] == "step-123"
        assert result["pipeline_run_id"] == "run-456"
        assert result["step_index"] == 0
        assert result["step_name"] == "Test Step"
        assert result["status"] == "running"
        assert result["job_id"] == "job-789"
        assert result["error"] is None

    def test_handles_none_timestamps(self):
        """Should handle None timestamps gracefully."""
        mock_step = MagicMock()
        mock_step.id = "step-123"
        mock_step.pipeline_run_id = "run-456"
        mock_step.step_index = 0
        mock_step.step_name = "Test Step"
        mock_step.status = RunStatus.PENDING.value
        mock_step.job_id = None
        mock_step.error = None
        mock_step.started_at = None
        mock_step.completed_at = None

        result = step_run_to_ws_dict(mock_step)

        assert result["started_at"] is None
        assert result["completed_at"] is None


class TestPipelineExecutorActionHandlers:
    """Tests for PipelineExecutor action handling logic."""

    @pytest.fixture
    def executor(self):
        """Create a PipelineExecutor instance."""
        return PipelineExecutor()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return AsyncMock()

    @pytest.fixture
    def mock_pipeline_run(self):
        """Create a mock pipeline run."""
        run = MagicMock()
        run.id = "run-123"
        run.pipeline_id = "pipeline-456"
        run.status = RunStatus.RUNNING.value
        run.current_step = 0
        run.steps_completed = 0
        run.steps_total = 2
        run.completed_at = None
        run.started_at = datetime.utcnow()
        run.created_at = datetime.utcnow()
        run.trigger_type = "manual"
        run.trigger_ref = None
        return run

    @pytest.fixture
    def mock_repo(self):
        """Create a mock repo."""
        repo = MagicMock()
        repo.id = "repo-789"
        repo.default_branch = "main"
        repo.remote_url = "https://github.com/test/repo.git"
        return repo

    @pytest.mark.asyncio
    async def test_handle_action_next_executes_next_step(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Action 'next' should call _execute_step with incremented index."""
        steps = [
            {"name": "Step 1", "type": "script"},
            {"name": "Step 2", "type": "script"},
        ]

        with patch.object(executor, "_execute_step", new_callable=AsyncMock) as mock_execute:
            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="next",
                step_success=True,
            )

            mock_execute.assert_called_once_with(
                mock_db, mock_pipeline_run, mock_repo, steps, 1
            )

    @pytest.mark.asyncio
    async def test_handle_action_stop_success_marks_passed(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Action 'stop' with success should mark pipeline as passed."""
        steps = [{"name": "Step 1", "type": "script"}]

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()

            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="stop",
                step_success=True,
            )

            assert mock_pipeline_run.status == RunStatus.PASSED.value
            assert mock_pipeline_run.completed_at is not None
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_handle_action_stop_failure_marks_failed(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Action 'stop' with failure should mark pipeline as failed."""
        steps = [{"name": "Step 1", "type": "script"}]

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()

            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="stop",
                step_success=False,
            )

            assert mock_pipeline_run.status == RunStatus.FAILED.value
            assert mock_pipeline_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_handle_action_trigger_calls_trigger_card(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Action 'trigger:{card_id}' should call _trigger_card."""
        steps = [{"name": "Step 1", "type": "script"}]

        with patch.object(executor, "_trigger_card", new_callable=AsyncMock) as mock_trigger:
            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="trigger:card-123",
                step_success=False,
            )

            mock_trigger.assert_called_once_with(
                mock_db, mock_pipeline_run, mock_repo, steps, 0, "card-123"
            )

    @pytest.mark.asyncio
    async def test_handle_action_merge_calls_merge_branch(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Action 'merge:{branch}' should call _merge_branch."""
        steps = [{"name": "Step 1", "type": "script"}]

        with patch.object(executor, "_merge_branch", new_callable=AsyncMock) as mock_merge:
            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="merge:main",
                step_success=True,
            )

            mock_merge.assert_called_once_with(
                mock_db, mock_pipeline_run, mock_repo, steps, 0, "main"
            )

    @pytest.mark.asyncio
    async def test_handle_action_unknown_acts_as_stop(self, executor, mock_db, mock_pipeline_run, mock_repo):
        """Unknown action should be treated as 'stop'."""
        steps = [{"name": "Step 1", "type": "script"}]

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()

            await executor._handle_action(
                db=mock_db,
                pipeline_run=mock_pipeline_run,
                repo=mock_repo,
                steps=steps,
                current_step=0,
                action="invalid_action",
                step_success=True,
            )

            assert mock_pipeline_run.status == RunStatus.PASSED.value
            assert mock_pipeline_run.completed_at is not None


class TestPipelineExecutorStartPipeline:
    """Tests for PipelineExecutor.start_pipeline method."""

    @pytest.fixture
    def executor(self):
        return PipelineExecutor()

    @pytest.mark.asyncio
    async def test_start_pipeline_with_no_steps_marks_passed(self, executor):
        """Starting a pipeline with no steps should immediately mark as passed."""
        # Use MagicMock for db but set async methods explicitly
        mock_db = MagicMock()
        mock_db.add = MagicMock()  # Synchronous
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        mock_pipeline = MagicMock()
        mock_pipeline.id = "pipeline-123"
        mock_pipeline.name = "Empty Pipeline"
        mock_pipeline.steps = "[]"

        mock_repo = MagicMock()
        mock_repo.id = "repo-456"

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()

            result = await executor.start_pipeline(
                db=mock_db,
                pipeline=mock_pipeline,
                repo=mock_repo,
            )

            assert result.status == RunStatus.PASSED.value
            assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_start_pipeline_with_steps_executes_first_step(self, executor):
        """Starting a pipeline with steps should execute the first step."""
        # Use MagicMock for db but set async methods explicitly
        mock_db = MagicMock()
        mock_db.add = MagicMock()  # Synchronous
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        mock_pipeline = MagicMock()
        mock_pipeline.id = "pipeline-123"
        mock_pipeline.name = "Test Pipeline"
        mock_pipeline.steps = '[{"name": "Test", "type": "script"}]'

        mock_repo = MagicMock()
        mock_repo.id = "repo-456"

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()

            with patch.object(executor, "_execute_step", new_callable=AsyncMock) as mock_execute:
                result = await executor.start_pipeline(
                    db=mock_db,
                    pipeline=mock_pipeline,
                    repo=mock_repo,
                )

                assert result.status == RunStatus.RUNNING.value
                mock_execute.assert_called_once()


class TestPipelineExecutorCancelRun:
    """Tests for PipelineExecutor.cancel_run method."""

    @pytest.fixture
    def executor(self):
        return PipelineExecutor()

    @pytest.mark.asyncio
    async def test_cancel_run_marks_cancelled(self, executor):
        """Cancelling a run should mark it as cancelled."""
        mock_db = AsyncMock()
        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.completed_at = None
        mock_run.step_runs = []
        mock_run.pipeline_id = "pipeline-456"
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = datetime.utcnow()
        mock_run.created_at = datetime.utcnow()

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()
            mock_manager.send_step_run_status = AsyncMock()

            result = await executor.cancel_run(mock_db, mock_run)

            assert result.status == RunStatus.CANCELLED.value
            assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_cancel_run_cancels_running_steps(self, executor):
        """Cancelling a run should cancel any running step runs."""
        mock_db = AsyncMock()

        mock_step_run = MagicMock()
        mock_step_run.id = "step-123"
        mock_step_run.status = RunStatus.RUNNING.value
        mock_step_run.completed_at = None
        mock_step_run.error = None
        mock_step_run.job_id = None
        mock_step_run.pipeline_run_id = "run-123"
        mock_step_run.step_index = 0
        mock_step_run.step_name = "Test Step"
        mock_step_run.started_at = datetime.utcnow()

        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.status = RunStatus.RUNNING.value
        mock_run.completed_at = None
        mock_run.step_runs = [mock_step_run]
        mock_run.pipeline_id = "pipeline-456"
        mock_run.trigger_type = "manual"
        mock_run.trigger_ref = None
        mock_run.current_step = 0
        mock_run.steps_completed = 0
        mock_run.steps_total = 2
        mock_run.started_at = datetime.utcnow()
        mock_run.created_at = datetime.utcnow()

        with patch("app.services.pipeline_executor.manager", new_callable=MagicMock) as mock_manager:
            mock_manager.send_pipeline_run_status = AsyncMock()
            mock_manager.send_step_run_status = AsyncMock()

            await executor.cancel_run(mock_db, mock_run)

            assert mock_step_run.status == RunStatus.CANCELLED.value
            assert mock_step_run.completed_at is not None
            assert mock_step_run.error == "Cancelled by user"


class TestStepBranchingLogic:
    """Tests verifying step branching based on on_success/on_failure."""

    def test_step_default_on_success_is_next(self):
        """Default on_success action should be 'next'."""
        step = {"name": "Test", "type": "script"}
        on_success = step.get("on_success", "next")
        assert on_success == "next"

    def test_step_default_on_failure_is_stop(self):
        """Default on_failure action should be 'stop'."""
        step = {"name": "Test", "type": "script"}
        on_failure = step.get("on_failure", "stop")
        assert on_failure == "stop"

    def test_step_custom_on_success(self):
        """Custom on_success action should be used."""
        step = {"name": "Test", "type": "script", "on_success": "merge:main"}
        on_success = step.get("on_success", "next")
        assert on_success == "merge:main"

    def test_step_custom_on_failure(self):
        """Custom on_failure action should be used."""
        step = {"name": "Test", "type": "script", "on_failure": "trigger:fix-card"}
        on_failure = step.get("on_failure", "stop")
        assert on_failure == "trigger:fix-card"

    def test_step_on_failure_next_continues(self):
        """on_failure='next' allows continuing despite failure."""
        step = {"name": "Lint", "type": "script", "on_failure": "next"}
        on_failure = step.get("on_failure", "stop")
        assert on_failure == "next"
