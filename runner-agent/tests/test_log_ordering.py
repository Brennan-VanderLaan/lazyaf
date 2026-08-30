"""Runner-origin lines never interleave with container logs - section 7.2.

Test contract item 6 (section 8, Agent D). Two streams now append to the same
``StepRun.logs``: the step container's HTTP POSTs to ``/api/steps/{id}/logs``
and this agent's WS ``log`` frames. Interleaved, they produce a log that reads
as though events happened out of order, and no amount of timestamping fixes
that across two hosts and two transports.

The mitigation is STRUCTURAL rather than best-effort: the agent emits
runner-origin lines only BEFORE ``container.start()`` and AFTER the container
exits. The two streams then cannot overlap in time, so append order is real
order. This file asserts exactly that, against a fake daemon that records every
call in the same ordered list the log sink writes to.
"""
from __future__ import annotations

import asyncio

import pytest

from lazyaf_runner.orchestrator.docker_orch import (
    EXIT_AGENT_ERROR,
    EXIT_CANCELLED,
    EXIT_TIMEOUT,
    DockerOrchestrator,
)
from lazyaf_runner.workspace import DockerWorkspaceProvisioner

from conftest import FakeDockerClient, make_config, make_step_config
from lazyaf_runner.types import StepAssignment

STEP_IMAGE = "lazyaf-base:dev"
CLONE_IMAGE = "python:3.12"


def build_orchestrator(client: FakeDockerClient, **config_kwargs) -> DockerOrchestrator:
    config = make_config(orchestrator="docker", **config_kwargs)
    provisioner = DockerWorkspaceProvisioner(client, network=config.step_network)
    return DockerOrchestrator(config, client=client, provisioner=provisioner)


def step_window(client: FakeDockerClient, image: str = STEP_IMAGE) -> tuple[int, int]:
    """(start index, exit index) of the STEP container in the daemon event log.

    Scoped to the step image on purpose: the workspace clone helper is also a
    container, and using the first `container.start` would measure the clone's
    window instead of the step's.
    """
    starts = client.indices("container.start", image)
    waits = client.indices("container.wait", image)
    assert starts, f"the {image} container never started"
    assert waits, f"the {image} container was never waited on"
    return starts[-1], waits[-1]


def assignment(**overrides) -> StepAssignment:
    return StepAssignment(
        step_id="s1", execution_key="k1", config=make_step_config(**overrides)
    )


async def run(orch, assign, events, *, cancel=None):
    """Run a step, recording log emissions into the daemon's own event list."""
    def on_log(lines):
        events.append(("log", list(lines)))

    return await orch.run_step(assign, on_log=on_log, cancel=cancel or asyncio.Event())


# ---------------------------------------------------------------------------

async def test_no_runner_lines_between_container_start_and_exit() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)

    outcome = await run(orch, assignment(), client.events)
    assert outcome.exit_code == 0

    start, exit_index = step_window(client)
    window = [name for name, _ in client.events[start:exit_index + 1]]

    assert "log" not in window, (
        "a runner-origin line was emitted between container start and exit; "
        f"window={window}"
    )


async def test_runner_lines_exist_on_both_sides_of_the_window() -> None:
    """Guards against the above passing vacuously by emitting nothing at all."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)

    await run(orch, assignment(), client.events)

    names = [name for name, _ in client.events]
    start, exit_index = step_window(client)
    assert "log" in names[:start], "nothing was reported before the container started"
    assert "log" in names[exit_index:], "nothing was reported after the container exited"


async def test_first_line_names_all_three_resolved_urls() -> None:
    """One grep answers 'why can't the step reach the backend' (section 3.4)."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(
        client,
        step_backend_url="http://10.0.0.5:8000",
        git_url_template="http://10.0.0.5:8000/git/{repo_id}.git",
    )
    events: list = []
    await run(orch, assignment(), events)

    first = events[0][1][0]
    assert "backend_url=http://10.0.0.5:8000" in first
    assert "clone_url=http://10.0.0.5:8000/git/r123.git" in first
    assert "volume=lazyaf-ws-run1" in first
    assert f"image={STEP_IMAGE}" in first


async def test_step_clock_starts_at_container_start_not_at_run_step() -> None:
    """Pull and clone time on a cold remote host must not eat the step's own
    timeout budget (section 7.1) - the deadline is set after start()."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)

    slow = 0.15
    original_populate = DockerWorkspaceProvisioner.populate

    def slow_populate(self, *args, **kwargs):
        import time

        time.sleep(slow)
        return original_populate(self, *args, **kwargs)

    DockerWorkspaceProvisioner.populate = slow_populate  # type: ignore[method-assign]
    try:
        # A timeout SHORTER than the provisioning time: if the clock started at
        # run_step() the step would time out before the container ever ran.
        outcome = await run(
            orch, assignment(container={"timeout": 1}), client.events
        )
    finally:
        DockerWorkspaceProvisioner.populate = original_populate  # type: ignore[method-assign]

    assert outcome.exit_code == 0, "provisioning time was charged to the step timeout"


# ---------------------------------------------------------------------------
# Control-mode sequence
# ---------------------------------------------------------------------------

async def test_control_mode_sequence_is_create_put_archive_start() -> None:
    """Mounts are bound at create, so the volume is addressable before the
    runtime boots and the step JWT never enters `docker inspect` env."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    await run(orch, assignment(), client.events)

    names = [name for name, _ in client.events]
    create = client.indices("container.create", STEP_IMAGE)[-1]
    archive = names.index("container.put_archive")
    start = client.indices("container.start", STEP_IMAGE)[-1]
    assert create < archive < start

    container = client.containers.created[-1]
    assert container.archives[0][0] == "/workspace"
    assert container.archives[0][1].startswith(b".control")


async def test_container_never_uses_host_networking() -> None:
    """failure_01 gave every step the host's network namespace on a machine the
    backend does not own. This attaches to a CONFIGURED network instead."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client, step_network="lazyaf-network")
    await run(orch, assignment(), client.events)

    container = client.containers.created[-1]
    assert container.kwargs["network"] == "lazyaf-network"
    assert container.kwargs.get("network_mode") is None


async def test_step_backend_url_override_reaches_container_env() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client, step_backend_url="http://10.0.0.5:8000")
    await run(orch, assignment(), client.events)

    env = client.containers.created[-1].kwargs["environment"]
    assert env["LAZYAF_BACKEND_URL"] == "http://10.0.0.5:8000"
    # Everything else the backend sent is preserved verbatim.
    assert env["LAZYAF_CONTROL"] == "1"
    assert env["CONFIG_PATH"] == "/workspace/.control/se1.json"


async def test_container_is_always_removed() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    await run(orch, assignment(), client.events)
    assert client.containers.created[-1].removed


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

async def test_missing_image_produces_the_local_paths_message() -> None:
    """Identical wording to LocalExecutor's ImageNotFound handler: an operator
    should not need a second vocabulary for the same fault on another host."""
    client = FakeDockerClient(images=[CLONE_IMAGE])
    orch = build_orchestrator(client)

    outcome = await run(orch, assignment(), client.events)
    assert outcome.exit_code == EXIT_AGENT_ERROR
    assert outcome.error == f"Image not found: {STEP_IMAGE}"
    assert client.indices("container.create", STEP_IMAGE) == [], (
        "no step container may be created for an image the host does not have"
    )


async def test_non_allowlisted_bind_mount_is_refused() -> None:
    """A backend must not be able to bind arbitrary host paths on a machine it
    does not own - 'the backend asked for it' is not authorization."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    assign = assignment(
        container={
            "mounts": [
                {"addressing": "bind", "source": "/etc", "target": "/host-etc", "mode": "ro"}
            ]
        }
    )

    outcome = await run(orch, assign, client.events)
    assert outcome.exit_code == EXIT_AGENT_ERROR
    assert "not on this runner's allowlist" in outcome.error
    assert "LAZYAF_BIND_ALLOWLIST" in outcome.error
    assert client.containers.created == [], "no container may be created for a refused mount"


async def test_allowlisted_bind_mount_is_honored() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client, bind_allowlist=("/var/run/docker.sock",))
    assign = assignment(
        container={
            "mounts": [
                {
                    "addressing": "bind",
                    "source": "/var/run/docker.sock",
                    "target": "/var/run/docker.sock",
                    "mode": "rw",
                }
            ]
        }
    )
    outcome = await run(orch, assign, client.events)
    assert outcome.exit_code == 0
    assert client.containers.created[-1].kwargs["mounts"]


async def test_mount_without_explicit_addressing_is_refused() -> None:
    """R6: volume vs bind is DECLARED, never inferred from path shape."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    assign = assignment(
        container={"mounts": [{"source": "data", "target": "/data"}]}
    )
    outcome = await run(orch, assign, client.events)
    assert outcome.exit_code == EXIT_AGENT_ERROR
    assert "addressing" in outcome.error


async def test_nonzero_exit_is_a_step_failure_not_an_agent_error() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    client.next_exit_code = 7
    orch = build_orchestrator(client)

    outcome = await run(orch, assignment(), client.events)
    assert outcome.exit_code == 7
    assert outcome.error is None, "the backend decides what a non-zero code means"


async def test_cancel_kills_the_container() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    cancel = asyncio.Event()

    def block_until_cancelled(container):
        # Model a container that outlives one wait slice.
        if not cancel.is_set():
            raise TimeoutError("read timed out")

    client.wait_hook = block_until_cancelled
    orch = build_orchestrator(client)

    events: list = []
    task = asyncio.create_task(run(orch, assignment(), events, cancel=cancel))
    await asyncio.sleep(0.05)
    cancel.set()
    outcome = await asyncio.wait_for(task, timeout=5)

    assert outcome.exit_code == EXIT_CANCELLED
    assert client.containers.created[-1].killed


async def test_timeout_kills_the_container_and_names_the_budget() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])

    def never_finishes(container):
        raise TimeoutError("read timed out")

    client.wait_hook = never_finishes
    orch = build_orchestrator(client)

    outcome = await run(orch, assignment(container={"timeout": 0}), client.events)
    assert outcome.exit_code == EXIT_TIMEOUT
    assert "timeout" in outcome.error


async def test_workspace_failure_fails_the_step_with_the_helper_log_tail() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    client.helper_exit_code = 128
    client.helper_logs = b"fatal: repository not found"
    orch = build_orchestrator(client)

    outcome = await run(orch, assignment(), client.events)
    assert outcome.exit_code == EXIT_AGENT_ERROR
    assert "repository not found" in outcome.error
    assert client.indices("container.create", STEP_IMAGE) == [], (
        "the step container must not be created when the workspace is unusable"
    )


async def test_assignment_without_a_volume_is_refused() -> None:
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    assign = assignment(workspace={"volume": None})

    outcome = await run(orch, assign, client.events)
    assert outcome.exit_code == EXIT_AGENT_ERROR
    assert "workspace.volume" in outcome.error


# ---------------------------------------------------------------------------
# Preflight / capabilities
# ---------------------------------------------------------------------------

async def test_preflight_reports_stale_images_as_a_label_not_a_refusal() -> None:
    """A merely-stale host can still run steps whose images it has; reporting
    it as a LABEL puts it in the runner list instead of in a step failure ten
    minutes later."""
    client = FakeDockerClient(images=[CLONE_IMAGE])
    config = make_config(
        orchestrator="docker", expect_images=("lazyaf-base:dev", "lazyaf-test-runner:dev")
    )
    orch = DockerOrchestrator(config, client=client)
    await orch.preflight()

    caps = orch.capabilities()
    assert caps["orchestrator"] == "docker"
    assert "docker" in caps["has"]
    assert "images:stale" in caps["has"]


async def test_preflight_on_a_healthy_host_advertises_only_docker() -> None:
    client = FakeDockerClient(images=["lazyaf-base:dev"])
    config = make_config(orchestrator="docker", expect_images=("lazyaf-base:dev",))
    orch = DockerOrchestrator(config, client=client)
    await orch.preflight()
    assert orch.capabilities() == {"orchestrator": "docker", "has": ["docker"]}


async def test_preflight_failure_is_actionable() -> None:
    from lazyaf_runner.orchestrator.base import OrchestratorUnavailable

    class DeadClient(FakeDockerClient):
        def ping(self):
            raise RuntimeError("connection refused")

    orch = DockerOrchestrator(make_config(orchestrator="docker"), client=DeadClient())
    with pytest.raises(OrchestratorUnavailable) as excinfo:
        await orch.preflight()
    message = str(excinfo.value)
    assert "docker.sock" in message and "daemon" in message
