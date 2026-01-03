"""
Mock infrastructure for Phase 12 testing.

Provides mock implementations of external dependencies (Docker, WebSocket, etc.)
that can be used for unit testing without real infrastructure.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable
from uuid import uuid4


# =============================================================================
# Mock Docker Client
# =============================================================================


class MockContainerState(str, Enum):
    """Mock container states."""
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    EXITED = "exited"
    DEAD = "dead"


@dataclass
class MockContainer:
    """Mock Docker container."""
    id: str
    name: str
    image: str
    status: MockContainerState = MockContainerState.RUNNING
    exit_code: int | None = None
    command: str | list[str] | None = None
    environment: dict = field(default_factory=dict)
    volumes: dict = field(default_factory=dict)
    logs_content: str = ""
    attrs: dict = field(default_factory=dict)

    def __post_init__(self):
        self.attrs = {
            "Id": self.id,
            "Name": self.name,
            "State": {
                "Status": self.status.value,
                "Running": self.status == MockContainerState.RUNNING,
                "Paused": self.status == MockContainerState.PAUSED,
                "ExitCode": self.exit_code,
            }
        }

    def kill(self, signal: str = "SIGKILL"):
        """Kill the container."""
        self.status = MockContainerState.EXITED
        self.exit_code = -9 if signal == "SIGKILL" else -15
        self._update_attrs()

    def pause(self):
        """Pause the container."""
        if self.status != MockContainerState.RUNNING:
            raise RuntimeError("Container not running")
        self.status = MockContainerState.PAUSED
        self._update_attrs()

    def unpause(self):
        """Unpause the container."""
        if self.status != MockContainerState.PAUSED:
            raise RuntimeError("Container not paused")
        self.status = MockContainerState.RUNNING
        self._update_attrs()

    def stop(self, timeout: int = 10):
        """Stop the container."""
        self.status = MockContainerState.EXITED
        self.exit_code = 0
        self._update_attrs()

    def remove(self, force: bool = False):
        """Remove the container (marks as dead)."""
        self.status = MockContainerState.DEAD
        self._update_attrs()

    def wait(self) -> dict:
        """Wait for container to complete."""
        # In mock, just return current state
        return {"StatusCode": self.exit_code or 0}

    def logs(self, stream: bool = False, stdout: bool = True, stderr: bool = True) -> bytes:
        """Get container logs."""
        return self.logs_content.encode()

    def reload(self):
        """Reload container state (no-op for mock)."""
        pass

    def _update_attrs(self):
        """Update attrs dict after state change."""
        self.attrs["State"]["Status"] = self.status.value
        self.attrs["State"]["Running"] = self.status == MockContainerState.RUNNING
        self.attrs["State"]["Paused"] = self.status == MockContainerState.PAUSED
        self.attrs["State"]["ExitCode"] = self.exit_code


@dataclass
class MockNetwork:
    """Mock Docker network."""
    id: str
    name: str
    connected_containers: set = field(default_factory=set)

    def connect(self, container_id: str):
        """Connect a container to this network."""
        self.connected_containers.add(container_id)

    def disconnect(self, container_id: str, force: bool = False):
        """Disconnect a container from this network."""
        self.connected_containers.discard(container_id)


class MockDockerClient:
    """
    Mock Docker client for unit testing.

    Provides a fake Docker API that tracks containers and networks in memory.

    Usage:
        client = MockDockerClient()
        container = client.containers.run("python:3.12", "echo hello")
        assert container.status == MockContainerState.RUNNING

        # Simulate failure
        client.fail_next_run = True
        with pytest.raises(DockerException):
            client.containers.run(...)
    """

    def __init__(self):
        self._containers: dict[str, MockContainer] = {}
        self._networks: dict[str, MockNetwork] = {}
        self.fail_next_run: bool = False
        self.fail_next_pull: bool = False
        self._run_callback: Callable[[str, str], None] | None = None

        # Initialize default bridge network
        self._networks["bridge"] = MockNetwork(
            id="bridge-id",
            name="bridge"
        )

    def ping(self) -> bool:
        """Check if Docker is available."""
        return True

    @property
    def containers(self) -> "MockContainersAPI":
        """Access containers API."""
        return MockContainersAPI(self)

    @property
    def networks(self) -> "MockNetworksAPI":
        """Access networks API."""
        return MockNetworksAPI(self)

    @property
    def images(self) -> "MockImagesAPI":
        """Access images API."""
        return MockImagesAPI(self)

    def on_run(self, callback: Callable[[str, str], None]):
        """Register a callback for container.run() calls."""
        self._run_callback = callback


class MockContainersAPI:
    """Mock containers API."""

    def __init__(self, client: MockDockerClient):
        self._client = client

    def run(
        self,
        image: str,
        command: str | list[str] | None = None,
        name: str | None = None,
        environment: dict | None = None,
        volumes: dict | None = None,
        detach: bool = True,
        remove: bool = True,
        **kwargs,
    ) -> MockContainer:
        """Run a container."""
        if self._client.fail_next_run:
            self._client.fail_next_run = False
            raise DockerException("Failed to run container")

        container_id = str(uuid4())[:12]
        container_name = name or f"mock-{container_id}"

        container = MockContainer(
            id=container_id,
            name=container_name,
            image=image,
            command=command,
            environment=environment or {},
            volumes=volumes or {},
        )

        self._client._containers[container_id] = container
        self._client._networks["bridge"].connect(container_id)

        if self._client._run_callback:
            self._client._run_callback(image, command)

        return container

    def get(self, container_id: str) -> MockContainer:
        """Get a container by ID."""
        if container_id not in self._client._containers:
            raise DockerNotFound(f"Container not found: {container_id}")
        return self._client._containers[container_id]

    def list(self, all: bool = False) -> list[MockContainer]:
        """List containers."""
        containers = list(self._client._containers.values())
        if not all:
            containers = [c for c in containers if c.status == MockContainerState.RUNNING]
        return containers


class MockNetworksAPI:
    """Mock networks API."""

    def __init__(self, client: MockDockerClient):
        self._client = client

    def get(self, network_id: str) -> MockNetwork:
        """Get a network by ID or name."""
        if network_id in self._client._networks:
            return self._client._networks[network_id]
        for net in self._client._networks.values():
            if net.name == network_id:
                return net
        raise DockerNotFound(f"Network not found: {network_id}")

    def list(self) -> list[MockNetwork]:
        """List networks."""
        return list(self._client._networks.values())


class MockImagesAPI:
    """Mock images API."""

    def __init__(self, client: MockDockerClient):
        self._client = client
        self._images: set[str] = set()

    def pull(self, image: str, **kwargs) -> None:
        """Pull an image."""
        if self._client.fail_next_pull:
            self._client.fail_next_pull = False
            raise DockerException(f"Failed to pull image: {image}")
        self._images.add(image)

    def get(self, image: str):
        """Get image info."""
        if image not in self._images:
            raise DockerNotFound(f"Image not found: {image}")
        return {"Id": image, "RepoTags": [image]}


class DockerException(Exception):
    """Mock Docker exception."""
    pass


class DockerNotFound(DockerException):
    """Mock Docker not found exception."""
    pass


# =============================================================================
# Mock WebSocket
# =============================================================================


@dataclass
class MockWebSocketMessage:
    """A message sent or received on the mock WebSocket."""
    data: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)
    direction: str = "sent"  # "sent" or "received"


class MockWebSocket:
    """
    Mock WebSocket for testing.

    Usage:
        ws = MockWebSocket()

        # Simulate receiving a message
        ws.add_incoming({"type": "register", "runner_id": "123"})

        # Send a message
        await ws.send_json({"type": "ack"})

        # Check sent messages
        assert ws.sent_messages[0]["type"] == "ack"
    """

    def __init__(self):
        self.connected: bool = True
        self.sent_messages: list[Any] = []
        self.received_messages: list[Any] = []
        self._incoming_queue: asyncio.Queue = asyncio.Queue()
        self._closed: bool = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def add_incoming(self, message: Any):
        """Add a message to the incoming queue (simulates receiving)."""
        self._incoming_queue.put_nowait(message)

    async def send_json(self, data: Any):
        """Send JSON message."""
        if self._closed:
            raise WebSocketClosed("WebSocket is closed")
        self.sent_messages.append(data)

    async def send_text(self, data: str):
        """Send text message."""
        if self._closed:
            raise WebSocketClosed("WebSocket is closed")
        self.sent_messages.append(data)

    async def receive_json(self, timeout: float | None = None) -> Any:
        """Receive JSON message."""
        if self._closed:
            raise WebSocketClosed("WebSocket is closed")
        try:
            if timeout:
                msg = await asyncio.wait_for(self._incoming_queue.get(), timeout)
            else:
                msg = await self._incoming_queue.get()
            self.received_messages.append(msg)
            return msg
        except asyncio.TimeoutError:
            raise WebSocketTimeout("Receive timeout")

    async def receive_text(self, timeout: float | None = None) -> str:
        """Receive text message."""
        msg = await self.receive_json(timeout)
        return str(msg)

    async def close(self, code: int = 1000, reason: str = ""):
        """Close the WebSocket."""
        self._closed = True
        self.connected = False
        self.close_code = code
        self.close_reason = reason

    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self.connected and not self._closed

    def clear(self):
        """Clear all messages."""
        self.sent_messages.clear()
        self.received_messages.clear()
        while not self._incoming_queue.empty():
            self._incoming_queue.get_nowait()


class WebSocketClosed(Exception):
    """Raised when operating on a closed WebSocket."""
    pass


class WebSocketTimeout(Exception):
    """Raised when WebSocket receive times out."""
    pass


# =============================================================================
# Mock Runner
# =============================================================================


@dataclass
class MockRunner:
    """
    Mock runner for testing backend runner handling.

    Simulates a runner that can:
    - Register with backend
    - Send heartbeats
    - Receive and execute jobs
    - Report completion

    Usage:
        runner = MockRunner(runner_type="claude-code")
        await runner.register(backend_url)

        # Simulate job execution
        job = await runner.poll_for_job()
        runner.execute_job(job)
        await runner.complete_job(success=True)
    """
    runner_id: str = field(default_factory=lambda: str(uuid4()))
    runner_type: str = "claude-code"
    name: str = "mock-runner"
    status: str = "idle"
    current_job: dict | None = None
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)

    # Tracking
    jobs_executed: list[dict] = field(default_factory=list)
    heartbeats_sent: int = 0
    registrations: int = 0

    # Behavior control
    fail_next_job: bool = False
    execution_time: float = 0.0  # Simulated execution time

    async def register(self, backend_url: str) -> bool:
        """Simulate registration with backend."""
        self.registrations += 1
        self.status = "idle"
        return True

    async def heartbeat(self) -> bool:
        """Simulate sending heartbeat."""
        self.heartbeats_sent += 1
        self.last_heartbeat = datetime.utcnow()
        return True

    async def poll_for_job(self) -> dict | None:
        """Simulate polling for a job."""
        if self.current_job:
            return self.current_job
        return None

    def assign_job(self, job: dict):
        """Assign a job to this runner."""
        self.current_job = job
        self.status = "busy"

    async def execute_job(self, job: dict) -> tuple[bool, str | None]:
        """
        Simulate job execution.

        Returns:
            Tuple of (success, error_message)
        """
        if self.execution_time > 0:
            await asyncio.sleep(self.execution_time)

        self.jobs_executed.append(job)

        if self.fail_next_job:
            self.fail_next_job = False
            return False, "Simulated job failure"

        return True, None

    async def complete_job(self, success: bool, error: str | None = None) -> bool:
        """Simulate job completion report."""
        self.current_job = None
        self.status = "idle"
        return True

    def disconnect(self):
        """Simulate runner disconnect."""
        self.status = "offline"

    def reconnect(self):
        """Simulate runner reconnection."""
        self.status = "idle"


# =============================================================================
# Chaos Controller
# =============================================================================


class ChaosController:
    """
    Controller for injecting failures during tests.

    Usage:
        chaos = ChaosController()

        # Inject a network partition for 5 seconds
        chaos.inject_failure("network_partition", target="runner-1", duration=5.0)

        # Check if failure is active
        if chaos.is_failure_active("network_partition", "runner-1"):
            # Simulate network failure behavior

        # Clear failures
        chaos.clear_all()
    """

    def __init__(self):
        self._active_failures: dict[str, dict] = {}
        self._failure_history: list[dict] = []

    def inject_failure(
        self,
        failure_type: str,
        target: str | None = None,
        duration: float | None = None,
        data: dict | None = None,
    ):
        """
        Inject a failure.

        Args:
            failure_type: Type of failure (network_partition, process_kill, disk_full, slow_io)
            target: Target identifier (e.g., container ID, runner ID)
            duration: How long the failure lasts (None = permanent until cleared)
            data: Additional failure configuration
        """
        key = f"{failure_type}:{target or 'global'}"
        failure = {
            "type": failure_type,
            "target": target,
            "duration": duration,
            "data": data or {},
            "started_at": datetime.utcnow(),
        }
        self._active_failures[key] = failure
        self._failure_history.append(failure)

    def is_failure_active(self, failure_type: str, target: str | None = None) -> bool:
        """Check if a specific failure is currently active."""
        key = f"{failure_type}:{target or 'global'}"

        if key not in self._active_failures:
            # Check for global failure of this type
            global_key = f"{failure_type}:global"
            if global_key not in self._active_failures:
                return False
            key = global_key

        failure = self._active_failures[key]

        # Check duration
        if failure["duration"] is not None:
            elapsed = (datetime.utcnow() - failure["started_at"]).total_seconds()
            if elapsed > failure["duration"]:
                del self._active_failures[key]
                return False

        return True

    def clear_failure(self, failure_type: str, target: str | None = None):
        """Clear a specific failure."""
        key = f"{failure_type}:{target or 'global'}"
        self._active_failures.pop(key, None)

    def clear_all(self):
        """Clear all active failures."""
        self._active_failures.clear()

    def get_active_failures(self) -> list[dict]:
        """Get list of all active failures."""
        return list(self._active_failures.values())

    def get_failure_history(self) -> list[dict]:
        """Get history of all injected failures."""
        return list(self._failure_history)
