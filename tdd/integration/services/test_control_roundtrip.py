"""
ROUND-TRIP test for the 12.3 control-mode reporting path (R1/R3), real
Docker + the REAL steps API served over real HTTP:

    lazyaf-base:dev container -> in-container control runtime
    -> POST /api/steps/{id}/status|logs (Bearer JWT, live uvicorn server)
    -> StepRun rows + StepExecution telemetry in the DB
    -> the SAME step_log/step_log_batch + step_update WS frames the
       frontend already consumes, through the REAL WebSocket manager
       (capturing transport, never an AsyncMock - R6)

and, per the ownership table, proof of NO DOUBLE LOGGING: the stdout-stream
consumer must persist nothing in control mode (each echoed marker lands in
StepRun.logs exactly once - via the router), while terminal StepRun state
still comes from the executor's result event through _finish_local_step
(RunStatus vocabulary, never the router's "completed").

Also covers the consume-once config contract end-to-end: a second
control-mode step asserts NO config json survives under /workspace/.control
when the user command runs - the runtime deleted its own config first
(naming-agnostic: holds for the per-step <step_execution_id>.json path and
the legacy step_config.json alike).

Docker is required (R4: fail loudly, never skip). The lazyaf-base:dev image
is built from images/base if missing - through scripts/build_images.py's
build_image with the correct CONTENT_HASH buildarg, so a test-built image
carries the real content-hash label and never poisons
`python scripts/build_images.py --check`.

Addressing (DooD-safe, see tdd/integration/conftest.py): the test binds
uvicorn on 0.0.0.0:<free_port> and advertises the address a SIBLING
container can reach - this container's own IP when the suite itself runs
inside a container (the CI path), host.docker.internal on the host (a
Linux-Engine host may need `--add-host host.docker.internal:host-gateway`;
the container path is the CI path).
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

from build_images import build_image, tree_hash
from tdd.shared.factories.pipelines import make_repo_and_graph_pipeline  # noqa: E402
from tdd.integration.conftest import advertise_addr, free_port, start_uvicorn, stop_uvicorn

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import Pipeline, PipelineRun, Repo, StepRun
from app.models.pipeline import RunStatus, StepExecution
from app.services.pipeline_executor import PipelineExecutor
from app.services.websocket import manager
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

CONTROL_IMAGE = "lazyaf-base:dev"
IMAGES_BASE_DIR = _repo_root / "images" / "base"


@pytest.fixture(scope="module", autouse=True)
def control_image(docker_client):
    """The real base image - built HASH-CORRECTLY if missing.

    Goes through scripts/build_images.py's build_image with the computed
    tree hash as the CONTENT_HASH buildarg (base chains no parent hash), so
    an image this test builds is byte-for-byte what the build script would
    tag and never leaves `build_images.py --check` reporting stale."""
    try:
        image = docker_client.images.get(CONTROL_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        content_hash = tree_hash(IMAGES_BASE_DIR)
        build_image(docker_client, IMAGES_BASE_DIR, CONTROL_IMAGE, content_hash)
        image = docker_client.images.get(CONTROL_IMAGE)
    labels = image.labels or {}
    assert "lazyaf.control-layer" in labels, (
        f"{CONTROL_IMAGE} lacks the control-layer capability label - "
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
    db_path = (tmp_path / "control_roundtrip.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # The REAL app + steps router, DB rerouted to this test's engine. Each
    # request gets its own session (concurrent POSTs from the container).
    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Serve over real HTTP on the SAME event loop (WS manager singleton is
    # shared with the in-process pipeline executor). Lifespan off: the app
    # startup (runner pool, orphan audit, pre-pull) is not under test.
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="0.0.0.0", port=port,
            log_level="warning", access_log=False, lifespan="off",
        )
    )
    server_task = await start_uvicorn(server)

    # The config file's backend_url must be reachable FROM the container -
    # a SIBLING on the daemon, not a child (DooD-safe: see conftest).
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
        factory, steps, name="control-roundtrip-pipeline", repo_name="control-roundtrip-repo",
    )


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


class TestControlModeRoundTrip:
    async def test_container_posts_land_in_step_run_and_ws(self, env):
        """The named 12.3 exit-gate round trip, two control-mode steps:

        step 1 echoes markers -> the control runtime POSTs them -> the
        router (sole writer) lands them in StepRun.logs + step_log frames;
        step 2 proves consume-once config deletion inside the container.
        """
        marker = f"control-roundtrip-{uuid4().hex[:8]}"
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            {
                "name": "ControlEcho",
                "type": "script",
                "timeout": 120,
                "config": {
                    "command": f"echo {marker}-one && echo {marker}-two",
                    "image": CONTROL_IMAGE,
                },
            },
            {
                "name": "ConfigConsumed",
                "type": "script",
                "timeout": 120,
                "config": {
                    # Naming-agnostic consume-once probe: no config json at
                    # all may survive under /workspace/.control (covers the
                    # per-step <step_execution_id>.json path and the legacy
                    # step_config.json alike).
                    "command": (
                        "[ -z \"$(find /workspace/.control -name '*.json' "
                        "2>/dev/null)\" ] && echo config-consumed-ok"
                    ),
                    "image": CONTROL_IMAGE,
                },
            },
        ])

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value, (
            f"run failed: {[ (s.step_index, s.status, s.error, s.logs) for s in run.step_runs ]}"
        )
        steps = sorted(run.step_runs, key=lambda s: s.step_index)
        echo_step, consume_step = steps

        # --- Executor observability (R1) + terminal ownership -------------
        assert echo_step.executor == "local"
        # RunStatus vocabulary from _finish_local_step - NOT the router's
        # StepExecution vocabulary ("completed")
        assert echo_step.status == RunStatus.PASSED.value
        assert echo_step.completed_at is not None

        # --- StepExecution row: created at dispatch, driven by the POSTs --
        executions = {e.step_run_id: e for e in await fetch_executions(env)}
        assert set(executions) == {echo_step.id, consume_step.id}
        echo_execution = executions[echo_step.id]
        assert echo_execution.status == "completed"  # POST /status flowed
        assert echo_execution.exit_code == 0
        assert echo_execution.started_at is not None
        assert echo_execution.completed_at is not None

        # --- Logs arrived via the router, and EXACTLY ONCE (no double
        # logging: the stdout-stream consumer drops in control mode) -------
        for m in (f"{marker}-one", f"{marker}-two"):
            assert echo_step.logs.count(m) == 1, (
                f"{m!r} appears {echo_step.logs.count(m)}x in StepRun.logs:\n"
                f"{echo_step.logs}"
            )

        # Control mode emits ONE step_log_batch frame per /logs POST; the
        # frontend consumes step_log_batch alongside step_log, so count each
        # marker exactly once across BOTH frame types (contract-tolerant).
        log_frames = [
            (p, [p["line"]]) for p in env.socket.of_type("step_log")
        ] + [
            (p, list(p["lines"])) for p in env.socket.of_type("step_log_batch")
        ]
        ws_lines = [line for _, lines in log_frames for line in lines]
        assert ws_lines.count(f"{marker}-one") == 1
        assert ws_lines.count(f"{marker}-two") == 1
        # Frames carry the run/step addressing the UI consumes
        marker_frames = [
            p for p, lines in log_frames if f"{marker}-one" in lines
        ]
        assert marker_frames[0]["pipeline_run_id"] == run_id
        assert marker_frames[0]["step_index"] == 0

        # --- step_update frames: `running` from the router's bridge,
        # terminal PASSED from _finish_local_step ---------------------------
        step0_statuses = [
            p["status"] for p in env.socket.of_type("step_update")
            if p["step_index"] == 0
        ]
        assert "running" in step0_statuses
        assert step0_statuses[-1] == RunStatus.PASSED.value
        # The executor's own preparing event was dropped by the consumer
        assert "preparing" not in step0_statuses

        # Legacy broadcast channels still serve existing UI consumers
        assert any(
            s["status"] == RunStatus.PASSED.value
            for s in env.socket.of_type("pipeline_run_status")
        )

        # --- Consume-once config delete, proven INSIDE the container ------
        assert "config-consumed-ok" in consume_step.logs
        assert consume_step.status == RunStatus.PASSED.value
