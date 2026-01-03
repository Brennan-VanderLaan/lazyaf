"""
LocalExecutor - Spawns containers directly for instant step execution.

The LocalExecutor is the "fast path" for step execution:
- Backend spawns containers directly via Docker SDK
- No polling - immediate execution
- Real-time log streaming
- Proper timeout and crash handling
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator, Optional, Union
from uuid import uuid4

from .step_state import StepState, StepStateMachine
from .idempotency import ExecutionKey, IdempotencyStore, get_idempotency_store


# Exception classes
class ExecutionError(Exception):
    """Base class for execution errors."""
    pass


class ContainerNotFoundError(ExecutionError):
    """Raised when a container cannot be found."""
    pass


class TimeoutError(ExecutionError):
    """Raised when execution times out."""
    pass


@dataclass
class ExecutionConfig:
    """
    Configuration for step execution.

    Defines what container to run and how.
    """
    image: str                              # Docker image name
    command: list[str]                      # Command to run
    workspace_path: str                     # Path to workspace directory
    timeout_seconds: int = 3600             # Timeout (default 1 hour)
    environment: dict[str, str] = field(default_factory=dict)  # Env vars
    working_dir: str = "/workspace"         # Working directory in container
    network_mode: str = "bridge"            # Docker network mode
    memory_limit: Optional[str] = None      # Memory limit (e.g., "2g")
    cpu_limit: Optional[float] = None       # CPU limit (e.g., 1.0)
    # Control layer support (Phase 12.3)
    use_control_layer: bool = False         # If True, use control layer image
    backend_url: Optional[str] = None       # Backend URL for control layer
    heartbeat_interval: float = 10.0        # Heartbeat interval (seconds)
    log_batch_size: int = 100               # Log batch size
    log_batch_interval: float = 1.0         # Log batch interval (seconds)


@dataclass
class ExecutionResult:
    """
    Result of a completed execution.
    """
    success: bool
    exit_code: int
    logs: str = ""
    error: Optional[str] = None
    duration: Optional[timedelta] = None
    container_id: Optional[str] = None


class LocalExecutor:
    """
    Executes steps by spawning Docker containers directly.

    Usage:
        executor = LocalExecutor()
        key = ExecutionKey("run-123", step_index=0, attempt=1)
        config = ExecutionConfig(image="python:3.12", command=["python", "-c", "print('hi')"])

        async for log_line in executor.execute_step(key, config):
            print(log_line)
    """

    def __init__(
        self,
        docker_client=None,
        idempotency_store: Optional[IdempotencyStore] = None,
    ):
        """
        Initialize LocalExecutor.

        Args:
            docker_client: Docker client (or mock for testing)
            idempotency_store: Store for idempotency tracking
        """
        self._docker = docker_client
        self._idempotency = idempotency_store or get_idempotency_store()
        self._state_machines: dict[ExecutionKey, StepStateMachine] = {}
        self._containers: dict[ExecutionKey, str] = {}  # key -> container_id
        self._cancel_events: dict[ExecutionKey, asyncio.Event] = {}

    def _get_docker_client(self):
        """Get or create Docker client."""
        if self._docker is not None:
            return self._docker

        try:
            import docker
            self._docker = docker.from_env()
            return self._docker
        except Exception as e:
            raise ExecutionError(f"Failed to connect to Docker: {e}")

    def _get_state_machine(self, key: ExecutionKey) -> StepStateMachine:
        """Get or create state machine for an execution."""
        if key not in self._state_machines:
            self._state_machines[key] = StepStateMachine(StepState.PENDING)
        return self._state_machines[key]

    def get_step_state(self, key: ExecutionKey) -> Optional[StepState]:
        """Get current state for an execution."""
        machine = self._state_machines.get(key)
        return machine.state if machine else None

    def _prepare_control_directory(
        self,
        key: ExecutionKey,
        config: ExecutionConfig,
    ) -> Optional[str]:
        """
        Prepare the .control directory for control layer execution.

        Creates /workspace/.control/step_config.json with:
        - Step ID and authentication token
        - Backend URL for communication
        - Command to execute
        - Environment and timeout settings

        Args:
            key: Execution key
            config: Execution configuration

        Returns:
            Step token if control layer is enabled, None otherwise
        """
        if not config.use_control_layer:
            return None

        from .step_token import generate_step_token

        # Generate step token for authentication
        step_id = str(key)
        token = generate_step_token(step_id)

        # Prepare control directory
        control_dir = Path(config.workspace_path) / ".control"
        control_dir.mkdir(parents=True, exist_ok=True)

        # Write step configuration
        step_config = {
            "step_id": step_id,
            "backend_url": config.backend_url or "http://host.docker.internal:8000",
            "token": token,
            "command": config.command,
            "working_dir": "/workspace/repo",
            "environment": config.environment,
            "timeout_seconds": config.timeout_seconds,
            "heartbeat_interval": config.heartbeat_interval,
            "log_batch_size": config.log_batch_size,
            "log_batch_interval": config.log_batch_interval,
        }

        config_path = control_dir / "step_config.json"
        config_path.write_text(json.dumps(step_config, indent=2))

        return token

    async def execute_step(
        self,
        key: ExecutionKey,
        config: ExecutionConfig,
    ) -> AsyncGenerator[Union[str, ExecutionResult], None]:
        """
        Execute a step, yielding log lines and final result.

        This is the main entry point for step execution.
        Idempotent: same key returns same execution.

        Args:
            key: Execution key for idempotency
            config: Execution configuration

        Yields:
            Log lines (str) during execution
            ExecutionResult at the end

        Raises:
            ExecutionError: If execution fails
            TimeoutError: If execution times out
        """
        # Check idempotency - return existing result if already complete
        existing_result = self._idempotency.get_result(key)
        if existing_result:
            yield ExecutionResult(
                success=existing_result.success,
                exit_code=existing_result.exit_code,
                error=existing_result.error,
            )
            return

        # Start or resume execution
        execution_id = self._idempotency.start_execution(key)
        machine = self._get_state_machine(key)

        # Set up cancellation
        cancel_event = asyncio.Event()
        self._cancel_events[key] = cancel_event

        try:
            # Transition to PREPARING
            if machine.state == StepState.PENDING:
                machine.transition(StepState.PREPARING)

            yield f"[executor] Preparing container with image {config.image}"

            # Prepare control directory if using control layer
            if config.use_control_layer:
                self._prepare_control_directory(key, config)
                yield f"[executor] Control layer configured"

            # Create container
            docker_client = self._get_docker_client()
            container = await self._create_container(docker_client, config, key)
            self._containers[key] = container.id

            yield f"[executor] Container created: {container.id[:12]}"

            # Transition to RUNNING
            machine.transition(StepState.RUNNING)
            yield f"[executor] Starting execution..."

            # Start container
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, container.start)

            start_time = datetime.utcnow()

            # Stream logs with timeout
            logs_buffer = []
            try:
                async for log_line in self._stream_logs(container, config.timeout_seconds, cancel_event):
                    logs_buffer.append(log_line)
                    yield log_line
            except asyncio.TimeoutError:
                # Kill and cleanup container on timeout
                await self._kill_container(container)
                await self._cleanup_container(container)
                machine.transition(StepState.FAILED, reason="timeout", exit_code=-1)
                self._idempotency.complete_execution(key, success=False, exit_code=-1, error="Timeout")
                raise TimeoutError(f"Execution timed out after {config.timeout_seconds}s")
            except asyncio.CancelledError:
                # Cancelled - kill and cleanup
                await self._kill_container(container)
                await self._cleanup_container(container)
                machine.transition(StepState.CANCELLED)
                raise

            # Get exit code
            await loop.run_in_executor(None, container.reload)
            exit_code = container.attrs.get("State", {}).get("ExitCode", -1)

            # Transition to COMPLETING
            machine.transition(StepState.COMPLETING)

            # Determine success
            success = exit_code == 0
            duration = datetime.utcnow() - start_time

            # Final transition
            if success:
                machine.transition(StepState.COMPLETED, exit_code=exit_code)
            else:
                machine.transition(StepState.FAILED, exit_code=exit_code)

            # Record in idempotency store
            self._idempotency.complete_execution(
                key,
                success=success,
                exit_code=exit_code,
                error=None if success else f"Exit code: {exit_code}",
            )

            # Cleanup container
            await self._cleanup_container(container)

            # Yield final result
            yield ExecutionResult(
                success=success,
                exit_code=exit_code,
                logs="\n".join(logs_buffer),
                duration=duration,
                container_id=container.id,
            )

        except (ExecutionError, TimeoutError):
            raise
        except Exception as e:
            # Handle unexpected errors - cleanup container if it exists
            if 'container' in locals():
                await self._kill_container(container)
                await self._cleanup_container(container)
            if machine.state not in {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED}:
                machine.transition(StepState.FAILED, reason=str(e))
            self._idempotency.complete_execution(key, success=False, exit_code=-1, error=str(e))
            raise ExecutionError(f"Execution failed: {e}")
        finally:
            # Cleanup
            self._cancel_events.pop(key, None)

    async def _create_container(self, docker_client, config: ExecutionConfig, key: ExecutionKey):
        """Create a Docker container for execution."""
        loop = asyncio.get_event_loop()

        # Build container config
        container_name = f"lazyaf-step-{key.step_index}-{str(uuid4())[:8]}"

        # Determine if workspace_path is a Docker volume name or host path
        # Docker volume names start with alphanumeric, host paths start with /
        is_volume = not config.workspace_path.startswith("/") and not config.workspace_path.startswith("\\")

        if is_volume:
            # Named Docker volume - use list format for volumes
            # Format: ["volume_name:/container/path:mode"]
            volumes = [f"{config.workspace_path}:{config.working_dir}:rw"]
        else:
            # Bind mount from host path
            volumes = {
                config.workspace_path: {"bind": config.working_dir, "mode": "rw"},
            }

        # Environment variables
        env = dict(config.environment)
        if config.use_control_layer:
            # Set HOME to workspace/home for cache persistence
            env["HOME"] = "/workspace/home"

        kwargs = {
            "image": config.image,
            "name": container_name,
            "volumes": volumes,
            "working_dir": config.working_dir,
            "environment": env,
            "network_mode": config.network_mode,
            "detach": True,
        }

        # Only pass command if NOT using control layer
        # Control layer reads command from step_config.json
        if not config.use_control_layer:
            kwargs["command"] = config.command

        if config.memory_limit:
            kwargs["mem_limit"] = config.memory_limit

        if config.cpu_limit:
            kwargs["cpu_quota"] = int(config.cpu_limit * 100000)
            kwargs["cpu_period"] = 100000

        try:
            container = await loop.run_in_executor(
                None,
                lambda: docker_client.containers.create(**kwargs)
            )
            return container
        except Exception as e:
            raise ExecutionError(f"Failed to create container: {e}")

    async def _stream_logs(
        self,
        container,
        timeout: int,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[str, None]:
        """Stream logs from container with timeout.

        Uses a background thread to read from the blocking Docker log stream
        and an asyncio queue to pass lines to the async code. This allows
        proper timeout checking even when no output is being produced.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        # Queue for passing log lines from reader thread to async code
        log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

        def read_logs_sync():
            """Synchronous log reader running in a thread."""
            try:
                logs = container.logs(stream=True, follow=True, stdout=True, stderr=True)
                for line in logs:
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    # Use call_soon_threadsafe to put items on the queue
                    loop.call_soon_threadsafe(log_queue.put_nowait, line.rstrip("\n\r"))
            except Exception:
                pass  # Log stream ended - container probably exited
            finally:
                # Signal end of stream
                loop.call_soon_threadsafe(log_queue.put_nowait, None)

        # Start reader thread
        reader_future = loop.run_in_executor(None, read_logs_sync)

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                if cancel_event.is_set():
                    raise asyncio.CancelledError()

                try:
                    # Wait for next line with short timeout to allow deadline/cancel checks
                    line = await asyncio.wait_for(
                        log_queue.get(),
                        timeout=min(remaining, 0.5)
                    )

                    if line is None:
                        # Reader done - container exited
                        break

                    yield line

                except asyncio.TimeoutError:
                    # No output within 0.5s - check if overall deadline exceeded
                    if loop.time() >= deadline:
                        raise
                    # Otherwise continue waiting for more output
        finally:
            # The reader thread will exit when container dies or logs close
            # We don't need to explicitly stop it as container.kill() handles that
            pass

    async def _kill_container(self, container) -> None:
        """Kill a container."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: container.kill(signal="SIGKILL"))
        except Exception:
            pass  # Container may already be dead

    async def _cleanup_container(self, container) -> None:
        """Remove a container."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except Exception:
            pass  # Best effort cleanup

    async def cancel(self, key: ExecutionKey) -> bool:
        """
        Cancel an execution.

        Args:
            key: Execution key

        Returns:
            True if cancellation was signaled
        """
        cancel_event = self._cancel_events.get(key)
        if cancel_event:
            cancel_event.set()

        # Also kill container if we have one
        container_id = self._containers.get(key)
        if container_id:
            try:
                docker_client = self._get_docker_client()
                container = docker_client.containers.get(container_id)
                await self._kill_container(container)
            except Exception:
                pass

        # Update state machine
        machine = self._state_machines.get(key)
        if machine and not machine.is_terminal:
            machine.transition(StepState.CANCELLED)

        return True

    async def get_orphaned_executions(self) -> list[ExecutionKey]:
        """
        Find executions that are stuck (container dead but state not terminal).

        Used for crash recovery on startup.

        Returns:
            List of orphaned execution keys
        """
        orphaned = []

        for key, machine in self._state_machines.items():
            if machine.is_terminal:
                continue

            container_id = self._containers.get(key)
            if not container_id:
                orphaned.append(key)
                continue

            try:
                docker_client = self._get_docker_client()
                container = docker_client.containers.get(container_id)
                status = container.status
                if status in ("exited", "dead"):
                    orphaned.append(key)
            except Exception:
                orphaned.append(key)

        return orphaned

    async def recover_orphaned(self, key: ExecutionKey) -> None:
        """
        Recover an orphaned execution by marking it as failed.

        Args:
            key: Execution key
        """
        machine = self._state_machines.get(key)
        if machine and not machine.is_terminal:
            machine.transition(StepState.FAILED, reason="Orphaned - recovered on restart")
            self._idempotency.complete_execution(
                key,
                success=False,
                exit_code=-1,
                error="Orphaned execution recovered on restart",
            )


# Global singleton instance
_local_executor: Optional[LocalExecutor] = None


def get_local_executor() -> LocalExecutor:
    """Get the global LocalExecutor instance."""
    global _local_executor
    if _local_executor is None:
        _local_executor = LocalExecutor()
    return _local_executor
