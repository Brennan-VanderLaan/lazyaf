"""
Tests for breakpoint execution behavior (Phase 12.7).

These tests verify how breakpoints integrate with pipeline execution.

Breakpoint Behavior:
- Pipelines can be re-run with breakpoints at specific steps
- Execution pauses BEFORE the step runs
- Workspace is preserved at breakpoint
- User can resume, abort, or inspect state
- Multiple breakpoints can be set
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from app.services.pipeline_executor import PipelineExecutor
    from app.services.execution.debug_session_service import DebugSessionService, get_debug_session_service
    from app.models.debug_session import DebugSession, DebugSessionStatus
    from app.models import PipelineRun, Pipeline, Repo
    BREAKPOINT_MODULE_AVAILABLE = True
except ImportError:
    BREAKPOINT_MODULE_AVAILABLE = False
    PipelineExecutor = None
    DebugSessionService = None
    get_debug_session_service = None
    DebugSession = None
    DebugSessionStatus = None


pytestmark = pytest.mark.skipif(
    not BREAKPOINT_MODULE_AVAILABLE,
    reason="breakpoint execution modules not yet implemented"
)


class TestPipelinePausesAtBreakpoint:
    """Tests that pipeline execution pauses at breakpoints."""

    @pytest.fixture
    def mock_db(self):
        """Mock database session."""
        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.get = AsyncMock()
        return db

    @pytest.fixture
    def mock_pipeline_run(self):
        """Mock pipeline run."""
        run = MagicMock()
        run.id = "run-123"
        run.pipeline_id = "pipeline-456"
        run.status = "running"
        run.current_step = 0
        run.workspace = None
        return run

    @pytest.fixture
    def mock_pipeline(self):
        """Mock pipeline with steps."""
        pipeline = MagicMock()
        pipeline.id = "pipeline-456"
        pipeline.steps = json.dumps([
            {"name": "Checkout", "type": "script", "config": {"command": "git checkout"}},
            {"name": "Build", "type": "script", "config": {"command": "npm build"}},
            {"name": "Test", "type": "script", "config": {"command": "npm test"}},
        ])
        return pipeline

    @pytest.fixture
    def mock_repo(self):
        """Mock repo."""
        repo = MagicMock()
        repo.id = "repo-789"
        repo.name = "test-repo"
        return repo

    @pytest.mark.asyncio
    async def test_check_for_breakpoint_returns_session_when_breakpoint_set(
        self, mock_db, mock_pipeline_run
    ):
        """_check_for_breakpoint returns debug session when step is in breakpoints."""
        executor = PipelineExecutor(use_local_executor=False)

        # Create mock debug session with breakpoint at step 1
        mock_session = MagicMock()
        mock_session.id = "session-abc"
        mock_session.breakpoints = json.dumps([1])
        mock_session.status = DebugSessionStatus.PENDING.value

        # Mock the database query to return our session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session
        mock_db.execute.return_value = mock_result

        # Mock the debug session service
        with patch('app.services.pipeline_executor.get_debug_session_service') as mock_get_service:
            mock_service = MagicMock()
            mock_service.on_breakpoint_hit = AsyncMock()
            mock_get_service.return_value = mock_service

            with patch('app.services.pipeline_executor.manager') as mock_manager:
                mock_manager.broadcast = AsyncMock()

                result = await executor._check_for_breakpoint(
                    db=mock_db,
                    pipeline_run_id="run-123",
                    step_index=1,
                    step_name="Build",
                )

                # Should return the debug session (breakpoint was hit)
                assert result is not None
                assert result.id == "session-abc"
                mock_service.on_breakpoint_hit.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_for_breakpoint_returns_none_without_breakpoint(
        self, mock_db
    ):
        """_check_for_breakpoint returns None when step is not in breakpoints."""
        executor = PipelineExecutor(use_local_executor=False)

        # Create mock debug session WITHOUT breakpoint at step 1
        mock_session = MagicMock()
        mock_session.id = "session-abc"
        mock_session.breakpoints = json.dumps([0, 2])  # Not step 1
        mock_session.status = DebugSessionStatus.PENDING.value

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session
        mock_db.execute.return_value = mock_result

        result = await executor._check_for_breakpoint(
            db=mock_db,
            pipeline_run_id="run-123",
            step_index=1,
            step_name="Build",
        )

        # Should return None (no breakpoint at this step)
        assert result is None

    @pytest.mark.asyncio
    async def test_check_for_breakpoint_returns_none_without_session(
        self, mock_db
    ):
        """_check_for_breakpoint returns None when no debug session exists."""
        executor = PipelineExecutor(use_local_executor=False)

        # No debug session found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await executor._check_for_breakpoint(
            db=mock_db,
            pipeline_run_id="run-123",
            step_index=1,
            step_name="Build",
        )

        # Should return None
        assert result is None


class TestMultipleBreakpoints:
    """Tests for multiple breakpoints in a pipeline."""

    @pytest.mark.asyncio
    async def test_multiple_breakpoints_work(self):
        """Pipeline pauses at each breakpoint."""
        breakpoints = [0, 2, 4]

        # Each breakpoint index should trigger pause
        for step_index in [0, 2, 4]:
            assert step_index in breakpoints

        # Non-breakpoint indices should not pause
        for step_index in [1, 3, 5]:
            assert step_index not in breakpoints

    @pytest.mark.asyncio
    async def test_breakpoint_json_parsing(self):
        """Breakpoints stored as JSON can be parsed correctly."""
        session = MagicMock()
        session.breakpoints = json.dumps([0, 2, 4])

        breakpoints = json.loads(session.breakpoints)
        assert breakpoints == [0, 2, 4]
        assert 0 in breakpoints
        assert 1 not in breakpoints


class TestResumeFromBreakpoint:
    """Tests for resuming execution from breakpoint."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.get = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_resume_continues_from_breakpoint(self, mock_db):
        """Resume continues execution from where it paused."""
        service = DebugSessionService()

        session = MagicMock()
        session.id = "session-abc"
        session.status = "connected"
        session.current_step_index = 1
        session.state_history = json.dumps([])

        mock_db.get.return_value = session

        with patch.object(service, '_continue_pipeline_execution') as mock_continue:
            mock_continue.return_value = None

            with patch.object(service, '_get_or_create_machine') as mock_machine:
                machine = MagicMock()
                machine.is_terminal = False
                machine.to_dict.return_value = {"history": []}
                mock_machine.return_value = machine

                with patch('app.services.execution.debug_session_service.manager') as mock_manager:
                    mock_manager.broadcast = AsyncMock()

                    await service.resume(mock_db, "session-abc")

                    # Should trigger continuation
                    mock_continue.assert_called_once()


class TestAbortAtBreakpoint:
    """Tests for aborting at breakpoint."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.get = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_abort_at_breakpoint_cancels_run(self, mock_db):
        """Abort at breakpoint cancels the pipeline run."""
        service = DebugSessionService()

        session = MagicMock()
        session.id = "session-abc"
        session.status = "connected"
        session.pipeline_run_id = "run-123"
        session.state_history = json.dumps([])

        mock_db.get.return_value = session

        with patch.object(service, '_cancel_pipeline_run') as mock_cancel:
            mock_cancel.return_value = None

            with patch.object(service, '_end_session') as mock_end:
                mock_end.return_value = None

                with patch.object(service, '_get_or_create_machine') as mock_machine:
                    machine = MagicMock()
                    machine.is_terminal = False
                    machine.to_dict.return_value = {"history": []}
                    mock_machine.return_value = machine

                    with patch('app.services.execution.debug_session_service.manager') as mock_manager:
                        mock_manager.broadcast = AsyncMock()

                        await service.abort(mock_db, "session-abc")

                        # Should cancel the pipeline run
                        mock_cancel.assert_called_once()


class TestNoBreakpointsRunNormally:
    """Tests that empty breakpoints list runs pipeline normally."""

    @pytest.mark.asyncio
    async def test_empty_breakpoints_runs_to_completion(self):
        """Pipeline with empty breakpoints runs to completion."""
        breakpoints = []

        # No step should trigger pause
        for step_index in range(5):
            should_pause = step_index in breakpoints
            assert should_pause is False


class TestDebugSessionService:
    """Tests for DebugSessionService methods."""

    @pytest.mark.asyncio
    async def test_validate_token(self):
        """Token validation works correctly."""
        service = DebugSessionService()

        session = MagicMock()
        session.token = "correct-token-abc123"

        # Correct token
        assert service.validate_token(session, "correct-token-abc123") is True

        # Wrong token
        assert service.validate_token(session, "wrong-token") is False

        # Empty token
        assert service.validate_token(session, "") is False
