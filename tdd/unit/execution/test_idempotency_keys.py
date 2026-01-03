"""
Tests for Idempotency Keys (Phase 12.1).

These tests DEFINE the idempotency contract for step executions.
Write tests first, then implement to make them pass.

Idempotency Key Format: "{pipeline_run_id}:{step_index}:{attempt}"

Purpose:
- Ensure at-most-once execution semantics
- Allow safe retries without duplicate execution
- Support multiple attempts of the same step
"""

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.idempotency import (
        ExecutionKey,
        IdempotencyStore,
        ExecutionResult as IdempotencyResult,
    )
    DuplicateExecutionError = Exception  # Not used in current implementation
    EXECUTION_MODULE_AVAILABLE = True
except ImportError:
    EXECUTION_MODULE_AVAILABLE = False
    ExecutionKey = None
    IdempotencyStore = None
    IdempotencyResult = None
    DuplicateExecutionError = Exception


pytestmark = pytest.mark.skipif(
    not EXECUTION_MODULE_AVAILABLE,
    reason="execution module not yet implemented"
)


class TestExecutionKeyFormat:
    """Tests for ExecutionKey format and parsing."""

    def test_key_format_includes_run_id(self):
        """Key format includes pipeline_run_id."""
        key = ExecutionKey(
            pipeline_run_id="run-123",
            step_index=0,
            attempt=1,
        )
        assert "run-123" in str(key)

    def test_key_format_includes_step_index(self):
        """Key format includes step index."""
        key = ExecutionKey(
            pipeline_run_id="run-123",
            step_index=5,
            attempt=1,
        )
        assert "5" in str(key)

    def test_key_format_includes_attempt(self):
        """Key format includes attempt number."""
        key = ExecutionKey(
            pipeline_run_id="run-123",
            step_index=0,
            attempt=3,
        )
        assert "3" in str(key)

    def test_key_format_is_colon_separated(self):
        """Key format uses colons as separator."""
        key = ExecutionKey(
            pipeline_run_id="run-123",
            step_index=2,
            attempt=1,
        )
        assert str(key) == "run-123:2:1"

    def test_key_from_string_parsing(self):
        """ExecutionKey can be parsed from string."""
        key = ExecutionKey.from_string("run-456:3:2")

        assert key.pipeline_run_id == "run-456"
        assert key.step_index == 3
        assert key.attempt == 2

    def test_key_equality(self):
        """Two keys with same values are equal."""
        key1 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key2 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        assert key1 == key2

    def test_key_inequality_different_attempt(self):
        """Keys with different attempts are not equal."""
        key1 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key2 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=2)

        assert key1 != key2

    def test_key_hashable(self):
        """ExecutionKey is hashable (can be used in sets/dicts)."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key_set = {key}

        assert key in key_set


class TestIdempotencyStore:
    """Tests for IdempotencyStore behavior."""

    @pytest.fixture
    def store(self):
        """Create a fresh idempotency store."""
        return IdempotencyStore()

    def test_first_execution_succeeds(self, store):
        """First execution with a key succeeds."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        execution_id = store.start_execution(key)

        assert execution_id is not None

    def test_same_key_returns_existing_execution(self, store):
        """Duplicate request with same key returns existing execution."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        execution_id_1 = store.start_execution(key)
        execution_id_2 = store.start_execution(key)

        assert execution_id_1 == execution_id_2

    def test_different_attempt_new_execution(self, store):
        """Retry with new attempt creates new execution."""
        key1 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key2 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=2)

        execution_id_1 = store.start_execution(key1)
        execution_id_2 = store.start_execution(key2)

        assert execution_id_1 != execution_id_2

    def test_different_step_new_execution(self, store):
        """Different step index creates new execution."""
        key1 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key2 = ExecutionKey(pipeline_run_id="run-123", step_index=1, attempt=1)

        execution_id_1 = store.start_execution(key1)
        execution_id_2 = store.start_execution(key2)

        assert execution_id_1 != execution_id_2

    def test_different_run_new_execution(self, store):
        """Different pipeline run creates new execution."""
        key1 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        key2 = ExecutionKey(pipeline_run_id="run-456", step_index=0, attempt=1)

        execution_id_1 = store.start_execution(key1)
        execution_id_2 = store.start_execution(key2)

        assert execution_id_1 != execution_id_2


class TestIdempotencyWithResults:
    """Tests for idempotency with execution results."""

    @pytest.fixture
    def store(self):
        """Create a fresh idempotency store."""
        return IdempotencyStore()

    def test_completed_execution_returns_result(self, store):
        """Completed execution returns stored result."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        execution_id = store.start_execution(key)
        store.complete_execution(key, success=True, exit_code=0)

        # Second call should return the stored result
        result = store.get_result(key)

        assert result is not None
        assert result.success is True
        assert result.exit_code == 0

    def test_pending_execution_has_no_result(self, store):
        """Pending execution has no result yet."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        store.start_execution(key)

        result = store.get_result(key)
        assert result is None

    def test_failed_execution_returns_failure(self, store):
        """Failed execution returns failure result."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        store.start_execution(key)
        store.complete_execution(key, success=False, exit_code=1, error="Test failed")

        result = store.get_result(key)

        assert result is not None
        assert result.success is False
        assert result.exit_code == 1
        assert result.error == "Test failed"


class TestIdempotencyStatus:
    """Tests for execution status queries."""

    @pytest.fixture
    def store(self):
        """Create a fresh idempotency store."""
        return IdempotencyStore()

    def test_is_executing_true_when_started(self, store):
        """is_executing() returns True for started executions."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key)

        assert store.is_executing(key) is True

    def test_is_executing_false_when_completed(self, store):
        """is_executing() returns False for completed executions."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key)
        store.complete_execution(key, success=True, exit_code=0)

        assert store.is_executing(key) is False

    def test_is_executing_false_when_not_started(self, store):
        """is_executing() returns False for never-started executions."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

        assert store.is_executing(key) is False

    def test_has_executed_true_when_completed(self, store):
        """has_executed() returns True for completed executions."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key)
        store.complete_execution(key, success=True, exit_code=0)

        assert store.has_executed(key) is True

    def test_has_executed_false_when_executing(self, store):
        """has_executed() returns False while still executing."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key)

        assert store.has_executed(key) is False


class TestAttemptManagement:
    """Tests for attempt number management."""

    @pytest.fixture
    def store(self):
        """Create a fresh idempotency store."""
        return IdempotencyStore()

    def test_get_next_attempt_starts_at_one(self, store):
        """First attempt for a step is 1."""
        next_attempt = store.get_next_attempt("run-123", step_index=0)

        assert next_attempt == 1

    def test_get_next_attempt_increments(self, store):
        """Next attempt increments after execution."""
        key = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key)
        store.complete_execution(key, success=False, exit_code=1)

        next_attempt = store.get_next_attempt("run-123", step_index=0)

        assert next_attempt == 2

    def test_get_next_attempt_per_step(self, store):
        """Attempt numbers are tracked per step."""
        # Complete step 0
        key0 = ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)
        store.start_execution(key0)
        store.complete_execution(key0, success=True, exit_code=0)

        # Step 1 should still be at attempt 1
        next_attempt = store.get_next_attempt("run-123", step_index=1)

        assert next_attempt == 1
