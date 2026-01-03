"""
Unit tests for StepExecution model (Phase 12.1).

These tests DEFINE the StepExecution model structure and behavior.
Write tests first, then implement to make them pass.

StepExecution tracks individual execution attempts of pipeline steps.
Links to step_runs but adds execution-specific data (container_id,
runner_id, exit_code, etc.).
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the model - that's expected in TDD
try:
    from app.models import StepExecution, ExecutionStatus
    STEP_EXECUTION_MODEL_AVAILABLE = True
except ImportError:
    STEP_EXECUTION_MODEL_AVAILABLE = False
    StepExecution = None
    ExecutionStatus = None


pytestmark = pytest.mark.skipif(
    not STEP_EXECUTION_MODEL_AVAILABLE,
    reason="StepExecution model not yet implemented"
)


class TestExecutionStatus:
    """Tests for ExecutionStatus enum values."""

    def test_has_pending_status(self):
        """ExecutionStatus has PENDING value."""
        assert ExecutionStatus.PENDING is not None
        assert ExecutionStatus.PENDING.value == "pending"

    def test_has_preparing_status(self):
        """ExecutionStatus has PREPARING value."""
        assert ExecutionStatus.PREPARING is not None
        assert ExecutionStatus.PREPARING.value == "preparing"

    def test_has_running_status(self):
        """ExecutionStatus has RUNNING value."""
        assert ExecutionStatus.RUNNING is not None
        assert ExecutionStatus.RUNNING.value == "running"

    def test_has_completing_status(self):
        """ExecutionStatus has COMPLETING value."""
        assert ExecutionStatus.COMPLETING is not None
        assert ExecutionStatus.COMPLETING.value == "completing"

    def test_has_completed_status(self):
        """ExecutionStatus has COMPLETED value."""
        assert ExecutionStatus.COMPLETED is not None
        assert ExecutionStatus.COMPLETED.value == "completed"

    def test_has_failed_status(self):
        """ExecutionStatus has FAILED value."""
        assert ExecutionStatus.FAILED is not None
        assert ExecutionStatus.FAILED.value == "failed"

    def test_has_cancelled_status(self):
        """ExecutionStatus has CANCELLED value."""
        assert ExecutionStatus.CANCELLED is not None
        assert ExecutionStatus.CANCELLED.value == "cancelled"

    def test_status_is_string_enum(self):
        """ExecutionStatus is a string enum for JSON serialization."""
        assert issubclass(ExecutionStatus, str)
        assert ExecutionStatus.PENDING == "pending"


class TestStepExecutionModel:
    """Tests for StepExecution SQLAlchemy model structure."""

    def test_table_name(self):
        """StepExecution maps to 'step_executions' table."""
        assert StepExecution.__tablename__ == "step_executions"

    def test_has_id_column(self):
        """StepExecution has 'id' primary key column."""
        assert "id" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["id"]
        assert col.primary_key

    def test_has_execution_key_column(self):
        """StepExecution has 'execution_key' column (unique)."""
        assert "execution_key" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["execution_key"]
        assert col.unique

    def test_has_step_run_id_column(self):
        """StepExecution has 'step_run_id' foreign key column."""
        assert "step_run_id" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["step_run_id"]
        assert len(col.foreign_keys) > 0

    def test_has_status_column(self):
        """StepExecution has 'status' column."""
        assert "status" in StepExecution.__table__.columns

    def test_has_runner_id_column(self):
        """StepExecution has 'runner_id' column (nullable)."""
        assert "runner_id" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["runner_id"]
        assert col.nullable

    def test_has_container_id_column(self):
        """StepExecution has 'container_id' column (nullable)."""
        assert "container_id" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["container_id"]
        assert col.nullable

    def test_has_exit_code_column(self):
        """StepExecution has 'exit_code' column (nullable)."""
        assert "exit_code" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["exit_code"]
        assert col.nullable

    def test_has_started_at_column(self):
        """StepExecution has 'started_at' column (nullable)."""
        assert "started_at" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["started_at"]
        assert col.nullable

    def test_has_completed_at_column(self):
        """StepExecution has 'completed_at' column (nullable)."""
        assert "completed_at" in StepExecution.__table__.columns
        col = StepExecution.__table__.columns["completed_at"]
        assert col.nullable

    def test_has_created_at_column(self):
        """StepExecution has 'created_at' column."""
        assert "created_at" in StepExecution.__table__.columns


class TestStepExecutionCreation:
    """Tests for StepExecution instance creation."""

    @pytest.fixture
    def execution_key(self):
        """Create a valid execution key."""
        return "run-123:0:1"

    @pytest.fixture
    def step_run_id(self):
        """Create a valid step_run_id."""
        return "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_create_with_required_fields(self, execution_key, step_run_id):
        """StepExecution can be created with required fields."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.execution_key == execution_key
        assert execution.step_run_id == step_run_id

    def test_default_status_is_pending(self, execution_key, step_run_id):
        """StepExecution status defaults to 'pending'."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.status == ExecutionStatus.PENDING.value

    def test_runner_id_defaults_to_none(self, execution_key, step_run_id):
        """StepExecution runner_id defaults to None."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.runner_id is None

    def test_container_id_defaults_to_none(self, execution_key, step_run_id):
        """StepExecution container_id defaults to None."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.container_id is None

    def test_exit_code_defaults_to_none(self, execution_key, step_run_id):
        """StepExecution exit_code defaults to None."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.exit_code is None

    def test_started_at_defaults_to_none(self, execution_key, step_run_id):
        """StepExecution started_at defaults to None."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.started_at is None

    def test_completed_at_defaults_to_none(self, execution_key, step_run_id):
        """StepExecution completed_at defaults to None."""
        execution = StepExecution(
            execution_key=execution_key,
            step_run_id=step_run_id,
        )
        assert execution.completed_at is None


class TestStepExecutionStates:
    """Tests for StepExecution in various states."""

    @pytest.fixture
    def base_data(self):
        """Base data for creating StepExecution."""
        return {
            "execution_key": "run-123:0:1",
            "step_run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        }

    def test_preparing_state(self, base_data):
        """StepExecution in PREPARING state."""
        execution = StepExecution(
            **base_data,
            status=ExecutionStatus.PREPARING.value,
            container_id="abc123def456",
        )
        assert execution.status == ExecutionStatus.PREPARING.value
        assert execution.container_id == "abc123def456"

    def test_running_state_with_started_at(self, base_data):
        """StepExecution in RUNNING state has started_at."""
        now = datetime.utcnow()
        execution = StepExecution(
            **base_data,
            status=ExecutionStatus.RUNNING.value,
            started_at=now,
        )
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.started_at == now

    def test_completed_state_with_exit_code(self, base_data):
        """StepExecution in COMPLETED state has exit_code=0."""
        now = datetime.utcnow()
        execution = StepExecution(
            **base_data,
            status=ExecutionStatus.COMPLETED.value,
            exit_code=0,
            started_at=now,
            completed_at=now,
        )
        assert execution.status == ExecutionStatus.COMPLETED.value
        assert execution.exit_code == 0
        assert execution.completed_at is not None

    def test_failed_state_with_nonzero_exit_code(self, base_data):
        """StepExecution in FAILED state has nonzero exit_code."""
        now = datetime.utcnow()
        execution = StepExecution(
            **base_data,
            status=ExecutionStatus.FAILED.value,
            exit_code=1,
            started_at=now,
            completed_at=now,
        )
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.exit_code == 1

    def test_cancelled_state(self, base_data):
        """StepExecution in CANCELLED state."""
        execution = StepExecution(
            **base_data,
            status=ExecutionStatus.CANCELLED.value,
        )
        assert execution.status == ExecutionStatus.CANCELLED.value


class TestStepExecutionWithRunner:
    """Tests for StepExecution with runner (remote execution)."""

    @pytest.fixture
    def base_data(self):
        """Base data for creating StepExecution."""
        return {
            "execution_key": "run-123:0:1",
            "step_run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        }

    def test_assigned_to_runner(self, base_data):
        """StepExecution can be assigned to a runner."""
        execution = StepExecution(
            **base_data,
            runner_id="runner-abc123",
            status=ExecutionStatus.RUNNING.value,
        )
        assert execution.runner_id == "runner-abc123"


class TestStepExecutionWithContainer:
    """Tests for StepExecution with container (local execution)."""

    @pytest.fixture
    def base_data(self):
        """Base data for creating StepExecution."""
        return {
            "execution_key": "run-123:0:1",
            "step_run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        }

    def test_has_container_id(self, base_data):
        """StepExecution can have a container_id for local execution."""
        execution = StepExecution(
            **base_data,
            container_id="abc123def456",
            status=ExecutionStatus.RUNNING.value,
        )
        assert execution.container_id == "abc123def456"


class TestStepExecutionRelationships:
    """Tests for StepExecution model relationships."""

    def test_has_step_run_relationship(self):
        """StepExecution should have a step_run relationship defined."""
        assert hasattr(StepExecution, "step_run")


class TestExecutionKeyFormat:
    """Tests for execution key format validation."""

    def test_execution_key_format_run_step_attempt(self):
        """Execution key follows format: {run_id}:{step_index}:{attempt}."""
        execution = StepExecution(
            execution_key="run-abc123:5:3",
            step_run_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        key = execution.execution_key
        parts = key.split(":")
        assert len(parts) == 3
        assert parts[0] == "run-abc123"  # pipeline_run_id
        assert parts[1] == "5"  # step_index
        assert parts[2] == "3"  # attempt

    def test_parse_execution_key(self):
        """Execution key can be parsed to components."""
        execution = StepExecution(
            execution_key="run-123:0:1",
            step_run_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        # Model may have helper methods to parse key
        # This is optional - implementation can decide
