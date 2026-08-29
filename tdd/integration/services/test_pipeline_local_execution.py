"""
Integration tests for the 12.2-INT local execution path with REAL Docker.

End-to-end through the real seams: PipelineExecutor -> ExecutionRouter ->
WorkspaceService (real NAMED docker volume, R6) -> LocalExecutor (real
containers) -> StepRun rows, with the REAL WebSocket manager carrying a
capturing transport (never an AsyncMock, R6).

The only stubbed seam is workspace population (the git clone needs a running
backend git server; population has its own integration tests). The named
volume itself is created, mounted, shared, and cleaned FOR REAL.

Docker is required (R4: no environment-dependent skips - if Docker is down
these fail loudly).
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

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import Base
from app.models import Card, Job, Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import RunStatus
from app.models.workspace import Workspace

# Import-strict against the 12.2-INT contract modules (R1: loud failure if a
# contracted module is missing, never a skip).
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.execution_router import ExecutionRouter  # noqa: F401
from app.services.workspace.state_machine import WorkspaceStatus, generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration, pytest.mark.local_exec]

STEP_IMAGE = "python:3.12-slim"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4).


@pytest.fixture(scope="module", autouse=True)
def step_image(docker_client):
    """Make sure the step image exists locally (pull once if needed)."""
    try:
        docker_client.images.get(STEP_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        docker_client.images.pull(STEP_IMAGE)
    return STEP_IMAGE


class CapturingSocket:
    """Capturing transport for the REAL ConnectionManager (R6)."""

    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch, docker_client):
    """Real-seam environment: file DB, fresh executor, stubbed population."""
    db_path = (tmp_path / "local_exec.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Population needs the backend git server; stub JUST the clone. The named
    # volume lifecycle stays fully real (created/mounted/cleaned by the real
    # WorkspaceService + Docker).
    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    executor = PipelineExecutor()
    socket = CapturingSocket()
    manager.active_connections.append(socket)

    run_ids: list[str] = []

    yield SimpleNamespace(
        factory=factory,
        executor=executor,
        socket=socket,
        run_ids=run_ids,
        docker=docker_client,
    )

    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    # Defensive: remove any volume a failing test leaked.
    for run_id in run_ids:
        try:
            docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
        except docker_sdk.errors.NotFound:
            pass
    await engine.dispose()


async def make_repo_and_pipeline(factory, steps: list[dict]):
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="local-exec-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="local-exec-pipeline",
            steps=json.dumps(steps),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        await db.refresh(repo)
        await db.refresh(pipeline)
        return repo, pipeline


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


async def fetch_workspace(env, run_id: str) -> Workspace | None:
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


def script_step(name: str, command: str, **extra) -> dict:
    step = {
        "name": name,
        "type": "script",
        "config": {"command": command, "image": STEP_IMAGE},
    }
    step.update(extra)
    return step


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

class TestTwoStepSharedWorkspace:
    async def test_two_step_pipeline_shares_volume_and_home(self, env):
        """Step 1 writes under /workspace/repo and $HOME; step 2 reads both
        back - proving one NAMED volume (and HOME persistence) spans the
        run's steps, and cleanup removes the volume afterwards."""
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            script_step(
                "Seed",
                'mkdir -p /workspace/repo && '
                'echo shared-data > /workspace/repo/marker.txt && '
                'echo home-data > "$HOME/home-marker.txt" && '
                'echo seeded',
            ),
            script_step(
                "Read",
                'cat /workspace/repo/marker.txt && '
                'cat "$HOME/home-marker.txt" && '
                'echo read-back-done',
            ),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert run.steps_completed == 2
        by_index = {sr.step_index: sr for sr in run.step_runs}
        assert by_index[0].executor == "local"
        assert by_index[1].executor == "local"
        assert by_index[0].status == RunStatus.PASSED.value
        assert by_index[1].status == RunStatus.PASSED.value
        assert "seeded" in by_index[0].logs
        # The proof: step 2 read what step 1 wrote, through the named volume
        assert "shared-data" in by_index[1].logs
        assert "home-data" in by_index[1].logs
        assert "read-back-done" in by_index[1].logs

        # Workspace cleaned on completion: row CLEANED, volume gone
        workspace = await fetch_workspace(env, run.id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.CLEANED.value
        assert not volume_exists(env.docker, run.id)


class TestLocalFailureCleanup:
    async def test_failing_step_fails_run_and_cleans_workspace(self, env):
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            script_step("Boom", "echo about-to-fail && exit 7"),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        step = run.step_runs[0]
        assert step.executor == "local"
        assert step.status == RunStatus.FAILED.value
        assert "exit code 7" in step.error
        assert "about-to-fail" in step.logs

        # Cleanup MUST also run on the failure path
        workspace = await fetch_workspace(env, run.id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.CLEANED.value
        assert not volume_exists(env.docker, run.id)


class TestContainerCleanup:
    async def test_step_containers_gone_synchronously_with_completion(self, env):
        """(fix 6) the step container is removed BEFORE the step completes -
        the moment the run is done (docker ps -a check), no container of this
        run exists, running or exited. Never dependent on generator GC."""
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            script_step("Quick", "echo cleanup-proof"),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        # docker ps -a equivalent, filtered by the label LocalExecutor stamps
        from app.services.execution.local_executor import (
            CONTAINER_LABEL_PIPELINE_RUN,
        )

        leftovers = env.docker.containers.list(
            all=True,
            filters={"label": f"{CONTAINER_LABEL_PIPELINE_RUN}={run.id}"},
        )
        assert leftovers == [], (
            f"step containers survived step completion: "
            f"{[c.id for c in leftovers]}"
        )

    async def test_failed_step_container_also_gone(self, env):
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            script_step("Boom", "exit 9"),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        from app.services.execution.local_executor import (
            CONTAINER_LABEL_PIPELINE_RUN,
        )

        leftovers = env.docker.containers.list(
            all=True,
            filters={"label": f"{CONTAINER_LABEL_PIPELINE_RUN}={run.id}"},
        )
        assert leftovers == []


class TestImagePreflight:
    async def test_missing_image_fails_run_before_any_container(self, env):
        """(12.3 hardening) A run naming an unresolvable image tag fails at
        preflight - ONE failure, no StepRun rows, no containers ever
        spawned - instead of dribbling per-step ImageNotFound errors."""
        missing_tag = f"lazyaf-preflight-missing:{uuid4().hex[:8]}"
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            {
                "name": "Never",
                "type": "script",
                "config": {"command": "echo unreachable", "image": missing_tag},
            },
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.FAILED.value
        assert run.step_runs == []  # failed BEFORE dispatching step 0
        from app.services.execution.local_executor import (
            CONTAINER_LABEL_PIPELINE_RUN,
        )

        leftovers = env.docker.containers.list(
            all=True,
            filters={"label": f"{CONTAINER_LABEL_PIPELINE_RUN}={run.id}"},
        )
        assert leftovers == []


class TestNoEnqueueOnLocalPath:
    async def test_local_steps_never_touch_job_queue(self, env, monkeypatch):
        """R1 spy at the integration level: the full real local stack makes
        zero job_queue.enqueue calls and creates no Card/Job rows."""
        calls = []

        class EnqueueSpy:
            async def enqueue(self, job):
                calls.append(job)
                return job.id

        import app.services.pipeline_executor as pe

        monkeypatch.setattr(pe, "job_queue", EnqueueSpy())

        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            script_step("OnlyLocal", "echo never-enqueued"),
        ])

        run = await start_and_wait(env, pipeline, repo)

        assert run.status == RunStatus.PASSED.value
        assert run.step_runs[0].executor == "local"
        assert calls == []
        async with env.factory() as db:
            assert (await db.execute(select(Card))).scalars().all() == []
            assert (await db.execute(select(Job))).scalars().all() == []
