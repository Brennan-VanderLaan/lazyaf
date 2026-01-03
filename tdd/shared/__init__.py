# Cross-cutting test utilities shared across all test types

from .docker_helpers import (
    ContainerHandle,
    docker_available,
    get_docker_client,
    spawn_test_container,
    kill_container,
    pause_container,
    unpause_container,
    disconnect_network,
    reconnect_network,
    get_container_status,
    get_container_exit_code,
    wait_for_container,
    test_container,
)

from .process_helpers import (
    ProcessHandle,
    spawn_process,
    kill_process,
    send_signal,
    stop_process,
    continue_process,
    is_process_running,
    wait_for_process,
    wait_for_condition,
    ProcessCrashSimulator,
    SimulatedCrash,
)

from .time_helpers import (
    FrozenTime,
    freeze_time,
    AsyncTimeController,
    mock_async_sleep,
    wait_with_timeout,
    HeartbeatSimulator,
)

from .mocks import (
    # Docker mocks
    MockContainerState,
    MockContainer,
    MockNetwork,
    MockDockerClient,
    MockContainersAPI,
    MockNetworksAPI,
    MockImagesAPI,
    DockerException,
    DockerNotFound,
    # WebSocket mocks
    MockWebSocketMessage,
    MockWebSocket,
    WebSocketClosed,
    WebSocketTimeout,
    # Runner mocks
    MockRunner,
    # Chaos testing
    ChaosController,
)

__all__ = [
    # Docker helpers
    "ContainerHandle",
    "docker_available",
    "get_docker_client",
    "spawn_test_container",
    "kill_container",
    "pause_container",
    "unpause_container",
    "disconnect_network",
    "reconnect_network",
    "get_container_status",
    "get_container_exit_code",
    "wait_for_container",
    "test_container",
    # Process helpers
    "ProcessHandle",
    "spawn_process",
    "kill_process",
    "send_signal",
    "stop_process",
    "continue_process",
    "is_process_running",
    "wait_for_process",
    "wait_for_condition",
    "ProcessCrashSimulator",
    "SimulatedCrash",
    # Time helpers
    "FrozenTime",
    "freeze_time",
    "AsyncTimeController",
    "mock_async_sleep",
    "wait_with_timeout",
    "HeartbeatSimulator",
    # Docker mocks
    "MockContainerState",
    "MockContainer",
    "MockNetwork",
    "MockDockerClient",
    "MockContainersAPI",
    "MockNetworksAPI",
    "MockImagesAPI",
    "DockerException",
    "DockerNotFound",
    # WebSocket mocks
    "MockWebSocketMessage",
    "MockWebSocket",
    "WebSocketClosed",
    "WebSocketTimeout",
    # Runner mocks
    "MockRunner",
    # Chaos testing
    "ChaosController",
]
