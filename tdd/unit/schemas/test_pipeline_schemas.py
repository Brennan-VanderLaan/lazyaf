"""
Unit tests for Pipeline Pydantic schemas.

These tests verify schema validation, serialization, and deserialization
for pipeline-related data structures.
"""
import sys
import json
from pathlib import Path
from datetime import datetime

import pytest
from pydantic import ValidationError

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas.pipeline import (
    PipelineStepConfig,
    PipelineBase,
    PipelineCreate,
    PipelineUpdate,
    PipelineRead,
    PipelineRunRead,
    PipelineRunCreate,
    StepRunRead,
)
from app.models.card import StepType
from app.models.pipeline import RunStatus


class TestPipelineStepConfigSchema:
    """Tests for PipelineStepConfig schema."""

    def test_valid_script_step(self):
        """Script step should be valid with required fields."""
        step = PipelineStepConfig(
            name="Lint",
            type=StepType.SCRIPT,
            config={"command": "npm run lint"},
        )
        assert step.name == "Lint"
        assert step.type == StepType.SCRIPT
        assert step.config["command"] == "npm run lint"

    def test_valid_docker_step(self):
        """Docker step should be valid with image and command."""
        step = PipelineStepConfig(
            name="Build",
            type=StepType.DOCKER,
            config={"image": "node:20", "command": "npm run build"},
        )
        assert step.type == StepType.DOCKER
        assert step.config["image"] == "node:20"

    def test_valid_agent_step(self):
        """Agent step should be valid with runner_type config."""
        step = PipelineStepConfig(
            name="Implement Feature",
            type=StepType.AGENT,
            config={
                "runner_type": "claude-code",
                "title": "Add login",
                "description": "Implement OAuth login",
            },
        )
        assert step.type == StepType.AGENT
        assert step.config["runner_type"] == "claude-code"

    def test_default_on_success_is_next(self):
        """Default on_success should be 'next'."""
        step = PipelineStepConfig(name="Test", type=StepType.SCRIPT)
        assert step.on_success == "next"

    def test_default_on_failure_is_stop(self):
        """Default on_failure should be 'stop'."""
        step = PipelineStepConfig(name="Test", type=StepType.SCRIPT)
        assert step.on_failure == "stop"

    def test_default_timeout_is_300(self):
        """Default timeout should be 300 seconds."""
        step = PipelineStepConfig(name="Test", type=StepType.SCRIPT)
        assert step.timeout == 300

    def test_custom_on_success_actions(self):
        """Custom on_success actions should be accepted."""
        for action in ["next", "stop", "merge:main", "trigger:card-123"]:
            step = PipelineStepConfig(
                name="Test",
                type=StepType.SCRIPT,
                on_success=action,
            )
            assert step.on_success == action

    def test_custom_on_failure_actions(self):
        """Custom on_failure actions should be accepted."""
        for action in ["next", "stop", "trigger:fix-card"]:
            step = PipelineStepConfig(
                name="Test",
                type=StepType.SCRIPT,
                on_failure=action,
            )
            assert step.on_failure == action

    def test_custom_timeout(self):
        """Custom timeout should be accepted."""
        step = PipelineStepConfig(
            name="Long Step",
            type=StepType.SCRIPT,
            timeout=600,
        )
        assert step.timeout == 600

    def test_missing_name_raises_error(self):
        """Missing name should raise ValidationError."""
        with pytest.raises(ValidationError):
            PipelineStepConfig(type=StepType.SCRIPT)

    def test_missing_type_raises_error(self):
        """Missing type should raise ValidationError."""
        with pytest.raises(ValidationError):
            PipelineStepConfig(name="Test")

    def test_empty_config_defaults_to_empty_dict(self):
        """Empty config should default to empty dict."""
        step = PipelineStepConfig(name="Test", type=StepType.SCRIPT)
        assert step.config == {}


class TestPipelineBaseSchema:
    """Tests for PipelineBase schema."""

    def test_valid_with_name_only(self):
        """PipelineBase should be valid with just name."""
        pipeline = PipelineBase(name="CI Pipeline")
        assert pipeline.name == "CI Pipeline"
        assert pipeline.description is None

    def test_valid_with_description(self):
        """PipelineBase should accept optional description."""
        pipeline = PipelineBase(
            name="CI Pipeline",
            description="Run tests and build",
        )
        assert pipeline.description == "Run tests and build"

    def test_missing_name_raises_error(self):
        """Missing name should raise ValidationError."""
        with pytest.raises(ValidationError):
            PipelineBase(description="No name")


class TestPipelineCreateSchema:
    """Tests for PipelineCreate schema."""

    def test_valid_empty_steps(self):
        """PipelineCreate should allow empty steps list."""
        pipeline = PipelineCreate(name="Empty Pipeline")
        assert pipeline.steps == []
        assert pipeline.is_template is False

    def test_valid_with_steps(self):
        """PipelineCreate should accept steps list."""
        steps = [
            PipelineStepConfig(name="Test", type=StepType.SCRIPT, config={"command": "npm test"}),
        ]
        pipeline = PipelineCreate(name="CI Pipeline", steps=steps)
        assert len(pipeline.steps) == 1
        assert pipeline.steps[0].name == "Test"

    def test_is_template_default_false(self):
        """is_template should default to False."""
        pipeline = PipelineCreate(name="Pipeline")
        assert pipeline.is_template is False

    def test_is_template_can_be_set(self):
        """is_template can be set to True."""
        pipeline = PipelineCreate(name="Template", is_template=True)
        assert pipeline.is_template is True


class TestPipelineUpdateSchema:
    """Tests for PipelineUpdate schema."""

    def test_all_fields_optional(self):
        """All fields should be optional for partial updates."""
        update = PipelineUpdate()
        assert update.name is None
        assert update.description is None
        assert update.steps is None
        assert update.is_template is None

    def test_partial_update_name(self):
        """Should accept name-only update."""
        update = PipelineUpdate(name="New Name")
        assert update.name == "New Name"
        assert update.steps is None

    def test_partial_update_steps(self):
        """Should accept steps-only update."""
        steps = [
            PipelineStepConfig(name="Test", type=StepType.SCRIPT),
        ]
        update = PipelineUpdate(steps=steps)
        assert len(update.steps) == 1
        assert update.name is None


class TestPipelineReadSchema:
    """Tests for PipelineRead schema."""

    def test_parse_steps_from_json_string(self):
        """Should parse steps from JSON string."""
        pipeline = PipelineRead(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test",
            steps='[{"name": "Test", "type": "script", "config": {}, "on_success": "next", "on_failure": "stop", "timeout": 300}]',
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        assert len(pipeline.steps) == 1
        assert pipeline.steps[0].name == "Test"

    def test_parse_steps_from_list(self):
        """Should accept steps as list directly."""
        pipeline = PipelineRead(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test",
            steps=[{"name": "Test", "type": "script"}],
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        assert len(pipeline.steps) == 1

    def test_parse_invalid_json_returns_empty(self):
        """Invalid JSON should result in empty steps list."""
        pipeline = PipelineRead(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test",
            steps="invalid json",
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        assert pipeline.steps == []

    def test_has_all_required_fields(self):
        """PipelineRead should have all required fields."""
        pipeline = PipelineRead(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test Pipeline",
            steps=[],
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        assert pipeline.id == "pipeline-123"
        assert pipeline.repo_id == "repo-456"
        assert pipeline.name == "Test Pipeline"
        assert pipeline.is_template is False


class TestPipelineRunCreateSchema:
    """Tests for PipelineRunCreate schema."""

    def test_default_trigger_type_is_manual(self):
        """Default trigger_type should be 'manual'."""
        run = PipelineRunCreate()
        assert run.trigger_type == "manual"

    def test_custom_trigger_type(self):
        """Should accept custom trigger types."""
        for trigger in ["manual", "webhook", "card", "push", "schedule"]:
            run = PipelineRunCreate(trigger_type=trigger)
            assert run.trigger_type == trigger

    def test_trigger_ref_optional(self):
        """trigger_ref should be optional."""
        run = PipelineRunCreate()
        assert run.trigger_ref is None

    def test_params_optional(self):
        """params should be optional."""
        run = PipelineRunCreate()
        assert run.params is None

    def test_params_can_be_dict(self):
        """params can be a dictionary."""
        run = PipelineRunCreate(params={"branch": "feature-x"})
        assert run.params["branch"] == "feature-x"


class TestPipelineRunReadSchema:
    """Tests for PipelineRunRead schema."""

    def test_has_all_required_fields(self):
        """PipelineRunRead should have all required fields."""
        run = PipelineRunRead(
            id="run-123",
            pipeline_id="pipeline-456",
            status=RunStatus.RUNNING,
            trigger_type="manual",
            current_step=0,
            steps_completed=0,
            steps_total=3,
            created_at=datetime.utcnow(),
        )
        assert run.id == "run-123"
        assert run.pipeline_id == "pipeline-456"
        assert run.status == RunStatus.RUNNING

    def test_optional_timestamps(self):
        """started_at and completed_at should be optional."""
        run = PipelineRunRead(
            id="run-123",
            pipeline_id="pipeline-456",
            status=RunStatus.PENDING,
            trigger_type="manual",
            current_step=0,
            steps_completed=0,
            steps_total=2,
            created_at=datetime.utcnow(),
        )
        assert run.started_at is None
        assert run.completed_at is None

    def test_step_runs_defaults_empty(self):
        """step_runs should default to empty list."""
        run = PipelineRunRead(
            id="run-123",
            pipeline_id="pipeline-456",
            status=RunStatus.PENDING,
            trigger_type="manual",
            current_step=0,
            steps_completed=0,
            steps_total=2,
            created_at=datetime.utcnow(),
        )
        assert run.step_runs == []


class TestStepRunReadSchema:
    """Tests for StepRunRead schema."""

    def test_has_all_required_fields(self):
        """StepRunRead should have all required fields."""
        step = StepRunRead(
            id="step-123",
            pipeline_run_id="run-456",
            step_index=0,
            step_name="Test Step",
            status=RunStatus.RUNNING,
        )
        assert step.id == "step-123"
        assert step.pipeline_run_id == "run-456"
        assert step.step_index == 0
        assert step.step_name == "Test Step"
        assert step.status == RunStatus.RUNNING

    def test_optional_fields(self):
        """Optional fields should have None defaults."""
        step = StepRunRead(
            id="step-123",
            pipeline_run_id="run-456",
            step_index=0,
            step_name="Test",
            status=RunStatus.PENDING,
        )
        assert step.job_id is None
        assert step.error is None
        assert step.started_at is None
        assert step.completed_at is None

    def test_logs_defaults_empty(self):
        """logs should default to empty string."""
        step = StepRunRead(
            id="step-123",
            pipeline_run_id="run-456",
            step_index=0,
            step_name="Test",
            status=RunStatus.PENDING,
        )
        assert step.logs == ""
