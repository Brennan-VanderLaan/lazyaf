"""
Remote step dispatch - Phase 12.6 (Agent C).

Three things are pinned here:

1. `_decide_route` ACCEPTS ExecutorMode.REMOTE and `StepRun.executor` records
   "remote" (R1). Until this phase it raised
   "...which has no execution path until Phase 12.6".

2. `execute_step.config` (section 3.2) is produced by the ONE producer,
   `runner_protocol.build_execute_step_config` (cross-agent contract #2),
   from the SAME control-file producers the local path uses - only the
   DELIVERY changes (R3).

3. `control_files` is the secret boundary (cross-agent contract #9). The step
   JWT and `secret_environment` appear ONLY there - never in
   `container.environment`, which is what `docker inspect` shows on a host
   the backend does not own. This is the remote twin of 12.5's
   secret-containment test.

Plus the round trip contract #2 names: backend produces -> the agent writes
the file -> `images/base/control/config.load_step_config` loads it with zero
key loss. Producer and consumer are driven in ONE process here so a shape
change cannot pass on one side and fail on the other.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))
# The in-container control runtime is the CONSUMER half of the round trip.
sys.path.insert(0, str(ROOT / "images" / "base"))

from control.config import load_step_config  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Pipeline, PipelineRun, Repo, StepRun  # noqa: E402
from app.models.pipeline import (  # noqa: E402
    ExecutorMode,
    RunStatus,
    StepExecution,
    StepExecutionStatus,
)
from app.services.execution.local_executor import (  # noqa: E402
    AGENT_CONFIG_PATH_ENV,
    CONTROL_CONFIG_DIR,
)
from app.services.execution.runner_protocol import PROTOCOL_VERSION  # noqa: E402
from app.services.pipeline_executor import PipelineExecutor  # noqa: E402

SECRET = "sk-ant-SENTINEL-DO-NOT-LEAK"
STEP_TIMEOUT = 600


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def env(tmp_path):
    db_path = (tmp_path / "remote_dispatch.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", echo=False, connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="remote-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()), repo_id=repo.id, name="remote-pipeline", steps="[]"
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            steps_total=1,
        )
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=3,
            step_name="flash-firmware",
            status=RunStatus.RUNNING.value,
        )
        db.add_all([repo, pipeline, run, step_run])
        await db.commit()
        for obj in (repo, run, step_run):
            await db.refresh(obj)

    yield SimpleNamespace(
        engine=engine, factory=factory, repo=repo, run=run, step_run=step_run
    )
    await engine.dispose()


async def build_config(
    env,
    step_type: str = "script",
    step_config: dict | None = None,
    requirements: dict | None = None,
    commit_sha: str | None = "2a513dd4",
) -> tuple[dict, dict, dict]:
    """Drive the REAL dispatch sequence and return (config, exec_config,
    exec_context).

    `executor=None` is deliberate: a remote step must never probe an image
    label through an executor, because the image lives on a host the backend
    does not own. Passing None proves the probe is not reached.
    """
    executor = PipelineExecutor()
    if step_config is None:
        step_config = (
            {"agent": "mock", "prompt": "do the thing"}
            if step_type == "agent"
            else {"command": "python3 flash.py", "image": "lazyaf-base:dev"}
        )
    async with env.factory() as db:
        exec_config, exec_context = executor._build_local_execution_config(
            env.run, env.step_run, step_type, step_config, STEP_TIMEOUT, None
        )
        if step_type == "agent":
            exec_config["secret_environment"] = {"ANTHROPIC_API_KEY": SECRET}
            # The FIELDS `_attach_agent_payload` supplies; the single
            # producer (generate_agent_config) owns the file shape (R3).
            exec_config["agent"] = {
                "agent": "mock",
                "prompt": "do the thing",
                "repo_id": env.repo.id,
                "step_index": env.step_run.step_index,
                "step_name": env.step_run.step_name,
            }
        await executor._prepare_control_mode(
            db=db,
            executor=None,
            step_run=env.step_run,
            step_config=step_config,
            exec_config=exec_config,
            exec_context=exec_context,
            timeout=STEP_TIMEOUT,
            mode=ExecutorMode.REMOTE,
        )
        config = executor._build_remote_execution_config(
            env.repo,
            exec_config,
            exec_context,
            branch="main",
            commit_sha=commit_sha,
            requirements=requirements or {},
        )
    return config, exec_config, exec_context


def control_file(config: dict, exec_context: dict) -> dict:
    path = f"/workspace/{CONTROL_CONFIG_DIR}/{exec_context['step_execution_id']}.json"
    assert path in config["control_files"], sorted(config["control_files"])
    return config["control_files"][path]


# -----------------------------------------------------------------------------
# Test contract 6: the route is accepted and recorded
# -----------------------------------------------------------------------------

class TestRemoteRouteAccepted:
    def test_decide_route_accepts_remote(self):
        executor = PipelineExecutor()
        mode, reason, requirements = executor._decide_route(
            "script",
            {"command": "x", "requires": {"has": ["gpio"], "arch": "aarch64"}},
            "flash",
        )
        assert mode is ExecutorMode.REMOTE
        assert reason == "runner-pin"
        # Normalized by the ONE parser, backend-side.
        assert requirements == {"has": ["gpio"], "arch": "arm64"}

    def test_decide_route_no_longer_raises_the_12_6_guard(self):
        """The RuntimeError this method raised on REMOTE is gone."""
        executor = PipelineExecutor()
        mode, _reason, _req = executor._decide_route(
            "script", {"command": "x", "executor": "remote"}, "flash"
        )
        assert mode is ExecutorMode.REMOTE

    def test_local_route_still_carries_no_requirements(self):
        executor = PipelineExecutor()
        mode, reason, requirements = executor._decide_route(
            "script", {"command": "x"}, "plain"
        )
        assert mode is ExecutorMode.LOCAL
        assert reason == "script-default-local"
        assert requirements == {}

    async def test_step_execution_row_is_created_and_pinned(self, env):
        """The StepExecution row IS the assignment unit: the dispatcher's CAS
        claims it and the step gate fences on it."""
        _config, _exec_config, exec_context = await build_config(env)
        async with env.factory() as db:
            result = await db.execute(
                select(StepExecution).where(
                    StepExecution.id == exec_context["step_execution_id"]
                )
            )
            execution = result.scalar_one()
        assert execution.step_run_id == env.step_run.id
        assert execution.status == StepExecutionStatus.PREPARING.value
        assert execution.timeout_at is not None

    async def test_requirements_are_stamped_on_the_context(self, env):
        """Durable on purpose: a requeued step must be re-matchable after a
        backend restart, and the dispatch closure does not survive one."""
        _config, _exec_config, exec_context = await build_config(
            env, requirements={"runner_id": "pi-workshop-1"}
        )
        assert exec_context["runner_requirements"] == {"runner_id": "pi-workshop-1"}


# -----------------------------------------------------------------------------
# Control mode is mandatory on the remote path
# -----------------------------------------------------------------------------

class TestControlModeIsMandatoryForRemote:
    async def test_control_false_on_a_remote_step_raises(self, env):
        with pytest.raises(ValueError) as exc:
            await build_config(
                env, step_config={"command": "x", "control": False}
            )
        message = str(exc.value)
        assert "stdout mode" in message
        assert "StepExecution" in message

    async def test_exec_form_command_on_a_remote_step_raises(self, env):
        with pytest.raises(ValueError) as exc:
            await build_config(env, step_config={"command": ["ls", "-la"]})
        assert "exec-form list" in str(exc.value)

    async def test_remote_control_mode_needs_no_image_probe(self, env):
        """`executor=None` would AttributeError if the probe were reached."""
        _config, _exec_config, exec_context = await build_config(env)
        assert exec_context["control_mode"] is True


# -----------------------------------------------------------------------------
# Test contract 7: control_files is the secret boundary
# -----------------------------------------------------------------------------

class TestSecretBoundary:
    async def test_token_is_only_in_control_files(self, env):
        config, _exec_config, exec_context = await build_config(env)
        token = exec_context["step_auth_token"]
        assert token

        assert control_file(config, exec_context)["auth_token"] == token

        environment = config["container"]["environment"]
        assert token not in json.dumps(environment)
        # And nowhere else in the frame outside control_files.
        outside = dict(config)
        outside.pop("control_files")
        assert token not in json.dumps(outside)

    async def test_secret_environment_is_only_in_control_files(self, env):
        config, _exec_config, exec_context = await build_config(env, "agent")
        file_env = control_file(config, exec_context)["environment"]
        assert file_env["ANTHROPIC_API_KEY"] == SECRET

        outside = dict(config)
        outside.pop("control_files")
        assert SECRET not in json.dumps(outside)

    async def test_container_environment_is_the_non_secret_table(self, env):
        config, _exec_config, exec_context = await build_config(env)
        environment = config["container"]["environment"]
        assert environment["LAZYAF_CONTROL"] == "1"
        assert environment["LAZYAF_PIPELINE_RUN_ID"] == env.run.id
        assert environment["LAZYAF_STEP_RUN_ID"] == env.step_run.id
        assert environment["LAZYAF_STEP_INDEX"] == "3"
        assert environment["LAZYAF_EXECUTION_KEY"] == exec_context["execution_key"]
        assert environment["CONFIG_PATH"].endswith(
            f"{exec_context['step_execution_id']}.json"
        )
        assert "auth_token" not in environment
        assert "ANTHROPIC_API_KEY" not in environment

    async def test_command_is_null_in_the_container_block(self, env):
        """Control mode: the runtime reads the command from the FILE, so a
        `docker inspect` on the remote host shows no command at all."""
        config, _exec_config, exec_context = await build_config(env)
        assert config["container"]["command"] is None
        assert control_file(config, exec_context)["command"] == "python3 flash.py"


# -----------------------------------------------------------------------------
# The frame shape (section 3.2)
# -----------------------------------------------------------------------------

class TestConfigShape:
    async def test_top_level_keys(self, env):
        config, _exec_config, _ctx = await build_config(env)
        assert set(config) == {
            "protocol_version",
            "backend_url",
            "workspace",
            "container",
            "control_files",
        }
        assert config["protocol_version"] == PROTOCOL_VERSION

    async def test_workspace_block_carries_the_provisioning_inputs(self, env):
        """The single piece of remote execution with no local analogue: the
        AGENT clones into its OWN volume, so it needs the url, the branch and
        the commit."""
        config, _exec_config, _ctx = await build_config(env)
        workspace = config["workspace"]
        assert workspace["volume"].startswith("lazyaf-ws-")
        assert workspace["retain_key"] == env.run.id
        assert workspace["mount_path"] == "/workspace"
        assert workspace["repo_id"] == env.repo.id
        assert workspace["clone_url"].endswith(f"/git/{env.repo.id}.git")
        assert workspace["branch"] == "main"
        assert workspace["commit_sha"] == "2a513dd4"

    async def test_commit_sha_may_be_absent(self, env):
        config, _exec_config, _ctx = await build_config(env, commit_sha=None)
        assert config["workspace"]["commit_sha"] is None

    async def test_one_volume_per_run(self, env):
        """HOME=/workspace/home persistence between steps has to work the
        same on both hosts, so retain_key is the RUN, not the step."""
        config, _exec_config, _ctx = await build_config(env)
        assert config["workspace"]["retain_key"] == env.run.id
        assert env.run.id[:8] in config["workspace"]["volume"]

    async def test_mounts_carry_explicit_addressing(self, env):
        config, _exec_config, _ctx = await build_config(
            env,
            step_config={
                "command": "docker ps",
                "image": "lazyaf-base:dev",
                "needs": ["docker"],
            },
        )
        mounts = config["container"]["mounts"]
        assert mounts, "needs: [docker] must produce a mount"
        for mount in mounts:
            # R6: never inferred from path shape, on either host.
            assert mount["addressing"] in ("volume", "bind")
            assert set(mount) == {"addressing", "source", "target", "mode"}

    async def test_agent_step_ships_two_control_files(self, env):
        config, _exec_config, exec_context = await build_config(env, "agent")
        step_execution_id = exec_context["step_execution_id"]
        agent_path = (
            f"/workspace/{CONTROL_CONFIG_DIR}/agent.{step_execution_id}.json"
        )
        assert agent_path in config["control_files"]
        # The step config points the wrapper at the agent file, in the FILE
        # environment - never in container env.
        file_env = control_file(config, exec_context)["environment"]
        assert file_env[AGENT_CONFIG_PATH_ENV] == agent_path

    async def test_script_step_ships_one_control_file(self, env):
        config, _exec_config, _ctx = await build_config(env)
        assert len(config["control_files"]) == 1


# -----------------------------------------------------------------------------
# Cross-agent contract #2: the round trip
# -----------------------------------------------------------------------------

class TestControlFileRoundTrip:
    async def test_backend_produces_what_the_runtime_loads(self, env, tmp_path):
        """Backend produces -> the agent writes the file -> the in-container
        loader reads it with zero key loss. Producer and consumer run in ONE
        process so a shape change cannot pass on one side alone."""
        config, _exec_config, exec_context = await build_config(env)
        payload = control_file(config, exec_context)

        written = tmp_path / "step.json"
        written.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_step_config(written)
        assert loaded is not None, "the control runtime rejected the frame"
        assert loaded.step_id == exec_context["step_execution_id"]
        assert loaded.step_run_id == env.step_run.id
        assert loaded.execution_key == exec_context["execution_key"]
        assert loaded.auth_token == exec_context["step_auth_token"]
        assert loaded.command == "python3 flash.py"
        assert loaded.timeout_seconds == STEP_TIMEOUT
        assert loaded.working_directory == "/workspace/repo"
        assert loaded.shell == "bash"

    async def test_no_producer_key_is_dropped_by_the_consumer(self, env, tmp_path):
        config, _exec_config, exec_context = await build_config(env)
        payload = control_file(config, exec_context)
        written = tmp_path / "step.json"
        written.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_step_config(written)

        # Every key the producer emitted must survive onto the dataclass.
        for key in payload:
            assert hasattr(loaded, key), f"consumer drops producer key {key!r}"

    async def test_config_path_points_at_the_file_that_was_shipped(self, env):
        """CONFIG_PATH is how the runtime finds its config; a mismatch is a
        step that boots and immediately reports 'config file not found'."""
        config, _exec_config, _ctx = await build_config(env)
        config_path = config["container"]["environment"]["CONFIG_PATH"]
        assert config_path in config["control_files"]

    async def test_agent_file_is_json_serializable_as_shipped(self, env):
        """The whole frame crosses a WebSocket as JSON; a non-serializable
        value would fail at send time, on another host, mid-run."""
        config, _exec_config, _ctx = await build_config(env, "agent")
        json.dumps(config)


# -----------------------------------------------------------------------------
# The remote half of workspace cleanup (section 3.4)
# -----------------------------------------------------------------------------

class CapturingRunnerSocket:
    """Capturing transport on the REAL registry (R6), never an AsyncMock."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, *args, **kwargs) -> None:
        return None


class TestRemoteWorkspaceCleanup:
    """`cleanup_workspace{retain_key}` is the remote half of the local volume
    reap: a runner agent provisions its OWN volume and cannot see the
    backend's. failure_01's whole class of bug was a designed frame nobody
    ever sent."""

    @pytest_asyncio.fixture(autouse=True)
    async def _clean_registry(self):
        from app.services.execution.runner_registry import runner_registry

        await runner_registry.reset()
        yield
        await runner_registry.reset()

    async def test_run_completion_announces_the_retain_key(self, env):
        from app.services.execution.runner_protocol import RegisterMessage
        from app.services.execution.runner_registry import runner_registry

        socket = CapturingRunnerSocket()
        async with env.factory() as db:
            await runner_registry.connect(
                db,
                socket,
                RegisterMessage(runner_id="pi-1", runner_type="generic"),
            )

        await PipelineExecutor()._cleanup_remote_workspaces(env.run.id)

        frames = [m for m in socket.sent if m["type"] == "cleanup_workspace"]
        assert frames == [
            {"type": "cleanup_workspace", "retain_key": env.run.id}
        ]

    async def test_no_connected_runners_is_a_silent_no_op(self, env):
        """The agent's idle reaper is the backstop, so this is an
        optimization and never a correctness dependency - it must not raise
        on a backend with no runners at all, which is the default."""
        await PipelineExecutor()._cleanup_remote_workspaces(env.run.id)
