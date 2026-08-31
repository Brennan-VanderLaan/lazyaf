"""
ROUND-TRIP test for the 12.2-INT step event contract (R3/R1), real Docker:

    container stdout -> LocalExecutor event stream -> pipeline executor
    -> StepRun row in the DB (incrementally) + typed WS publishes through
    the REAL WebSocket manager (capturing transport, never an AsyncMock - R6).

A script step `echo hello-roundtrip` must land 'hello-roundtrip' in the
StepRun.logs COLUMN (read back through a separate session) with
StepRun.executor == 'local'.

Docker is required (R4: no environment-dependent skips - if Docker is down
these fail loudly).
"""
import asyncio
import json
import sys
import time
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
tdd_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories.pipelines import make_repo_and_graph_pipeline  # noqa: E402

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import RunStatus

# Import-strict against the 12.2-INT contract modules (R1: loud failure if a
# contracted module is missing, never a skip).
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.execution_router import ExecutionRouter  # noqa: F401
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration, pytest.mark.local_exec]

STEP_IMAGE = "python:3.12-slim"


# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4).


@pytest.fixture(scope="module", autouse=True)
def step_image(docker_client):
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
    db_path = (tmp_path / "roundtrip.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Stub ONLY the git clone (needs the backend git server; population has
    # its own integration suite). Volume lifecycle stays real.
    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    executor = PipelineExecutor()
    socket = CapturingSocket()
    manager.active_connections.append(socket)

    run_ids: list[str] = []
    yield SimpleNamespace(
        factory=factory, executor=executor, socket=socket,
        run_ids=run_ids, docker=docker_client,
    )

    if socket in manager.active_connections:
        manager.active_connections.remove(socket)
    for run_id in run_ids:
        try:
            docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
        except docker_sdk.errors.NotFound:
            pass
    await engine.dispose()


async def make_repo_and_pipeline(factory, command: str, timeout: int = 300):
    """One echo step, persisted as the one-node GRAPH it describes (12.8).

    The node id is `step_0`; nothing here asserts on it, but the StepRun
    carries it and that is what the debug and log routes key on.
    """
    return await make_repo_and_graph_pipeline(
        factory,
        [
            {
                "name": "Echo",
                "type": "script",
                "timeout": timeout,
                "config": {"command": command, "image": STEP_IMAGE},
            }
        ],
        name="roundtrip-pipeline",
        repo_name="roundtrip-repo",
    )


async def fetch_run(env, run_id: str) -> PipelineRun:
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


class TestEchoRoundTrip:
    async def test_echo_lands_in_step_run_row_and_ws(self, env):
        """The named round-trip: 'echo hello-roundtrip' through the local
        path lands 'hello-roundtrip' in the StepRun.logs DB column, the
        executor column says 'local', and the same line went out over the
        real WS manager as a typed step_log publish."""
        repo, pipeline = await make_repo_and_pipeline(env.factory, "echo hello-roundtrip")

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        # DB row, read back through a fresh session (the real seam)
        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value
        step = run.step_runs[0]
        assert step.executor == "local"
        assert "hello-roundtrip" in step.logs
        assert step.status == RunStatus.PASSED.value
        assert step.started_at is not None
        assert step.completed_at is not None

        # Typed WS publishes through the REAL manager
        log_lines = [p["line"] for p in env.socket.of_type("step_log")]
        assert any("hello-roundtrip" in line for line in log_lines)
        statuses = [p["status"] for p in env.socket.of_type("step_update")]
        assert "preparing" in statuses
        assert "running" in statuses
        assert statuses[-1] == RunStatus.PASSED.value
        # Legacy broadcast channel still serves existing UI consumers
        step_statuses = env.socket.of_type("step_run_status")
        assert any(s["status"] == RunStatus.PASSED.value for s in step_statuses)
        run_statuses = env.socket.of_type("pipeline_run_status")
        assert any(s["status"] == RunStatus.PASSED.value for s in run_statuses)

    async def test_logs_persist_incrementally_while_step_still_running(self, env):
        """Log lines must be visible in the StepRun row WHILE the container
        is still running (incremental persistence, not end-of-step dumps)."""
        repo, pipeline = await make_repo_and_pipeline(
            env.factory, "echo first-line && sleep 6 && echo second-line"
        )

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)

        saw_incremental = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            async with env.factory() as db:
                result = await db.execute(
                    select(StepRun).where(StepRun.pipeline_run_id == run_id)
                )
                step = result.scalar_one_or_none()
                result = await db.execute(
                    select(PipelineRun).where(PipelineRun.id == run_id)
                )
                current = result.scalar_one()
            if (
                step is not None
                and "first-line" in (step.logs or "")
                and "second-line" not in (step.logs or "")
                and current.status == RunStatus.RUNNING.value
            ):
                saw_incremental = True
                break
            if current.status != RunStatus.RUNNING.value:
                break  # run finished before we observed - fail below
            await asyncio.sleep(0.2)

        await env.executor.wait_for_run(run_id)
        run = await fetch_run(env, run_id)

        assert saw_incremental, (
            "first-line was never observable in the StepRun row while the "
            "step was still running - logs are not persisted incrementally"
        )
        assert run.status == RunStatus.PASSED.value
        assert "first-line" in run.step_runs[0].logs
        assert "second-line" in run.step_runs[0].logs

    async def test_batched_flush_keeps_full_log_fidelity(self, env):
        """(fix 7) log persistence is batched (~200 lines / 500ms per
        commit), so a burst spanning multiple flush windows must land EVERY
        line, in order, in both the StepRun row and the WS stream."""
        line_count = 450  # > 2 full batches at LOG_FLUSH_MAX_LINES=200
        repo, pipeline = await make_repo_and_pipeline(
            env.factory,
            f'for i in $(seq 1 {line_count}); do echo "fidelity-$i"; done',
        )

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value
        logs = run.step_runs[0].logs
        db_lines = [l for l in logs.splitlines() if l.startswith("fidelity-")]
        assert db_lines == [f"fidelity-{i}" for i in range(1, line_count + 1)]

        ws_lines = [
            p["line"]
            for p in env.socket.of_type("step_log")
            if p["line"].startswith("fidelity-")
        ]
        assert ws_lines == [f"fidelity-{i}" for i in range(1, line_count + 1)]
