"""
Unit tests for LocalExecutor's control mode (Phase 12.3, wave2-123-wiring +
the 12.3 adversarial-review hardening):

- image_supports_control_layer: capability requires the image's
  `lazyaf.control-layer` LABEL with VALUE "1" (declared by the image author,
  R6 - presence alone is not a declaration). The tag is resolved on every
  dispatch; the verdict is cached by resolved IMAGE ID, so a rebuilt tag
  (new ID) is re-evaluated while repeat dispatches of the same build reuse
  the verdict. Missing images stay uncached-False (the step then fails
  loudly on spawn).
- Config delivery: control-mode spawn is create -> put_archive -> start;
  the step config file travels ONLY in the workspace tar (mode 0600, no
  uid/gid stamping - the image entrypoint's chown owns readability), named
  PER STEP EXECUTION (.control/<step_execution_id>.json) with CONFIG_PATH
  pointing the runtime at exactly that file - parallel steps of one run can
  no longer clobber each other's config. The payload is VERBATIM
  generate_step_config output (frozen producer contract, R3) with the RAW
  string command.
- Container command is None in control mode (the entrypoint's
  LAZYAF_CONTROL=1 switch runs the control runtime); stdout mode still uses
  containers.run with LAZYAF_CONTROL=0 and shell-wrapped commands.
- Forensics tail: in control mode the executor ships NO per-line log events
  (the router is the sole log writer, R3) and instead surfaces a bounded
  stdout tail on the result event.
"""
import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from docker.errors import ImageNotFound

from app.services.execution.local_executor import (
    CONTROL_LAYER_LABEL,
    CONTROL_MODE_LOG_TAIL_LINES,
    LocalExecutor,
    build_step_config_archive,
)


LABELED_IMAGE = "lazyaf-base:dev"


@pytest.fixture
def mock_docker_client():
    client = MagicMock()
    client.containers = MagicMock()
    client.images = MagicMock()
    client.networks = MagicMock()
    return client


@pytest.fixture
def control_context():
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
        "step_auth_token": "test-jwt-token",
    }


@pytest.fixture
def control_step_config():
    return {
        "type": "script",
        "command": "echo control && echo done",
        "image": LABELED_IMAGE,
        "timeout": 60,
        "environment": {"DEBUG": "1"},
    }


def make_labeled_image(image_id: str = "sha256:aaa", value: str = "1"):
    image = MagicMock()
    image.id = image_id
    image.labels = {CONTROL_LAYER_LABEL: value}
    return image


def make_finished_container(log_lines=(b"line\n",)):
    container = MagicMock()
    container.id = "container-123"
    container.logs = MagicMock(return_value=iter(log_lines))
    container.wait = MagicMock(return_value={"StatusCode": 0})
    return container


def extract_config_from_tar(tar_bytes: bytes, filename: str) -> tuple[dict, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        info = tar.getmember(f".control/{filename}")
        payload = json.load(tar.extractfile(info))
    return payload, info


# -----------------------------------------------------------------------------
# Capability label inspection (label VALUE trust + image-ID cache)
# -----------------------------------------------------------------------------

class TestImageCapabilityLabel:
    async def test_labeled_image_supports_control(self, mock_docker_client):
        mock_docker_client.images.get.return_value = make_labeled_image()

        executor = LocalExecutor(mock_docker_client)
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is True

    async def test_unlabeled_image_is_stdout_mode(self, mock_docker_client):
        image = MagicMock()
        image.id = "sha256:bbb"
        image.labels = {"maintainer": "someone"}
        mock_docker_client.images.get.return_value = image

        executor = LocalExecutor(mock_docker_client)
        assert await executor.image_supports_control_layer("python:3.12") is False

    async def test_label_value_must_be_1(self, mock_docker_client):
        """Label TRUST hardening: `lazyaf.control-layer=true` (or 0, or
        empty) is NOT a capability declaration - only the value "1" is."""
        executor = LocalExecutor(mock_docker_client)
        for image_id, value in (("sha256:t", "true"), ("sha256:z", "0"), ("sha256:e", "")):
            mock_docker_client.images.get.return_value = make_labeled_image(image_id, value)
            assert await executor.image_supports_control_layer(LABELED_IMAGE) is False

    async def test_verdict_cached_by_image_id_not_tag(self, mock_docker_client):
        """The tag is resolved per dispatch (images.get every call); the
        verdict is keyed by the RESOLVED image id, so a rebuilt tag with a
        new id is re-evaluated instead of serving a stale tag-cache hit."""
        executor = LocalExecutor(mock_docker_client)

        mock_docker_client.images.get.return_value = make_labeled_image("sha256:aaa")
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is True
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is True
        # Re-resolved per dispatch - no tag-keyed shortcut
        assert mock_docker_client.images.get.call_count == 2

        # Same tag rebuilt WITHOUT the label (new image id): the old True
        # verdict must not leak onto the new build
        rebuilt = MagicMock()
        rebuilt.id = "sha256:rebuilt"
        rebuilt.labels = {}
        mock_docker_client.images.get.return_value = rebuilt
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is False

    async def test_missing_image_is_false_and_not_cached(self, mock_docker_client):
        mock_docker_client.images.get.side_effect = ImageNotFound("nope")

        executor = LocalExecutor(mock_docker_client)
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is False
        # Not cached: once the image is built, the next dispatch re-inspects
        mock_docker_client.images.get.side_effect = None
        mock_docker_client.images.get.return_value = make_labeled_image()
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is True

    async def test_reset_clears_label_cache(self, mock_docker_client):
        mock_docker_client.images.get.return_value = make_labeled_image()

        executor = LocalExecutor(mock_docker_client)
        await executor.image_supports_control_layer(LABELED_IMAGE)
        assert executor._control_label_cache
        executor.reset()
        assert executor._control_label_cache == {}
        assert await executor.image_supports_control_layer(LABELED_IMAGE) is True


# -----------------------------------------------------------------------------
# Image preflight resolution (12.3 hardening)
# -----------------------------------------------------------------------------

class TestFindMissingImages:
    async def test_reports_only_unresolvable_tags(self, mock_docker_client):
        def get_image(tag):
            if tag == "ghost:one":
                raise ImageNotFound(tag)
            return make_labeled_image(f"sha256:{tag}")

        mock_docker_client.images.get.side_effect = get_image

        executor = LocalExecutor(mock_docker_client)
        missing = await executor.find_missing_images(
            ["ghost:one", "python:3.12", LABELED_IMAGE]
        )
        assert missing == ["ghost:one"]

    async def test_daemon_errors_are_not_missing(self, mock_docker_client):
        """A daemon hiccup must not fail preflight - dispatch surfaces it."""
        mock_docker_client.images.get.side_effect = RuntimeError("daemon down")

        executor = LocalExecutor(mock_docker_client)
        assert await executor.find_missing_images(["python:3.12"]) == []


# -----------------------------------------------------------------------------
# Config archive
# -----------------------------------------------------------------------------

class TestStepConfigArchive:
    def test_archive_contains_per_step_config_file(self):
        tar_bytes = build_step_config_archive(
            {"step_id": "s-1", "auth_token": "t"}, "s-1.json"
        )
        payload, info = extract_config_from_tar(tar_bytes, "s-1.json")

        assert payload == {"step_id": "s-1", "auth_token": "t"}
        assert info.mode == 0o600
        # No uid/gid stamping (12.3 cleanup): the image entrypoint's chown
        # of /workspace/.control owns in-container readability.
        assert info.uid == 0
        assert info.gid == 0

    def test_archive_includes_control_directory_entry(self):
        tar_bytes = build_step_config_archive({}, "whatever.json")
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            dir_info = tar.getmember(".control")
        assert dir_info.isdir()
        assert dir_info.uid == 0
        assert dir_info.gid == 0


# -----------------------------------------------------------------------------
# Control-mode spawn: create -> put_archive -> start
# -----------------------------------------------------------------------------

class TestControlModeSpawn:
    async def _run(self, executor, step_config, context):
        events = []
        async for event in executor.execute_step(step_config, context):
            events.append(event)
        return events

    async def test_create_put_archive_start_sequence(
        self, mock_docker_client, control_step_config, control_context
    ):
        container = make_finished_container()
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        events = await self._run(executor, control_step_config, control_context)

        mock_docker_client.containers.create.assert_called_once()
        mock_docker_client.containers.run.assert_not_called()
        container.put_archive.assert_called_once()
        container.start.assert_called_once()
        assert events[-1]["status"] == "completed"

        # put_archive lands the tar at the workspace root
        archive_args = container.put_archive.call_args
        assert archive_args[0][0] == "/workspace"

    async def test_config_file_is_per_step_execution(
        self, mock_docker_client, control_step_config, control_context
    ):
        """Cross-agent contract #1: the config lands at
        .control/<step_execution_id>.json and CONFIG_PATH points the runtime
        at exactly that file - the fan-out collision killer."""
        container = make_finished_container()
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        await self._run(executor, control_step_config, control_context)

        execution_id = control_context["step_execution_id"]
        tar_bytes = container.put_archive.call_args[0][1]
        payload, _ = extract_config_from_tar(tar_bytes, f"{execution_id}.json")
        assert payload["step_id"] == execution_id

        create_kwargs = mock_docker_client.containers.create.call_args[1]
        assert create_kwargs["environment"]["CONFIG_PATH"] == (
            f"/workspace/.control/{execution_id}.json"
        )

    async def test_config_payload_is_frozen_producer_contract(
        self, mock_docker_client, control_step_config, control_context
    ):
        container = make_finished_container()
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        await self._run(executor, control_step_config, control_context)

        tar_bytes = container.put_archive.call_args[0][1]
        payload, _ = extract_config_from_tar(
            tar_bytes, f"{control_context['step_execution_id']}.json"
        )

        assert payload["step_id"] == control_context["step_execution_id"]
        assert payload["step_run_id"] == control_context["step_run_id"]
        assert payload["execution_key"] == control_context["execution_key"]
        # RAW user command string - NOT shell-wrapped here (the runtime
        # wraps it with the same set -e semantics)
        assert payload["command"] == "echo control && echo done"
        assert payload["auth_token"] == "test-jwt-token"
        assert payload["timeout_seconds"] == 60
        assert payload["environment"] == {"DEBUG": "1"}
        # Frozen key names (audit renames)
        assert "working_directory" in payload
        assert "token" not in payload
        assert "working_dir" not in payload

    async def test_container_command_is_none_and_control_env_set(
        self, mock_docker_client, control_step_config, control_context
    ):
        container = make_finished_container()
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        await self._run(executor, control_step_config, control_context)

        create_kwargs = mock_docker_client.containers.create.call_args[1]
        assert create_kwargs["command"] is None
        env = create_kwargs["environment"]
        assert env["LAZYAF_CONTROL"] == "1"
        # The token must NEVER travel in inspectable env
        assert "test-jwt-token" not in set(env.values())
        # create must not receive run-only kwargs
        assert "remove" not in create_kwargs
        assert "detach" not in create_kwargs

    async def test_control_mode_ships_tail_not_log_events(
        self, mock_docker_client, control_step_config, control_context
    ):
        """Forensics hardening (12.3): the router is the sole log shipper in
        control mode - the executor emits NO per-line log events and instead
        surfaces the container stdout as a bounded tail on the result."""
        container = make_finished_container(
            log_lines=(b"tail-1\n", b"tail-2\n")
        )
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        events = await self._run(executor, control_step_config, control_context)

        assert [e for e in events if e["type"] == "log"] == []
        result = events[-1]
        assert result["type"] == "result"
        assert result["log_tail"] == ["tail-1", "tail-2"]

    async def test_tail_is_bounded_to_last_lines(
        self, mock_docker_client, control_step_config, control_context
    ):
        total = CONTROL_MODE_LOG_TAIL_LINES + 50
        container = make_finished_container(
            log_lines=tuple(f"line-{i}\n".encode() for i in range(total))
        )
        mock_docker_client.containers.create = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        events = await self._run(executor, control_step_config, control_context)

        tail = events[-1]["log_tail"]
        assert len(tail) == CONTROL_MODE_LOG_TAIL_LINES
        assert tail[0] == "line-50"
        assert tail[-1] == f"line-{total - 1}"

    async def test_stdout_mode_still_uses_run_with_control_off(
        self, mock_docker_client, control_step_config, control_context
    ):
        control_context["control_mode"] = False
        container = make_finished_container()
        mock_docker_client.containers.run = MagicMock(return_value=container)

        executor = LocalExecutor(mock_docker_client)
        events = await self._run(executor, control_step_config, control_context)

        mock_docker_client.containers.run.assert_called_once()
        mock_docker_client.containers.create.assert_not_called()
        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["environment"]["LAZYAF_CONTROL"] == "0"
        # shell-wrapped as today, log events still stream per line
        assert run_kwargs["command"][0] == "bash"
        assert [e["line"] for e in events if e["type"] == "log"] == ["line"]
        assert events[-1]["status"] == "completed"
        assert "log_tail" not in events[-1]
