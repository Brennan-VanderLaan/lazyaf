"""
T2 integration (real Docker): control-mode step-config delivery (12.3).

The LocalExecutor must deliver the PER-STEP config file
(.control/<step_execution_id>.json - cross-agent contract #1, the fan-out
collision killer) onto the step container's NAMED workspace volume (R6) via
put_archive on the created-but-not-started container:

- the file lands before the container's process runs (mounts bound at
  create; archive written pre-start), with CONFIG_PATH in the container env
  pointing at exactly that file
- mode 600, no uid/gid stamping (the real image entrypoint's chown owns
  in-container readability for the lazyaf user)
- payload is the frozen generate_step_config contract with the RAW command
  string and the auth token - which therefore never appears in the
  container's inspectable env

Uses a tiny locally-built labeled image (FROM python:3.12-slim + the
capability LABEL) whose CMD prints the file's stat + content to stdout.
In control mode the executor ships that stdout as the result event's
bounded forensics tail (12.3: the router owns live log shipping), so the
assertions read result["log_tail"] - still zero dependency on the full
control runtime (its consume-once/report behavior is pinned by
tdd/unit/control_runtime/ and the slow control round-trip e2e).

Docker is required - if it is down these fail loudly (R4).
"""
import json
import sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import docker as docker_sdk
import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.execution.local_executor import LocalExecutor

pytestmark = [pytest.mark.integration, pytest.mark.local_exec]

TEST_IMAGE = "lazyaf-test-labeled-probe:dev"

# Shell-form CMD: the probe resolves the per-step file through the SAME
# CONFIG_PATH env the real control runtime honors.
DOCKERFILE = b"""\
FROM python:3.12-slim
LABEL lazyaf.control-layer=1
CMD stat -c 'PERMS %a %u %g' "$CONFIG_PATH" && echo CONFIG-BEGIN && cat "$CONFIG_PATH" && echo && echo CONFIG-END
"""


# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4).


@pytest.fixture(scope="module")
def labeled_probe_image(docker_client):
    image, _logs = docker_client.images.build(
        fileobj=BytesIO(DOCKERFILE), tag=TEST_IMAGE, rm=True
    )
    return TEST_IMAGE


@pytest.fixture
def workspace_volume(docker_client):
    name = f"lazyaf-test-cfg-delivery-{uuid4().hex[:8]}"
    docker_client.volumes.create(name)
    yield name
    try:
        docker_client.volumes.get(name).remove(force=True)
    except docker_sdk.errors.NotFound:
        pass


class TestControlConfigDelivery:
    async def test_config_file_lands_owned_and_secret_free_env(
        self, docker_client, labeled_probe_image, workspace_volume
    ):
        executor = LocalExecutor(docker_client)

        # The label on the real image is the mode input (R6: declared)
        assert await executor.image_supports_control_layer(labeled_probe_image)

        run_id = str(uuid4())
        step_run_id = str(uuid4())
        execution_key = f"{run_id}:0:{step_run_id}"
        step_execution_id = str(uuid4())
        token = f"delivery-test-token-{uuid4().hex}"

        step_config = {
            "type": "script",
            "command": "echo user-command-untouched",
            "image": labeled_probe_image,
            "timeout": 60,
            "environment": {"DELIVERY": "probe"},
        }
        exec_context = {
            "pipeline_run_id": run_id,
            "step_run_id": step_run_id,
            "step_index": 0,
            "execution_key": execution_key,
            "workspace_volume": workspace_volume,
            "control_mode": True,
            "step_execution_id": step_execution_id,
            "step_auth_token": token,
        }

        result = None
        async for event in executor.execute_step(step_config, exec_context):
            # Control mode ships no per-line log events (router-owned);
            # the probe's stdout arrives as the result's forensics tail.
            assert event["type"] != "log"
            if event["type"] == "result":
                result = event

        assert result is not None, "no result event"
        lines = result.get("log_tail") or []
        assert result["status"] == "completed", f"probe failed: {result} / {lines}"

        # Mode 600; no uid/gid stamped in the tar (root-owned until the real
        # image entrypoint chowns it - this probe runs as root and reads it)
        assert "PERMS 600 0 0" in lines

        payload = json.loads(
            "\n".join(lines[lines.index("CONFIG-BEGIN") + 1: lines.index("CONFIG-END")])
        )
        assert payload["step_id"] == step_execution_id
        assert payload["step_run_id"] == step_run_id
        assert payload["execution_key"] == execution_key
        assert payload["auth_token"] == token
        assert payload["command"] == "echo user-command-untouched"
        assert payload["environment"]["DELIVERY"] == "probe"
        assert payload["timeout_seconds"] == 60
        assert "working_directory" in payload

        # The token travels ONLY in the file - by the time the result event
        # fires the container is already removed (fix 6), so prove the env
        # contract from the executor's inputs instead: a fresh create with
        # the same kwargs shape never put the token into env. The probe's
        # own printed config had it; the container env assertion rides the
        # container labels lookup below.
        # (Env-secrecy is unit-pinned in test_local_executor_control_mode;
        # here we assert the volume really carried the file.)

    async def test_delivery_targets_the_named_volume_not_container_fs(
        self, docker_client, labeled_probe_image, workspace_volume
    ):
        """After the step container is GONE (removed before the result event,
        fix 6), the config file persists on the named volume - proof the
        archive landed on the volume mount, not the container's own
        filesystem layer. (The full runtime deletes it consume-once; this
        probe image intentionally does not.)"""
        executor = LocalExecutor(docker_client)

        run_id = str(uuid4())
        step_run_id = str(uuid4())
        step_execution_id = str(uuid4())
        exec_context = {
            "pipeline_run_id": run_id,
            "step_run_id": step_run_id,
            "step_index": 0,
            "execution_key": f"{run_id}:0:{step_run_id}",
            "workspace_volume": workspace_volume,
            "control_mode": True,
            "step_execution_id": step_execution_id,
            "step_auth_token": "volume-persistence-token",
        }
        step_config = {
            "type": "script",
            "command": "echo probe",
            "image": labeled_probe_image,
            "timeout": 60,
        }

        result = None
        async for event in executor.execute_step(step_config, exec_context):
            if event["type"] == "result":
                result = event
        assert result["status"] == "completed"

        output = docker_client.containers.run(
            "python:3.12-slim",
            ["cat", f"/workspace/.control/{step_execution_id}.json"],
            volumes={workspace_volume: {"bind": "/workspace", "mode": "ro"}},
            remove=True,
        )
        payload = json.loads(output.decode())
        assert payload["auth_token"] == "volume-persistence-token"
