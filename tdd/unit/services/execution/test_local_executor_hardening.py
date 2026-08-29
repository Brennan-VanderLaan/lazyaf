"""
Unit tests for LocalExecutor hardening (Phase 12.2-INT).

Covers the hardening contract on top of the base executor contract
(test_local_executor_contract.py):

(a) shell-wrapping of string script commands as ["bash", "-c", ...]
    (docker-py shlex-splits raw strings - multiline piped scripts break
    otherwise); list commands pass through untouched; shell overridable
    explicitly via step_config["shell"].
(b) explicit mount addressing enum (volume | bind) - NEVER inferred from
    path shape; a Windows path (C:\\...) declared bind goes through the
    typed Mount API; a path-shaped source declared volume fails loudly (R6).
(c) network kwarg sourced from settings.container_network.
(d) default image + working_dir sourced from settings.
(e) HOME pinned to settings.step_home_dir and the dir ensured on start
    (tool installs in one step survive to the next via the shared volume).
(f) migration-compat matrix (ported from failure_01's
    test_migration_compatibility.py ideas, re-targeted at THIS executor):
    old yaml shapes, missing image key, multiline commands, all step types.

Plus: the streaming deadline - timeout fires DURING log streaming (the
failure mode that kept the real-docker timeout test skipped).
"""
import sys
import time
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.config import get_settings  # noqa: E402
from app.services.execution.local_executor import (  # noqa: E402
    DEFAULT_STEP_IMAGE,
    LocalExecutor,
    MountAddressing,
    MountSpec,
    build_container_mounts,
    build_step_command,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

def make_mock_container(log_lines=(), exit_code=0):
    container = MagicMock()
    container.id = "container-123"
    container.logs = MagicMock(return_value=iter(list(log_lines)))
    container.wait = MagicMock(return_value={"StatusCode": exit_code})
    return container


@pytest.fixture
def mock_docker_client():
    client = MagicMock()
    client.containers = MagicMock()
    client.volumes = MagicMock()
    client.networks = MagicMock()
    return client


@pytest.fixture
def execution_context():
    run_id = str(uuid4())
    return {
        "pipeline_run_id": run_id,
        "step_run_id": str(uuid4()),
        "step_index": 0,
        "execution_key": f"{run_id}:0:1",
        "workspace_volume": f"lazyaf-ws-{uuid4().hex[:8]}",
    }


async def run_step(client, step_config, execution_context):
    """Drive execute_step to completion; return (events, run_call)."""
    executor = LocalExecutor(docker_client=client)
    events = []
    async for event in executor.execute_step(step_config, execution_context):
        events.append(event)
    return events, client.containers.run.call_args


# -----------------------------------------------------------------------------
# (a) Shell wrapping
# -----------------------------------------------------------------------------

class TestShellWrapping:
    async def test_string_command_wrapped_in_bash(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo hello | grep hello"},
            execution_context,
        )
        command = call[1]["command"]
        assert command[0] == "bash"
        assert command[1] == "-c"
        assert "echo hello | grep hello" in command[2]

    async def test_multiline_piped_script_preserved_verbatim(self, mock_docker_client, execution_context):
        script = (
            'export PATH="$HOME/.local/bin:$PATH"\n'
            "cd backend\n"
            "uv sync --all-extras 2>&1 | tail -5\n"
            "uv run pytest ../tdd -m 'not slow'"
        )
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": script},
            execution_context,
        )
        command = call[1]["command"]
        assert command[:2] == ["bash", "-c"]
        # The whole multiline script arrives as ONE argv element - docker-py
        # never gets the chance to shlex-split it.
        assert script in command[2]

    async def test_list_command_passes_through_untouched(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": ["sh", "-c", "echo hi"]},
            execution_context,
        )
        assert call[1]["command"] == ["sh", "-c", "echo hi"]

    async def test_shell_explicitly_overridable(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo hi", "shell": "sh"},
            execution_context,
        )
        assert call[1]["command"][0] == "sh"

    def test_build_step_command_unit(self):
        command = build_step_command({"command": "pytest -v"}, "/workspace/home")
        assert command[0:2] == ["bash", "-c"]
        # (fix 8) fail-fast prelude: the FIRST failing line fails the step,
        # not just the last line's exit code
        assert command[2].startswith("set -e\n")
        # (e) the wrapper ensures HOME exists before the user command runs
        assert "mkdir -p /workspace/home" in command[2]
        assert command[2].endswith("pytest -v")

    async def test_wrapped_script_runs_under_set_e(self, mock_docker_client, execution_context):
        """(fix 8) every shell-wrapped step script starts with set -e."""
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "false\necho survived"},
            execution_context,
        )
        script = call[1]["command"][2]
        assert script.startswith("set -e\n")

    def test_list_form_commands_own_their_home(self):
        """(fix 8) exec-form commands get NO shell prelude: they own their
        HOME (documented contract - no set -e / mkdir injection possible)."""
        command = build_step_command(
            {"command": ["python", "-m", "pytest"]}, "/workspace/home"
        )
        assert command == ["python", "-m", "pytest"]


# -----------------------------------------------------------------------------
# (b) Explicit mount addressing (R6)
# -----------------------------------------------------------------------------

class TestMountAddressing:
    def test_windows_path_declared_bind_is_bind(self):
        """A Windows host path declared BIND is a typed bind Mount - shape never consulted."""
        spec = MountSpec(
            addressing=MountAddressing.BIND,
            source=r"C:\Users\brennan\seed-repo",
            target="/seed",
            mode="ro",
        )
        volumes, mounts = build_container_mounts([spec])
        assert volumes == {}  # never lands in the engine-inferred volumes dict
        assert len(mounts) == 1
        assert mounts[0]["Type"] == "bind"
        assert mounts[0]["Source"] == r"C:\Users\brennan\seed-repo"
        assert mounts[0]["Target"] == "/seed"
        assert mounts[0]["ReadOnly"] is True

    def test_named_volume_declared_volume_is_volume(self):
        spec = MountSpec(
            addressing=MountAddressing.VOLUME,
            source="lazyaf-ws-abc123",
            target="/workspace",
        )
        volumes, mounts = build_container_mounts([spec])
        assert mounts == []
        assert volumes == {"lazyaf-ws-abc123": {"bind": "/workspace", "mode": "rw"}}

    def test_windows_path_declared_volume_fails_loudly(self):
        """A path-shaped source declared VOLUME raises - no silent bind mount."""
        spec = MountSpec(
            addressing=MountAddressing.VOLUME,
            source=r"C:\Users\brennan\not-a-volume",
            target="/workspace",
        )
        with pytest.raises(ValueError, match="not a valid docker volume name"):
            build_container_mounts([spec])

    def test_unix_path_declared_volume_fails_loudly(self):
        spec = MountSpec(
            addressing=MountAddressing.VOLUME,
            source="/tmp/some/dir",
            target="/workspace",
        )
        with pytest.raises(ValueError):
            build_container_mounts([spec])

    def test_mount_dict_without_addressing_rejected(self):
        with pytest.raises(ValueError, match="addressing"):
            build_container_mounts(
                [{"source": "lazyaf-ws-x", "target": "/workspace"}]
            )

    def test_mount_dict_with_invalid_addressing_rejected(self):
        with pytest.raises(ValueError, match="invalid mount addressing"):
            build_container_mounts(
                [{"addressing": "guess", "source": "x", "target": "/y"}]
            )

    async def test_allowlisted_bind_mount_flows_through(self, mock_docker_client, execution_context):
        """(fix 10) the docker socket is the one default-allowlisted bind
        source from step config; it flows through the typed Mount API."""
        from app.services.execution.local_executor import DOCKER_SOCKET_SOURCE

        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "docker ps",
                "mounts": [
                    {
                        "addressing": "bind",
                        "source": DOCKER_SOCKET_SOURCE,
                        "target": DOCKER_SOCKET_SOURCE,
                        "mode": "rw",
                    }
                ],
            },
            execution_context,
        )
        kwargs = call[1]
        # workspace volume still in the volumes dict (base contract)
        assert execution_context["workspace_volume"] in kwargs["volumes"]
        # the bind went through the typed Mount API
        assert len(kwargs["mounts"]) == 1
        assert kwargs["mounts"][0]["Type"] == "bind"
        assert kwargs["mounts"][0]["Source"] == DOCKER_SOCKET_SOURCE

    async def test_non_allowlisted_bind_mount_fails_step_loudly(
        self, mock_docker_client, execution_context
    ):
        """(fix 10) a raw bind mount outside the allowlist is a clear config
        error at dispatch - the container is never spawned."""
        mock_docker_client.containers.run.return_value = make_mock_container()
        events, _ = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "cat /host/etc/passwd",
                "mounts": [
                    {
                        "addressing": "bind",
                        "source": "/etc",
                        "target": "/host/etc",
                        "mode": "ro",
                    }
                ],
            },
            execution_context,
        )
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "failed"
        assert "not permitted" in result["error"]
        assert "/etc" in result["error"]
        mock_docker_client.containers.run.assert_not_called()

    async def test_windows_bind_mount_from_step_config_rejected(
        self, mock_docker_client, execution_context
    ):
        """(fix 10) path shape is irrelevant: a Windows host path is rejected
        by POLICY (not addressing inference) when not allowlisted."""
        mock_docker_client.containers.run.return_value = make_mock_container()
        events, _ = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "ls /seed",
                "mounts": [
                    {
                        "addressing": "bind",
                        "source": r"C:\seed\repos",
                        "target": "/seed",
                        "mode": "ro",
                    }
                ],
            },
            execution_context,
        )
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "failed"
        assert "not permitted" in result["error"]
        mock_docker_client.containers.run.assert_not_called()

    async def test_extra_named_volume_mount_still_allowed(
        self, mock_docker_client, execution_context
    ):
        """(fix 10) the allowlist gates BIND sources only; extra named
        volumes carry no host paths and pass through."""
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "ls /cache",
                "mounts": [
                    {
                        "addressing": "volume",
                        "source": "lazyaf-cache",
                        "target": "/cache",
                    }
                ],
            },
            execution_context,
        )
        assert call[1]["volumes"]["lazyaf-cache"] == {"bind": "/cache", "mode": "rw"}

    async def test_bad_mount_config_fails_step_loudly(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        events, _ = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "ls",
                "mounts": [{"source": "/x", "target": "/y"}],  # no addressing
            },
            execution_context,
        )
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "failed"
        assert "addressing" in result["error"]
        # No container was ever spawned with the ambiguous mount
        mock_docker_client.containers.run.assert_not_called()


# -----------------------------------------------------------------------------
# (c) Network kwarg from settings
# -----------------------------------------------------------------------------

class TestNetworkKwarg:
    async def test_container_attached_to_settings_network(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo hi"},
            execution_context,
        )
        assert call[1]["network"] == get_settings().container_network
        assert call[1]["network"] == "lazyaf-network"


# -----------------------------------------------------------------------------
# (d) Default image + working_dir from settings
# -----------------------------------------------------------------------------

class TestSettingsDrivenDefaults:
    async def test_default_image_from_settings(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo hi"},  # no image key
            execution_context,
        )
        assert call[0][0] == get_settings().step_default_image
        assert DEFAULT_STEP_IMAGE == get_settings().step_default_image

    async def test_default_working_dir_from_settings(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo hi"},
            execution_context,
        )
        assert call[1]["working_dir"] == get_settings().step_working_dir
        assert call[1]["working_dir"] == "/workspace/repo"

    async def test_working_dir_overridable_per_step(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "npm test", "working_dir": "/workspace/repo/frontend"},
            execution_context,
        )
        assert call[1]["working_dir"] == "/workspace/repo/frontend"


# -----------------------------------------------------------------------------
# (e) HOME persistence contract
# -----------------------------------------------------------------------------

class TestHomeDirectory:
    async def test_home_env_set_from_settings(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "pip install pytest"},
            execution_context,
        )
        env = call[1]["environment"]
        assert env["HOME"] == get_settings().step_home_dir
        assert env["HOME"] == "/workspace/home"

    async def test_home_dir_ensured_in_wrapped_command(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "pip install uv"},
            execution_context,
        )
        script = call[1]["command"][2]
        assert "mkdir -p /workspace/home" in script

    async def test_explicit_user_home_wins(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "echo $HOME",
                "environment": {"HOME": "/custom/home"},
            },
            execution_context,
        )
        assert call[1]["environment"]["HOME"] == "/custom/home"


# -----------------------------------------------------------------------------
# (f) Migration-compat matrix (ported from failure_01, re-targeted here)
# -----------------------------------------------------------------------------

class TestMigrationCompatMatrix:
    """Old pipeline yaml shapes keep working through the hardened executor."""

    async def test_script_step_without_image_uses_default(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "pytest -v"},
            execution_context,
        )
        assert call[0][0] == DEFAULT_STEP_IMAGE

    async def test_script_step_with_multiline_command_completes(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        events, call = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": (
                    "\n"
                    '    export PATH="$HOME/.local/bin:$PATH"\n'
                    "    cd backend\n"
                    "    uv sync --all-extras\n"
                ),
            },
            execution_context,
        )
        assert call[1]["command"][0] == "bash"
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "completed"

    async def test_docker_step_existing_format(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "docker", "command": "pip install pytest && pytest", "image": "python:3.12"},
            execution_context,
        )
        assert call[0][0] == "python:3.12"
        # && only means "and then" inside a real shell
        assert call[1]["command"][:2] == ["bash", "-c"]

    async def test_docker_step_private_registry_image(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "docker", "command": "run-tests", "image": "ghcr.io/myorg/myimage:v1.0.0"},
            execution_context,
        )
        assert call[0][0] == "ghcr.io/myorg/myimage:v1.0.0"

    async def test_custom_image_step(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "pytest", "image": "lazyaf-test-runner:latest"},
            execution_context,
        )
        assert call[0][0] == "lazyaf-test-runner:latest"

    async def test_environment_variables_preserved(self, mock_docker_client, execution_context):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {
                "type": "script",
                "command": "npm test",
                "environment": {"NODE_ENV": "test", "CI": "true", "COVERAGE": "1"},
            },
            execution_context,
        )
        env = call[1]["environment"]
        assert env["NODE_ENV"] == "test"
        assert env["CI"] == "true"
        assert env["COVERAGE"] == "1"

    @pytest.mark.parametrize("step_type", ["script", "docker", "agent"])
    async def test_all_step_types_execute(self, mock_docker_client, execution_context, step_type):
        """The executor itself is type-agnostic (routing happens upstream)."""
        mock_docker_client.containers.run.return_value = make_mock_container()
        events, _ = await run_step(
            mock_docker_client,
            {"type": step_type, "command": "echo hi"},
            execution_context,
        )
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "completed"

    async def test_timeout_config_respected(self, mock_docker_client, execution_context):
        container = make_mock_container()
        container.wait = MagicMock(side_effect=TimeoutError("timed out"))
        mock_docker_client.containers.run.return_value = container
        events, _ = await run_step(
            mock_docker_client,
            {"type": "script", "command": "sleep 1000", "timeout": 7},
            execution_context,
        )
        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "timeout"
        assert result["timeout_seconds"] == 7


# -----------------------------------------------------------------------------
# Container cleanup at step end (fix 6): removal happens BEFORE the result
# event is yielded - never dependent on generator GC
# -----------------------------------------------------------------------------

class TestCleanupBeforeResult:
    async def test_container_removed_before_result_event_yielded(
        self, mock_docker_client, execution_context
    ):
        container = make_mock_container(log_lines=[b"working\n"])
        container.remove = MagicMock()
        mock_docker_client.containers.run.return_value = container

        executor = LocalExecutor(docker_client=mock_docker_client)
        async for event in executor.execute_step(
            {"type": "script", "command": "echo hi"}, execution_context
        ):
            if event["type"] == "result":
                # The moment the consumer SEES the result, the container is
                # already gone and the registry entry cleared.
                container.remove.assert_called_once()
                assert executor._running_containers == {}
                break

    async def test_container_removed_before_result_on_failure(
        self, mock_docker_client, execution_context
    ):
        container = make_mock_container(exit_code=3)
        container.remove = MagicMock()
        mock_docker_client.containers.run.return_value = container

        executor = LocalExecutor(docker_client=mock_docker_client)
        async for event in executor.execute_step(
            {"type": "script", "command": "exit 3"}, execution_context
        ):
            if event["type"] == "result":
                assert event["status"] == "failed"
                container.remove.assert_called_once()
                assert executor._running_containers == {}
                break

    async def test_consumer_stopping_at_result_still_cleans_up(
        self, mock_docker_client, execution_context
    ):
        """A consumer that returns on the result event (like the pipeline
        executor) must not leave cleanup to garbage collection."""
        container = make_mock_container()
        container.remove = MagicMock()
        mock_docker_client.containers.run.return_value = container

        executor = LocalExecutor(docker_client=mock_docker_client)
        stream = executor.execute_step(
            {"type": "script", "command": "echo hi"}, execution_context
        )
        async for event in stream:
            if event["type"] == "result":
                break  # abandon without exhausting - the real consumer shape
        container.remove.assert_called_once()
        assert executor._running_containers == {}


# -----------------------------------------------------------------------------
# Backend URL env injection (fix 12)
# -----------------------------------------------------------------------------

class TestBackendUrlInjection:
    async def test_lazyaf_backend_url_injected_alongside_run_id_vars(
        self, mock_docker_client, execution_context
    ):
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "echo $LAZYAF_BACKEND_URL"},
            execution_context,
        )
        env = call[1]["environment"]
        assert "LAZYAF_PIPELINE_RUN_ID" in env  # run-id vars still there
        expected = getattr(
            get_settings(), "container_backend_url", "http://backend:8000"
        )
        assert env["LAZYAF_BACKEND_URL"] == expected

    async def test_backend_url_default_when_setting_absent(
        self, mock_docker_client, execution_context, monkeypatch
    ):
        """The setting is owned by a parallel config change; the executor
        must default sanely when it has not landed."""
        import app.services.execution.local_executor as le_mod

        real_settings = get_settings()

        class NoUrlSettings:
            def __getattr__(self, name):
                if name == "container_backend_url":
                    raise AttributeError(name)
                return getattr(real_settings, name)

        monkeypatch.setattr(le_mod, "get_settings", lambda: NoUrlSettings())
        mock_docker_client.containers.run.return_value = make_mock_container()
        _, call = await run_step(
            mock_docker_client,
            {"type": "script", "command": "env"},
            execution_context,
        )
        assert call[1]["environment"]["LAZYAF_BACKEND_URL"] == "http://backend:8000"


# -----------------------------------------------------------------------------
# make_docker_client (cross-file contract #1)
# -----------------------------------------------------------------------------

class TestMakeDockerClient:
    def test_honors_docker_host_setting(self, monkeypatch):
        import docker as docker_sdk

        import app.services.execution.local_executor as le_mod
        from app.services.execution.local_executor import make_docker_client

        real_settings = get_settings()

        class HostedSettings:
            docker_host = "tcp://dockerd.example:2375"

            def __getattr__(self, name):
                return getattr(real_settings, name)

        built = {}

        class FakeClient:
            def __init__(self, base_url=None):
                built["base_url"] = base_url

        monkeypatch.setattr(le_mod, "get_settings", lambda: HostedSettings())
        monkeypatch.setattr(docker_sdk, "DockerClient", FakeClient)

        client = make_docker_client()
        assert isinstance(client, FakeClient)
        assert built["base_url"] == "tcp://dockerd.example:2375"

    def test_falls_back_to_from_env_without_docker_host(self, monkeypatch):
        import docker as docker_sdk

        import app.services.execution.local_executor as le_mod
        from app.services.execution.local_executor import make_docker_client

        real_settings = get_settings()

        class NoHostSettings:
            docker_host = None

            def __getattr__(self, name):
                return getattr(real_settings, name)

        sentinel = object()
        monkeypatch.setattr(le_mod, "get_settings", lambda: NoHostSettings())
        monkeypatch.setattr(docker_sdk, "from_env", lambda: sentinel)

        assert make_docker_client() is sentinel


# -----------------------------------------------------------------------------
# Streaming deadline: timeout fires DURING log streaming
# -----------------------------------------------------------------------------

class TestStreamingDeadline:
    async def test_timeout_fires_while_logs_stream(self, mock_docker_client, execution_context):
        """The deadline no longer waits for the log stream to end.

        (The failure mode that kept the real-docker timeout test skipped:
        blocking log iteration meant container.wait's timeout never ran.)
        """
        def endless_logs(**kwargs):
            def gen():
                yield b"starting\n"
                time.sleep(3)  # simulates a container that keeps running
                yield b"never seen\n"
            return gen()

        container = MagicMock()
        container.id = "container-123"
        container.logs = MagicMock(side_effect=endless_logs)
        container.kill = MagicMock()
        mock_docker_client.containers.run.return_value = container

        executor = LocalExecutor(docker_client=mock_docker_client)
        events = []
        start = time.monotonic()
        async for event in executor.execute_step(
            {"type": "script", "command": "sleep 1000", "timeout": 0.5},
            execution_context,
        ):
            events.append(event)
        elapsed = time.monotonic() - start

        result = next(e for e in events if e["type"] == "result")
        assert result["status"] == "timeout"
        container.kill.assert_called_once()
        # Fired at the deadline, not at end-of-stream (3s+)
        assert elapsed < 2.5
        # The line emitted before the deadline still streamed through
        assert any(e.get("line") == "starting" for e in events if e["type"] == "log")
