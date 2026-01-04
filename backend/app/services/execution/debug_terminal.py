"""
Debug Terminal Service - Handles sidecar and shell terminal connections.

This service manages:
- Starting sidecar containers for filesystem inspection
- Exec into running step containers for shell mode
- WebSocket terminal I/O bridging
- Special command handling (@resume, @abort, @status)
"""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.debug_session import DebugSession


# Singleton instance
_debug_terminal_service: Optional["DebugTerminalService"] = None


def get_debug_terminal_service() -> "DebugTerminalService":
    """Get the singleton DebugTerminalService instance."""
    global _debug_terminal_service
    if _debug_terminal_service is None:
        _debug_terminal_service = DebugTerminalService()
    return _debug_terminal_service


# Special commands available in debug terminal
SPECIAL_COMMANDS = {
    "@resume": "Continue pipeline execution to next breakpoint",
    "@abort": "Cancel debug session and pipeline run",
    "@status": "Show current debug session status",
    "@help": "Show available commands",
}


async def handle_special_command(
    command: str,
    session: DebugSession,
    db: AsyncSession,
) -> Optional[str]:
    """
    Handle special @ commands.

    Args:
        command: Command string (e.g., "@resume")
        session: Debug session
        db: Database session

    Returns:
        Response message, or None if not a special command
    """
    if not command.startswith("@"):
        return None

    from app.services.execution.debug_session_service import get_debug_session_service

    cmd = command.strip().lower()
    service = get_debug_session_service()

    if cmd == "@help":
        lines = ["Available commands:"]
        for name, description in SPECIAL_COMMANDS.items():
            lines.append(f"  {name} - {description}")
        return "\n".join(lines)

    if cmd == "@status":
        return f"""Debug Session: {session.id}
Status: {session.status}
Step: {session.current_step_name or 'N/A'} (index {session.current_step_index})
Expires: {session.expires_at or 'N/A'}"""

    if cmd == "@resume":
        await service.resume(db, session.id)
        return "Resuming pipeline execution..."

    if cmd == "@abort":
        await service.abort(db, session.id)
        return "Aborting debug session..."

    return f"Unknown command: {command}. Type @help for available commands."


class DebugTerminalService:
    """
    Service for managing terminal connections to debug sessions.

    Supports two modes:
    - sidecar: Spawns a debug container with workspace mounted
    - shell: Exec into the running step container
    """

    def __init__(self):
        """Initialize the terminal service."""
        self._docker = None
        self._active_sessions: dict[str, dict] = {}

    def _get_docker_client(self):
        """Get or create Docker client."""
        if self._docker is None:
            try:
                import docker
                self._docker = docker.from_env()
            except Exception as e:
                raise RuntimeError(f"Docker not available: {e}")
        return self._docker

    async def start_sidecar(
        self,
        workspace_volume: str,
    ) -> str:
        """
        Start a sidecar container for filesystem inspection.

        Args:
            workspace_volume: Name of the Docker volume to mount

        Returns:
            Container ID
        """
        client = self._get_docker_client()

        container = client.containers.run(
            image="lazyaf-debug-sidecar:latest",
            volumes={workspace_volume: {"bind": "/workspace", "mode": "rw"}},
            network_mode="host",
            detach=True,
            stdin_open=True,
            tty=True,
            name=f"lazyaf-debug-sidecar-{workspace_volume[:8]}",
            labels={
                "lazyaf.type": "debug-sidecar",
                "lazyaf.workspace": workspace_volume,
            },
        )

        return container.id

    async def exec_into_container(
        self,
        container_id: str,
    ):
        """
        Start an exec session in a running container.

        Args:
            container_id: ID of the running container

        Returns:
            Exec instance for terminal I/O

        Raises:
            ValueError: If container not found or not running
        """
        client = self._get_docker_client()

        try:
            import docker.errors
            container = client.containers.get(container_id)
        except docker.errors.NotFound:
            raise ValueError(f"Container {container_id} not found")

        if container.status != "running":
            raise ValueError(f"Container {container_id} is not running (status: {container.status})")

        exec_instance = container.exec_run(
            cmd="/bin/bash",
            stdin=True,
            tty=True,
            stream=True,
            socket=True,
        )

        return exec_instance

    async def handle_websocket(
        self,
        websocket,
        session_id: str,
        mode: str,
        container_id: str,
    ):
        """
        Handle WebSocket terminal I/O.

        Bridges WebSocket messages to container stdin/stdout.

        Args:
            websocket: WebSocket connection
            session_id: Debug session ID
            mode: Connection mode ("sidecar" or "shell")
            container_id: Container ID for terminal
        """
        # TODO: Implement full terminal I/O bridging
        # For now, just track the session
        self._active_sessions[session_id] = {
            "mode": mode,
            "container_id": container_id,
        }

        try:
            while True:
                data = await websocket.receive_text()
                # Forward to container (not yet implemented)
                await websocket.send_json({
                    "type": "echo",
                    "data": data,
                })
        except Exception:
            pass
        finally:
            await self.cleanup(session_id)

    async def cleanup(self, session_id: str):
        """
        Cleanup containers for a session.

        Args:
            session_id: Debug session ID
        """
        session_info = self._active_sessions.pop(session_id, None)
        if not session_info:
            return

        # Only cleanup sidecar containers, not step containers
        if session_info.get("mode") == "sidecar":
            container_id = session_info.get("container_id")
            if container_id:
                try:
                    client = self._get_docker_client()
                    container = client.containers.get(container_id)
                    container.stop(timeout=5)
                    container.remove()
                except Exception:
                    pass

    def get_active_session(self, session_id: str) -> Optional[dict]:
        """
        Get info about an active terminal session.

        Args:
            session_id: Debug session ID

        Returns:
            Session info dict or None
        """
        return self._active_sessions.get(session_id)

    def validate_token(self, session: DebugSession, token: str) -> bool:
        """
        Validate session token.

        Args:
            session: Debug session
            token: Token to validate

        Returns:
            True if valid, False otherwise
        """
        if not token or not session.token:
            return False
        import secrets
        return secrets.compare_digest(session.token, token)
