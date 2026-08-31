"""The 12.7 exit gate, end to end - T3 (R7).

The design named this file and it was not delivered, so the debug re-run loop
had no tier the ratchet gates: T3 stayed at 19 while the whole feature shipped
behind a Playwright spec (the R8 surface, which no tier runs). This is that
file, and it drives the loop the phase exists for, on the real stack:

    a run FAILS  ->  debug re-run with a breakpoint  ->  the CLI attaches a
    real terminal  ->  the developer fixes the workspace from the shell  ->
    @resume  ->  the pipeline COMPLETES

Nothing in the middle is a double. The pipeline runs on real containers, the
workspace is a real named volume, the sidecar is a real
`lazyaf-debug-sidecar:dev` container, the terminal is a REAL WebSocket to a
REAL uvicorn serving the real app, and the client on the other end of it is
the shipped CLI terminal client (`lazyaf.debug_cmd.run_terminal`) - not a
test harness that happens to speak the same JSON. That last point is the
reason this test earns its runtime: it is the only place where the CLI's
codec and the server's codec meet over a socket rather than in a contract
test.

The ONE stub is workspace population: the git clone needs the backend's git
server reachable from the container network, which is the `e2e-lane` skip
already baselined for the population suite. Everything the clone would have
produced, step 0 writes itself - and the assertion that matters is that the
SIDECAR and the RESUMED STEP see the same bytes.

`local_exec` is deliberate: it opts this test out of the root conftest's
Docker-free stubs, so the local execution path here is the production one.
T3 already carries the `build_images.py --check` preflight, so a missing
sidecar image is a loud preflight failure, never a skip (R4).
"""
import asyncio
import json
import socket as socket_mod
import sys
from pathlib import Path
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT / "backend"), str(REPO_ROOT / "cli")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.main import app
from app.models import Pipeline, PipelineRun, Repo
from app.models.pipeline import RunStatus
from app.routers.debug import get_debug_session_factory, read_join_token
from app.services.execution import debug_terminal as terminal
from app.services.execution.debug_session_service import debug_session_service
from app.services.execution.debug_state import DebugState
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

from lazyaf import debug_cmd

pytestmark = [pytest.mark.e2e, pytest.mark.local_exec]

STEP_IMAGE = "python:3.12-slim"
MARKER = "bytes-written-by-the-first-step"
FIX_FILE = "/workspace/repo/fix.txt"


# -----------------------------------------------------------------------------
# The pipeline: a step that fails for a reason the developer can fix from a
# shell. That is what makes "resume" a loop and not just an unpause.
# -----------------------------------------------------------------------------

#: A two-step LINEAR GRAPH. 12.8 retires the v1 array, so the breakpoints
#: below name their step by graph `step_id` ("build" / "verify") instead of by
#: position ("0" / "1"), and the ids are what the paused session reports.
STEPS_GRAPH = {
    "version": 2,
    "entry_points": ["build"],
    "steps": {
        "build": {
            "name": "build",
            "type": "script",
            "config": {
                "image": STEP_IMAGE,
                # 0777 on purpose and stated: the step containers run as root,
                # the sidecar execs as uid 1000 (it must, or every file it
                # creates is root-owned and the resumed step trips over it).
                # Without this the developer could read the workspace and not
                # write to it.
                "command": (
                    f"mkdir -p /workspace/repo && chmod 0777 /workspace/repo && "
                    f"echo {MARKER} > /workspace/repo/marker.txt && echo built"
                ),
            },
        },
        "verify": {
            "name": "verify",
            "type": "script",
            "config": {
                "image": STEP_IMAGE,
                "command": (
                    f"if [ -f {FIX_FILE} ]; then echo verify-passed; else "
                    f"echo 'missing {FIX_FILE}'; exit 1; fi"
                ),
            },
        },
    },
    "edges": [
        {
            "id": "edge_0_success",
            "from_step": "build",
            "to_step": "verify",
            "condition": "success",
        }
    ],
}


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def free_port() -> int:
    with socket_mod.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def docker_client():
    """Loud on a down daemon (R4) - never a skip."""
    client = docker_sdk.from_env(timeout=180)
    client.ping()
    return client


@pytest.fixture(autouse=True)
def images(docker_client):
    try:
        docker_client.images.get(STEP_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        docker_client.images.pull(STEP_IMAGE)
    sidecar = terminal.sidecar_image()
    try:
        docker_client.images.get(sidecar)
    except docker_sdk.errors.ImageNotFound as exc:
        raise AssertionError(
            f"the debug sidecar image {sidecar!r} is missing. It is a T3 "
            f"preflight requirement (scripts/run_tier.py runs "
            f"`build_images.py --check`). Build it with:\n"
            f"    python scripts/build_images.py"
        ) from exc
    return sidecar


@pytest_asyncio.fixture
async def stack(async_engine, monkeypatch, docker_client):
    """A REAL server for the terminal socket, bound to the TEST engine.

    HTTP goes through the ASGI client the rest of the e2e tier uses; the
    WebSocket cannot (an ASGI transport has no socket), so uvicorn serves the
    same `app` object in the same process on a loopback port. Both paths see
    one app, one set of dependency overrides and one database.

    `lifespan="off"`: the app's startup hook runs migrations and recovery
    sweeps against the CONFIGURED database. A test server must not touch it.
    """
    import uvicorn

    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    # The endpoint takes its session factory as a DEPENDENCY precisely so a
    # test can bind it to its own engine - the app's global factory points at
    # the configured database, where this run does not exist.
    app.dependency_overrides[get_debug_session_factory] = lambda: factory

    port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while not server.started:
        if serve_task.done() or loop.time() > deadline:
            serve_task.cancel()
            raise RuntimeError("the terminal test server never started")
        await asyncio.sleep(0.05)

    run_ids: list[str] = []
    state = type(
        "Stack",
        (),
        {
            "factory": factory,
            "ws_base": f"ws://127.0.0.1:{port}",
            "run_ids": run_ids,
            "docker": docker_client,
        },
    )()
    try:
        yield state
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=15)
        except asyncio.TimeoutError:
            serve_task.cancel()
            raise RuntimeError("the terminal test server did not shut down")
        app.dependency_overrides.pop(get_debug_session_factory, None)
        await debug_session_service.reset()
        await terminal.debug_terminal_service.reset()
        for run_id in run_ids:
            try:
                docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
            except docker_sdk.errors.NotFound:
                pass
        for container in docker_client.containers.list(
            all=True,
            filters={"label": f"{terminal.LABEL_TYPE}={terminal.LABEL_TYPE_VALUE}"},
        ):
            if (container.labels or {}).get(terminal.LABEL_RUN) in run_ids:
                container.remove(force=True)


@pytest_asyncio.fixture
async def repo_and_pipeline(async_engine):
    """A repo + a two-step pipeline, inserted directly.

    Ingestion has its own suites; what this test is the gate for starts at
    "a run failed", so the rows are seeded rather than driven through the
    ingest API.
    """
    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="debug-rerun-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="debug-rerun-pipeline",
            steps_graph=json.dumps(STEPS_GRAPH),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        return {"repo_id": repo.id, "pipeline_id": pipeline.id}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def wait_until(predicate, timeout=120.0, message="condition never held"):
    """Poll a coroutine predicate; a blown deadline is a LOUD failure naming
    the last value seen - never a silent pass."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(f"{message} (last value: {last!r})")


async def run_status(api_client, run_id: str) -> dict:
    response = await api_client.get(f"/api/pipeline-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for_run(api_client, run_id: str, *statuses) -> dict:
    async def _done():
        run = await run_status(api_client, run_id)
        return run if run["status"] in statuses else None

    return await wait_until(
        _done, message=f"run {run_id[:8]} never reached {statuses}"
    )


class DrivenConsole:
    """A console the TEST types into, handed to the real terminal client.

    Replaces exactly one thing - the keyboard and the screen - so everything
    between it and the container (the escape decoder, the frame codec, the
    websocket, the endpoint, the exec'd shell) is production code.
    """

    def __init__(self, size=(100, 30)):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._size = size
        self.output = bytearray()
        self._wakeup = asyncio.Event()

    # -- the terminal client's surface --------------------------------------

    async def next_input(self):
        return await self._queue.get()

    def write_output(self, data: bytes) -> None:
        self.output += data
        self._wakeup.set()

    def size(self):
        return self._size

    # -- the test's surface --------------------------------------------------

    def type(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    async def wait_for_output(self, needle: bytes, timeout: float = 60.0) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        while needle not in self.output:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"{needle!r} never appeared on the terminal: "
                    f"{bytes(self.output)!r}"
                )
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
        return bytes(self.output)


# -----------------------------------------------------------------------------
# The exit gate
# -----------------------------------------------------------------------------


class TestDebugRerunLoop:
    async def test_failed_run_debugged_fixed_from_the_shell_and_completed(
        self, api_client, stack, repo_and_pipeline
    ):
        pipeline_id = repo_and_pipeline["pipeline_id"]

        # --- 1. a run fails ------------------------------------------------
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        assert response.status_code == 200, response.text
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)

        failed = await wait_for_run(api_client, original_id, RunStatus.FAILED.value)
        # Keyed by graph step ID, which is what a step IS after 12.8 - and
        # what the breakpoint below names.
        steps = {s["step_id"]: s for s in failed["step_runs"]}
        assert steps["build"]["status"] == RunStatus.PASSED.value
        assert steps["verify"]["status"] == RunStatus.FAILED.value
        assert FIX_FILE in (steps["verify"]["logs"] or ""), (
            "the failing step must say what it could not find - that sentence "
            "is what a developer debugs from"
        )

        # --- 2. debug re-run, breakpointed before the failing step ---------
        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["verify"], "use_original_commit": True},
        )
        assert response.status_code == 200, response.text
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)
        assert rerun["join_command"] == f"lazyaf debug attach {session_id}"

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        info = await wait_until(_paused, message="the gate never paused")
        assert info["current_step"]["key"] == "verify"
        assert info["current_step"]["name"] == "verify"
        assert info["attach_available"] is True
        assert "token" not in info, (
            "C14: GET /api/debug/{id} must never carry the session's "
            "credential - a secret in a polled response is not a secret"
        )

        # --- 3. mint a credential and ATTACH a real terminal ---------------
        response = await api_client.post(f"/api/debug/{session_id}/join-token")
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        assert read_join_token(token) == session_id

        console = DrivenConsole()
        notices: list[str] = []
        url = debug_cmd.terminal_url(stack.ws_base, session_id)
        attach = asyncio.create_task(
            debug_cmd.attach_socket(url, token, console, notice=notices.append)
        )
        try:
            # The sidecar mounts the paused run's workspace: step 0's bytes.
            console.type(b"cat /workspace/repo/marker.txt\n")
            await console.wait_for_output(MARKER.encode())

            # --- 4. fix it from the shell ------------------------------
            console.type(f"echo fixed > {FIX_FILE} && echo wrote-the-fix\n".encode())
            await console.wait_for_output(b"wrote-the-fix")
            console.type(b"ls /workspace/repo && echo listed\n")
            listing = await console.wait_for_output(b"listed")
            assert b"fix.txt" in listing

            # --- 5. @resume, over the wire, from the CLI's escape key ---
            console.type(b"\x1dr")
            result = await asyncio.wait_for(attach, timeout=60)
        finally:
            attach.cancel()

        assert result.commands == ["@resume"], (
            "Ctrl-] r must reach the server as a `command` frame - never as "
            "the bytes '@resume' sniffed out of stdin (C12)"
        )
        assert result.reason == "resumed"
        assert result.exit_code == 0
        assert any("READ-WRITE" in n for n in notices), (
            "the server's banner must reach the operator: /workspace is "
            "read-write and the resumed step sees the edits"
        )

        # --- 6. the pipeline completes ------------------------------------
        completed = await wait_for_run(api_client, run_id, RunStatus.PASSED.value)
        steps = {s["step_id"]: s for s in completed["step_runs"]}
        assert steps["verify"]["status"] == RunStatus.PASSED.value
        assert "verify-passed" in (steps["verify"]["logs"] or ""), (
            "THE loop: the step that failed on the first run passed on the "
            "re-run because of a file created from the debug shell"
        )
        assert "[debug] paused before step" in (steps["verify"]["logs"] or "")

        # The session ends with the run, saying why (R1), and everything it
        # created goes with it (C9: sidecar before volume).
        #
        # POLLED, not read once: "the run reached PASSED" and "the session
        # reached ENDED" are two commits inside `_complete_pipeline`, and the
        # API can answer the first before the second lands. Asserting
        # instantly here is a real race - it cost this file one red run in ten
        # before the wait went in.
        async def _ended():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.ENDED.value else None

        session = await wait_until(
            _ended, timeout=60, message="the session outlived its run"
        )
        assert session["end_reason"]
        assert session["attach_available"] is False
        assert session["attach_unavailable_reason"]

        async def _torn_down():
            containers = stack.docker.containers.list(
                all=True, filters={"label": f"{terminal.LABEL_RUN}={run_id}"}
            )
            if containers:
                return None
            try:
                stack.docker.volumes.get(generate_volume_name(run_id))
            except docker_sdk.errors.NotFound:
                return True
            return None

        await wait_until(
            _torn_down,
            timeout=60,
            message="the sidecar and the workspace volume outlived the session",
        )

    async def test_a_terminal_without_a_credential_is_refused_at_the_upgrade(
        self, api_client, stack, repo_and_pipeline
    ):
        """C14 over a real socket - and the fact the contract got wrong.

        `routers/debug.py` refuses BEFORE `accept()` and writes its reason
        into the close frame, on the stated assumption that "the reason
        travels in the close REASON". Against a real ASGI server it does not:
        Starlette turns a pre-accept `close()` into a plain **HTTP 403**
        during the handshake, and the sentence never leaves the process.
        Only a real socket could show that, which is exactly why this test is
        in a tier and not in a fixture.

        So the refusal is still LOUD and still terminal - it is simply not
        self-describing, and the CLI says so rather than inventing a reason
        (R1). The backend fix is in this agent's report as a requested edit.
        """
        pipeline_id = repo_and_pipeline["pipeline_id"]
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)
        await wait_for_run(api_client, original_id, RunStatus.FAILED.value)

        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["verify"], "use_original_commit": True},
        )
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        await wait_until(_paused, message="the gate never paused")

        url = debug_cmd.terminal_url(stack.ws_base, session_id)
        result = await asyncio.wait_for(
            debug_cmd.attach_socket(
                url, "not-a-real-token", DrivenConsole(), session_id=session_id
            ),
            timeout=30,
        )

        assert result.exit_code == 1, "a refused attach must not read as success"
        assert "403" in result.reason, (
            "the CLI must name the status it actually got, not a close code "
            "the handshake never carried"
        )
        assert f"lazyaf debug status {session_id}" in result.reason, (
            "R1: when the reason is undeliverable, the CLI must say where it "
            "CAN be read rather than guessing"
        )
        # No sidecar was created for a refused upgrade.
        assert stack.docker.containers.list(
            all=True, filters={"label": f"{terminal.LABEL_RUN}={run_id}"}
        ) == []

        # Leave the run terminal rather than a paused gate holding a volume.
        assert (
            await api_client.post(f"/api/debug/{session_id}/abort")
        ).status_code == 200
        await wait_for_run(api_client, run_id, RunStatus.CANCELLED.value)


class TestDebugRerunIsNotTheOriginalRun:
    async def test_the_rerun_carries_only_branch_and_commit(
        self, api_client, stack, repo_and_pipeline, async_engine
    ):
        """C10 at the API surface: a debug re-run can never merge a branch and
        never moves a card, because the context it carries has nothing to act
        on. Asserted on the persisted row, not on a call list."""
        pipeline_id = repo_and_pipeline["pipeline_id"]
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)
        await wait_for_run(api_client, original_id, RunStatus.FAILED.value)

        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["build"], "use_original_commit": True},
        )
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        await wait_until(_paused, message="the gate never paused")

        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as db:
            run = (
                await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one()
        context = json.loads(run.trigger_context or "{}")
        assert set(context) <= {"branch", "commit_sha"}, (
            f"a debug re-run's trigger_context must carry branch and commit "
            f"and nothing else (C10); got {sorted(context)}"
        )
        assert run.trigger_ref == original_id

        # Resume past the single breakpoint so the run reaches a terminal
        # state on its own rather than being torn down mid-pause.
        assert (
            await api_client.post(
                f"/api/debug/{session_id}/resume", json={"clear_remaining": True}
            )
        ).status_code == 200
        await wait_for_run(api_client, run_id, RunStatus.FAILED.value)
