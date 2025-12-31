"""
Unit tests for Pipeline, PipelineRun, and StepRun models.

These tests verify the model structures, status transitions,
relationships, and JSON serialization without touching the database.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.pipeline import Pipeline, PipelineRun, StepRun, RunStatus

from tdd.shared.factories import (
    PipelineFactory,
    PipelineRunFactory,
    StepRunFactory,
)
from tdd.shared.assertions import (
    assert_model_has_id,
    assert_model_has_timestamps,
)


class TestRunStatus:
    """Tests for RunStatus enum."""

    def test_run_status_values(self):
        """RunStatus enum should have all expected values."""
        expected_statuses = {"pending", "running", "passed", "failed", "cancelled"}
        actual_statuses = {status.value for status in RunStatus}
        assert actual_statuses == expected_statuses

    def test_run_status_is_string_enum(self):
        """RunStatus should be a string enum for JSON serialization."""
        assert issubclass(RunStatus, str)
        assert RunStatus.PENDING == "pending"
        assert RunStatus.RUNNING == "running"
        assert RunStatus.PASSED == "passed"
        assert RunStatus.FAILED == "failed"
        assert RunStatus.CANCELLED == "cancelled"


class TestPipelineModel:
    """Tests for Pipeline SQLAlchemy model."""

    def test_pipeline_creation_with_required_fields(self):
        """Pipeline can be created with required fields only."""
        pipeline = PipelineFactory.build(
            name="CI Pipeline",
            description="Run tests and build",
        )

        assert pipeline.name == "CI Pipeline"
        assert pipeline.description == "Run tests and build"
        assert_model_has_id(pipeline)

    def test_pipeline_default_steps_is_empty_json(self):
        """Pipeline steps should default to empty JSON array."""
        pipeline = PipelineFactory.build()
        assert pipeline.steps == "[]"

    def test_pipeline_default_is_template_is_false(self):
        """Pipeline is_template should default to False."""
        pipeline = PipelineFactory.build()
        assert pipeline.is_template is False

    def test_pipeline_has_timestamps(self):
        """Pipeline should have created_at and updated_at timestamps."""
        pipeline = PipelineFactory.build()
        assert_model_has_timestamps(pipeline)

    def test_pipeline_table_name(self):
        """Pipeline model maps to 'pipelines' table."""
        assert Pipeline.__tablename__ == "pipelines"

    def test_pipeline_requires_repo_id(self):
        """Pipeline must have a repo_id."""
        pipeline = PipelineFactory.build()
        assert pipeline.repo_id is not None
        assert len(pipeline.repo_id) == 36  # UUID format

    def test_pipeline_with_steps_json(self):
        """Pipeline can store steps as JSON string."""
        steps_json = '[{"name": "Test", "type": "script", "config": {"command": "npm test"}}]'
        pipeline = PipelineFactory.build(steps=steps_json)
        assert pipeline.steps == steps_json

    def test_pipeline_template_trait(self):
        """Pipeline with template trait has is_template=True."""
        pipeline = PipelineFactory.build(is_template=True)
        assert pipeline.is_template is True


class TestPipelineRunModel:
    """Tests for PipelineRun SQLAlchemy model."""

    def test_pipeline_run_creation(self):
        """PipelineRun can be created with required fields."""
        run = PipelineRunFactory.build()

        assert_model_has_id(run)
        assert run.pipeline_id is not None

    def test_pipeline_run_default_status_is_pending(self):
        """PipelineRun status should default to 'pending'."""
        run = PipelineRunFactory.build()
        assert run.status == RunStatus.PENDING.value

    def test_pipeline_run_default_trigger_type_is_manual(self):
        """PipelineRun trigger_type should default to 'manual'."""
        run = PipelineRunFactory.build()
        assert run.trigger_type == "manual"

    def test_pipeline_run_default_step_counters(self):
        """PipelineRun should have zero step counters by default."""
        run = PipelineRunFactory.build()
        assert run.current_step == 0
        assert run.steps_completed == 0
        assert run.steps_total == 0

    def test_pipeline_run_timestamps_nullable(self):
        """PipelineRun started_at and completed_at can be None."""
        run = PipelineRunFactory.build()
        assert run.started_at is None
        assert run.completed_at is None

    def test_pipeline_run_table_name(self):
        """PipelineRun model maps to 'pipeline_runs' table."""
        assert PipelineRun.__tablename__ == "pipeline_runs"

    def test_pipeline_run_running_state(self):
        """PipelineRun in RUNNING state has started_at set."""
        run = PipelineRunFactory.build(running=True)
        assert run.status == RunStatus.RUNNING.value
        assert run.started_at is not None

    def test_pipeline_run_passed_state(self):
        """PipelineRun in PASSED state has completed_at set."""
        run = PipelineRunFactory.build(passed=True)
        assert run.status == RunStatus.PASSED.value
        assert run.completed_at is not None

    def test_pipeline_run_failed_state(self):
        """PipelineRun in FAILED state has completed_at set."""
        run = PipelineRunFactory.build(failed=True)
        assert run.status == RunStatus.FAILED.value
        assert run.completed_at is not None

    def test_pipeline_run_cancelled_state(self):
        """PipelineRun in CANCELLED state has completed_at set."""
        run = PipelineRunFactory.build(cancelled=True)
        assert run.status == RunStatus.CANCELLED.value
        assert run.completed_at is not None


class TestStepRunModel:
    """Tests for StepRun SQLAlchemy model."""

    def test_step_run_creation(self):
        """StepRun can be created with required fields."""
        step_run = StepRunFactory.build(
            step_index=0,
            step_name="Test Step",
        )

        assert_model_has_id(step_run)
        assert step_run.step_index == 0
        assert step_run.step_name == "Test Step"

    def test_step_run_default_status_is_pending(self):
        """StepRun status should default to 'pending'."""
        step_run = StepRunFactory.build()
        assert step_run.status == RunStatus.PENDING.value

    def test_step_run_default_logs_is_empty(self):
        """StepRun logs should default to empty string."""
        step_run = StepRunFactory.build()
        assert step_run.logs == ""

    def test_step_run_optional_job_id(self):
        """StepRun job_id can be None."""
        step_run = StepRunFactory.build()
        assert step_run.job_id is None

    def test_step_run_optional_error(self):
        """StepRun error can be None."""
        step_run = StepRunFactory.build()
        assert step_run.error is None

    def test_step_run_table_name(self):
        """StepRun model maps to 'step_runs' table."""
        assert StepRun.__tablename__ == "step_runs"

    def test_step_run_running_state(self):
        """StepRun in RUNNING state has started_at set."""
        step_run = StepRunFactory.build(running=True)
        assert step_run.status == RunStatus.RUNNING.value
        assert step_run.started_at is not None

    def test_step_run_passed_state(self):
        """StepRun in PASSED state has completed_at set."""
        step_run = StepRunFactory.build(passed=True)
        assert step_run.status == RunStatus.PASSED.value
        assert step_run.completed_at is not None

    def test_step_run_failed_state(self):
        """StepRun in FAILED state has error and completed_at set."""
        step_run = StepRunFactory.build(failed=True)
        assert step_run.status == RunStatus.FAILED.value
        assert step_run.error is not None
        assert step_run.completed_at is not None

    def test_step_run_with_job(self):
        """StepRun can be linked to a job."""
        step_run = StepRunFactory.build(with_job=True)
        assert step_run.job_id is not None
        assert len(step_run.job_id) == 36  # UUID format


class TestPipelineRelationships:
    """Tests for Pipeline model relationships."""

    def test_pipeline_has_repo_relationship(self):
        """Pipeline should have a repo relationship defined."""
        assert hasattr(Pipeline, "repo")

    def test_pipeline_has_runs_relationship(self):
        """Pipeline should have a runs relationship defined."""
        assert hasattr(Pipeline, "runs")


class TestPipelineRunRelationships:
    """Tests for PipelineRun model relationships."""

    def test_pipeline_run_has_pipeline_relationship(self):
        """PipelineRun should have a pipeline relationship defined."""
        assert hasattr(PipelineRun, "pipeline")

    def test_pipeline_run_has_step_runs_relationship(self):
        """PipelineRun should have a step_runs relationship defined."""
        assert hasattr(PipelineRun, "step_runs")


class TestStepRunRelationships:
    """Tests for StepRun model relationships."""

    def test_step_run_has_pipeline_run_relationship(self):
        """StepRun should have a pipeline_run relationship defined."""
        assert hasattr(StepRun, "pipeline_run")
