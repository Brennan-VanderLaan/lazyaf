"""
Shared fixtures + DooD-safe addressing helpers for the integration tier.

THE docker_client fixture for the Docker-real T2 subtree
(tdd/integration/services) lives here - one definition, ping-on-create, so
Docker being down fails LOUDLY in every suite (R4: never a silent skip).
The ten per-file copies were migrated here; the copies that had drifted
into ping-less `docker.from_env()` (a down daemon then surfaced as a
confusing mid-test error) now share the loud version.

DooD-safe addressing (the T2-breaking finding): T2 runs inside a runner
container with the HOST's /var/run/docker.sock mounted, so containers a
test launches are SIBLINGS of the test process's own container - not
children. A test that binds a server (uvicorn, stub backend) on
0.0.0.0:<free_port> must advertise an address the sibling can reach:

- inside a container (the CI path): this container's own IP on the shared
  bridge network (lazyaf-network) - `host.docker.internal` points at the
  HOST there and misses a server bound inside the runner container;
- on the host (the dev path): `host.docker.internal`, which Docker Desktop
  resolves from user-defined networks. A Linux-Engine host may need
  `--add-host host.docker.internal:host-gateway` on the launched container;
  the container path above is the CI path and needs no such mapping.

Helpers are plain module functions - import them as
`from tdd.integration.conftest import advertise_addr, free_port, ...`.
"""
import asyncio
import os
import socket
import time
from pathlib import Path
from uuid import uuid4

import docker as docker_sdk
import pytest


# -----------------------------------------------------------------------------
# DooD-safe addressing helpers
# -----------------------------------------------------------------------------

def running_in_container() -> bool:
    """True when the test process itself runs inside a container (the DooD
    runner / step-container path). /.dockerenv is the daemon's marker;
    LAZYAF_CONTROL is set on every lazyaf step container."""
    return Path("/.dockerenv").exists() or bool(os.environ.get("LAZYAF_CONTROL"))


def free_port() -> int:
    """An OS-assigned free TCP port (bound briefly on all interfaces)."""
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def advertise_addr() -> str:
    """The address a SIBLING container can use to reach a server bound on
    0.0.0.0 in THIS process. See the module docstring for the two paths."""
    if running_in_container():
        # Our own IP on the container network the daemon attached us to
        # (lazyaf-network in the compose stack) - reachable by siblings.
        return socket.gethostbyname(socket.gethostname())
    return "host.docker.internal"


def sibling_network(docker_client):
    """The docker network a SIBLING must join to reach `advertise_addr()`.

    None on the host: there `advertise_addr()` is host.docker.internal, which
    Docker Desktop resolves from any network, the default `bridge` included.

    Inside a container it is the network that carries our advertised IP - and
    it matters as much as the address does. Docker isolates bridge networks
    from each other, so a sibling started on the default `bridge` cannot reach
    an IP that lives on a user-defined network (lazyaf-network in the compose
    stack). The runner agent's DEFAULT_STEP_NETWORK is exactly `bridge`
    (runner-agent/lazyaf_runner/config.py:58), which is why the loopback lane's
    three step-reports-home tests passed on every host run and failed in the
    dogfood container, each one waiting out its whole budget until tier2 hit
    its 900 s step timeout (dogfood run 5931f877, 2026-09-17).

    Derived from the daemon rather than hardcoded, and it RAISES when it cannot
    tell: quietly answering `bridge` is the bug this exists to remove (R1).
    """
    if not running_in_container():
        return None
    ours = advertise_addr()
    try:
        me = docker_client.containers.get(socket.gethostname())
        networks = me.attrs["NetworkSettings"]["Networks"]
    except Exception as exc:  # noqa: BLE001 - reported with the remedy below
        raise RuntimeError(
            "running inside a container but could not inspect it to find the "
            f"network siblings must join (hostname {socket.gethostname()!r}): "
            f"{exc}. The tier needs the docker socket (`needs: [docker]`) and a "
            "hostname equal to the container id, which is docker's default."
        ) from exc
    for name, spec in networks.items():
        if spec.get("IPAddress") == ours:
            return name
    raise RuntimeError(
        f"this container advertises {ours} but none of its networks carries "
        f"that address: { {n: v.get('IPAddress') for n, v in networks.items()} }"
    )


def wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> None:
    """Readiness poll: block until a TCP connect to host:port succeeds,
    or raise loudly after the deadline."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError as e:
            last_error = e
            time.sleep(0.05)
    raise RuntimeError(
        f"server on {host}:{port} not ready after {timeout}s: {last_error!r}"
    )


async def start_uvicorn(server) -> "asyncio.Task":
    """Serve a configured uvicorn.Server on the CURRENT loop.

    Returns the serve task once startup completed; raises if the server
    died or did not come up within 10s (readiness poll on server.started,
    which flips only after the listener is bound)."""
    task = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not server.started:
        if task.done() or loop.time() > deadline:
            task.cancel()
            raise RuntimeError("uvicorn test server failed to start")
        await asyncio.sleep(0.05)
    return task


async def stop_uvicorn(server, task, timeout: float = 10.0) -> None:
    """Shutdown-with-deadline: signal exit and await the serve task; a hung
    shutdown is cancelled and raised rather than wedging the whole tier."""
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"uvicorn test server did not shut down within {timeout}s"
        )


def stop_http_server(server, thread, timeout: float = 10.0) -> None:
    """Shutdown-with-deadline for a threaded http.server: shutdown + close,
    then join the serve thread; a thread still alive after the deadline is
    raised loudly (a leaked serve loop, never silently abandoned)."""
    server.shutdown()
    server.server_close()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise RuntimeError(
            f"stub HTTP server thread still alive {timeout}s after shutdown"
        )


# -----------------------------------------------------------------------------
# Shared Docker fixtures (T2 subtree: tdd/integration/services)
# -----------------------------------------------------------------------------

@pytest.fixture(scope="session")
def docker_client():
    """THE real Docker client for the Docker tier.

    from_env + ping: Docker being down fails LOUDLY here (R4) - never a
    skip, and never a ping-less client that half-works until first use.

    timeout=180: the default 60s HTTP read timeout underruns
    container.wait(timeout=90) under nested-DooD daemon load (dogfood run
    #9: ReadTimeoutError inside the T2 step container while the daemon
    was busy) - the client timeout must exceed the longest wait budget."""
    client = docker_sdk.from_env(timeout=180)
    client.ping()  # Fail loudly here if Docker is down (R4)
    return client


@pytest.fixture
def named_volume_factory(docker_client):
    """Factory for REAL named volumes (R6): fresh and root-owned exactly
    like production workspace volumes; all force-removed on teardown."""
    created: list[str] = []

    def make(prefix: str = "lazyaf-test") -> str:
        name = f"{prefix}-{uuid4().hex[:8]}"
        docker_client.volumes.create(name)
        created.append(name)
        return name

    yield make
    for name in created:
        try:
            docker_client.volumes.get(name).remove(force=True)
        except docker_sdk.errors.NotFound:
            pass


@pytest.fixture
def named_volume(named_volume_factory):
    """One fresh REAL named volume (R6), removed after the test."""
    return named_volume_factory("lazyaf-test-img")
