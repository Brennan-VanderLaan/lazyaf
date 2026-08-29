"""
Parallel control-mode fan-out (12.3 adversarial fix 3), real Docker + the
REAL steps API over live HTTP.

The guarded landmine: two control-mode steps of ONE run share ONE workspace
volume. With a single well-known config filename, parallel entry steps
clobbered each other's step config - one container would execute the OTHER
step's command and report under the other step's identity. The fix is the
per-step config path (.control/<step_execution_id>.json + CONFIG_PATH), and
this test drives it end-to-end:

    graph pipeline, TWO parallel control-mode entry steps (lazyaf-base:dev)
    -> each in-container runtime reads ITS OWN config, runs ITS OWN echo
       marker, POSTs to /api/steps/* with ITS OWN token
    -> each StepRun's logs carry exactly its own marker (never the
       sibling's), terminal states are correct in both vocabularies.

The lazyaf-base:dev image is built hash-correctly if missing OR STALE
(content-hash label mismatch vs the images/base tree) - this suite depends
on the current control runtime honoring CONFIG_PATH, so a stale local image
must not silently test yesterday's runtime.

Docker is required (R4: fail loudly, never skip). Addressing is DooD-safe
(see tdd/integration/conftest.py): uvicorn binds 0.0.0.0:<free_port> and
advertises an address a SIBLING container can reach.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

# Add backend (app imports), scripts (build_images) and the repo root
# (tdd.integration.conftest helpers) to path
_repo_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "backend"))
sys.path.insert(0, str(_repo_root / "scripts"))

from build_images import HASH_LABEL, build_image, tree_hash
from tdd.integration.conftest import advertise_addr, free_port, start_uvicorn, stop_uvicorn

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import RunStatus, StepExecution, StepExecutionStatus
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

CONTROL_IMAGE = "lazyaf-base:dev"
IMAGES_BASE_DIR = _repo_root / "images" / "base"


@pytest.fixture(scope="module", autouse=True)
def control_image(docker_client):
    """The real base image - built HASH-CORRECTLY if missing OR stale.

    Unlike a build-if-missing fixture, a local image whose content-hash
    label no longer matches the images/base tree is REBUILT: this suite
    exercises the per-step CONFIG_PATH contract, which lives in the image's
    control runtime - testing a stale runtime would prove nothing."""
    expected_hash = tree_hash(IMAGES_BASE_DIR)
    try:
        image = docker_client.images.get(CONTROL_IMAGE)
        current_hash = (image.labels or {}).get(HASH_LABEL)
    except docker_sdk.errors.ImageNotFound:
        current_hash = None
    if current_hash != expected_hash:
        build_image(docker_client, IMAGES_BASE_DIR, CONTROL_IMAGE, expected_hash)
        image = docker_client.images.get(CONTROL_IMAGE)
    labels = image.labels or {}
    assert labels.get("lazyaf.control-layer") == "1", (
        f"{CONTROL_IMAGE} lacks lazyaf.control-layer=1 - "
        "rebuild with scripts/build_images.py"
    )
    return CONTROL_IMAGE


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
    db_path = (tmp_path / "parallel_control.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # The REAL app + steps router, DB rerouted to this test's engine. Each
    # request gets its own session (concurrent POSTs from BOTH containers).
    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Serve over real HTTP on the SAME event loop (WS manager singleton is
    # shared with the in-process pipeline executor). Lifespan off: app
    # startup (runner pool, orphan audit, pre-pull) is not under test.
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="0.0.0.0", port=port,
            log_level="warning", access_log=False, lifespan="off",
        )
    )
    server_task = await start_uvicorn(server)

    # The config file's backend_url must be reachable FROM the containers -
    # SIBLINGS on the daemon, not children (DooD-safe: see conftest).
    settings = get_settings()
    monkeypatch.setattr(
        settings, "container_backend_url", f"http://{advertise_addr()}:{port}"
    )

    # Stub ONLY the git clone (population has its own suites).
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
    await stop_uvicorn(server, server_task)
    app.dependency_overrides.clear()
    for run_id in run_ids:
        try:
            docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
        except docker_sdk.errors.NotFound:
            pass
    await engine.dispose()


async def fetch_run(env, run_id: str) -> PipelineRun:
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


async def fetch_executions(env) -> list[StepExecution]:
    async with env.factory() as db:
        result = await db.execute(select(StepExecution))
        return list(result.scalars().all())


class TestParallelControlSteps:
    async def test_parallel_entry_steps_keep_their_own_configs_and_logs(self, env):
        """Two parallel control-mode entry steps of ONE run, ONE shared
        workspace volume: each step's logs carry ITS OWN echo marker and
        never the sibling's, and terminal states are correct end-to-end -
        the per-step config path (fan-out collision guard) at work."""
        token = uuid4().hex[:8]
        marker_alpha = f"parallel-{token}-alpha"
        marker_beta = f"parallel-{token}-beta"

        graph = {
            "steps": {
                "alpha": {
                    "name": "Alpha",
                    "type": "script",
                    "timeout": 120,
                    "config": {
                        "command": f"echo {marker_alpha}",
                        "image": CONTROL_IMAGE,
                    },
                },
                "beta": {
                    "name": "Beta",
                    "type": "script",
                    "timeout": 120,
                    "config": {
                        "command": f"echo {marker_beta}",
                        "image": CONTROL_IMAGE,
                    },
                },
            },
            "edges": [],
            "entry_points": ["alpha", "beta"],
        }

        async with env.factory() as db:
            repo = Repo(
                id=str(uuid4()),
                name="parallel-control-repo",
                default_branch="main",
                is_ingested=True,
            )
            pipeline = Pipeline(
                id=str(uuid4()),
                repo_id=repo.id,
                name="parallel-control-pipeline",
                steps="[]",
                steps_graph=json.dumps(graph),
            )
            db.add(repo)
            db.add(pipeline)
            await db.commit()
            await db.refresh(repo)
            await db.refresh(pipeline)

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value, (
            f"run failed: "
            f"{[(s.step_id, s.status, s.error, s.logs) for s in run.step_runs]}"
        )
        by_id = {s.step_id: s for s in run.step_runs}
        assert set(by_id) == {"alpha", "beta"}
        alpha, beta = by_id["alpha"], by_id["beta"]

        # Terminal states: RunStatus vocabulary from _finish_local_step
        for step_run in (alpha, beta):
            assert step_run.executor == "local"
            assert step_run.status == RunStatus.PASSED.value
            assert step_run.completed_at is not None
            assert "exit code: 0" in step_run.logs

        # THE fan-out collision guard: each step's logs carry exactly its
        # OWN marker - a clobbered config would run one command twice and
        # land a marker in the sibling's logs (or duplicate it in one).
        assert alpha.logs.count(marker_alpha) == 1, alpha.logs
        assert marker_beta not in alpha.logs, alpha.logs
        assert beta.logs.count(marker_beta) == 1, beta.logs
        assert marker_alpha not in beta.logs, beta.logs

        # StepExecution telemetry: one row per step, driven terminal by the
        # runtime's own POSTs (never-reported reconciliation did NOT fire)
        executions = {e.step_run_id: e for e in await fetch_executions(env)}
        assert set(executions) == {alpha.id, beta.id}
        for step_run in (alpha, beta):
            execution = executions[step_run.id]
            assert execution.status == StepExecutionStatus.COMPLETED.value
            assert execution.exit_code == 0
            assert execution.started_at is not None
            assert execution.completed_at is not None
            assert step_run.error is None

        # And the logs arrived via the router as step_log_batch frames
        # addressed per step (one reporting path per datum, R3).
        batch_frames = env.socket.of_type("step_log_batch")
        alpha_lines = [
            line
            for frame in batch_frames
            if frame["step_index"] == alpha.step_index
            for line in frame["lines"]
        ]
        beta_lines = [
            line
            for frame in batch_frames
            if frame["step_index"] == beta.step_index
            for line in frame["lines"]
        ]
        assert marker_alpha in alpha_lines
        assert marker_beta in beta_lines
