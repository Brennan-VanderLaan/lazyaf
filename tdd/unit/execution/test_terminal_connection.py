"""
Tests for terminal connection (Phase 12.7).

These tests verify the terminal connection behavior for debug sessions.

Connection Modes:
- sidecar: Spawns a debug container with workspace mounted (filesystem inspection)
- shell: Exec into the running step container (live debugging)

Special Commands:
- @resume: Continue pipeline execution
- @abort: Cancel debug session
- @status: Show session info
- @help: List available commands
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from app.services.execution.debug_terminal import DebugTerminalService, handle_special_command
    from app.models.debug_session import DebugSession, DebugSessionStatus
    TERMINAL_MODULE_AVAILABLE = True
except ImportError:
    TERMINAL_MODULE_AVAILABLE = False
    DebugTerminalService = None
    handle_special_command = None
    DebugSession = None
    DebugSessionStatus = None


pytestmark = pytest.mark.skipif(
    not TERMINAL_MODULE_AVAILABLE,
    reason="debug_terminal module not yet implemented"
)


class TestSidecarMode:
    """Tests for sidecar mode (filesystem inspection)."""

    @pytest.mark.asyncio
    async def test_sidecar_mode_spawns_container(self):
        """Sidecar mode spawns a debug container."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_container.id = "container-abc123"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            container_id = await service.start_sidecar(
                workspace_volume="lazyaf-ws-run123"
            )

            mock_client.containers.run.assert_called_once()
            assert container_id == "container-abc123"

    @pytest.mark.asyncio
    async def test_sidecar_mounts_workspace(self):
        """Sidecar container has workspace volume mounted."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_container.id = "container-abc123"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            await service.start_sidecar(workspace_volume="lazyaf-ws-run123")

            call_kwargs = mock_client.containers.run.call_args
            volumes = call_kwargs.kwargs.get('volumes', {})

            assert "lazyaf-ws-run123" in volumes
            assert volumes["lazyaf-ws-run123"]["bind"] == "/workspace"

    @pytest.mark.asyncio
    async def test_sidecar_uses_debug_image(self):
        """Sidecar uses the debug sidecar image."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            await service.start_sidecar(workspace_volume="lazyaf-ws-run123")

            call_args = mock_client.containers.run.call_args
            image = call_args.kwargs.get('image') or call_args.args[0]

            assert "debug-sidecar" in image

    @pytest.mark.asyncio
    async def test_sidecar_has_tty(self):
        """Sidecar container has TTY enabled for interactive use."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            await service.start_sidecar(workspace_volume="lazyaf-ws-run123")

            call_kwargs = mock_client.containers.run.call_args.kwargs
            assert call_kwargs.get('tty') is True
            assert call_kwargs.get('stdin_open') is True


class TestShellMode:
    """Tests for shell mode (exec into running container)."""

    @pytest.mark.asyncio
    async def test_shell_mode_execs_into_running(self):
        """Shell mode execs into the running step container."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_exec = MagicMock()
        mock_container.exec_run.return_value = mock_exec

        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container

        # Patch docker at the import location where exec_into_container imports it
        with patch.dict('sys.modules', {'docker': MagicMock(), 'docker.errors': MagicMock()}):
            import sys
            sys.modules['docker'].errors = MagicMock()
            sys.modules['docker'].errors.NotFound = Exception

            with patch.object(service, '_get_docker_client', return_value=mock_client):
                await service.exec_into_container(
                    container_id="step-container-123"
                )

                mock_client.containers.get.assert_called_once_with("step-container-123")
                mock_container.exec_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_shell_mode_uses_bash(self):
        """Shell mode runs bash for interactive shell."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_container.status = "running"
        mock_exec = MagicMock()
        mock_container.exec_run.return_value = mock_exec

        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container

        with patch.dict('sys.modules', {'docker': MagicMock(), 'docker.errors': MagicMock()}):
            import sys
            sys.modules['docker'].errors = MagicMock()
            sys.modules['docker'].errors.NotFound = Exception

            with patch.object(service, '_get_docker_client', return_value=mock_client):
                await service.exec_into_container(container_id="step-container-123")

                # Check that exec_run was called with bash
                mock_container.exec_run.assert_called_once()
                call_args = mock_container.exec_run.call_args

                # Get the cmd argument
                if call_args.kwargs:
                    cmd = call_args.kwargs.get('cmd', '')
                elif call_args.args:
                    cmd = call_args.args[0]
                else:
                    cmd = ''

                assert "bash" in str(cmd)

    @pytest.mark.asyncio
    async def test_shell_mode_requires_running_container(self):
        """Shell mode fails if container is not running."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_container.status = "exited"

        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container

        with patch.dict('sys.modules', {'docker': MagicMock(), 'docker.errors': MagicMock()}):
            import sys
            sys.modules['docker'].errors = MagicMock()
            sys.modules['docker'].errors.NotFound = Exception

            with patch.object(service, '_get_docker_client', return_value=mock_client):
                with pytest.raises(ValueError) as exc_info:
                    await service.exec_into_container(container_id="stopped-container")

                assert "not running" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_shell_mode_container_not_found(self):
        """Shell mode raises error if container doesn't exist."""
        service = DebugTerminalService()

        mock_client = MagicMock()

        # Create a custom exception class
        class NotFoundError(Exception):
            pass

        mock_client.containers.get.side_effect = NotFoundError("Container not found")

        with patch.dict('sys.modules', {'docker': MagicMock(), 'docker.errors': MagicMock()}):
            import sys
            sys.modules['docker'].errors.NotFound = NotFoundError

            with patch.object(service, '_get_docker_client', return_value=mock_client):
                with pytest.raises(ValueError) as exc_info:
                    await service.exec_into_container(container_id="nonexistent")

                assert "not found" in str(exc_info.value).lower()


class TestSpecialCommands:
    """Tests for special @ commands in debug terminal."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.id = "session-abc"
        session.status = "connected"
        session.current_step_index = 2
        session.current_step_name = "Build"
        session.expires_at = None
        return session

    @pytest.mark.asyncio
    async def test_special_command_resume(self, mock_db, mock_session):
        """@resume command triggers pipeline resume."""
        # Patch where the function is imported from
        with patch("app.services.execution.debug_session_service.get_debug_session_service") as mock_get_service:
            service = MagicMock()
            service.resume = AsyncMock()
            mock_get_service.return_value = service

            result = await handle_special_command(
                command="@resume",
                session=mock_session,
                db=mock_db,
            )

            service.resume.assert_called_once_with(mock_db, mock_session.id)
            assert "resum" in result.lower()

    @pytest.mark.asyncio
    async def test_special_command_abort(self, mock_db, mock_session):
        """@abort command cancels debug session."""
        # Patch where the function is imported from
        with patch("app.services.execution.debug_session_service.get_debug_session_service") as mock_get_service:
            service = MagicMock()
            service.abort = AsyncMock()
            mock_get_service.return_value = service

            result = await handle_special_command(
                command="@abort",
                session=mock_session,
                db=mock_db,
            )

            service.abort.assert_called_once_with(mock_db, mock_session.id)
            assert "abort" in result.lower()

    @pytest.mark.asyncio
    async def test_special_command_status(self, mock_db, mock_session):
        """@status shows session info."""
        result = await handle_special_command(
            command="@status",
            session=mock_session,
            db=mock_db,
        )

        assert mock_session.id in result
        assert "Build" in result or str(mock_session.current_step_index) in result

    @pytest.mark.asyncio
    async def test_special_command_help(self, mock_db, mock_session):
        """@help lists available commands."""
        result = await handle_special_command(
            command="@help",
            session=mock_session,
            db=mock_db,
        )

        assert "@resume" in result
        assert "@abort" in result
        assert "@status" in result

    @pytest.mark.asyncio
    async def test_non_special_command_returns_none(self, mock_db, mock_session):
        """Non-@ commands return None (pass through to shell)."""
        result = await handle_special_command(
            command="ls -la",
            session=mock_session,
            db=mock_db,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_special_command(self, mock_db, mock_session):
        """Unknown @ command returns error message."""
        result = await handle_special_command(
            command="@unknown",
            session=mock_session,
            db=mock_db,
        )

        assert "unknown" in result.lower() or "@help" in result.lower()


class TestTokenValidation:
    """Tests for token-based authentication."""

    @pytest.mark.asyncio
    async def test_token_required(self):
        """Terminal connection requires valid token."""
        service = DebugTerminalService()

        session = MagicMock()
        session.token = "valid-token-123"

        # Valid token
        is_valid = service.validate_token(session, "valid-token-123")
        assert is_valid is True

        # Invalid token
        is_valid = service.validate_token(session, "wrong-token")
        assert is_valid is False

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self):
        """Invalid token is rejected."""
        service = DebugTerminalService()

        session = MagicMock()
        session.token = "correct-token"

        assert service.validate_token(session, "incorrect-token") is False
        assert service.validate_token(session, "") is False
        assert service.validate_token(session, None) is False


class TestWebSocketDisconnect:
    """Tests for WebSocket disconnect cleanup."""

    @pytest.mark.asyncio
    async def test_websocket_disconnect_cleanup_sidecar(self):
        """Sidecar container is cleaned up on disconnect."""
        service = DebugTerminalService()

        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container

        # Track an active sidecar
        service._active_sessions["session-abc"] = {
            "mode": "sidecar",
            "container_id": "sidecar-container-123",
        }

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            await service.cleanup("session-abc")

            # Container should be stopped and removed
            mock_container.stop.assert_called_once()
            mock_container.remove.assert_called_once()

            # Session should be removed from tracking
            assert "session-abc" not in service._active_sessions

    @pytest.mark.asyncio
    async def test_websocket_disconnect_shell_no_cleanup(self):
        """Shell mode doesn't stop the step container on disconnect."""
        service = DebugTerminalService()

        mock_client = MagicMock()

        # Track an active shell session
        service._active_sessions["session-abc"] = {
            "mode": "shell",
            "container_id": "step-container-123",
        }

        with patch.object(service, '_get_docker_client', return_value=mock_client):
            await service.cleanup("session-abc")

            # Should NOT stop the step container (it's running the pipeline)
            mock_client.containers.get.assert_not_called()

            # Session should still be removed from tracking
            assert "session-abc" not in service._active_sessions


class TestDebugTerminalService:
    """Tests for DebugTerminalService initialization and helpers."""

    def test_service_initialization(self):
        """Service initializes with empty tracking."""
        service = DebugTerminalService()

        assert hasattr(service, '_active_sessions')
        assert isinstance(service._active_sessions, dict)
        assert len(service._active_sessions) == 0

    @pytest.mark.asyncio
    async def test_get_active_session(self):
        """Can retrieve active session info."""
        service = DebugTerminalService()

        service._active_sessions["session-abc"] = {
            "mode": "sidecar",
            "container_id": "container-123",
        }

        info = service.get_active_session("session-abc")
        assert info is not None
        assert info["mode"] == "sidecar"
        assert info["container_id"] == "container-123"

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self):
        """Returns None for non-existent session."""
        service = DebugTerminalService()

        info = service.get_active_session("nonexistent")
        assert info is None
