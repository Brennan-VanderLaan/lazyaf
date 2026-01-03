"""
Docker manipulation helpers for Phase 12 testing.

These helpers allow tests to spawn, kill, pause, and disconnect containers
for testing resilience and failure scenarios.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator

try:
    import docker
    from docker.models.containers import Container
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    Container = None


@dataclass
class ContainerHandle:
    """Handle to a test container."""
    id: str
    name: str
    image: str
    status: str

    def __repr__(self) -> str:
        return f"ContainerHandle({self.name}, status={self.status})"


def docker_available() -> bool:
    """Check if Docker is available for testing."""
    if not DOCKER_AVAILABLE:
        return False
    try:
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def get_docker_client():
    """Get a Docker client. Raises if unavailable."""
    if not DOCKER_AVAILABLE:
        raise RuntimeError("docker package not installed. Run: pip install docker")
    return docker.from_env()


async def spawn_test_container(
    image: str,
    command: str | list[str] | None = None,
    name: str | None = None,
    environment: dict | None = None,
    volumes: dict | None = None,
    detach: bool = True,
    remove: bool = True,
) -> ContainerHandle:
    """
    Spawn a container for testing.

    Args:
        image: Docker image to use
        command: Command to run (or None for image default)
        name: Container name (auto-generated if not provided)
        environment: Environment variables
        volumes: Volume mounts as {host_path: {'bind': container_path, 'mode': 'rw'}}
        detach: Run in background (default True)
        remove: Auto-remove when stopped (default True)

    Returns:
        ContainerHandle with container info
    """
    client = get_docker_client()

    # Run in thread pool to not block async loop
    loop = asyncio.get_event_loop()
    container = await loop.run_in_executor(
        None,
        lambda: client.containers.run(
            image,
            command=command,
            name=name,
            environment=environment or {},
            volumes=volumes or {},
            detach=detach,
            remove=remove,
        )
    )

    return ContainerHandle(
        id=container.id,
        name=container.name,
        image=image,
        status="running",
    )


async def kill_container(container_id: str, signal: str = "SIGKILL") -> bool:
    """
    Force kill a container.

    Args:
        container_id: Container ID or name
        signal: Signal to send (default SIGKILL)

    Returns:
        True if killed successfully
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.get(container_id)
        )
        await loop.run_in_executor(None, lambda: container.kill(signal=signal))
        return True
    except Exception:
        return False


async def pause_container(container_id: str) -> bool:
    """
    Pause a container (simulates hang/freeze).

    Args:
        container_id: Container ID or name

    Returns:
        True if paused successfully
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.get(container_id)
        )
        await loop.run_in_executor(None, container.pause)
        return True
    except Exception:
        return False


async def unpause_container(container_id: str) -> bool:
    """
    Unpause a paused container.

    Args:
        container_id: Container ID or name

    Returns:
        True if unpaused successfully
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.get(container_id)
        )
        await loop.run_in_executor(None, container.unpause)
        return True
    except Exception:
        return False


async def disconnect_network(container_id: str, network: str = "bridge") -> bool:
    """
    Disconnect a container from a network (simulates network partition).

    Args:
        container_id: Container ID or name
        network: Network to disconnect from (default: bridge)

    Returns:
        True if disconnected successfully
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        net = await loop.run_in_executor(
            None,
            lambda: client.networks.get(network)
        )
        await loop.run_in_executor(
            None,
            lambda: net.disconnect(container_id, force=True)
        )
        return True
    except Exception:
        return False


async def reconnect_network(container_id: str, network: str = "bridge") -> bool:
    """
    Reconnect a container to a network.

    Args:
        container_id: Container ID or name
        network: Network to connect to (default: bridge)

    Returns:
        True if reconnected successfully
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        net = await loop.run_in_executor(
            None,
            lambda: client.networks.get(network)
        )
        await loop.run_in_executor(
            None,
            lambda: net.connect(container_id)
        )
        return True
    except Exception:
        return False


async def get_container_status(container_id: str) -> str | None:
    """
    Get container status.

    Returns:
        Status string (running, paused, exited, etc.) or None if not found
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.get(container_id)
        )
        await loop.run_in_executor(None, container.reload)
        return container.status
    except Exception:
        return None


async def get_container_exit_code(container_id: str) -> int | None:
    """
    Get container exit code (for completed containers).

    Returns:
        Exit code or None if container is still running or not found
    """
    client = get_docker_client()
    try:
        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: client.containers.get(container_id)
        )
        await loop.run_in_executor(None, container.reload)
        return container.attrs.get("State", {}).get("ExitCode")
    except Exception:
        return None


async def wait_for_container(container_id: str, timeout: float = 30.0) -> int:
    """
    Wait for a container to complete.

    Args:
        container_id: Container ID or name
        timeout: Maximum wait time in seconds

    Returns:
        Exit code

    Raises:
        TimeoutError if container doesn't complete in time
    """
    client = get_docker_client()
    loop = asyncio.get_event_loop()
    container = await loop.run_in_executor(
        None,
        lambda: client.containers.get(container_id)
    )

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, container.wait),
            timeout=timeout
        )
        return result.get("StatusCode", -1)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Container {container_id} did not complete within {timeout}s")


@asynccontextmanager
async def test_container(
    image: str,
    command: str | list[str] | None = None,
    **kwargs
) -> AsyncGenerator[ContainerHandle, None]:
    """
    Context manager for test containers. Auto-cleans up on exit.

    Usage:
        async with test_container("python:3.12", "sleep 60") as container:
            # Test with container
            await kill_container(container.id)
    """
    handle = await spawn_test_container(image, command, **kwargs)
    try:
        yield handle
    finally:
        # Ensure cleanup
        try:
            await kill_container(handle.id)
        except Exception:
            pass
