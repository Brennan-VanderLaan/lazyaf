"""
Docker Orchestrator for executing steps in containers.

Spawns Docker containers to execute step commands.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

try:
    import docker
    from docker.models.containers import Container
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None
    Container = None

logger = logging.getLogger(__name__)


class DockerOrchestrator:
    """
    Executes steps by spawning Docker containers.

    Usage:
        orch = DockerOrchestrator()
        async for log_lines in orch.execute(config):
            print(log_lines)
        print(f"Exit code: {orch.last_exit_code}")
    """

    def __init__(self):
        """Initialize Docker orchestrator."""
        self._client: Optional[docker.DockerClient] = None
        self._last_exit_code: int = 0

    @property
    def last_exit_code(self) -> int:
        """Get exit code from last execution."""
        return self._last_exit_code

    def _get_client(self) -> docker.DockerClient:
        """Get or create Docker client."""
        if not DOCKER_AVAILABLE:
            raise RuntimeError("Docker SDK not available")
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def execute(self, config: dict) -> AsyncIterator[list[str]]:
        """
        Execute a step in a Docker container.

        Args:
            config: Execution configuration with:
                - image: Docker image to use
                - command: Command to run
                - environment: Environment variables
                - working_dir: Working directory
                - timeout: Timeout in seconds

        Yields:
            Batches of log lines

        After execution, check last_exit_code for result.
        """
        image = config.get("image", "python:3.12-slim")
        command = config.get("command", "echo 'Hello from LazyAF'")
        environment = config.get("environment", {})
        working_dir = config.get("working_dir", "/workspace")
        timeout = config.get("timeout", 300)

        logger.info(f"Executing: {command} in {image}")

        # Run in thread pool since docker-py is sync
        loop = asyncio.get_event_loop()

        try:
            container = await loop.run_in_executor(
                None,
                lambda: self._create_container(image, command, environment, working_dir)
            )

            logger.info(f"Container created: {container.id[:12]}")

            # Start container
            await loop.run_in_executor(None, container.start)

            # Stream logs
            log_buffer = []
            log_stream = container.logs(stream=True, follow=True)

            async def read_logs():
                """Read logs from container."""
                for chunk in log_stream:
                    line = chunk.decode("utf-8", errors="replace").rstrip()
                    if line:
                        log_buffer.append(line)

            # Start log reader
            log_task = asyncio.create_task(
                loop.run_in_executor(None, lambda: list(read_logs()))
            )

            # Wait for container with timeout
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: container.wait(timeout=timeout)),
                    timeout=timeout + 5,
                )
                self._last_exit_code = result.get("StatusCode", 1)
            except asyncio.TimeoutError:
                logger.warning(f"Container timeout after {timeout}s")
                await loop.run_in_executor(None, container.kill)
                self._last_exit_code = -1

            # Cancel log task
            log_task.cancel()
            try:
                await log_task
            except asyncio.CancelledError:
                pass

            # Yield any buffered logs
            if log_buffer:
                yield log_buffer

            # Get final logs
            final_logs = await loop.run_in_executor(
                None,
                lambda: container.logs().decode("utf-8", errors="replace")
            )
            if final_logs:
                yield final_logs.strip().split("\n")

        except Exception as e:
            logger.error(f"Container execution failed: {e}")
            self._last_exit_code = 1
            yield [f"Error: {e}"]

        finally:
            # Cleanup container
            try:
                await loop.run_in_executor(
                    None,
                    lambda: container.remove(force=True)
                )
            except Exception as e:
                logger.warning(f"Failed to remove container: {e}")

    def _create_container(
        self,
        image: str,
        command: str,
        environment: dict,
        working_dir: str,
    ) -> Container:
        """Create Docker container."""
        client = self._get_client()

        # Pull image if needed
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            logger.info(f"Pulling image: {image}")
            client.images.pull(image)

        return client.containers.create(
            image=image,
            command=["sh", "-c", command],
            environment=environment,
            working_dir=working_dir,
            detach=True,
            network_mode="host",  # Allow access to host network
        )
