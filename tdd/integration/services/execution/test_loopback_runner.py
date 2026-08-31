"""
THE LOOPBACK LANE (Phase 12.6, R7) - remote execution, end to end, for real.

Remote hardware is manual (owner decision), so the TESTED remote path is a
real `lazyaf_runner` agent PROCESS on this host speaking the real WebSocket
protocol to a real backend. Nothing here is simulated:

    subprocess `python -m lazyaf_runner`
      -> ws://<backend>/ws/runner, Bearer enrollment secret, real handshake
      -> RunnerRegistry (real state machine, real DB rows)
      -> RunnerDispatcher's compare-and-swap assignment
      -> execute_step over the socket, ACKed by the agent
      -> DockerOrchestrator creates its OWN named volume, clones the repo
         from the backend's git server, and runs a control-mode container
      -> the STEP CONTAINER POSTs its own status/logs to /api/steps/*
         over HTTP, exactly as on the local path
      -> step_complete over the socket closes the assignment out

The only thing loopback does not exercise is physical network latency.

Two proofs carry the weight, and neither has a local analogue:

 1. `ls /workspace/repo/PLAN-ish-file` inside the step container. A remote
    agent cannot see the backend's `lazyaf-workspaces` volume, so it must
    create its own volume on its own daemon and clone into it from the
    backend's git server. If that clone silently did nothing, `echo` would
    still succeed and the step would still pass - this is what makes the
    difference visible.
 2. Container logs arriving through POST /api/steps/{id}/logs while
    `[runner] ` lines arrive over the WebSocket. That split IS the 12.6
    channel decision: five control channels work on another host with zero
    new server code because the step JWT is location-independent.

TIER: T2 (Docker-real). Placed under tdd/integration/services/execution/
rather than the design's tdd/integration/execution/ because scripts/run_tier.py
selects T2 as `tdd/integration/services` and T1 as everything else in
tdd/integration - a Docker-real suite outside services/ would run in the
NO-DOCKER tier and fail there. Same tier the design asks for, correct
directory for it.

Docker is required and fails loudly (R4: never a silent skip). `git` on PATH
is required too - the fixture pushes a real repo over git-http, exactly as
tdd/integration/services/execution/test_workspace_population.py's e2e lane
does.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

_repo_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "backend"))
sys.path.insert(0, str(_repo_root / "scripts"))

from build_images import build_image, tree_hash  # noqa: E402
from tdd.shared.factories.pipelines import graph_json  # noqa: E402
from tdd.integration.conftest import (  # noqa: E402
    advertise_addr,
    free_port,
    start_uvicorn,
    stop_uvicorn,
)

from app.config import get_settings  # noqa: E402
import app.database as app_database  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Pipeline, PipelineRun, Repo, StepRun  # noqa: E402
from app.models.pipeline import RunStatus, StepExecution  # noqa: E402
from app.models.runner import Runner  # noqa: E402
from app.routers.ws_runners import get_runner_session_factory  # noqa: E402
from app.services.execution.runner_dispatcher import runner_dispatcher  # noqa: E402
from app.services.execution.runner_registry import runner_registry  # noqa: E402
from app.services.git_server import git_repo_manager  # noqa: E402
from app.services.pipeline_executor import PipelineExecutor  # noqa: E402
from app.services.workspace.state_machine import generate_volume_name  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

STEP_IMAGE = "lazyaf-base:dev"
IMAGES_BASE_DIR = _repo_root / "images" / "base"

#: A file the fixture commits into the seed repo. The step reads it back
#: from inside the container - that is the proof the AGENT populated its
#: own workspace volume by cloning from the backend's git server.
PROOF_FILE = "PLAN.md"
#: Content that exists in exactly one place: the seed commit. Anything
#: short of a real clone cannot produce it.
SEED_CONTENT = "loopback seed repo"

#: How long to wait for an agent process to enroll and reach `idle`.
ENROLL_TIMEOUT = 45.0


# -----------------------------------------------------------------------------
# Image + git helpers
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def step_image(docker_client):
    """The real base image, built HASH-CORRECTLY if missing (never pulled)."""
    try:
        docker_client.images.get(STEP_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        build_image(
            docker_client, IMAGES_BASE_DIR, STEP_IMAGE, tree_hash(IMAGES_BASE_DIR)
        )
    return STEP_IMAGE


def _git(*args, cwd):
    """Run git, failing loudly with its own stderr."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}):\n{result.stderr}"
        )
    return result.stdout


def _fetch_json_blocking(url: str, timeout: float = 10.0):
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


async def fetch_json(url: str, timeout: float = 10.0):
    """Every HTTP read in this module goes through a THREAD.

    The test server runs on THIS event loop (that is deliberate: the WS
    manager singleton has to be shared with the in-process executor). A
    blocking urlopen on the same loop therefore deadlocks - uvicorn can
    never serve the request the caller is sitting inside. Every helper here
    hands the blocking call to a thread for exactly that reason.
    """
    return await asyncio.to_thread(_fetch_json_blocking, url, timeout)


# -----------------------------------------------------------------------------
# The lane fixture: real backend, real dispatcher, real agent subprocess
# -----------------------------------------------------------------------------


class _Agent:
    """A `lazyaf_runner` subprocess, with its output captured for diagnosis."""

    def __init__(self, runner_id: str, process: subprocess.Popen, log_path: Path):
        self.runner_id = runner_id
        self.process = process
        self.log_path = log_path

    def output(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<agent log unreadable>"

    def kill(self) -> None:
        """SIGKILL-equivalent: no graceful drain, no `disconnected` frame.

        The point of using kill rather than terminate in the failover test is
        that the backend must discover the death by HEARTBEAT TIMEOUT, which
        is the path a yanked network cable takes.
        """
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.process.kill()
            self.process.wait(timeout=15)


@pytest_asyncio.fixture
async def lane(tmp_path, monkeypatch, docker_client):
    """A live backend + dispatcher, a seeded git repo, and an agent spawner."""
    db_path = (tmp_path / "loopback.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", echo=False, connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # The runner socket deliberately opens ONE SESSION PER MESSAGE from an
    # injected factory (a session held across a multi-hour connection is a
    # pooled connection pinned for hours). That factory is a FastAPI
    # dependency precisely so a test can point it at its own engine - a
    # get_db override alone would leave every WS handler on the production
    # database.
    app.dependency_overrides[get_runner_session_factory] = lambda: factory
    # RemoteExecutor and the dispatcher open their own sessions too - they
    # outlive any request, so they cannot take one from a dependency. Their
    # documented test seam is rebinding the module-level factory, which is
    # what points them at this test's engine instead of the real database.
    monkeypatch.setattr(app_database, "async_session", factory)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
            # Lifespan OFF and the registry/dispatcher started by hand below,
            # against THIS test's session factory. The app's own lifespan would
            # bind them to the production engine.
            lifespan="off",
        )
    )
    server_task = await start_uvicorn(server)

    # Two addresses, and the difference is the whole remote-deployment hazard
    # section 7.1 names:
    #   - the AGENT runs in this process's namespace, so it dials 127.0.0.1;
    #   - the STEP CONTAINERS it spawns are SIBLINGS on the daemon and must be
    #     handed an address a sibling can reach.
    local_url = f"http://127.0.0.1:{port}"
    container_url = f"http://{advertise_addr()}:{port}"
    settings = get_settings()
    monkeypatch.setattr(settings, "container_backend_url", container_url)
    monkeypatch.setattr(
        settings, "container_git_url_template", container_url + "/git/{repo_id}.git"
    )

    # A real repo, pushed over real git-http into the backend's own git server.
    # The agent clones from here, which is exactly what a remote host does.
    # Both calls talk to the server on THIS loop, so both go through a thread.
    #
    # `/ingest`, not `/repos`: the plain create only writes the DB row, while
    # ingest is the endpoint that also initializes the BARE repo the git
    # router serves. A push at a repo that exists only in the database gets a
    # 404 from git with no hint about which of the two things is missing.
    import urllib.request

    # Hermetic git storage. The manager is a module-level singleton whose
    # default (`/app/data/git_repos`) is the CONTAINER's path; left alone it
    # would write into a real directory shared with the dev stack.
    monkeypatch.setattr(git_repo_manager, "repos_dir", tmp_path / "git_repos")
    monkeypatch.setattr(git_repo_manager, "_initialized", False)

    def _create_repo():
        request = urllib.request.Request(
            f"{local_url}/api/repos/ingest",
            data=json.dumps(
                {"name": f"loopback-{uuid4().hex[:8]}", "default_branch": "main"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["id"]

    repo_id = await asyncio.to_thread(_create_repo)
    assert git_repo_manager.repo_exists(repo_id), (
        "POST /api/repos/ingest did not leave a bare repo the git router can "
        f"serve (repos_dir={git_repo_manager.repos_dir})"
    )

    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "test@lazyaf.local", cwd=work)
    _git("config", "user.name", "LazyAF Test", cwd=work)
    (work / PROOF_FILE).write_text(SEED_CONTENT + "\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    await asyncio.to_thread(
        _git, "push", f"{local_url}/git/{repo_id}.git", "HEAD:main", cwd=work
    )

    await runner_registry.reset()
    await runner_dispatcher.reset()
    async with factory() as session:
        await runner_registry.bootstrap(session)
    await runner_dispatcher.start(factory)

    executor = PipelineExecutor()
    agents: list[_Agent] = []
    run_ids: list[str] = []
    volumes: list[str] = []

    def spawn_agent(runner_id: str, labels: str = "has=docker,has=remote-lane") -> _Agent:
        log_path = tmp_path / f"agent-{runner_id}.log"
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(_repo_root / "runner-agent"),
                "PYTHONUNBUFFERED": "1",
                "LAZYAF_BACKEND_URL": local_url,
                "LAZYAF_RUNNER_ID": runner_id,
                "LAZYAF_RUNNER_NAME": runner_id,
                "LAZYAF_RUNNER_TYPE": "generic",
                "LAZYAF_RUNNER_LABELS": labels,
                "LAZYAF_ORCHESTRATOR": "docker",
                # Step containers are siblings: hand them the sibling-reachable
                # address for BOTH the control POSTs and the git clone.
                "LAZYAF_STEP_BACKEND_URL": container_url,
                "LAZYAF_GIT_URL_TEMPLATE": container_url + "/git/{repo_id}.git",
                "LAZYAF_RUNNER_LOG_LEVEL": "DEBUG",
            }
        )
        handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", "lazyaf_runner"],
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(_repo_root),
        )
        agent = _Agent(runner_id, process, log_path)
        agents.append(agent)
        return agent

    yield SimpleNamespace(
        factory=factory,
        executor=executor,
        local_url=local_url,
        container_url=container_url,
        repo_id=repo_id,
        spawn_agent=spawn_agent,
        run_ids=run_ids,
        volumes=volumes,
        docker=docker_client,
    )

    for agent in agents:
        agent.stop()
    await runner_dispatcher.stop()
    await runner_registry.drain("loopback teardown")
    await runner_registry.reset()
    await stop_uvicorn(server, server_task)
    app.dependency_overrides.clear()
    for run_id in run_ids:
        for name in (generate_volume_name(run_id), *volumes):
            try:
                docker_client.volumes.get(name).remove(force=True)
            except docker_sdk.errors.NotFound:
                pass
            except docker_sdk.errors.APIError:
                pass
    await engine.dispose()


# -----------------------------------------------------------------------------
# Polling helpers - loud on timeout, never a soft pass
# -----------------------------------------------------------------------------


async def wait_for_runner_state(
    lane, runner_id: str, states: set[str], timeout: float = ENROLL_TIMEOUT
) -> dict:
    """Poll GET /api/runners until `runner_id` is in one of `states`."""
    deadline = time.monotonic() + timeout
    last: list = []
    while time.monotonic() < deadline:
        last = await fetch_json(f"{lane.local_url}/api/runners")
        for row in last:
            if row["id"] == runner_id and row["status"] in states:
                return row
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"runner {runner_id!r} never reached {sorted(states)} within {timeout}s. "
        f"GET /api/runners: {last}"
    )


async def make_pipeline(lane, steps: list[dict]):
    async with lane.factory() as db:
        repo = (
            await db.execute(select(Repo).where(Repo.id == lane.repo_id))
        ).scalar_one()
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name=f"loopback-{uuid4().hex[:8]}",
            steps_graph=graph_json(steps),
        )
        db.add(pipeline)
        await db.commit()
        await db.refresh(pipeline)
        await db.refresh(repo)
        return repo, pipeline


async def fetch_run(lane, run_id: str) -> PipelineRun:
    async with lane.factory() as db:
        return (
            await db.execute(
                select(PipelineRun)
                .where(PipelineRun.id == run_id)
                .options(selectinload(PipelineRun.step_runs))
            )
        ).scalar_one()


async def fetch_executions(lane, step_run_id: str) -> list[StepExecution]:
    async with lane.factory() as db:
        return list(
            (
                await db.execute(
                    select(StepExecution).where(
                        StepExecution.step_run_id == step_run_id
                    )
                )
            )
            .scalars()
            .all()
        )


def pinned_step(runner_id: str, marker: str) -> dict:
    """The remote-probe shape: pinned by runner_id, proving its own clone."""
    return {
        "name": "Loopback remote probe",
        "type": "script",
        "timeout": 300,
        "config": {
            "image": STEP_IMAGE,
            "requires": {"runner_id": runner_id},
            "command": (
                f"echo {marker}-start\n"
                f"ls /workspace/repo/{PROOF_FILE}\n"
                f"echo {marker}-done\n"
            ),
        },
    }


# -----------------------------------------------------------------------------
# The lane
# -----------------------------------------------------------------------------


class TestLoopbackRunner:
    async def test_a_pinned_step_executes_on_the_agent_and_reports_home(self, lane):
        """The whole lane in one test (Agent E contract 1).

        Every assertion below is a different link in the chain, and each one
        can break while the others still pass:

          - the step PASSED               -> the agent ran a container at all
          - executor == 'remote'          -> the router honoured `requires:`
          - StepExecution.runner_id       -> the assignment CAS actually fired
          - the container's own echo      -> POST /api/steps/{id}/logs landed
                                             FROM the remote lane (the 12.6
                                             channel split holds)
          - `ls PLAN.md` exit 0           -> the AGENT cloned its own
                                             workspace volume (no local
                                             analogue for this one)
          - a `[runner] ` line            -> the WS log channel carries what
                                             a step container cannot say
        """
        runner_id = f"loopback-{uuid4().hex[:8]}"
        agent = lane.spawn_agent(runner_id)
        await wait_for_runner_state(lane, runner_id, {"idle"})

        marker = f"loopback-{uuid4().hex[:8]}"
        repo, pipeline = await make_pipeline(lane, [pinned_step(runner_id, marker)])

        async with lane.factory() as db:
            run = await lane.executor.start_pipeline(
                db=db, pipeline=pipeline, repo=repo
            )
            run_id = run.id
        lane.run_ids.append(run_id)
        await lane.executor.wait_for_run(run_id)

        run = await fetch_run(lane, run_id)
        step = run.step_runs[0]
        assert run.status == RunStatus.PASSED.value, (
            f"run failed: status={step.status} error={step.error!r}\n"
            f"--- step logs ---\n{step.logs}\n"
            f"--- agent log ---\n{agent.output()}"
        )

        # R1 observability: the lane the step took is recorded, not inferred.
        assert step.executor == "remote", (
            f"step ran on {step.executor!r}; a pinned step that silently fell "
            f"back to local is the regression this lane exists to catch"
        )

        # The assignment CAS's own output, read back.
        executions = await fetch_executions(lane, step.id)
        assert len(executions) == 1, executions
        execution = executions[0]
        assert execution.runner_id == runner_id
        assert execution.assigned_at is not None
        assert execution.status == "completed"

        # The container's own stdout, delivered over HTTP by the in-container
        # control runtime - the 12.6 channel decision, proved from a remote
        # lane with zero new server code.
        assert f"{marker}-start" in step.logs, (
            "the step container's own log lines never reached "
            "POST /api/steps/{id}/logs from the remote lane:\n" + step.logs
        )
        assert f"{marker}-done" in step.logs, (
            "the command stopped before its last echo - `ls " + PROOF_FILE + "` "
            "most likely failed, which means the agent did NOT clone its own "
            "workspace:\n" + step.logs
        )

        # Runner-origin lines: what a step container cannot say because it does
        # not exist yet. They travel the WebSocket, not the steps API.
        assert "[runner] " in step.logs, (
            "no [runner] line on the step log stream - the WS `log` channel "
            "never reached append_step_logs(source='runner'):\n" + step.logs
        )

        # The runner is idle again and holds nothing: `step_complete` closed
        # the assignment out on both sides.
        row = await wait_for_runner_state(lane, runner_id, {"idle"}, timeout=30)
        assert row["current_step_execution_id"] is None
        assert row["connection"] == "websocket"

        # And the assignment is READABLE THROUGH THE API, not only in the DB.
        # scripts/verify_executor.py assertion 10 runs inside a bare step
        # container with nothing but urllib - if the runner id were reachable
        # only through a SQLAlchemy session, the dogfood gate could never
        # tell a completed remote assignment from a remote step that never
        # found a runner. This is that path, exercised the way the gate uses
        # it.
        payload = await fetch_json(f"{lane.local_url}/api/pipeline-runs/{run_id}")
        api_step = payload["step_runs"][0]
        assert api_step["executor"] == "remote"
        assert api_step["runner_id"] == runner_id, (
            "GET /api/pipeline-runs/{id} did not expose the step's runner - "
            "verify_executor assertion 10 reads exactly this field: "
            + repr(api_step)
        )

    async def test_the_agent_cloned_the_repo_into_its_own_workspace(self, lane):
        """The one piece of remote execution with no local analogue.

        The backend's `lazyaf-workspaces` volume is not visible to a remote
        host, so the agent must create a volume on its OWN daemon and clone
        the repo into it from the backend's git server. The proof is the
        CONTENT of a committed file, read from inside the step container:
        `ls` proves a path exists, but only content that was never written by
        anything but the seed commit proves the clone actually transferred
        objects.

        Read from INSIDE the container rather than out of the volume
        afterwards, deliberately: the run's workspace is reaped at completion
        (the backend sends `cleanup_workspace`, the agent removes the
        volume), so an after-the-fact volume inspection tests the reaper
        rather than the clone - and on a real remote host there is no volume
        to inspect anyway. This assertion is the one that travels.
        """
        runner_id = f"loopback-{uuid4().hex[:8]}"
        agent = lane.spawn_agent(runner_id)
        await wait_for_runner_state(lane, runner_id, {"idle"})

        marker = f"clone-{uuid4().hex[:8]}"
        repo, pipeline = await make_pipeline(
            lane,
            [
                {
                    "name": "Clone proof",
                    "type": "script",
                    "timeout": 300,
                    "config": {
                        "image": STEP_IMAGE,
                        "requires": {"runner_id": runner_id},
                        "command": (
                            f"echo {marker}-start\n"
                            f"cat /workspace/repo/{PROOF_FILE}\n"
                            "git -C /workspace/repo rev-parse --abbrev-ref HEAD\n"
                            f"echo {marker}-done\n"
                        ),
                    },
                }
            ],
        )
        async with lane.factory() as db:
            run = await lane.executor.start_pipeline(
                db=db, pipeline=pipeline, repo=repo
            )
            run_id = run.id
        lane.run_ids.append(run_id)
        await lane.executor.wait_for_run(run_id)

        run = await fetch_run(lane, run_id)
        step = run.step_runs[0]
        assert run.status == RunStatus.PASSED.value, (
            f"{step.error!r}\n{step.logs}\n--- agent ---\n{agent.output()}"
        )

        # SEED_CONTENT exists only in the commit the fixture pushed to the
        # backend's git server. Its presence here means the agent fetched
        # objects over HTTP into a volume it created itself.
        assert SEED_CONTENT in step.logs, (
            "the seed commit's file content never appeared - the agent did "
            "not clone the repo into its own workspace volume:\n" + step.logs
        )
        assert f"{marker}-done" in step.logs

    async def test_an_unsatisfiable_pin_fails_the_step_with_a_readable_message(
        self, lane
    ):
        """A typo in `requires:` must not hang a pipeline forever.

        A pin nobody can satisfy is indistinguishable from a hung backend
        without this: the step fails after NO_RUNNER_TIMEOUT with a message
        naming both the requirements and the labels of every connected
        runner, so the operator can see WHY nothing matched.
        """
        from app.services.execution import remote_executor as remote_executor_module

        runner_id = f"loopback-{uuid4().hex[:8]}"
        lane.spawn_agent(runner_id)
        await wait_for_runner_state(lane, runner_id, {"idle"})

        # Shrink the no-runner budget: the real 300s is a production choice,
        # not something a test should sit through. Patched on the CONSUMER
        # module, not on runner_protocol: the constant is imported by value,
        # so rebinding it at the source would leave every caller looking at
        # the old number and the test would silently sit for five minutes.
        original = remote_executor_module.NO_RUNNER_TIMEOUT
        remote_executor_module.NO_RUNNER_TIMEOUT = 5
        try:
            repo, pipeline = await make_pipeline(
                lane,
                [
                    {
                        "name": "Unsatisfiable pin",
                        "type": "script",
                        "timeout": 120,
                        "config": {
                            "image": STEP_IMAGE,
                            "requires": {"has": ["a-capability-nobody-has"]},
                            "command": "echo should-never-run",
                        },
                    }
                ],
            )
            async with lane.factory() as db:
                run = await lane.executor.start_pipeline(
                    db=db, pipeline=pipeline, repo=repo
                )
                run_id = run.id
            lane.run_ids.append(run_id)
            await lane.executor.wait_for_run(run_id)
        finally:
            remote_executor_module.NO_RUNNER_TIMEOUT = original

        run = await fetch_run(lane, run_id)
        step = run.step_runs[0]
        assert step.status == RunStatus.FAILED.value
        message = f"{step.error or ''}\n{step.logs or ''}"
        assert "a-capability-nobody-has" in message, message
        assert runner_id in message, (
            "the failure must name the CONNECTED runners' labels, or the "
            "operator cannot see why the pin did not match:\n" + message
        )


class TestLoopbackRegistryProjection:
    async def test_the_api_snapshot_reflects_a_live_socket(self, lane):
        """Gate assertion 9's data, read the way the gate reads it.

        `connection` is stamped from the registry's live socket table, never
        from the row: an 'idle' row left behind by a crashed process is
        indistinguishable in the DB alone. Killing the agent must therefore
        flip `connection` away from 'websocket'.
        """
        runner_id = f"loopback-{uuid4().hex[:8]}"
        agent = lane.spawn_agent(runner_id, labels="has=docker,has=remote-lane,zone=lab")
        row = await wait_for_runner_state(lane, runner_id, {"idle"})

        assert row["connection"] == "websocket"
        assert row["protocol_version"] is not None
        assert "remote-lane" in row["labels"].get("has", [])
        assert row["labels"].get("zone") == "lab"
        # The agent reports platform.machine() raw; the backend normalizes it,
        # so exactly one implementation of that mapping exists.
        assert row["labels"].get("arch") in {"amd64", "arm64", "armv7"}, row["labels"]

        agent.kill()
        row = await wait_for_runner_state(
            lane, runner_id, {"disconnected", "dead"}, timeout=60
        )
        assert row["connection"] != "websocket"

        async with lane.factory() as db:
            stored = (
                await db.execute(select(Runner).where(Runner.id == runner_id))
            ).scalar_one()
        assert stored.websocket_id is None or stored.status in {
            "disconnected",
            "dead",
        }
