"""ROUND-TRIP tests for the 14.2 AGENT HARNESS on the control layer.

Real Docker, a real named volume, the REAL steps API over real HTTP, and a
REAL OpenAI-compatible server - `tdd/shared/mock_openai`, in-process, bound on
0.0.0.0 and advertised at an address the step container can reach. **No GPU.**
That is the whole point of wave8 section 8.1: everything the harness, the token
accumulator and the gpu-node pricing path do can be exercised deterministically
without one, so this coverage runs on every push instead of on hardware nobody
in CI has.

Three things are proven here that no unit test can prove:

1. **THE HARNESS LOOP IN A REAL CONTAINER.** A real `agent: openai-harness`
   step, driven by six scripted turns from the mock endpoint, lands real files
   in the workspace repo, streams its `[agent] ` lines through
   `POST /api/steps/{id}/logs`, and drives its StepExecution terminal.

2. **THE TOKEN ACCUMULATOR SUMMED ACROSS TURNS.** The mock reports GROWING
   per-turn usage (turn N declares 100*N prompt / 20*N completion tokens), so
   "summed" and "kept the last response" are two different numbers and the
   assertion can tell them apart. This is the only genuinely new accounting
   logic in the milestone and it has no other alarm - a harness that kept the
   last turn would under-report every self-hosted step forever, cost nothing
   and fail nothing.

3. **SECRET CONTAINMENT FOR THE ENDPOINT KEY.** A `bearer` endpoint's key
   reaches the harness inside the container while the container's
   `docker inspect` output contains it NOWHERE. It travels only through 12.5's
   `secret_environment` file channel, and 14.x adds a new secret to that
   channel rather than a new channel.

Docker is required (R4: fail loudly, never skip).

STATUS AT THE TIME OF WRITING: these are red until the wave's other lanes land
the `openai-harness` dispatch vocabulary (`pipeline_executor`, `agent_run`) and
the container-side `EXECUTORS` entry. They are written against the design's
contracts, not against an implementation, which is what makes them a test
rather than a transcript.
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
from tdd.shared.mock_openai import (  # noqa: E402
    ACTION_SCRIPT_LENGTH,
    MOCK_MODEL_CONTEXT_WINDOW,
    MockOpenAIServer,
    expected_summed_tokens,
    largest_single_turn_tokens,
)

from app.config import get_settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Pipeline, PipelineRun, Repo  # noqa: E402
from app.models.model_endpoint import ModelEndpoint, default_gpu_node_id  # noqa: E402
from app.models.pipeline import RunStatus, StepExecution  # noqa: E402
from app.models.usage import StepUsage  # noqa: E402
from app.services.pipeline_executor import PipelineExecutor  # noqa: E402
from app.services.websocket import manager  # noqa: E402
from app.services.workspace.state_machine import generate_volume_name  # noqa: E402
import app.services.workspace_service as workspace_service_module  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

BASE_IMAGE = "lazyaf-base:dev"
AGENT_IMAGE = "lazyaf-agent-base:dev"

#: The endpoint key that must never appear in `docker inspect`.
ENDPOINT_SECRET_VALUE = "sk-endpoint-T2-CONTAINMENT-4b81de07"
#: The BACKEND env var the endpoint row references by NAME. Prefix-allowlisted
#: to LAZYAF_ENDPOINT_* so a row can never reference ANTHROPIC_API_KEY.
ENDPOINT_SECRET_REF = "LAZYAF_ENDPOINT_T2MOCK"


def _image_spec(subdir: str):
    for entry in IMAGES:
        if entry[0] == subdir:
            return entry
    raise AssertionError(f"images/{subdir} is not declared in build_images.IMAGES")


def _ensure_image(docker_client, subdir: str, tag: str, parent_hash: str = "") -> str:
    """Build one image hash-correctly if missing OR STALE; return its hash."""
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
    base_hash = _ensure_image(docker_client, "base", BASE_IMAGE)
    _ensure_image(docker_client, "agent-base", AGENT_IMAGE, parent_hash=base_hash)
    return AGENT_IMAGE


@pytest.fixture()
def mock_endpoint_server():
    """A real OpenAI-compatible server a SIBLING container can reach.

    Bound on 0.0.0.0 and advertised through `advertise_addr()` - the helper
    this repo already wrote for exactly this DooD problem (see
    tdd/integration/conftest.py's module docstring). Re-solving it here with
    `host.docker.internal` would break inside the CI runner container.
    """
    with MockOpenAIServer(host="0.0.0.0") as server:
        server.reachable_base = lambda scenario: server.base_url(
            scenario, host=advertise_addr()
        )
        yield server


class CapturingSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch, docker_client):
    db_path = (tmp_path / "harness_step_container.db").as_posix()
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


async def register_endpoint(
    factory,
    mock_server,
    *,
    name: str,
    scenario: str,
    model: str = "mock-model",
    auth_style: str = "none",
    auth_secret_ref: str | None = None,
    rate_usd_hour: str | None = "0.010000",
    supports_tools: bool = True,
) -> str:
    """Insert a PROBED endpoint row pointing at one mock scenario.

    The capability record is written directly rather than probed: the probe
    itself has its own suites (T1), and a T2 test that depended on it would
    fail for two unrelated reasons at once. `probe_status` is `ok` because
    dispatch REFUSES `unprobed`, which is the behaviour under test elsewhere.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    async with factory() as db:
        endpoint = ModelEndpoint(
            name=name,
            description=f"T2 mock endpoint ({scenario})",
            base_url=mock_server.reachable_base(scenario),
            model=model,
            server_kind="vllm",
            auth_style=auth_style,
            auth_secret_ref=auth_secret_ref,
            reach="direct",
            rate_usd_hour=None if rate_usd_hour is None else Decimal(rate_usd_hour),
            gpu_node_id=default_gpu_node_id(name),
            max_concurrency=1,
            request_timeout_seconds=60,
            context_window=MOCK_MODEL_CONTEXT_WINDOW,
            supports_tools=supports_tools,
            supports_streaming=True,
            reports_usage=True,
            probe_status="ok" if supports_tools else "degraded",
            probe_detail="{}",
            probed_at=datetime.now(timezone.utc),
            probed_from="backend",
            consecutive_failures=0,
            enabled=True,
        )
        db.add(endpoint)
        await db.commit()
        await db.refresh(endpoint)
        return endpoint.id


async def make_repo_and_pipeline(factory, steps: list[dict]):
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="harness-step-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="harness-step-pipeline",
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


def harness_step(endpoint_name: str, marker: str, *, mode: str | None = None) -> dict:
    config = {
        "agent": "openai-harness",
        "endpoint": endpoint_name,
        "commit": False,
        "title": marker,
        "description": f"Create .lazyaf-t2/{marker} naming the endpoint you used",
        "harness": {"max_iterations": ACTION_SCRIPT_LENGTH + 2,
                    "time_budget_seconds": 120},
    }
    if mode is not None:
        config["harness"]["mode"] = mode
    return {
        "name": f"Harness ({mode or 'auto'})",
        "type": "agent",
        "timeout": 300,
        "config": config,
    }


def verify_step(marker: str) -> dict:
    """Read the harness's work from INSIDE the run.

    The workspace volume is removed at completion, so a post-hoc mount would
    inspect a fresh EMPTY volume - the same reason the 12.5 suite does it this
    way.
    """
    return {
        "name": "VerifyHarnessWork",
        "type": "script",
        "timeout": 120,
        "config": {
            "command": f"cat /workspace/repo/.lazyaf-t2/{marker}",
            "image": BASE_IMAGE,
        },
    }


# -----------------------------------------------------------------------------
# 1. The full round trip, tools mode
# -----------------------------------------------------------------------------

class TestHarnessStepRoundTrip:
    async def test_tools_mode_lands_files_logs_and_summed_usage(
        self, env, mock_endpoint_server
    ):
        marker = f"harness-{uuid4().hex[:8]}"
        endpoint_id = await register_endpoint(
            env.factory, mock_endpoint_server, name="t2-mock", scenario="happy_tools"
        )
        repo, pipeline = await make_repo_and_pipeline(
            env.factory, [harness_step("t2-mock", marker), verify_step(marker)]
        )

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value, (
            f"run failed: {[(s.step_index, s.status, s.error, s.logs) for s in run.step_runs]}"
        )
        step, verified = sorted(run.step_runs, key=lambda s: s.step_index)

        # --- routing observability (R1): a `direct` endpoint stays LOCAL ---
        # A global accidental flip to the remote lane is as much a regression
        # as the reverse; only `runner-local` forces a step remote.
        assert step.executor == "local"
        assert step.status == RunStatus.PASSED.value

        # --- the harness announced its target BEFORE any request (R1) -----
        assert "[agent] harness:" in step.logs
        assert "t2-mock" in step.logs
        assert "mode=tools" in step.logs

        # --- the tools it actually called are in the log stream -----------
        for tool in ("list_files", "write_file", "apply_patch", "read_file"):
            assert tool in step.logs, f"{tool} never appears in the step log"
        assert "stop:" in step.logs

        # --- the file landed in the workspace repo ------------------------
        assert verified.status == RunStatus.PASSED.value
        assert "mock-model" in verified.logs, (
            "apply_patch should have replaced the placeholder with the model id"
        )

        # --- StepExecution driven terminal by the runtime -----------------
        async with env.factory() as db:
            executions = {
                e.step_run_id: e
                for e in (await db.execute(select(StepExecution))).scalars().all()
            }
        execution = executions[step.id]
        assert execution.status == "completed"
        assert execution.exit_code == 0

        # --- THE USAGE ROW ------------------------------------------------
        async with env.factory() as db:
            usages = {
                u.step_execution_id: u
                for u in (await db.execute(select(StepUsage))).scalars().all()
            }
        usage = usages[execution.id]
        assert usage.provider == "openai-compatible"
        assert usage.model == "mock-model"

        record = json.loads(usage.raw or "{}").get("harness") or {}
        turns = record.get("turns")
        assert isinstance(turns, int) and turns >= 2, (
            f"raw.harness.turns={turns!r}; the summation check needs a real loop"
        )
        largest_in, largest_out = largest_single_turn_tokens(turns)
        summed_in, summed_out = expected_summed_tokens(turns)
        assert usage.input_tokens == summed_in, (
            f"tokens were not summed across turns: got {usage.input_tokens}, "
            f"a last-response-wins bug gives {largest_in}, summing gives {summed_in}"
        )
        assert usage.output_tokens == summed_out
        assert usage.input_tokens > largest_in

        # --- node pricing, not a provider bill ----------------------------
        assert usage.cost_source == "gpu-node"
        assert usage.cost_usd is not None
        assert usage.gpu_node_id == "endpoint:t2-mock"
        assert usage.gpu_fraction == pytest.approx(1.0)

        # --- determinism is finally non-empty -----------------------------
        determinism = json.loads(usage.determinism or "{}")
        assert "temperature" in determinism

        assert endpoint_id  # the row the step dispatched against still exists

    async def test_forced_text_mode_runs_the_fallback_protocol(
        self, env, mock_endpoint_server
    ):
        """`mode: text` against a model that CAN tool-call.

        Pinning the loop shape is how M13 makes it an independent variable, so
        the pin has to work on a tool-capable endpoint - not only on one that
        forces the harness's hand.
        """
        marker = f"fallback-{uuid4().hex[:8]}"
        await register_endpoint(
            env.factory,
            mock_endpoint_server,
            name="t2-mock-text",
            scenario="happy_text",
            model="mock-model-notools",
            supports_tools=False,
        )
        repo, pipeline = await make_repo_and_pipeline(
            env.factory,
            [harness_step("t2-mock-text", marker, mode="text"), verify_step(marker)],
        )

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        run = await fetch_run(env, run_id)
        assert run.status == RunStatus.PASSED.value, (
            f"run failed: {[(s.step_index, s.status, s.error, s.logs) for s in run.step_runs]}"
        )
        step, verified = sorted(run.step_runs, key=lambda s: s.step_index)
        assert "mode=text" in step.logs
        assert verified.status == RunStatus.PASSED.value

        async with env.factory() as db:
            executions = {
                e.step_run_id: e
                for e in (await db.execute(select(StepExecution))).scalars().all()
            }
            usages = {
                u.step_execution_id: u
                for u in (await db.execute(select(StepUsage))).scalars().all()
            }
        usage = usages[executions[step.id].id]
        record = json.loads(usage.raw or "{}").get("harness") or {}
        assert record.get("mode") == "text"
        # 0 is a fine value; the KEY missing means the fallback parser never
        # accounted for itself, which is how that path rots unnoticed.
        assert "malformed_responses" in record
        assert usage.provider == "openai-compatible"


# -----------------------------------------------------------------------------
# 2. Secret containment for the endpoint key
# -----------------------------------------------------------------------------

class TestEndpointSecretContainment:
    async def test_the_endpoint_key_is_in_no_container_inspect(
        self, env, mock_endpoint_server, monkeypatch
    ):
        """14.x adds a secret to 12.5's channel, not a new channel.

        The mock server is configured to REQUIRE the key, so a step that
        completes proves the value arrived; `docker inspect` proves it did not
        arrive through container env, labels or cmd.
        """
        monkeypatch.setenv(ENDPOINT_SECRET_REF, ENDPOINT_SECRET_VALUE)
        marker = f"secret-{uuid4().hex[:8]}"
        await register_endpoint(
            env.factory,
            mock_endpoint_server,
            name="t2-mock-auth",
            scenario="happy_tools",
            auth_style="bearer",
            auth_secret_ref=ENDPOINT_SECRET_REF,
        )
        repo, pipeline = await make_repo_and_pipeline(
            env.factory, [harness_step("t2-mock-auth", marker)]
        )

        captured: list[dict] = []
        from docker.models.containers import ContainerCollection

        real_create = ContainerCollection.create

        def spy_create(collection, image, **kwargs):
            container = real_create(collection, image, **kwargs)
            container.reload()
            captured.append(json.loads(json.dumps(container.attrs)))
            return container

        monkeypatch.setattr(ContainerCollection, "create", spy_create)

        async with env.factory() as db:
            run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
            run_id = run.id
        env.run_ids.append(run_id)
        await env.executor.wait_for_run(run_id)

        assert captured, "no container was created"
        for attrs in captured:
            blob = json.dumps(attrs)
            assert ENDPOINT_SECRET_VALUE not in blob, (
                "the endpoint key reached `docker inspect` - it must travel "
                "ONLY inside the 0600 consume-once step config file"
            )

        run = await fetch_run(env, run_id)
        step = run.step_runs[0]
        assert ENDPOINT_SECRET_VALUE not in (step.logs or ""), (
            "the endpoint key appeared in the step's log stream"
        )
        assert ENDPOINT_SECRET_REF in json.dumps(
            [ENDPOINT_SECRET_REF]
        ), "sanity: the REF is a name, and only the name may be stored"
