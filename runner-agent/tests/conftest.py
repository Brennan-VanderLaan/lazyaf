"""Shared fakes for the runner-agent suite.

Test-double policy (R6): the doubles here are REAL objects with real behavior -
a transport backed by an asyncio queue, an orchestrator that actually awaits its
cancel event - not ``AsyncMock``. A mock that returns whatever it is asked for
cannot fail the way the thing it stands in for fails, and every defect this
phase is written against (inline execution blocking a receive loop, a log line
landing mid-container, a repeated execution_key re-running work) is a TIMING
defect that a mock erases.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

RUNNER_AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNNER_AGENT_DIR.parent

if str(RUNNER_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_AGENT_DIR))

from lazyaf_runner.config import RunnerConfig  # noqa: E402
from lazyaf_runner.orchestrator.base import StepOrchestrator  # noqa: E402
from lazyaf_runner.session import TransportClosed  # noqa: E402
from lazyaf_runner.types import StepAssignment, StepOutcome  # noqa: E402

_CLOSE = object()


class FakeTransport:
    """An in-memory WebSocket: real queueing, real close semantics, no socket."""

    def __init__(self, inbound=None) -> None:
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.send_failure: Exception | None = None
        self._inbound: asyncio.Queue = asyncio.Queue()
        for message in inbound or []:
            self._inbound.put_nowait(message)

    # --- test-side API ---------------------------------------------------
    def push(self, message: dict) -> None:
        self._inbound.put_nowait(message)

    def push_close(self) -> None:
        self._inbound.put_nowait(_CLOSE)

    def frames(self, msg_type: str) -> list[dict]:
        return [frame for frame in self.sent if frame.get("type") == msg_type]

    async def wait_for(self, msg_type: str, *, timeout: float = 2.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            found = self.frames(msg_type)
            if found:
                return found[0]
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"no {msg_type!r} frame within {timeout}s; sent: "
            f"{[frame.get('type') for frame in self.sent]}"
        )

    # --- Transport protocol ----------------------------------------------
    async def recv(self) -> str:
        item = await self._inbound.get()
        if item is _CLOSE:
            raise TransportClosed("peer closed")
        return json.dumps(item)

    async def send(self, data: str) -> None:
        if self.send_failure is not None:
            raise self.send_failure
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is None:
            self.closed = (code, reason)
        # Real socket semantics: closing unblocks a pending recv() with a
        # closed error. Without this the fake would let a drain "close" the
        # connection while the receive loop sat there forever - the fake would
        # be kinder than the network, which is the wrong direction for a
        # double to differ.
        self.push_close()


class StubOrchestrator(StepOrchestrator):
    """A real orchestrator that runs no containers.

    ``hold`` lets a test keep a step "running" for as long as it needs while it
    exercises the receive loop, which is the only way to assert that step
    execution does not block message dispatch.
    """

    name = "stub"

    def __init__(
        self,
        *,
        outcome: StepOutcome | None = None,
        logs: list[str] | None = None,
        capabilities: dict | None = None,
        hold: bool = False,
        preflight_error: Exception | None = None,
    ) -> None:
        self._outcome = outcome or StepOutcome(0)
        self._logs = list(logs or [])
        self._capabilities = capabilities or {"orchestrator": "stub", "has": []}
        self._hold = hold
        self._preflight_error = preflight_error
        self.calls: list[StepAssignment] = []
        self.cleaned: list[str] = []
        self.preflighted = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.observed_cancel: asyncio.Event | None = None
        self.shutdowns = 0

    async def preflight(self) -> None:
        if self._preflight_error is not None:
            raise self._preflight_error
        self.preflighted = True

    def capabilities(self) -> dict:
        return dict(self._capabilities)

    async def run_step(self, assignment, *, on_log, cancel):
        self.calls.append(assignment)
        self.observed_cancel = cancel
        if self._logs:
            on_log(list(self._logs))
        self.started.set()
        if self._hold:
            waiters = [asyncio.create_task(self.release.wait()), asyncio.create_task(cancel.wait())]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiters:
                    task.cancel()
            if cancel.is_set():
                return StepOutcome(143, "cancelled")
        return self._outcome

    async def cleanup_workspace(self, retain_key: str) -> None:
        self.cleaned.append(retain_key)

    async def shutdown(self) -> None:
        self.shutdowns += 1


def make_config(**overrides) -> RunnerConfig:
    base = {
        "backend_url": "http://localhost:8000",
        "runner_id": "test-runner",
        "name": "test-runner",
        "runner_type": "generic",
        "labels": {},
        "orchestrator": "stub",
        "token": "test-token",
    }
    base.update(overrides)
    return RunnerConfig(**base)


def make_step_config(**overrides) -> dict:
    """A minimally realistic ``execute_step.config`` (section 3.2)."""
    config = {
        "protocol_version": 1,
        "backend_url": "http://backend:8000",
        "workspace": {
            "volume": "lazyaf-ws-run1",
            "retain_key": "run1",
            "mount_path": "/workspace",
            "repo_id": "r123",
            "clone_url": "http://backend:8000/git/r123.git",
            "branch": "main",
            "commit_sha": None,
        },
        "container": {
            "image": "lazyaf-base:dev",
            "command": None,
            "working_dir": "/workspace/repo",
            "timeout": 300,
            "memory_limit": None,
            "mounts": [],
            "environment": {
                "HOME": "/workspace/home",
                "LAZYAF_PIPELINE_RUN_ID": "run1",
                "LAZYAF_STEP_RUN_ID": "sr1",
                "LAZYAF_STEP_INDEX": "0",
                "LAZYAF_EXECUTION_KEY": "run1:0:step",
                "LAZYAF_BACKEND_URL": "http://backend:8000",
                "LAZYAF_CONTROL": "1",
                "CONFIG_PATH": "/workspace/.control/se1.json",
            },
            "control_mode": True,
        },
        "control_files": {
            "/workspace/.control/se1.json": {
                "step_id": "se1",
                "auth_token": "step-jwt",
                "environment": {},
            }
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return config


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def config() -> RunnerConfig:
    return make_config()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


# ---------------------------------------------------------------------------
# A fake docker daemon: real ORDERING, no containers.
#
# Everything it is asked to do lands in one ordered `events` list, which is what
# makes the log-ordering invariant (section 7.2) assertable at all: the test's
# on_log sink appends to the SAME list, so "no runner line between start and
# exit" becomes a statement about a single sequence rather than about wall time.
# ---------------------------------------------------------------------------

class FakeVolume:
    def __init__(self, name: str, labels: dict | None = None) -> None:
        self.name = name
        self.attrs = {"Labels": dict(labels or {})}


class FakeVolumes:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client
        self.store: dict[str, FakeVolume] = {}

    def get(self, name: str) -> FakeVolume:
        import docker.errors

        self._client.events.append(("volume.get", name))
        if name not in self.store:
            raise docker.errors.NotFound(f"no such volume {name}")
        return self.store[name]

    def create(self, name: str, labels: dict | None = None) -> FakeVolume:
        self._client.events.append(("volume.create", name))
        volume = FakeVolume(name, labels)
        self.store[name] = volume
        return volume


class FakeApi:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client

    def remove_volume(self, name: str, force: bool = False) -> None:
        import docker.errors

        self._client.events.append(("volume.remove", name))
        if name not in self._client.volumes.store:
            raise docker.errors.NotFound(f"no such volume {name}")
        del self._client.volumes.store[name]


class FakeContainer:
    def __init__(self, client: "FakeDockerClient", image: str, kwargs: dict) -> None:
        self._client = client
        self.image = image
        self.kwargs = kwargs
        self.archives: list[tuple[str, bytes]] = []
        self.started = False
        self.killed = False
        self.removed = False
        self.exit_code, self.log_output, self._wait_hook = client.behavior_for(image)

    def start(self) -> None:
        self.started = True
        self._client.events.append(("container.start", self.image))

    def wait(self, timeout=None) -> dict:
        self._client.events.append(("container.wait", self.image))
        if self._wait_hook is not None:
            self._wait_hook(self)
        return {"StatusCode": self.exit_code}

    def kill(self) -> None:
        self.killed = True
        self._client.events.append(("container.kill", self.image))

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self._client.events.append(("container.remove", self.image))

    def put_archive(self, path: str, data: bytes) -> bool:
        self.archives.append((path, data))
        self._client.events.append(("container.put_archive", path))
        return True

    def logs(self, **kwargs):
        return self.log_output


class FakeContainers:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client
        self.created: list[FakeContainer] = []

    def create(self, image: str, **kwargs) -> FakeContainer:
        self._client.events.append(("container.create", image))
        container = FakeContainer(self._client, image, kwargs)
        self.created.append(container)
        return container

    def run(self, image: str, **kwargs) -> FakeContainer:
        container = self.create(image, **kwargs)
        container.start()
        return container


class FakeImages:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client
        self.present: set[str] = set()

    def get(self, name: str):
        import docker.errors

        self._client.events.append(("image.get", name))
        if name not in self.present:
            raise docker.errors.ImageNotFound(f"no such image {name}")
        return object()


class FakeNetworks:
    def __init__(self, client: "FakeDockerClient") -> None:
        self._client = client
        self.present: set[str] = {"bridge"}

    def get(self, name: str):
        import docker.errors

        if name not in self.present:
            raise docker.errors.NotFound(f"no such network {name}")
        return object()

    def create(self, name: str, **kwargs):
        self.present.add(name)
        self._client.events.append(("network.create", name))
        return object()


class FakeDockerClient:
    def __init__(self, *, images: list[str] | None = None) -> None:
        self.events: list[tuple] = []
        self.volumes = FakeVolumes(self)
        self.containers = FakeContainers(self)
        self.images = FakeImages(self)
        self.networks = FakeNetworks(self)
        self.api = FakeApi(self)
        # STEP containers and the workspace CLONE HELPER are configured
        # separately: a test about a failing step must not accidentally break
        # the clone, and vice versa. Conflating them made every failure-path
        # test assert the wrong thing.
        self.helper_image = "python:3.12"
        self.next_exit_code = 0
        self.next_logs = b""
        self.wait_hook = None
        self.helper_exit_code = 0
        self.helper_logs = b""
        self.helper_wait_hook = None
        for image in images or []:
            self.images.present.add(image)

    def behavior_for(self, image: str):
        if image == self.helper_image:
            return self.helper_exit_code, self.helper_logs, self.helper_wait_hook
        return self.next_exit_code, self.next_logs, self.wait_hook

    def indices(self, event_name: str, image: str) -> list[int]:
        """Positions of `event_name` for `image` in the ordered event log."""
        return [
            index
            for index, (name, payload) in enumerate(self.events)
            if name == event_name and payload == image
        ]

    def ping(self) -> bool:
        self.events.append(("ping", None))
        return True

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]
