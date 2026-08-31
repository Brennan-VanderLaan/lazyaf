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


class TestPipelineReadDefinitionError:
    """PipelineRead.definition_error - the one net-new schema surface (12.8).

    sync_repo_pipelines swallows every parse exception into a logger.warning
    and keeps the STALE definition on purpose ("a broken CI file must not
    break the push"). A conversion REFUSAL landing there would be dark by
    construction, which would make the whole refuse-loudly strategy an R1
    violation. This field is the channel those refusals surface on.
    """

    def _read(self, **overrides):
        kwargs = dict(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test Pipeline",
            steps=[],
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        kwargs.update(overrides)
        return PipelineRead(**kwargs)

    def test_defaults_to_none_when_absent(self):
        """A pipeline whose definition materialized carries no error."""
        assert self._read().definition_error is None

    def test_carries_the_refusal_text(self):
        """The reason travels to the client, not just to a log line."""
        reason = (
            "step 'deploy': 'trigger:pipeline:' is retired (12.8); chain "
            "pipelines with a card_complete or push trigger"
        )
        assert self._read(definition_error=reason).definition_error == reason

    def test_it_is_on_the_wire(self):
        assert "definition_error" in PipelineRead.model_fields
        assert self._read().model_dump()["definition_error"] is None

    def test_it_is_read_only(self):
        """A client does not get to declare its own pipeline broken (or fixed).

        The field is written by upsert_materialized_pipeline from a real
        conversion outcome. Accepting it on the input schemas would let a
        caller either hide a refusal or invent one.
        """
        assert "definition_error" not in PipelineCreate.model_fields
        assert "definition_error" not in PipelineUpdate.model_fields

    def test_from_attributes_falls_back_when_the_attribute_is_absent(self):
        """The mechanism the sequencing rests on, isolated.

        This field lands at P1; the additive ALTER that gives Pipeline the
        matching column is the migration's, at P4. Between the two, the ORM
        row has no such attribute at all - so `from_attributes` MUST fall
        back to the default rather than raise, or every pipeline response
        500s for the whole window.
        """
        class RowWithoutTheColumn:
            id = "pipeline-123"
            repo_id = "repo-456"
            name = "Test Pipeline"
            description = None
            steps = "[]"
            steps_graph = None
            triggers = "[]"
            is_template = False
            created_at = datetime.utcnow()
            updated_at = datetime.utcnow()

        read = PipelineRead.model_validate(RowWithoutTheColumn())
        assert read.definition_error is None
        assert read.id == "pipeline-123"

    def test_a_real_orm_row_serializes(self):
        """The same claim against the actual model, and it stays true after
        the column lands: absent today, NULL on an unflushed row tomorrow,
        None on the wire either way."""
        from app.models.pipeline import Pipeline

        row = Pipeline(
            id="pipeline-123",
            repo_id="repo-456",
            name="Test Pipeline",
            steps="[]",
            triggers="[]",
            is_template=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        read = PipelineRead.model_validate(row)
        assert read.definition_error is None
        assert read.id == "pipeline-123"
        assert read.name == "Test Pipeline"


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


# =============================================================================
# 12.8 P2 - PipelineStepConfig.id (§1.6b)
# =============================================================================


class TestPipelineStepConfigId:
    """§1.6(b): the array step gains an optional stable id.

    `PipelineStepYaml.id` has always accepted one ("Optional stable ID for
    context directory references") and `.lazyaf/pipelines/test-suite.yaml`
    uses it on all ten steps - but `PipelineStepConfig`, the type
    `array_to_graph` converts, had nowhere to put it. So the conversion
    renamed every node to `step_{index}`, taking the context-directory names,
    the debug breakpoint keys and the readability of the graph with it.

    Optional, not required: every v1 row already persisted was written
    without one, and `array_to_graph` falls back to `step_{index}` exactly as
    before. Purely additive.
    """

    def test_id_defaults_to_none(self):
        step = PipelineStepConfig(name="Test", type=StepType.SCRIPT)

        assert step.id is None

    def test_an_id_is_carried(self):
        step = PipelineStepConfig(name="Tier 1", type=StepType.SCRIPT, id="tier1")

        assert step.id == "tier1"

    def test_a_pre_p2_step_dict_still_validates_unchanged(self):
        """Every persisted `Pipeline.steps` row looks like this. It must keep
        parsing, with every other field untouched."""
        step = PipelineStepConfig.model_validate({
            "name": "Run Tests",
            "type": "script",
            "config": {"command": "pytest -q"},
            "on_success": "next",
            "on_failure": "stop",
            "timeout": 1800,
            "continue_in_context": True,
        })

        assert step.id is None
        assert step.name == "Run Tests"
        assert step.type == StepType.SCRIPT
        assert step.config == {"command": "pytest -q"}
        assert step.on_success == "next"
        assert step.on_failure == "stop"
        assert step.timeout == 1800
        assert step.continue_in_context is True

    def test_the_yaml_step_shape_validates_straight_through(self):
        """§4.2's caller adapter is
        `PipelineStepConfig.model_validate(s.model_dump())` over a
        `PipelineStepYaml`. `id` is the field that used to be dropped on that
        hop - pinned here so the two shapes cannot drift apart again."""
        yaml_step = {
            "id": "verify-executor",
            "name": "Verify Executor",
            "type": "script",
            "config": {"command": "python scripts/verify_executor.py"},
            "on_success": "next",
            "on_failure": "stop",
            "timeout": 300,
            "continue_in_context": False,
        }

        step = PipelineStepConfig.model_validate(yaml_step)

        assert step.id == "verify-executor"

    def test_id_is_on_the_wire(self):
        step = PipelineStepConfig(name="T", type=StepType.SCRIPT, id="t")

        assert step.model_dump()["id"] == "t"

    def test_a_defaulted_id_serializes_as_null_not_as_a_missing_key(self):
        """A reader distinguishing 'no id' from 'id absent' would be a second
        code path; there is one shape."""
        dumped = PipelineStepConfig(name="T", type=StepType.SCRIPT).model_dump()

        assert "id" in dumped
        assert dumped["id"] is None

    def test_a_non_string_id_is_refused(self):
        with pytest.raises(ValidationError):
            PipelineStepConfig(name="T", type=StepType.SCRIPT, id=3)

    def test_an_id_survives_a_pipeline_read_round_trip(self):
        """`PipelineRead.steps` is `list[PipelineStepConfig]`, so the id has
        to survive the JSON-string parse the ORM row goes through."""
        pipeline = PipelineRead.model_validate({
            "id": "p1",
            "repo_id": "r1",
            "name": "P",
            "steps": json.dumps([
                {"id": "tier1", "name": "T1", "type": "script"},
                {"name": "T2", "type": "script"},
            ]),
            "steps_graph": None,
            "triggers": "[]",
            "is_template": False,
            "created_at": datetime(2026, 8, 30, 12, 0, 0),
            "updated_at": datetime(2026, 8, 30, 12, 0, 0),
        })

        assert [s.id for s in pipeline.steps] == ["tier1", None]
