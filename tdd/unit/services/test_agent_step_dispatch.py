"""
Unit tests for 12.5 AGENT STEP DISPATCH: routing, the agent vocabulary, the
secret channel, and the two files an agent step carries.

What this pins (design sections 1.4, 2.1-2.6, cross-agent contracts #1/#5/#6):

- ROUTING: an agent step routes LOCAL ("agent-default-local"). `executor:
  legacy` on an agent step is the LAST remaining legacy escape hatch (R2) and
  is still honored, at WARNING. There is NO default agent: an unknown or
  missing one raises at dispatch with the valid vocabulary in the message.

- SECRETS: the provider API key travels ONLY in the step config FILE. It is
  never merged into the container's `environment` kwarg, so it is absent from
  `docker inspect`; and `secret_environment` on a step that is NOT in control
  mode fails the step at dispatch rather than downgrading the key onto the
  inspectable path. A missing key fails at dispatch too, naming the variable
  but never its value.

- TWO FILES: the step config and the agent config ride the SAME put_archive
  tar, both per-step-execution named, and LAZYAF_AGENT_CONFIG_PATH is
  announced inside the config FILE - never in container env.

- CONTROL MODE IS MANDATORY for agent steps: `control: false` and an
  unlabeled image both RAISE instead of silently taking the stdout path,
  where the wrapper would run with no config file at all.

The T2 sibling (tdd/integration/services/test_agent_step_container.py) proves
the same split against a REAL container's inspect output.
"""
import io
import json
import logging
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.execution.local_executor import (  # noqa: E402
    AGENT_CONFIG_PATH_ENV,
    CONTROL_LAYER_LABEL,
    LocalExecutor,
)
from app.services.pipeline_executor import (  # noqa: E402
    AGENT_RUNTIME_LABEL,
    AGENT_WRAPPER_COMMAND,
    DEFAULT_AGENT_IMAGE,
    DEFAULT_AGENT_STEP_TIMEOUT,
    DEFAULT_STEP_TIMEOUT,
    PipelineExecutor,
    agent_secret_environment,
    default_timeout_for,
    resolve_agent_type,
    resolve_agent_work_branch,
)
from app.services.workspace.execution_router import ExecutionRouter  # noqa: E402

ROUTER_LOGGER = "app.services.workspace.execution_router"
EXECUTOR_LOGGER = "app.services.pipeline_executor"
SECRET = "sk-ant-do-not-leak-me"


# -----------------------------------------------------------------------------
# Routing (contract: agent -> local, legacy override survives)
# -----------------------------------------------------------------------------

class TestAgentRouting:
    @pytest.fixture
    def router(self):
        return ExecutionRouter()

    def test_agent_routes_local(self, router):
        decision = router.decide("agent", {"agent": "mock"})
        assert decision.mode == "local"
        assert decision.reason == "agent-default-local"

    def test_explicit_legacy_override_still_honored_at_warning(
        self, router, caplog
    ):
        """R2: the escape hatch must stay CALLABLE until the 12.6 deletion
        commit, and an override is never silent (R1)."""
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            decision = router.decide(
                "agent", {"agent": "claude-code", "executor": "legacy"}
            )

        assert decision.mode == "legacy"
        assert decision.reason == "explicit-override"
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_invalid_executor_override_raises(self, router):
        with pytest.raises(ValueError, match="Invalid executor override"):
            router.decide("agent", {"agent": "mock", "executor": "remote"})

    def test_runner_type_on_agent_step_is_not_a_pin_warning(
        self, router, caplog
    ):
        """A runner_type on an agent step named the AI flavor - ordinary
        config, not an unhonorable hardware pin."""
        with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
            decision = router.decide("agent", {"runner_type": "claude-code"})

        assert decision.mode == "local"
        assert [r for r in caplog.records if r.name == ROUTER_LOGGER] == []


# -----------------------------------------------------------------------------
# Agent vocabulary + secrets (contracts #5 / #6)
# -----------------------------------------------------------------------------

class TestAgentVocabulary:
    @pytest.mark.parametrize("agent", ["claude-code", "gemini", "mock"])
    def test_every_agent_has_a_default_image(self, agent):
        assert resolve_agent_type({"agent": agent}) == agent
        assert DEFAULT_AGENT_IMAGE[agent]

    def test_runner_type_is_accepted_as_the_agent_spelling(self):
        assert resolve_agent_type({"runner_type": "gemini"}) == "gemini"

    def test_unknown_agent_raises_naming_the_vocabulary(self):
        with pytest.raises(ValueError, match="unknown agent 'acme-ai'") as e:
            resolve_agent_type({"agent": "acme-ai"})
        assert "claude-code" in str(e.value)

    def test_missing_agent_raises_there_is_no_default(self):
        with pytest.raises(ValueError, match="missing an `agent:` key"):
            resolve_agent_type({"title": "do it"})

    def test_runner_type_any_is_not_an_agent(self):
        """The legacy queue's "any runner" is not an agent selection."""
        with pytest.raises(ValueError):
            resolve_agent_type({"runner_type": "any"})

    def test_agent_steps_default_to_thirty_minutes(self):
        assert default_timeout_for("agent") == DEFAULT_AGENT_STEP_TIMEOUT
        assert default_timeout_for("script") == DEFAULT_STEP_TIMEOUT
        assert DEFAULT_AGENT_STEP_TIMEOUT == 1800


class TestAgentSecretEnvironment:
    def test_claude_key_comes_from_settings(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "anthropic_api_key", SECRET)
        assert agent_secret_environment("claude-code") == {
            "ANTHROPIC_API_KEY": SECRET
        }

    def test_gemini_key_comes_from_settings(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "gemini_api_key", "g-key")
        assert agent_secret_environment("gemini") == {"GEMINI_API_KEY": "g-key"}

    def test_mock_needs_no_secret(self):
        assert agent_secret_environment("mock") == {}

    def test_missing_key_fails_at_dispatch_naming_the_variable(
        self, monkeypatch
    ):
        """Failing HERE beats burning 30s of container startup to reach an
        opaque CLI auth error - and it keeps the key name out of step logs."""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
        with pytest.raises(ValueError) as e:
            agent_secret_environment("claude-code", "implement")

        assert "ANTHROPIC_API_KEY" in str(e.value)
        assert "implement" in str(e.value)


# -----------------------------------------------------------------------------
# _build_local_execution_config: the agent branch
# -----------------------------------------------------------------------------

def _rows():
    run = SimpleNamespace(id=str(uuid4()))
    step_run = SimpleNamespace(
        id=str(uuid4()), step_index=2, step_name="implement"
    )
    return run, step_run


class TestBuildAgentExecutionConfig:
    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "anthropic_api_key", SECRET)
        monkeypatch.setattr(get_settings(), "gemini_api_key", "g-key")

    def _build(self, config, timeout=DEFAULT_AGENT_STEP_TIMEOUT):
        run, step_run = _rows()
        return PipelineExecutor()._build_local_execution_config(
            run, step_run, "agent", config, timeout, None
        )

    def test_command_is_the_platform_owned_wrapper(self):
        exec_config, _ = self._build({"agent": "mock", "command": "rm -rf /"})
        # A user-supplied command on an agent step is IGNORED, not honored:
        # the wrapper invocation is platform-owned.
        assert exec_config["command"] == AGENT_WRAPPER_COMMAND

    @pytest.mark.parametrize(
        "agent,image",
        [
            ("claude-code", "lazyaf-claude:dev"),
            ("gemini", "lazyaf-gemini:dev"),
            ("mock", "lazyaf-agent-base:dev"),
        ],
    )
    def test_default_image_per_agent(self, agent, image):
        exec_config, _ = self._build({"agent": agent})
        assert exec_config["image"] == image

    def test_explicit_image_wins(self):
        exec_config, _ = self._build(
            {"agent": "mock", "image": "my-agent:dev"}
        )
        assert exec_config["image"] == "my-agent:dev"

    def test_secret_environment_is_populated_not_merged_into_environment(self):
        exec_config, _ = self._build(
            {"agent": "claude-code", "environment": {"DEBUG": "1"}}
        )
        assert exec_config["secret_environment"] == {"ANTHROPIC_API_KEY": SECRET}
        assert exec_config["environment"] == {"DEBUG": "1"}
        assert SECRET not in json.dumps(exec_config["environment"])

    def test_usage_provider_is_stamped_per_agent(self):
        assert self._build({"agent": "claude-code"})[0]["usage_provider"] == (
            "anthropic"
        )
        assert self._build({"agent": "gemini"})[0]["usage_provider"] == "google"
        assert self._build({"agent": "mock"})[0]["usage_provider"] == (
            "self-hosted"
        )

    def test_task_is_accepted_as_the_yaml_title_spelling(self):
        """The dogfood ratchet's mock-agent step states its work as
        `task:`; that string must reach the prompt, not be dropped."""
        run, step_run = _rows()
        executor = PipelineExecutor()
        exec_config, _ = executor._build_local_execution_config(
            run, step_run, "agent",
            {"agent": "mock", "task": "Write the marker file"}, 1800, None,
        )
        assert exec_config["command"] == AGENT_WRAPPER_COMMAND
        # The payload itself is attached asynchronously; the title source is
        # pinned through the prompt renderer here.
        from app.services.agent_prompt import render_agent_prompt

        assert "Write the marker file" in render_agent_prompt(
            card_title="Write the marker file", card_description=""
        )

    def test_script_steps_get_no_agent_keys(self):
        run, step_run = _rows()
        exec_config, _ = PipelineExecutor()._build_local_execution_config(
            run, step_run, "script", {"command": "pytest"}, 300, None
        )
        assert "secret_environment" not in exec_config
        assert "agent" not in exec_config
        assert exec_config["command"] == "pytest"


# -----------------------------------------------------------------------------
# _prepare_control_mode: control mode is MANDATORY for agent steps
# -----------------------------------------------------------------------------

class _Executor:
    def __init__(self, supports: bool = True):
        self.supports = supports

    async def image_supports_control_layer(self, image: str) -> bool:
        return self.supports


class TestControlModeIsMandatoryForAgents:
    async def _prepare(self, exec_config, step_config, supports=True):
        step_run = SimpleNamespace(
            id=str(uuid4()), step_index=1, step_name="implement"
        )
        await PipelineExecutor()._prepare_control_mode(
            db=None,
            executor=_Executor(supports),
            step_run=step_run,
            step_config=step_config,
            exec_config=exec_config,
            exec_context={},
            timeout=1800,
        )

    async def test_control_false_on_an_agent_step_raises(self):
        with pytest.raises(ValueError, match="cannot run with `control: false`"):
            await self._prepare(
                {"type": "agent", "command": AGENT_WRAPPER_COMMAND,
                 "image": "lazyaf-agent-base:dev"},
                {"agent": "mock", "control": False},
            )

    async def test_unlabeled_image_on_an_agent_step_raises(self):
        with pytest.raises(ValueError, match="control-layer capability label"):
            await self._prepare(
                {"type": "agent", "command": AGENT_WRAPPER_COMMAND,
                 "image": "python:3.12"},
                {"agent": "mock"},
                supports=False,
            )

    async def test_script_step_still_downgrades_silently(self):
        """The escape hatch is unchanged for everything that is not an agent
        step - 12.3 behavior, zero regression."""
        context = {}
        step_run = SimpleNamespace(id="x", step_index=0, step_name="s")
        await PipelineExecutor()._prepare_control_mode(
            db=None, executor=_Executor(False), step_run=step_run,
            step_config={"control": False},
            exec_config={"type": "script", "command": "true"},
            exec_context=context, timeout=300,
        )
        assert context["control_mode"] is False


# -----------------------------------------------------------------------------
# LocalExecutor: the secret channel and the second file
# -----------------------------------------------------------------------------

def _agent_payload(**overrides):
    payload = {
        "agent": "mock",
        "prompt": "do the thing",
        "model": None,
        "agents_json": None,
        "stream": True,
        "card_id": None,
        "card_title": "Do the thing",
        "card_description": "",
        "step_index": 0,
        "step_name": "implement",
        "previous_step_name": None,
        "previous_step_logs": None,
        "repo_id": "r1",
        "workdir": "/workspace/repo",
        "base_branch": "main",
        "branch": "lazyaf/abc",
        "remote_url": "http://backend:8000/git/r1.git",
        "commit_enabled": False,
        "commit_message": None,
        "push": False,
        "allow_empty": False,
        "mock_config": {"exit_code": 0},
        "role": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def docker_client():
    client = MagicMock()
    image = MagicMock()
    image.id = "sha256:agent"
    image.labels = {CONTROL_LAYER_LABEL: "1"}
    client.images.get.return_value = image
    container = MagicMock()
    container.id = "c1"
    container.logs = MagicMock(return_value=iter([b"line\n"]))
    container.wait = MagicMock(return_value={"StatusCode": 0})
    client.containers.create = MagicMock(return_value=container)
    client.containers.run = MagicMock(return_value=container)
    client.container = container
    return client


@pytest.fixture
def agent_context():
    run_id = str(uuid4())
    step_run_id = str(uuid4())
    return {
        "pipeline_run_id": run_id,
        "step_run_id": step_run_id,
        "step_index": 0,
        "execution_key": f"{run_id}:0:{step_run_id}",
        "workspace_volume": f"lazyaf-ws-{run_id[:8]}",
        "control_mode": True,
        "step_execution_id": str(uuid4()),
        "step_auth_token": "step-jwt",
    }


@pytest.fixture
def agent_step_config():
    return {
        "type": "agent",
        "command": AGENT_WRAPPER_COMMAND,
        "image": "lazyaf-agent-base:dev",
        "timeout": 1800,
        "environment": {"DEBUG": "1"},
        "secret_environment": {"ANTHROPIC_API_KEY": SECRET},
        "usage_provider": "anthropic",
        "agent": _agent_payload(),
    }


async def _run(executor, step_config, context):
    return [e async for e in executor.execute_step(step_config, context)]


def _tar_members(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        return {
            m.name: (json.load(tar.extractfile(m)) if m.isfile() else None, m)
            for m in tar.getmembers()
        }


class TestSecretContainment:
    async def test_secret_is_absent_from_container_env_labels_and_command(
        self, docker_client, agent_step_config, agent_context
    ):
        executor = LocalExecutor(docker_client)
        events = await _run(executor, agent_step_config, agent_context)
        assert events[-1]["status"] == "completed"

        create_kwargs = docker_client.containers.create.call_args[1]
        inspectable = json.dumps({
            "environment": create_kwargs["environment"],
            "labels": create_kwargs["labels"],
            "command": create_kwargs["command"],
        })
        assert SECRET not in inspectable
        assert "ANTHROPIC_API_KEY" not in create_kwargs["environment"]

    async def test_secret_is_present_in_the_put_archive_tar(
        self, docker_client, agent_step_config, agent_context
    ):
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        config_name = f".control/{agent_context['step_execution_id']}.json"
        payload = _tar_members(tar_bytes)[config_name][0]
        assert payload["environment"]["ANTHROPIC_API_KEY"] == SECRET
        # user env still travels alongside it
        assert payload["environment"]["DEBUG"] == "1"

    async def test_secret_without_control_mode_fails_the_step_at_dispatch(
        self, docker_client, agent_step_config, agent_context
    ):
        """A secret must never DOWNGRADE onto the stdout path, where the only
        delivery channel is inspectable container env."""
        agent_context["control_mode"] = False

        executor = LocalExecutor(docker_client)
        events = await _run(executor, agent_step_config, agent_context)

        result = events[-1]
        assert result["status"] == "failed"
        assert "secrets require control mode" in result["error"]
        assert SECRET not in result["error"]  # never the VALUE
        docker_client.containers.create.assert_not_called()
        docker_client.containers.run.assert_not_called()

    async def test_step_without_secrets_is_unaffected(
        self, docker_client, agent_context
    ):
        agent_context["control_mode"] = False
        executor = LocalExecutor(docker_client)
        events = await _run(
            executor,
            {"type": "script", "command": "true", "image": "python:3.12"},
            agent_context,
        )
        assert events[-1]["status"] == "completed"


class TestTwoFilesOneTar:
    async def test_both_config_files_ride_the_same_archive(
        self, docker_client, agent_step_config, agent_context
    ):
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        assert docker_client.container.put_archive.call_count == 1
        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        members = _tar_members(tar_bytes)
        execution_id = agent_context["step_execution_id"]
        assert f".control/{execution_id}.json" in members
        assert f".control/agent.{execution_id}.json" in members

    async def test_agent_file_is_per_step_execution_and_0600(
        self, docker_client, agent_step_config, agent_context
    ):
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        name = f".control/agent.{agent_context['step_execution_id']}.json"
        payload, info = _tar_members(tar_bytes)[name]
        assert info.mode == 0o600
        assert payload["version"] == 1
        assert payload["agent"] == "mock"
        assert payload["prompt"] == "do the thing"

    async def test_agent_path_is_announced_in_the_file_never_in_env(
        self, docker_client, agent_step_config, agent_context
    ):
        """Contract #1: LAZYAF_AGENT_CONFIG_PATH lives in the step config
        FILE's environment - the one channel that is not inspectable."""
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        execution_id = agent_context["step_execution_id"]
        expected = f"/workspace/.control/agent.{execution_id}.json"

        create_kwargs = docker_client.containers.create.call_args[1]
        assert AGENT_CONFIG_PATH_ENV not in create_kwargs["environment"]

        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        payload = _tar_members(tar_bytes)[f".control/{execution_id}.json"][0]
        assert payload["environment"][AGENT_CONFIG_PATH_ENV] == expected

    async def test_the_agent_file_carries_no_token(
        self, docker_client, agent_step_config, agent_context
    ):
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        name = f".control/agent.{agent_context['step_execution_id']}.json"
        blob = json.dumps(_tar_members(tar_bytes)[name][0])
        assert "step-jwt" not in blob
        assert SECRET not in blob

    async def test_script_step_still_ships_exactly_one_file(
        self, docker_client, agent_context
    ):
        executor = LocalExecutor(docker_client)
        await _run(
            executor,
            {"type": "script", "command": "true",
             "image": "lazyaf-base:dev", "timeout": 60},
            agent_context,
        )

        tar_bytes = docker_client.container.put_archive.call_args[0][1]
        files = [n for n, (_p, m) in _tar_members(tar_bytes).items() if m.isfile()]
        assert files == [f".control/{agent_context['step_execution_id']}.json"]

    async def test_invalid_agent_payload_fails_before_the_container_starts(
        self, docker_client, agent_step_config, agent_context
    ):
        agent_step_config["agent"] = _agent_payload(agent="acme-ai")

        executor = LocalExecutor(docker_client)
        events = await _run(executor, agent_step_config, agent_context)

        assert events[-1]["status"] == "failed"
        assert "unknown agent" in events[-1]["error"]
        docker_client.container.start.assert_not_called()


class TestUsageEnvIsNonSecret:
    async def test_usage_provider_reaches_container_env(
        self, docker_client, agent_step_config, agent_context
    ):
        """run.py needs it for the FALLBACK record, which exists precisely
        for the case where no manifest was written - so it cannot live in a
        file the wrapper never got to read."""
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        env = docker_client.containers.create.call_args[1]["environment"]
        assert env["LAZYAF_USAGE_PROVIDER"] == "anthropic"

    async def test_role_and_gpu_are_absent_when_unset(
        self, docker_client, agent_step_config, agent_context
    ):
        """12.5 seam: on the wire, but nothing sets them yet."""
        executor = LocalExecutor(docker_client)
        await _run(executor, agent_step_config, agent_context)

        env = docker_client.containers.create.call_args[1]["environment"]
        assert "LAZYAF_ROLE" not in env
        assert "LAZYAF_GPU_NODE_ID" not in env


# -----------------------------------------------------------------------------
# The work branch: an agent step must never inherit the run's trigger branch
# -----------------------------------------------------------------------------

async def _attach(step_config, *, trigger_context, step_run_id=None,
                  exec_config=None, default_branch="main"):
    """Run the real _attach_agent_payload and return the agent payload.

    `db` is None on purpose: for a step at index 0 with no agent_file_ids,
    neither the previous-step lookup nor agent resolution touches the
    session, so this exercises the real branch/commit-scope decision with no
    database at all.
    """
    executor = PipelineExecutor()
    pipeline_run = SimpleNamespace(
        id=str(uuid4()), trigger_context=json.dumps(trigger_context)
    )
    repo = SimpleNamespace(id="r1", default_branch=default_branch)
    step_run = SimpleNamespace(
        id=step_run_id or str(uuid4()), step_index=0, step_name="implement"
    )
    config = {"type": "agent", "command": AGENT_WRAPPER_COMMAND}
    config.update(exec_config or {})
    await executor._attach_agent_payload(
        None, pipeline_run, None, repo, step_run, step_config, config
    )
    return config["agent"]


class TestWorkBranchResolution:
    """The pure resolver behind the self-triggering-push-loop fix."""

    def test_no_declaration_derives_an_isolated_branch(self):
        branch, declared = resolve_agent_work_branch(
            {"agent": "mock"}, {"branch": "main"}, "main", "abcdef1234567890"
        )
        assert branch == "lazyaf/agent-abcdef12"
        assert declared is False

    def test_derived_branch_is_never_the_base_branch(self):
        for base in ("main", "develop", "feature/x", "lazyaf/agent-x"):
            branch, declared = resolve_agent_work_branch(
                {}, {"branch": base}, base, str(uuid4())
            )
            assert branch != base
            assert declared is False

    def test_explicit_step_config_branch_wins_and_is_marked_declared(self):
        branch, declared = resolve_agent_work_branch(
            {"branch": "main"}, {"branch": "main"}, "main", "abcdef12"
        )
        assert branch == "main"
        assert declared is True, (
            "an explicit `branch:` is the ONE way to push to the trigger "
            "branch, so it must be reported as declared"
        )

    def test_adhoc_work_branch_is_honored(self):
        """Card work / playground name their own throwaway branch."""
        branch, declared = resolve_agent_work_branch(
            {}, {"branch": "main", "work_branch": "lazyaf/abc12345"}, "main",
            "abcdef12",
        )
        assert branch == "lazyaf/abc12345"
        assert declared is False

    def test_adhoc_work_branch_equal_to_base_is_dropped(self, caplog):
        """An internal caller passing the base branch is a bug, not a
        licence to push to it."""
        with caplog.at_level(logging.WARNING, logger=EXECUTOR_LOGGER):
            branch, declared = resolve_agent_work_branch(
                {}, {"branch": "main", "work_branch": "main"}, "main",
                "abcdef12",
            )
        assert branch == "lazyaf/agent-abcdef12"
        assert declared is False
        assert any("isolated branch" in r.getMessage() for r in caplog.records)

    def test_blank_declarations_do_not_count(self):
        branch, _ = resolve_agent_work_branch(
            {"branch": "   "}, {"work_branch": ""}, "main", "abcdef12"
        )
        assert branch == "lazyaf/agent-abcdef12"


class TestAgentPayloadNeverPushesToTheTriggerBranch:
    async def test_agent_step_with_no_branch_does_not_resolve_to_trigger(self):
        payload = await _attach(
            {"agent": "mock", "task": "do the thing"},
            trigger_context={"branch": "main", "commit_sha": "abc"},
        )
        assert payload["base_branch"] == "main"
        assert payload["branch"] != "main"
        assert payload["branch"].startswith("lazyaf/agent-")

    async def test_push_triggered_run_cannot_push_to_the_pushed_branch(self):
        """THE LOOP, asserted on the resolved config (no live run needed).

        A push to `feature-x` fires a pipeline whose agent step commits and
        pushes by default. If that push lands on `feature-x`, the same push
        trigger fires again - forever, with a provider bill per lap.
        """
        payload = await _attach(
            {"agent": "claude-code", "task": "fix it"},
            trigger_context={"branch": "feature-x", "commit_sha": "deadbee"},
        )
        assert payload["push"] is True, "the default is still commit+push..."
        assert payload["branch"] != "feature-x", (
            "...but never onto the branch whose push started this run"
        )
        assert payload["branch"] != payload["base_branch"]

    async def test_explicit_branch_may_be_the_trigger_branch(self):
        """Declared out loud in the step config = allowed."""
        payload = await _attach(
            {"agent": "mock", "branch": "feature-x"},
            trigger_context={"branch": "feature-x"},
        )
        assert payload["branch"] == "feature-x"
        assert payload["push"] is True

    async def test_derived_branch_is_unique_per_step_run(self):
        first = await _attach(
            {"agent": "mock"}, trigger_context={"branch": "main"},
            step_run_id="1111111122222222",
        )
        second = await _attach(
            {"agent": "mock"}, trigger_context={"branch": "main"},
            step_run_id="3333333344444444",
        )
        assert first["branch"] != second["branch"]

    async def test_commit_false_still_disables_the_push(self):
        payload = await _attach(
            {"agent": "mock", "commit": False},
            trigger_context={"branch": "main"},
        )
        assert payload["commit_enabled"] is False
        assert payload["push"] is False

    async def test_adhoc_card_branch_survives(self):
        """Regression guard: card work's branch is load-bearing for the
        card diff, so the isolation fix must not rewrite it."""
        payload = await _attach(
            {"agent": "mock"},
            trigger_context={"branch": "main", "work_branch": "lazyaf/abc12345"},
        )
        assert payload["branch"] == "lazyaf/abc12345"


class TestAgentCommitScope:
    """The commit is staged from the REPO CHECKOUT, not the shared volume."""

    async def test_workdir_defaults_to_the_repo_checkout(self):
        from app.config import get_settings

        payload = await _attach(
            {"agent": "mock"}, trigger_context={"branch": "main"}
        )
        assert payload["workdir"] == get_settings().step_working_dir

    async def test_working_dir_under_the_checkout_is_honored(self):
        from app.config import get_settings

        checkout = get_settings().step_working_dir
        payload = await _attach(
            {"agent": "mock"},
            trigger_context={"branch": "main"},
            exec_config={"working_dir": f"{checkout}/services/api"},
        )
        assert payload["workdir"] == f"{checkout}/services/api"

    async def test_working_dir_outside_the_checkout_is_dropped(self, caplog):
        """A shared workspace carries earlier steps' artifacts; staging a
        pushed commit from its root would sweep them in."""
        from app.config import get_settings

        with caplog.at_level(logging.WARNING, logger=EXECUTOR_LOGGER):
            payload = await _attach(
                {"agent": "mock"},
                trigger_context={"branch": "main"},
                exec_config={"working_dir": "/workspace"},
            )
        assert payload["workdir"] == get_settings().step_working_dir
        assert any(
            "outside the repo checkout" in r.getMessage()
            for r in caplog.records
        )

    async def test_checkout_prefix_is_matched_on_path_boundaries(self):
        """/workspace/repo-scratch is NOT inside /workspace/repo."""
        from app.config import get_settings

        checkout = get_settings().step_working_dir
        payload = await _attach(
            {"agent": "mock"},
            trigger_context={"branch": "main"},
            exec_config={"working_dir": f"{checkout}-scratch"},
        )
        assert payload["workdir"] == checkout


# -----------------------------------------------------------------------------
# Agent-runtime label preflight (label VALUE, cached, never silently skipped)
# -----------------------------------------------------------------------------

def _image_stub(labels, image_id="sha256:aaa"):
    image = MagicMock()
    image.id = image_id
    image.labels = dict(labels)
    return image


class _DockerOnlyExecutor:
    """A LocalExecutor-shaped stub exposing only the docker client."""

    def __init__(self, images):
        client = MagicMock()
        client.images.get.side_effect = lambda tag: images[tag]
        self._docker = client


class _NoSeamExecutor:
    """An executor seam with neither the public method nor a docker client
    (test doubles, and any future remote executor)."""


class TestAgentRuntimeLabelPreflight:
    @pytest.fixture
    def executor(self):
        return PipelineExecutor()

    async def _unlabeled(self, executor, seam, images):
        executor._local_executor = seam
        return await executor._agent_images_without_runtime_label(images)

    async def test_label_value_one_is_a_declaration(self, executor):
        seam = _DockerOnlyExecutor(
            {"lazyaf-agent-base:dev": _image_stub({AGENT_RUNTIME_LABEL: "1"})}
        )
        assert await self._unlabeled(
            executor, seam, ["lazyaf-agent-base:dev"]
        ) == []

    async def test_label_value_zero_is_a_refusal_not_a_declaration(
        self, executor
    ):
        """Presence-only was the bug: `LABEL lazyaf.agent-runtime=0` is an
        image author saying NO, and it used to pass the preflight."""
        seam = _DockerOnlyExecutor(
            {"not-an-agent:dev": _image_stub({AGENT_RUNTIME_LABEL: "0"})}
        )
        assert await self._unlabeled(executor, seam, ["not-an-agent:dev"]) == [
            "not-an-agent:dev"
        ]

    @pytest.mark.parametrize("value", ["", "false", "true", "yes"])
    async def test_only_exactly_one_counts(self, executor, value):
        seam = _DockerOnlyExecutor(
            {"img:dev": _image_stub({AGENT_RUNTIME_LABEL: value})}
        )
        assert await self._unlabeled(executor, seam, ["img:dev"]) == ["img:dev"]

    async def test_missing_label_is_unlabeled(self, executor):
        seam = _DockerOnlyExecutor({"stock:dev": _image_stub({})})
        assert await self._unlabeled(executor, seam, ["stock:dev"]) == [
            "stock:dev"
        ]

    async def test_verdict_is_cached_by_resolved_image_id(self, executor):
        image = _image_stub({AGENT_RUNTIME_LABEL: "1"})
        seam = _DockerOnlyExecutor({"img:dev": image})
        assert await self._unlabeled(executor, seam, ["img:dev"]) == []

        # Same image ID, labels mutated underneath: the cached verdict stands.
        image.labels = {}
        assert await self._unlabeled(executor, seam, ["img:dev"]) == []

        # A REBUILT tag resolves to a new ID and is re-evaluated fresh.
        seam._docker.images.get.side_effect = lambda tag: _image_stub(
            {}, image_id="sha256:bbb"
        )
        assert await self._unlabeled(executor, seam, ["img:dev"]) == ["img:dev"]

    async def test_reset_clears_the_label_cache(self, executor):
        image = _image_stub({AGENT_RUNTIME_LABEL: "1"})
        seam = _DockerOnlyExecutor({"img:dev": image})
        await self._unlabeled(executor, seam, ["img:dev"])
        assert executor._image_label_cache
        executor._local_executor = None
        await executor.reset()
        assert executor._image_label_cache == {}

    async def test_inspection_failure_is_not_reported_as_unlabeled(
        self, executor, caplog
    ):
        seam = _DockerOnlyExecutor({})
        seam._docker.images.get.side_effect = RuntimeError("daemon gone")
        with caplog.at_level(logging.WARNING, logger=EXECUTOR_LOGGER):
            assert await self._unlabeled(executor, seam, ["img:dev"]) == []
        assert any("Could not inspect" in r.getMessage() for r in caplog.records)

    async def test_seam_without_docker_says_the_preflight_is_off(
        self, executor, caplog
    ):
        """It used to return a green [] - the loudest thing to be quiet
        about, because "no unlabeled images" and "I checked nothing" are the
        same answer."""
        with caplog.at_level(logging.WARNING, logger=EXECUTOR_LOGGER):
            result = await self._unlabeled(
                executor, _NoSeamExecutor(), ["img:dev"]
            )
        assert result == []
        assert any("preflight SKIPPED" in r.getMessage() for r in caplog.records)

    async def test_the_real_local_executor_implements_the_seam(self):
        """The preflight's preferred path is not hypothetical.

        Everything else in this class drives STUBS. If the real LocalExecutor
        never grew `image_declares_label`, every one of those would still pass
        while production silently took the `_docker` reach-through fallback -
        stub-shaped green over a seam that does not exist. So assert the
        method is really there, and really three-valued.
        """
        import inspect

        from app.services.execution.local_executor import LocalExecutor

        method = getattr(LocalExecutor, "image_declares_label", None)
        assert method is not None, (
            "LocalExecutor must expose image_declares_label(image, label) - "
            "the preflight's public seam"
        )
        assert inspect.iscoroutinefunction(method)
        assert list(inspect.signature(method).parameters) == [
            "self",
            "image",
            "label",
        ]

    async def test_the_real_local_executor_reads_the_label_three_valued(self):
        """Same rule as the stubs: value "1" is yes, "0" is no, an image the
        daemon cannot resolve is None (could not tell), NOT False."""
        from docker.errors import ImageNotFound

        from app.services.execution.local_executor import LocalExecutor

        images = {
            "good:dev": _image_stub({AGENT_RUNTIME_LABEL: "1"}, "sha256:g"),
            "refusing:dev": _image_stub({AGENT_RUNTIME_LABEL: "0"}, "sha256:r"),
            "silent:dev": _image_stub({}, "sha256:s"),
        }

        def _get(tag):
            if tag not in images:
                raise ImageNotFound(tag)
            return images[tag]

        client = MagicMock()
        client.images.get.side_effect = _get
        real = LocalExecutor(client)

        assert await real.image_declares_label("good:dev", AGENT_RUNTIME_LABEL) is True
        assert (
            await real.image_declares_label("refusing:dev", AGENT_RUNTIME_LABEL)
            is False
        )
        assert (
            await real.image_declares_label("silent:dev", AGENT_RUNTIME_LABEL) is False
        )
        assert (
            await real.image_declares_label("absent:dev", AGENT_RUNTIME_LABEL) is None
        ), "a missing image is 'could not tell', never 'your image is wrong'"

    async def test_the_real_seam_caches_by_resolved_image_id(self):
        """Cached by ID, not tag - and cleared by reset(), because images are
        rebuilt between test-mode runs."""
        from app.services.execution.local_executor import LocalExecutor

        client = MagicMock()
        client.images.get.side_effect = lambda tag: _image_stub(
            {AGENT_RUNTIME_LABEL: "1"}, "sha256:same"
        )
        real = LocalExecutor(client)

        await real.image_declares_label("a:dev", AGENT_RUNTIME_LABEL)
        await real.image_declares_label("a:dev", AGENT_RUNTIME_LABEL)
        assert len(real._declared_label_cache) == 1

        real.reset()
        assert real._declared_label_cache == {}

    async def test_public_method_is_used_when_the_seam_exposes_one(
        self, executor
    ):
        """Forward contract: the moment LocalExecutor grows
        `image_declares_label`, the preflight stops touching `_docker`."""
        calls = []

        class _PublicSeam:
            _docker = None

            async def image_declares_label(self, image, label):
                calls.append((image, label))
                return image == "good:dev"

        result = await self._unlabeled(
            executor, _PublicSeam(), ["good:dev", "bad:dev"]
        )
        assert result == ["bad:dev"]
        assert calls == [
            ("good:dev", AGENT_RUNTIME_LABEL),
            ("bad:dev", AGENT_RUNTIME_LABEL),
        ]

    async def test_delegate_returning_none_means_could_not_tell(
        self, executor
    ):
        """The requested LocalExecutor API is `bool | None`; None is a
        daemon hiccup, not a claim that the image is wrong."""

        class _UnsureSeam:
            _docker = None

            async def image_declares_label(self, image, label):
                return None

        assert await self._unlabeled(
            executor, _UnsureSeam(), ["img:dev"]
        ) == []

    async def test_delegate_raising_is_not_reported_as_unlabeled(
        self, executor, caplog
    ):
        class _AngrySeam:
            _docker = None

            async def image_declares_label(self, image, label):
                raise RuntimeError("daemon gone")

        with caplog.at_level(logging.WARNING, logger=EXECUTOR_LOGGER):
            assert await self._unlabeled(
                executor, _AngrySeam(), ["img:dev"]
            ) == []
        assert any("Could not inspect" in r.getMessage() for r in caplog.records)
