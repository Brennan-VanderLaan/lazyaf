"""
ROUND-TRIP tests for 12.5 AGENT STEPS on the control layer - real Docker,
real named volume, the REAL steps API over real HTTP.

Two things are proven here that no unit test can prove:

1. SECRET CONTAINMENT (design 1.4, cross-agent #6). A step that declares
   `secret_environment` runs in a REAL container whose `docker inspect`
   output - env, labels, cmd - contains the secret value NOWHERE, while the
   step process itself receives it (the in-container executor merges the
   config file's `environment` before Popen). Both config files land in
   /workspace/.control owned 1000:1000, and both are GONE when the step
   ends. That combination is the whole security argument for the file
   channel, and it can only be observed against a real daemon.

2. THE FULL AGENT ROUND TRIP (design 2.x + 3.x). A real agent step -
   lazyaf-agent-base:dev, the mock executor, zero API cost - produces:
   logs through POST /api/steps/{id}/logs, a StepExecution driven terminal,
   StepRun.executor == "local", the mock's file operations landed in the
   workspace repo, and a StepUsage row with non-null tokens from the wrapper's
   own manifest via POST /api/steps/{id}/usage.

Docker is required (R4: fail loudly, never skip). Images are built through
scripts/build_images.py's staged-context path with the correct CONTENT_HASH
buildarg, so a test-built image carries the real content-hash label and never
poisons `python scripts/build_images.py --check`.

Addressing (DooD-safe, see tdd/integration/conftest.py): uvicorn binds
0.0.0.0:<free_port> and advertises the address a SIBLING container can reach.
"""
import io
import json
import logging
import sys
import tarfile
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

_repo_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "backend"))
sys.path.insert(0, str(_repo_root / "scripts"))

from build_images import (  # noqa: E402
    IMAGES,
    build_image,
    local_hash,
    stage_context,
    tree_hash,
)
from tdd.integration.conftest import (  # noqa: E402
    advertise_addr,
    free_port,
    start_uvicorn,
    stop_uvicorn,
)

from app.config import get_settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Pipeline, PipelineRun, Repo  # noqa: E402
from app.models.pipeline import RunStatus, StepExecution  # noqa: E402
from app.models.usage import StepUsage  # noqa: E402
from app.services.execution.local_executor import (  # noqa: E402
    AGENT_CONFIG_PATH_ENV,
    LocalExecutor,
)
from app.services.pipeline_executor import PipelineExecutor  # noqa: E402
from app.services.websocket import manager  # noqa: E402
from app.services.workspace.state_machine import generate_volume_name  # noqa: E402
import app.services.workspace_service as workspace_service_module  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

BASE_IMAGE = "lazyaf-base:dev"
AGENT_IMAGE = "lazyaf-agent-base:dev"

# A value that must never appear in `docker inspect`. Distinctive enough that
# a substring search over the whole inspect blob is meaningful.
SECRET_VALUE = "sk-ant-T2-CONTAINMENT-9f2a11c4"


def _image_spec(subdir: str):
    for entry in IMAGES:
        if entry[0] == subdir:
            return entry
    raise AssertionError(f"images/{subdir} is not declared in build_images.IMAGES")


def _ensure_image(docker_client, subdir: str, tag: str, parent_hash: str = "") -> str:
    """Build one image hash-correctly if missing OR STALE; return its hash.

    Goes through the SAME staging + hashing the build script uses, so an
    image this test builds is byte-for-byte what `build_images.py` would tag.

    Rebuilding on a hash MISMATCH (not just on absence) is load-bearing:
    this suite asserts the behaviour of the control runtime baked INTO the
    image, so a stale tag would silently test the previous commit's runtime
    and report green. The chained parent hash means a change to
    images/base/control/run.py restamps agent-base too.
    """
    _sub, _name, _parent, extras = _image_spec(subdir)
    staged = stage_context(_repo_root / "images" / subdir, extras)
    try:
        content_hash = tree_hash(staged, extra=parent_hash)
        if local_hash(docker_client, tag) != content_hash:
            build_image(docker_client, staged, tag, content_hash)
        return content_hash
    finally:
        import shutil

        shutil.rmtree(staged, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def agent_image(docker_client):
    """lazyaf-agent-base:dev (and its parent), built hash-correctly if absent."""
    base_hash = _ensure_image(docker_client, "base", BASE_IMAGE)
    _ensure_image(docker_client, "agent-base", AGENT_IMAGE, parent_hash=base_hash)

    labels = docker_client.images.get(AGENT_IMAGE).labels or {}
    assert labels.get("lazyaf.control-layer") == "1", (
        f"{AGENT_IMAGE} must inherit the control-layer capability label"
    )
    assert labels.get("lazyaf.agent-runtime") == "1", (
        f"{AGENT_IMAGE} must DECLARE lazyaf.agent-runtime=1 - the preflight "
        "assertion that turns a wrong image into one clear message"
    )
    return AGENT_IMAGE


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
    db_path = (tmp_path / "agent_step_container.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="0.0.0.0", port=port,
            log_level="warning", access_log=False, lifespan="off",
        )
    )
    server_task = await start_uvicorn(server)

    settings = get_settings()
    monkeypatch.setattr(
        settings, "container_backend_url", f"http://{advertise_addr()}:{port}"
    )

    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    executor = PipelineExecutor()
    socket = CapturingSocket()
    manager.active_connections.append(socket)

    run_ids: list[str] = []
    yield SimpleNamespace(
        factory=factory, executor=executor, socket=socket,
        run_ids=run_ids, docker=docker_client, port=port,
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
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="agent-step-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="agent-step-pipeline",
            steps=json.dumps(steps),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        await db.refresh(repo)
        await db.refresh(pipeline)
        return repo, pipeline


async def fetch_run(env, run_id: str) -> PipelineRun:
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


# -----------------------------------------------------------------------------
# 1. Secret containment against a REAL container
# -----------------------------------------------------------------------------

class TestSecretContainmentRealDocker:
    async def test_secret_reaches_the_step_but_not_docker_inspect(
        self, env, docker_client, named_volume, monkeypatch
    ):
        """The whole security argument for the file channel, observed.

        The step echoes its own $ANTHROPIC_API_KEY (proving the value
        arrived) while the container's inspect output - env, labels, cmd -
        contains it nowhere.

        The `env` fixture is here for its LIVE backend only: the runtime's
        POSTs answer 401 (no StepExecution row backs this hand-built
        dispatch) and 4xx is not retried, so the step runs promptly instead
        of burning the patient status budget against a dead address.
        """
        executor = LocalExecutor(docker_client)

        captured: dict = {}

        # DockerClient.containers is a PROPERTY returning a fresh collection
        # per access, so the spy goes on the class - patching one instance
        # would never be seen by the executor.
        from docker.models.containers import ContainerCollection

        real_create = ContainerCollection.create

        def spy_create(collection, image, **kwargs):
            container = real_create(collection, image, **kwargs)
            container.reload()  # inspect BEFORE the executor removes it
            captured["attrs"] = json.loads(json.dumps(container.attrs))
            return container

        monkeypatch.setattr(ContainerCollection, "create", spy_create)

        import app.services.execution.local_executor as le_mod

        real_build = le_mod.build_control_archive

        def spy_build(files):
            data = real_build(files)
            captured["tar"] = data
            return data

        monkeypatch.setattr(le_mod, "build_control_archive", spy_build)

        run_id, step_run_id = str(uuid4()), str(uuid4())
        execution_id = str(uuid4())
        step_config = {
            "type": "script",
            "image": AGENT_IMAGE,
            "timeout": 90,
            "command": (
                'echo "KEY_SEEN=$ANTHROPIC_API_KEY"; '
                'ls -n /workspace/.control'
            ),
            "secret_environment": {"ANTHROPIC_API_KEY": SECRET_VALUE},
            "agent": {
                "agent": "mock",
                "prompt": "unused - this step runs a plain command",
                "repo_id": "r1",
                "workdir": "/workspace/repo",
                "base_branch": "main",
                "branch": "lazyaf/t2",
                "remote_url": "http://backend:8000/git/r1.git",
                "commit_enabled": False,
                "push": False,
                "mock_config": {"exit_code": 0},
            },
        }
        context = {
            "pipeline_run_id": run_id,
            "step_run_id": step_run_id,
            "step_index": 0,
            "execution_key": f"{run_id}:0:{step_run_id}",
            "workspace_volume": named_volume,
            "control_mode": True,
            "step_execution_id": execution_id,
            "step_auth_token": "unused-token-backend-unreachable",
        }

        events = [e async for e in executor.execute_step(step_config, context)]
        result = events[-1]
        tail = "\n".join(result.get("log_tail") or [])

        # --- the secret ARRIVED at the step process ----------------------
        assert f"KEY_SEEN={SECRET_VALUE}" in tail, tail

        # --- ...and is NOWHERE in `docker inspect` -----------------------
        inspect_blob = json.dumps(captured["attrs"])
        assert SECRET_VALUE not in inspect_blob, (
            "the API key leaked into the container's inspectable state"
        )
        env_entries = captured["attrs"]["Config"]["Env"]
        assert not any(e.startswith("ANTHROPIC_API_KEY=") for e in env_entries)
        # The step token must not be there either (12.3 contract, unchanged)
        assert "unused-token-backend-unreachable" not in inspect_blob

        # --- ...but IS in the put_archive tar ----------------------------
        with tarfile.open(fileobj=io.BytesIO(captured["tar"])) as tar:
            names = tar.getnames()
            payload = json.load(
                tar.extractfile(tar.getmember(f".control/{execution_id}.json"))
            )
        assert payload["environment"]["ANTHROPIC_API_KEY"] == SECRET_VALUE
        assert f".control/agent.{execution_id}.json" in names
        assert payload["environment"][AGENT_CONFIG_PATH_ENV] == (
            f"/workspace/.control/agent.{execution_id}.json"
        )

        # --- both files landed 1000:1000 and are GONE afterwards ---------
        # `ls -n` prints numeric uid/gid; the step config is already deleted
        # by then (consume-once), the agent config is not yet consumed.
        assert f"agent.{execution_id}.json" in tail
        agent_line = [
            line for line in tail.splitlines()
            if f"agent.{execution_id}.json" in line and line.startswith("-")
        ]
        assert agent_line, tail
        fields = agent_line[0].split()
        assert fields[2] == "1000" and fields[3] == "1000", agent_line[0]

        leftovers = docker_client.containers.run(
            AGENT_IMAGE,
            command=["sh", "-c", "ls -A /workspace/.control"],
            volumes={named_volume: {"bind": "/workspace", "mode": "rw"}},
            remove=True,
            environment={"LAZYAF_CONTROL": "0"},
        ).decode()
        assert execution_id not in leftovers, (
            f"config files survived the step: {leftovers!r}"
        )

    async def test_secret_without_control_mode_never_spawns_a_container(
        self, docker_client, named_volume
    ):
        """A secret may not DOWNGRADE onto the stdout path, where container
        env is the only channel - and no container may be created at all."""
        executor = LocalExecutor(docker_client)
        run_id, step_run_id = str(uuid4()), str(uuid4())
        before = len(docker_client.containers.list(all=True))

        events = [
            e
            async for e in executor.execute_step(
                {
                    "type": "script",
                    "image": AGENT_IMAGE,
                    "timeout": 30,
                    "command": "true",
                    "secret_environment": {"ANTHROPIC_API_KEY": SECRET_VALUE},
                },
                {
                    "pipeline_run_id": run_id,
                    "step_run_id": step_run_id,
                    "step_index": 0,
                    "execution_key": f"{run_id}:0:{step_run_id}",
                    "workspace_volume": named_volume,
                    "control_mode": False,
                },
            )
        ]

        assert events[-1]["status"] == "failed"
        assert "secrets require control mode" in events[-1]["error"]
        assert SECRET_VALUE not in events[-1]["error"]
        assert len(docker_client.containers.list(all=True)) == before


# -----------------------------------------------------------------------------
# 2. The full agent round trip (mock agent, zero cost)
# -----------------------------------------------------------------------------

class TestAgentStepRoundTrip:
    async def test_mock_agent_step_produces_logs_files_and_usage(self, env):
        """The 12.5 exit-gate round trip on a real ephemeral container."""
        marker = f"agent-roundtrip-{uuid4().hex[:8]}"
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            {
                "name": "MockAgent",
                "type": "agent",
                "timeout": 300,
                "config": {
                    "agent": "mock",
                    "commit": False,
                    "title": marker,
                    "description": "T2 round trip",
                    "mock_config": {
                        "response_mode": "streaming",
                        "delay_ms": 1,
                        "file_operations": [
                            {
                                "action": "create",
                                "path": ".lazyaf-t2/agent-ran",
                                "content": f"{marker}\n",
                            }
                        ],
                        "output_events": [
                            {"type": "content", "text": f"{marker}-thinking"},
                            {"type": "complete", "text": f"{marker}-done"},
                        ],
                        "exit_code": 0,
                    },
                },
            },
            {
                # The agent's file operations are asserted from INSIDE the
                # run: the workspace volume is removed at completion, so a
                # post-hoc mount would inspect a fresh EMPTY volume. This
                # step also proves a SCRIPT step gets its own usage row.
                "name": "VerifyAgentWork",
                "type": "script",
                "timeout": 120,
                "config": {
                    "command": "cat /workspace/repo/.lazyaf-t2/agent-ran",
                    "image": BASE_IMAGE,
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
            f"run failed: {[(s.step_index, s.status, s.error, s.logs) for s in run.step_runs]}"
        )
        step, verify_step = sorted(run.step_runs, key=lambda s: s.step_index)

        # --- routing observability (R1) ----------------------------------
        assert step.executor == "local"
        assert step.status == RunStatus.PASSED.value

        # --- logs arrived through POST /api/steps/{id}/logs ---------------
        assert f"{marker}-thinking" in step.logs
        assert f"{marker}-done" in step.logs
        # the wrapper announces its resolved target BEFORE invoking
        assert "[agent] agent=mock" in step.logs

        # --- StepExecution driven terminal by the runtime -----------------
        async with env.factory() as db:
            executions = {
                e.step_run_id: e
                for e in (await db.execute(select(StepExecution))).scalars().all()
            }
        assert set(executions) == {step.id, verify_step.id}
        execution = executions[step.id]
        assert execution.status == "completed"
        assert execution.exit_code == 0

        # --- the mock's file operation landed in the workspace repo -------
        # (asserted by the NEXT step, on the same volume, before cleanup)
        assert verify_step.status == RunStatus.PASSED.value
        assert marker in verify_step.logs

        # --- StepUsage rows: the agent's from the wrapper's own manifest,
        # the script step's from run.py's fallback record ------------------
        async with env.factory() as db:
            usages = {
                u.step_execution_id: u
                for u in (await db.execute(select(StepUsage))).scalars().all()
            }
        assert len(usages) == 2, (
            "every control-mode step produces a usage row from day one - "
            f"got {sorted(usages)}"
        )
        usage = usages[execution.id]
        assert usage.pipeline_run_id == run_id
        assert usage.provider == "self-hosted"  # mock
        assert usage.model == "mock"
        assert usage.cost_source == "cli-reported"
        assert usage.input_tokens is not None and usage.input_tokens > 0
        assert usage.output_tokens is not None and usage.output_tokens > 0
        # run.py owns timing and always fills it
        assert usage.wall_clock_ms >= 0
        assert usage.container_seconds is not None

        # The script step told us nothing about cost - a RECORDED FACT, not
        # a gap (M13 counts these as cost_coverage < 1.0).
        script_usage = usages[executions[verify_step.id].id]
        assert script_usage.cost_source == "unknown"
        assert script_usage.cost_usd is None
        assert script_usage.input_tokens is None
        assert script_usage.wall_clock_ms >= 0

    async def test_agent_step_on_a_non_agent_image_fails_with_one_message(
        self, env, caplog
    ):
        """The preflight assertion: an agent step pointed at a plain
        control-layer image must not reach `ModuleNotFoundError`."""
        repo, pipeline = await make_repo_and_pipeline(env.factory, [
            {
                "name": "WrongImage",
                "type": "agent",
                "timeout": 120,
                "config": {"agent": "mock", "image": BASE_IMAGE},
            },
        ])

        with caplog.at_level(
            logging.ERROR, logger="app.services.pipeline_executor"
        ):
            async with env.factory() as db:
                run = await env.executor.start_pipeline(
                    db=db, pipeline=pipeline, repo=repo
                )
                run_id = run.id
            env.run_ids.append(run_id)
            await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.FAILED.value
        # The run never dispatched a step: preflight refused it up front.
        assert run.step_runs == []
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "lazyaf.agent-runtime" in blob, blob
        assert "lazyaf-agent-base:dev" in blob  # names the way OUT
        assert "ModuleNotFoundError" not in blob
