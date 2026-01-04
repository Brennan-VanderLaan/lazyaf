"""
Tests for Debug API endpoints (Phase 12.7).

These tests DEFINE the API contract for debug sessions.
Write tests first, then implement to make them pass.

Endpoints:
- POST /api/pipeline-runs/{run_id}/debug-rerun - Create debug re-run
- GET /api/debug/{session_id} - Get session info
- POST /api/debug/{session_id}/resume - Resume pipeline
- POST /api/debug/{session_id}/abort - Abort debug session
- POST /api/debug/{session_id}/extend - Extend timeout
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import will fail until we implement the modules - that's expected in TDD
try:
    from app.routers.debug import router
    from app.schemas.debug import (
        DebugRerunRequest,
        DebugRerunResponse,
        DebugSessionInfo,
        DebugStepInfo,
        DebugCommitInfo,
        DebugRuntimeInfo,
    )
    from app.models.debug_session import DebugSession, DebugSessionStatus
    DEBUG_API_AVAILABLE = True
except ImportError:
    DEBUG_API_AVAILABLE = False
    # Define placeholders for test collection
    router = None
    DebugRerunRequest = None
    DebugRerunResponse = None
    DebugSessionInfo = None
    DebugSession = None
    DebugSessionStatus = None


pytestmark = pytest.mark.skipif(
    not DEBUG_API_AVAILABLE,
    reason="debug API module not yet implemented"
)


class TestDebugRerunRequestSchema:
    """Tests for DebugRerunRequest Pydantic schema."""

    def test_valid_request_minimal(self):
        """Minimal valid request with just breakpoints."""
        request = DebugRerunRequest(breakpoints=[0, 2])
        assert request.breakpoints == [0, 2]
        assert request.use_original_commit is True  # Default
        assert request.commit_sha is None
        assert request.branch is None

    def test_valid_request_full(self):
        """Full request with all optional fields."""
        request = DebugRerunRequest(
            breakpoints=[1, 3, 5],
            use_original_commit=False,
            commit_sha="abc123",
            branch="feature/test",
        )
        assert request.breakpoints == [1, 3, 5]
        assert request.use_original_commit is False
        assert request.commit_sha == "abc123"
        assert request.branch == "feature/test"

    def test_empty_breakpoints_allowed(self):
        """Empty breakpoints list is allowed (runs to completion)."""
        request = DebugRerunRequest(breakpoints=[])
        assert request.breakpoints == []


class TestDebugRerunResponseSchema:
    """Tests for DebugRerunResponse Pydantic schema."""

    def test_response_has_required_fields(self):
        """Response includes run_id, debug_session_id, token."""
        response = DebugRerunResponse(
            run_id="run-123",
            debug_session_id="session-456",
            token="secret-token-789",
        )
        assert response.run_id == "run-123"
        assert response.debug_session_id == "session-456"
        assert response.token == "secret-token-789"


class TestDebugSessionInfoSchema:
    """Tests for DebugSessionInfo Pydantic schema."""

    def test_session_info_at_breakpoint(self):
        """Session info when at breakpoint."""
        info = DebugSessionInfo(
            id="session-123",
            status="waiting_at_bp",
            current_step=DebugStepInfo(name="Build", index=2, type="script"),
            commit=DebugCommitInfo(sha="abc123", message="Fix bug"),
            runtime=DebugRuntimeInfo(
                host="localhost",
                orchestrator="docker",
                image="lazyaf-base:latest",
                image_sha="sha256:abc",
            ),
            logs="Step 1 output...\nStep 2 starting...",
            join_command="lazyaf debug session-123 --token xxx",
            token="secret-token",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        assert info.status == "waiting_at_bp"
        assert info.current_step.name == "Build"
        assert info.current_step.index == 2

    def test_session_info_pending(self):
        """Session info when pending (not at breakpoint yet)."""
        info = DebugSessionInfo(
            id="session-123",
            status="pending",
            current_step=None,  # No current step yet
            commit=DebugCommitInfo(sha="abc123", message="Fix bug"),
            runtime=DebugRuntimeInfo(
                host="localhost",
                orchestrator="docker",
                image="lazyaf-base:latest",
                image_sha=None,
            ),
            logs="",
            join_command="lazyaf debug session-123 --token xxx",
            token="secret-token",
            expires_at=None,
        )
        assert info.current_step is None
        assert info.expires_at is None


class TestCreateDebugRerunEndpoint:
    """Tests for POST /api/pipeline-runs/{run_id}/debug-rerun endpoint."""

    @pytest.fixture
    def mock_db(self):
        """Mock database session."""
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        """Mock debug session service."""
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_create_debug_rerun_returns_session(self, mock_db, mock_debug_service):
        """Creating debug re-run returns session ID and token."""
        from app.routers.debug import create_debug_rerun

        # Setup mocks
        mock_session = MagicMock()
        mock_session.id = "session-123"
        mock_session.token = "secret-token"

        mock_run = MagicMock()
        mock_run.id = "run-456"

        mock_debug_service.create_debug_rerun.return_value = (mock_session, mock_run)

        # Make request
        request = DebugRerunRequest(breakpoints=[0, 2])
        response = await create_debug_rerun(
            run_id="original-run-id",
            request=request,
            db=mock_db,
        )

        # Verify
        assert response.debug_session_id == "session-123"
        assert response.run_id == "run-456"
        assert response.token == "secret-token"
        mock_debug_service.create_debug_rerun.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_debug_rerun_requires_failed_or_cancelled(self, mock_db, mock_debug_service):
        """Can only create debug re-run from failed or cancelled pipeline."""
        from app.routers.debug import create_debug_rerun
        from fastapi import HTTPException

        # Setup mock to raise error for non-failed run
        mock_debug_service.create_debug_rerun.side_effect = ValueError(
            "Can only debug re-run failed or cancelled pipelines"
        )

        request = DebugRerunRequest(breakpoints=[0])

        with pytest.raises(HTTPException) as exc_info:
            await create_debug_rerun(
                run_id="running-pipeline-id",
                request=request,
                db=mock_db,
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_debug_rerun_not_found(self, mock_db, mock_debug_service):
        """Returns 404 for non-existent pipeline run."""
        from app.routers.debug import create_debug_rerun
        from fastapi import HTTPException

        mock_debug_service.create_debug_rerun.side_effect = ValueError(
            "Pipeline run not found"
        )

        request = DebugRerunRequest(breakpoints=[0])

        with pytest.raises(HTTPException) as exc_info:
            await create_debug_rerun(
                run_id="nonexistent-id",
                request=request,
                db=mock_db,
            )

        assert exc_info.value.status_code == 404


class TestGetDebugSessionEndpoint:
    """Tests for GET /api/debug/{session_id} endpoint."""

    @pytest.fixture
    def mock_db(self):
        """Mock database session."""
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        """Mock debug session service."""
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_get_debug_session_returns_info(self, mock_db, mock_debug_service):
        """Get session returns full context."""
        from app.routers.debug import get_debug_session

        mock_debug_service.get_session_info.return_value = {
            "id": "session-123",
            "status": "waiting_at_bp",
            "current_step": {"name": "Test", "index": 1, "type": "script"},
            "commit": {"sha": "abc123", "message": "Fix"},
            "runtime": {
                "host": "localhost",
                "orchestrator": "docker",
                "image": "test:latest",
                "image_sha": None,
            },
            "logs": "Previous output...",
            "join_command": "lazyaf debug session-123 --token xxx",
            "token": "secret",
            "expires_at": datetime.utcnow().isoformat(),
        }

        response = await get_debug_session(session_id="session-123", db=mock_db)

        assert response.id == "session-123"
        assert response.status == "waiting_at_bp"
        assert response.current_step.name == "Test"
        assert response.join_command is not None
        assert response.token is not None

    @pytest.mark.asyncio
    async def test_get_debug_session_includes_join_command(self, mock_db, mock_debug_service):
        """Session info includes CLI join command."""
        from app.routers.debug import get_debug_session

        mock_debug_service.get_session_info.return_value = {
            "id": "session-123",
            "status": "waiting_at_bp",
            "current_step": None,
            "commit": {"sha": "abc", "message": "msg"},
            "runtime": {"host": "h", "orchestrator": "o", "image": "i", "image_sha": None},
            "logs": "",
            "join_command": "lazyaf debug session-123 --token abc123",
            "token": "abc123",
            "expires_at": None,
        }

        response = await get_debug_session(session_id="session-123", db=mock_db)

        assert "lazyaf debug" in response.join_command
        assert "session-123" in response.join_command

    @pytest.mark.asyncio
    async def test_get_debug_session_not_found(self, mock_db, mock_debug_service):
        """Returns 404 for non-existent session."""
        from app.routers.debug import get_debug_session
        from fastapi import HTTPException

        mock_debug_service.get_session_info.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await get_debug_session(session_id="nonexistent", db=mock_db)

        assert exc_info.value.status_code == 404


class TestResumeDebugSessionEndpoint:
    """Tests for POST /api/debug/{session_id}/resume endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_resume_continues_pipeline(self, mock_db, mock_debug_service):
        """Resume endpoint continues pipeline execution."""
        from app.routers.debug import resume_debug_session

        mock_debug_service.resume.return_value = None

        response = await resume_debug_session(session_id="session-123", db=mock_db)

        mock_debug_service.resume.assert_called_once_with(mock_db, "session-123")
        assert response["status"] == "resumed"

    @pytest.mark.asyncio
    async def test_resume_requires_connected_state(self, mock_db, mock_debug_service):
        """Resume only works when session is connected."""
        from app.routers.debug import resume_debug_session
        from fastapi import HTTPException

        mock_debug_service.resume.side_effect = ValueError(
            "Can only resume from connected state"
        )

        with pytest.raises(HTTPException) as exc_info:
            await resume_debug_session(session_id="session-123", db=mock_db)

        assert exc_info.value.status_code == 400


class TestAbortDebugSessionEndpoint:
    """Tests for POST /api/debug/{session_id}/abort endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_abort_cancels_pipeline(self, mock_db, mock_debug_service):
        """Abort endpoint cancels the debug session and pipeline."""
        from app.routers.debug import abort_debug_session

        mock_debug_service.abort.return_value = None

        response = await abort_debug_session(session_id="session-123", db=mock_db)

        mock_debug_service.abort.assert_called_once_with(mock_db, "session-123")
        assert response["status"] == "aborted"

    @pytest.mark.asyncio
    async def test_abort_not_found(self, mock_db, mock_debug_service):
        """Abort returns 404 for non-existent session."""
        from app.routers.debug import abort_debug_session
        from fastapi import HTTPException

        mock_debug_service.abort.side_effect = ValueError("Session not found")

        with pytest.raises(HTTPException) as exc_info:
            await abort_debug_session(session_id="nonexistent", db=mock_db)

        assert exc_info.value.status_code == 404


class TestExtendDebugSessionEndpoint:
    """Tests for POST /api/debug/{session_id}/extend endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_extend_adds_time(self, mock_db, mock_debug_service):
        """Extend endpoint adds time to session timeout."""
        from app.routers.debug import extend_debug_session

        new_expiry = datetime.utcnow() + timedelta(hours=2)
        mock_debug_service.extend_timeout.return_value = new_expiry

        response = await extend_debug_session(
            session_id="session-123",
            additional_minutes=60,
            db=mock_db,
        )

        mock_debug_service.extend_timeout.assert_called_once_with(
            mock_db, "session-123", 3600  # 60 minutes in seconds
        )
        assert hasattr(response, 'expires_at')
        assert response.expires_at == new_expiry

    @pytest.mark.asyncio
    async def test_extend_respects_max_timeout(self, mock_db, mock_debug_service):
        """Extend cannot exceed maximum timeout (4 hours)."""
        from app.routers.debug import extend_debug_session
        from fastapi import HTTPException

        mock_debug_service.extend_timeout.side_effect = ValueError(
            "Cannot extend beyond maximum timeout of 4 hours"
        )

        with pytest.raises(HTTPException) as exc_info:
            await extend_debug_session(
                session_id="session-123",
                additional_minutes=300,  # 5 hours
                db=mock_db,
            )

        assert exc_info.value.status_code == 400


class TestInvalidSessionResponses:
    """Tests for error responses with invalid session IDs."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_debug_service(self):
        with patch("app.routers.debug.get_debug_session_service") as mock:
            service = AsyncMock()
            mock.return_value = service
            yield service

    @pytest.mark.asyncio
    async def test_get_invalid_session_404(self, mock_db, mock_debug_service):
        """GET returns 404 for invalid session."""
        from app.routers.debug import get_debug_session
        from fastapi import HTTPException

        mock_debug_service.get_session_info.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await get_debug_session(session_id="invalid-id", db=mock_db)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_expired_session_410(self, mock_db, mock_debug_service):
        """Returns 410 Gone for expired session."""
        from app.routers.debug import get_debug_session
        from fastapi import HTTPException

        mock_debug_service.get_session_info.side_effect = ValueError("Session expired")

        with pytest.raises(HTTPException) as exc_info:
            await get_debug_session(session_id="expired-id", db=mock_db)

        assert exc_info.value.status_code == 410
