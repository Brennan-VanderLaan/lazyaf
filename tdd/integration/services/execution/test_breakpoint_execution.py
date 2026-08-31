"""Breakpoint execution against REAL Docker - Phase 12.7's T2 gate (R7).

12.7's own design named this file and did not deliver it, so the phase shipped
with zero Docker-real coverage and the ratchet did not grow: T2 stayed at 70
while a whole subsystem - a gate that pins a workspace, a sidecar container
that mounts it, a terminal that execs into it - landed with only unit fakes
underneath. This is that file, written against the running stack.

What is REAL here, and it is nearly everything:

- the `PipelineExecutor`, its `ExecutionRouter`, and its `LocalExecutor`
  spawning actual step containers;
- the `WorkspaceService` on an actual NAMED docker volume (R6 - never a
  `tmp_path` bind: a bind mount would not exercise the refcount, the
  CLEANING refusal, or the volume-removal ordering this file asserts);
- the `DebugSessionService` gate holding a real step task at a breakpoint,
  against a file-backed SQLite engine;
- the `DebugTerminalService` spawning an actual `lazyaf-debug-sidecar:dev`
  container, exec-ing a real login shell into it, and pumping real bytes
  through the real frame codec;
- the real `ConnectionManager` with a capturing transport (never an
  AsyncMock on a broadcast path).

The ONE stub is workspace population - the git clone needs a running backend
git server, and it has its own integration tests. Everything the clone would
have produced, step 0 writes itself; the assertion that matters is that the
SIDECAR sees those bytes, which proves it mounted the same named volume the
steps ran against.

Docker down fails LOUDLY in the shared `docker_client` fixture (R4), and the
step/sidecar images are a run_tier.py preflight - never a skip.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

backend_path = Path(__file__).resolve().parents[4] / "backend"
tdd_path = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories.pipelines import make_repo_and_graph_pipeline  # noqa: E402

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.debug import DebugSession
from app.models.pipeline import RunStatus, StepExecution
from app.models.workspace import Workspace
from app.services.execution import debug_terminal as terminal
from app.services.execution.debug_session_service import (
    TRIGGER_TYPE_DEBUG_RERUN,
    debug_session_service,
)
from app.services.execution.debug_state import DebugState
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.state_machine import WorkspaceStatus, generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration]

STEP_IMAGE = "python:3.12-slim"
MARKER = "workspace-bytes-the-sidecar-can-see"

# How long a step may sit paused before the gate times it out on its own.
# Well above every wait in this file: a timeout here would be the gate doing
# its job, not the test's exit path.
PAUSE_TIMEOUT_SECONDS = 300


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

# docker_client comes from the shared tdd/integration/conftest.py (from_env +
# ping: Docker down fails loudly there, R4).


@pytest.fixture(scope="module", autouse=True)
def images(docker_client):
    """Both images this suite needs, present before anything runs.

    The step image is pulled on demand; the SIDECAR is built from this repo
    (`python scripts/build_images.py`) and a missing one is a loud failure
    naming that command - never a skip, and never a test that quietly proves
    less than it claims.
    """
    try:
        docker_client.images.get(STEP_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        docker_client.images.pull(STEP_IMAGE)
    sidecar = terminal.sidecar_image()
    try:
        docker_client.images.get(sidecar)
    except docker_sdk.errors.ImageNotFound as exc:
        raise AssertionError(
            f"the debug sidecar image {sidecar!r} is missing. It is a preflight "
            f"requirement of T2 and T3 (scripts/run_tier.py runs "
            f"`build_images.py --check`). Build it with:\n"
            f"    python scripts/build_images.py"
        ) from exc
    return sidecar


class CapturingSocket:
    """Capturing transport for the REAL ConnectionManager (R6)."""

    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch, docker_client, images):
    """Real-seam environment: file DB, fresh executor, stubbed population."""
    db_path = (tmp_path / "breakpoints.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    executor = PipelineExecutor()
    # The service drives the executor UNDER TEST, not the process-wide
    # singleton - otherwise `create()` would start the re-run on an executor
    # bound to a different engine.
    debug_session_service._executor = executor

    socket = CapturingSocket()
    manager.active_connections.append(socket)

    run_ids: list[str] = []
    state = SimpleNamespace(
        factory=factory,
        executor=executor,
        socket=socket,
        run_ids=run_ids,
        docker=docker_client,
        sidecar_image=images,
    )
    try:
        yield state
    finally:
        if socket in manager.active_connections:
            manager.active_connections.remove(socket)
        await debug_session_service.reset()
        await terminal.debug_terminal_service.reset()
        await executor.reset()
        debug_session_service._executor = None
        # Defensive: nothing this suite created may outlive it.
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
        await engine.dispose()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def script_step(name: str, command: str) -> dict:
    return {
        "name": name,
        "type": "script",
        "config": {"command": command, "image": STEP_IMAGE},
    }


async def make_repo_and_pipeline(factory, steps: list[dict]):
    """A repo and a pipeline whose definition is the LINEAR GRAPH `steps` describes.

    12.8: the argument shape is unchanged - the same `list[dict]` every call
    site below already passes - and so is the persisted node ORDER, whose ids
    are `step_0..step_N`. What changed is the column: `steps_graph`, not
    `steps`.

    This was one of seven byte-identical copies. It is now one line onto
    `tdd/shared/factories/pipelines`, so the next change to how a test
    pipeline is persisted happens once (R3).
    """
    return await make_repo_and_graph_pipeline(
        factory, steps, name="breakpoint-pipeline", repo_name="breakpoint-repo",
    )


async def start_and_wait(env, pipeline, repo, **kwargs) -> PipelineRun:
    async with env.factory() as db:
        run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo, **kwargs)
        run_id = run.id
    env.run_ids.append(run_id)
    await env.executor.wait_for_run(run_id)
    return await fetch_run(env, run_id)


async def fetch_run(env, run_id: str) -> PipelineRun:
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


async def fetch_session(env, session_id: str) -> DebugSession:
    async with env.factory() as db:
        return await debug_session_service.get(db, session_id)


async def fetch_workspace(env, run_id: str) -> "Workspace | None":
    async with env.factory() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.pipeline_run_id == run_id)
        )
        return result.scalar_one_or_none()


def volume_exists(docker_client, run_id: str) -> bool:
    try:
        docker_client.volumes.get(generate_volume_name(run_id))
        return True
    except docker_sdk.errors.NotFound:
        return False


async def wait_until(predicate, timeout=90.0, message="condition never held"):
    """Poll a coroutine predicate. A blown deadline is a LOUD failure that
    prints the last value - never a silent pass."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"{message} (last value: {last!r})")


async def wait_for_pause(env, session_id: str, key: str) -> DebugSession:
    async def _paused():
        session = await fetch_session(env, session_id)
        if session is not None and session.current_step_key == key and session.status in (
            DebugState.WAITING_AT_BP.value,
            DebugState.CONNECTED.value,
        ):
            return session
        return None

    return await wait_until(
        _paused, message=f"the gate never paused at breakpoint {key!r}"
    )


async def start_debug_rerun(env, original_run, pipeline, repo, breakpoints):
    async with env.factory() as db:
        original = await db.get(PipelineRun, original_run.id)
        session, run = await debug_session_service.create(
            db,
            original_run=original,
            pipeline=pipeline,
            repo=repo,
            breakpoints=breakpoints,
            timeout_seconds=PAUSE_TIMEOUT_SECONDS,
        )
        env.run_ids.append(run.id)
        return session.id, run.id


async def resume(env, session_id: str, clear_remaining: bool = False):
    async with env.factory() as db:
        return await debug_session_service.resume(
            db, session_id, clear_remaining=clear_remaining
        )


async def sidecar_shell(env, session_id: str, run_id: str) -> tuple:
    """Spawn the session's sidecar and attach a REAL login shell to it.

    Exactly what `routers/debug.py` does on a terminal upgrade: ensure the
    container, exec into it, and record the container on the session row so
    teardown (C9) can find it. Returns (container_id, stream).
    """
    session = await fetch_session(env, session_id)
    container_id = await terminal.debug_terminal_service.ensure_sidecar(
        session_id, run_id, session.sidecar_container_id
    )
    stream = await terminal.debug_terminal_service.attach(session_id, container_id)
    async with env.factory() as db:
        await debug_session_service.mark_connected(
            db, session_id, container_id, terminal.CONNECTION_MODE_SIDECAR
        )
    return container_id, stream


async def shell_read_until(stream, needle: bytes, timeout: float = 30.0) -> bytes:
    """Drain the sidecar's real output until `needle` appears.

    The bytes travel the production path: a blocking `recv` on the exec socket
    in a daemon thread, handed to the loop through `call_soon_threadsafe`
    (R5), and read here off the same `asyncio.Queue` the WS endpoint reads.
    """
    seen = bytearray()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        kind, payload = await asyncio.wait_for(stream.queue.get(), timeout=remaining)
        if kind == "data":
            seen += payload
            if needle in seen:
                return bytes(seen)
        elif kind in ("eof", "error"):
            raise AssertionError(
                f"the sidecar shell ended before {needle!r} appeared "
                f"({kind}: {payload!r}); saw {bytes(seen)!r}"
            )
    raise AssertionError(f"{needle!r} never appeared in the sidecar output: {bytes(seen)!r}")


def sidecar_containers(docker_client, run_id: str) -> list:
    return docker_client.containers.list(
        all=True,
        filters={"label": f"{terminal.LABEL_RUN}={run_id}"},
    )


async def wait_for_teardown(env, run_id: str, timeout: float = 60.0) -> None:
    """Both the sidecar and the workspace volume are gone (contract C9).

    Bounded rather than instant, because "the run task finished" and "the
    session's teardown committed" are separate awaits - but the ORDER is
    still what is proved: docker refuses to remove a volume a running
    container mounts, so a volume that is gone is a volume whose sidecar went
    first. A blown deadline fails loudly naming which half survived.
    """

    async def _gone():
        containers = sidecar_containers(env.docker, run_id)
        if containers:
            return None
        return not volume_exists(env.docker, run_id)

    await wait_until(
        _gone,
        timeout=timeout,
        message=(
            f"the debug session's sidecar and/or workspace volume outlived "
            f"run {run_id[:8]}: containers="
            f"{[c.id[:12] for c in sidecar_containers(env.docker, run_id)]} "
            f"volume={volume_exists(env.docker, run_id)}"
        ),
    )


THREE_STEPS = [
    script_step(
        "Seed",
        f"mkdir -p /workspace/repo && echo {MARKER} > /workspace/repo/marker.txt "
        "&& echo seeded",
    ),
    script_step("Middle", "cat /workspace/repo/marker.txt && echo middle-ran"),
    script_step("Last", "echo last-ran"),
]


# -----------------------------------------------------------------------------
# The loop: pause, inspect, resume, pause again, run to completion
# -----------------------------------------------------------------------------


class TestBreakpointLoopOnRealDocker:
    async def test_two_breakpoints_a_real_sidecar_and_a_completed_run(self, env):
        """The 12.7 exit gate at T2, in one run.

        Pause before step 1; prove the workspace is pinned and populated;
        spawn a REAL sidecar and read the bytes step 0 wrote through a REAL
        exec'd shell; resume; hit a SECOND breakpoint (C5 - the bug that made
        multi-breakpoint debugging impossible in failure_01); resume again;
        the run completes and everything the debug session created is gone.
        """
        repo, pipeline = await make_repo_and_pipeline(env.factory, THREE_STEPS)
        original = await start_and_wait(env, pipeline, repo)
        assert original.status == RunStatus.PASSED.value

        session_id, run_id = await start_debug_rerun(
            env, original, pipeline, repo, ["step_1", "step_2"]
        )

        # --- paused before step 1 -----------------------------------------
        session = await wait_for_pause(env, session_id, "step_1")
        assert session.current_step_name == "Middle"
        assert session.current_step_executor == "local"
        assert json.loads(session.hit_breakpoints) == ["step_1"]

        run = await fetch_run(env, run_id)
        assert run.trigger_type == TRIGGER_TYPE_DEBUG_RERUN
        by_index = {sr.step_index: sr for sr in run.step_runs}
        assert by_index[0].status == RunStatus.PASSED.value, (
            "step 0 must have RUN - the breakpoint is on step 1"
        )
        assert "seeded" in (by_index[0].logs or "")
        assert by_index[1].status == RunStatus.RUNNING.value
        assert "[debug] paused before step" in (by_index[1].logs or ""), (
            "R1/C11: a step sitting RUNNING while paused must say why in its "
            "own log, through the one log writer"
        )
        assert 2 not in by_index, "step 2 must not have been dispatched yet"

        # C3: a paused step has NO StepExecution row, so nothing exists for
        # the heartbeat reaper to find. Suspension by placement, not a flag.
        async with env.factory() as db:
            executions = (
                await db.execute(
                    select(StepExecution).where(
                        StepExecution.step_run_id == by_index[1].id
                    )
                )
            ).scalars().all()
        assert executions == [], (
            "a paused step must have no StepExecution row (contract C3)"
        )

        # C8: the pause holds a real REFCOUNT on a real named volume.
        workspace = await fetch_workspace(env, run_id)
        assert workspace is not None
        assert workspace.use_count == 1, (
            "the gate must hold exactly one workspace pin while paused"
        )
        assert workspace.status == WorkspaceStatus.IN_USE.value, (
            "a pinned workspace is IN_USE, which is exactly what makes the "
            "state machine refuse CLEANING while the pause holds it"
        )
        assert volume_exists(env.docker, run_id), (
            "the pinned volume must exist - without it the sidecar would "
            "attach to nothing"
        )

        # --- a REAL sidecar on the SAME volume ----------------------------
        container_id, stream = await sidecar_shell(env, session_id, run_id)
        container = env.docker.containers.get(container_id)
        assert container.status == "running"
        labels = container.labels or {}
        assert labels.get(terminal.LABEL_TYPE) == terminal.LABEL_TYPE_VALUE
        assert labels.get(terminal.LABEL_SESSION) == session_id
        assert labels.get(terminal.LABEL_RUN) == run_id
        mounts = {
            m.get("Destination"): m.get("Name")
            for m in container.attrs.get("Mounts", [])
        }
        assert mounts.get("/workspace") == generate_volume_name(run_id), (
            "the sidecar must mount the RUN's workspace volume - a sidecar on "
            "any other volume shows the developer the wrong bytes"
        )

        # THE proof: bytes step 0 wrote, read back through a real exec'd shell.
        await stream.write(b"cat /workspace/repo/marker.txt\n")
        assert MARKER.encode() in await shell_read_until(stream, MARKER.encode())

        # The workspace is READ-WRITE on purpose, and the resumed step sees it.
        await stream.write(b"echo touched-by-the-developer > /workspace/repo/edit.txt; echo wrote-edit\n")
        await shell_read_until(stream, b"wrote-edit")

        async with env.factory() as db:
            row = await debug_session_service.get(db, session_id)
        assert row.status == DebugState.CONNECTED.value
        assert row.sidecar_container_id == container_id

        # --- resume into the SECOND breakpoint (C5) -----------------------
        await resume(env, session_id)
        second = await wait_for_pause(env, session_id, "step_2")
        assert json.loads(second.hit_breakpoints) == ["step_1", "step_2"], (
            "resume must return the session to PENDING so the next breakpoint "
            "has a live session to pause into - failure_01 ended it here"
        )
        assert second.current_step_name == "Last"

        run = await fetch_run(env, run_id)
        by_index = {sr.step_index: sr for sr in run.step_runs}
        assert by_index[1].status == RunStatus.PASSED.value
        assert "middle-ran" in (by_index[1].logs or "")
        assert MARKER in (by_index[1].logs or ""), (
            "the resumed step read the workspace the sidecar was looking at"
        )

        # --- resume to completion -----------------------------------------
        await resume(env, session_id)
        await env.executor.wait_for_run(run_id)
        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value
        assert run.steps_completed == 3

        ended = await fetch_session(env, session_id)
        assert ended.status == DebugState.ENDED.value
        assert ended.end_reason, "a session never ends without saying why (R1)"

        # --- C9 teardown order, observed on real docker objects -----------
        await wait_for_teardown(env, run_id)
        workspace = await fetch_workspace(env, run_id)
        assert workspace.status == WorkspaceStatus.CLEANED.value
        assert workspace.use_count == 0


class TestWorkspacePreservedAtBreakpoint:
    async def test_workspace_preserved_at_breakpoint(self, env):
        """A pause on the FIRST step still has a workspace to show.

        Without the gate's `get_or_create` + `acquire`, a breakpoint on step 0
        would pause before the workspace block ever ran and the sidecar would
        attach to a volume that does not exist. This asserts the volume is
        real, mountable and NOT cleaned while the pause holds it.
        """
        repo, pipeline = await make_repo_and_pipeline(
            env.factory, [script_step("First", "echo first-ran")]
        )
        original = await start_and_wait(env, pipeline, repo)
        assert original.status == RunStatus.PASSED.value

        session_id, run_id = await start_debug_rerun(
            env, original, pipeline, repo, ["step_0"]
        )
        await wait_for_pause(env, session_id, "step_0")

        assert volume_exists(env.docker, run_id), (
            "a breakpoint on step 0 must still have a workspace volume - that "
            "is what the gate's pin buys (C8)"
        )
        workspace = await fetch_workspace(env, run_id)
        assert workspace.use_count == 1
        assert workspace.status != WorkspaceStatus.CLEANING.value

        # A sidecar can mount it and write to it right now.
        container_id, stream = await sidecar_shell(env, session_id, run_id)
        await stream.write(b"touch /workspace/proof && ls /workspace && echo listed\n")
        assert b"proof" in await shell_read_until(stream, b"listed")

        await resume(env, session_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value
        assert "first-ran" in (run.step_runs[0].logs or "")
        workspace = await fetch_workspace(env, run_id)
        assert workspace.use_count == 0, (
            "the pin must be released exactly once when the pause ends"
        )
        await wait_for_teardown(env, run_id)


class TestAbortAtBreakpoint:
    async def test_abort_removes_the_sidecar_before_the_volume(self, env):
        """C9's ordering, proved against docker rather than against a mock.

        `_cleanup_workspace` asks docker to REMOVE the workspace volume.
        Docker refuses while a running container mounts it, so an abort that
        tore down in the wrong order would leave the volume behind. Both
        being gone afterwards is the ordering assertion.
        """
        repo, pipeline = await make_repo_and_pipeline(
            env.factory,
            [script_step("First", "echo one"), script_step("Second", "echo two")],
        )
        original = await start_and_wait(env, pipeline, repo)

        session_id, run_id = await start_debug_rerun(
            env, original, pipeline, repo, ["step_1"]
        )
        await wait_for_pause(env, session_id, "step_1")
        container_id, _stream = await sidecar_shell(env, session_id, run_id)
        assert env.docker.containers.get(container_id).status == "running"

        async with env.factory() as db:
            session = await debug_session_service.abort(db, session_id)
        assert session.status == DebugState.ENDED.value
        assert session.end_reason

        await env.executor.wait_for_run(run_id)

        await wait_for_teardown(env, run_id)
        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.CANCELLED.value


class TestOrphanSidecarSweep:
    async def test_the_sweep_removes_a_sidecar_whose_session_is_gone(self, env):
        """C20: a backend restart kills the paused gate, so any sidecar still
        running belongs to a session nothing will ever end. The startup sweep
        is that backstop, and it runs here against real containers."""
        repo, pipeline = await make_repo_and_pipeline(
            env.factory,
            [script_step("First", "echo one"), script_step("Second", "echo two")],
        )
        original = await start_and_wait(env, pipeline, repo)
        session_id, run_id = await start_debug_rerun(
            env, original, pipeline, repo, ["step_1"]
        )
        await wait_for_pause(env, session_id, "step_1")
        container_id, _stream = await sidecar_shell(env, session_id, run_id)

        # A sweep that still knows about this session must NOT touch it.
        removed = await terminal.debug_terminal_service.sweep_orphan_sidecars(
            {session_id}
        )
        assert removed == 0
        assert env.docker.containers.get(container_id).status == "running"

        # A sweep that does not - the restart case - removes it.
        await terminal.debug_terminal_service.detach(session_id)
        removed = await terminal.debug_terminal_service.sweep_orphan_sidecars(set())
        assert removed >= 1
        with pytest.raises(docker_sdk.errors.NotFound):
            env.docker.containers.get(container_id)

        # Leave the run in a terminal state rather than a paused gate.
        async with env.factory() as db:
            await debug_session_service.abort(db, session_id)
        await env.executor.wait_for_run(run_id)
