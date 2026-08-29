"""
Real-Docker integration tests for the built lazyaf-base:dev image (Phase 12.3).

Prove with actual containers what the unit tier asserts on text:
- the capability label is baked in (control-mode selection input)
- a fresh root-owned NAMED VOLUME (R6) is made writable by the entrypoint
  chown, HOME lands at /workspace/home, and content persists across containers
- config delivery via put_archive onto a created-but-not-started container
  works, and the control runtime runs /control/run.py against a stubbed
  backend: status/logs arrive as real HTTP POSTs and the config file is
  consumed-once (deleted).

No skips (R4): Docker being down fails LOUDLY in the shared docker_client
fixture (tdd/integration/conftest.py), matching this file's siblings, and a
missing lazyaf-base:dev image fails loudly with the rebuild command -
`scripts/run_tier.py T2` preflights `python scripts/build_images.py --check`
so CI never even reaches pytest with stale/missing images.

The stub backend validates every /logs body against the REAL LogsRequest
schema from the steps router, so runtime/schema drift fails this suite too.
"""
import io
import json
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

# Add backend (app imports) and the repo root (conftest helpers) to path
_repo_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "backend"))

from pydantic import ValidationError

from app.routers.steps import LogsRequest
from tdd.integration.conftest import advertise_addr, stop_http_server, wait_for_port

BASE_IMAGE = "lazyaf-base:dev"

pytestmark = [pytest.mark.integration, pytest.mark.local_exec]


# -----------------------------------------------------------------------------
# Fixtures (docker_client + named_volume come from tdd/integration/conftest.py)
# -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def base_image(docker_client):
    """The locally-built base image - or a LOUD failure with the rebuild
    command (never a skip: run_tier.py T2 preflights build_images --check,
    so a host dev is the only one who can reach this message)."""
    import docker as docker_sdk

    try:
        return docker_client.images.get(BASE_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        pytest.fail(
            f"{BASE_IMAGE} not built on this daemon - "
            "run: python scripts/build_images.py"
        )


def _run_in_volume(docker_client, volume_name, command, environment=None, timeout=60):
    """Run a container on the named volume; return (exit_code, logs)."""
    container = docker_client.containers.run(
        BASE_IMAGE,
        command=command,
        volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
        environment=environment or {},
        detach=True,
    )
    try:
        result = container.wait(timeout=timeout)
        logs = container.logs().decode("utf-8", errors="replace")
        return result.get("StatusCode", -1), logs
    finally:
        container.remove(force=True)


# -----------------------------------------------------------------------------
# Stub backend (captures the control runtime's POSTs)
# -----------------------------------------------------------------------------

class _StubBackend:
    """Captures the control runtime's POSTs.

    Shape pin: every /logs body is validated against the REAL LogsRequest
    schema from app.routers.steps - if the runtime's payload drifts from
    what the backend accepts, this stub fails the test too (recorded in
    schema_errors and answered 422, exactly like the real router would)."""

    def __init__(self):
        self.posts = []  # (path, payload dict)
        self.schema_errors = []  # (path, payload, error string)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    payload = {"_raw": body.decode(errors="replace")}
                stub.posts.append((self.path, payload))

                if self.path.endswith("/logs"):
                    try:
                        LogsRequest.model_validate(payload)
                    except ValidationError as exc:
                        stub.schema_errors.append((self.path, payload, str(exc)))
                        response = b'{"detail": "logs body failed LogsRequest validation"}'
                        self.send_response(422)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response)))
                        self.end_headers()
                        self.wfile.write(response)
                        return

                response = b'{"status": "ok", "lines_appended": 0, "timeout_extended": false, "last_seen": "", "progress_updated": false}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        wait_for_port(self.port)  # readiness poll (shared helper)

    def stop(self):
        # Shutdown-with-deadline (shared helper): never silently leak the
        # serve thread.
        stop_http_server(self.server, self.thread)

    def paths(self):
        return [p for p, _ in self.posts]


@pytest.fixture
def stub_backend():
    stub = _StubBackend()
    yield stub
    stub.stop()


def _config_tar(config: dict) -> bytes:
    """In-memory tar of .control/step_config.json, mode 0600, uid/gid 1000
    (the lazyaf user pinned in the image) — the §2 delivery contract."""
    raw = json.dumps(config).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=".control/step_config.json")
        info.size = len(raw)
        info.mode = 0o600
        info.uid = 1000
        info.gid = 1000
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

class TestImageLabels:
    def test_control_layer_capability_label_baked_in(self, docker_client):
        """LocalExecutor selects control mode from this label."""
        image = docker_client.images.get(BASE_IMAGE)
        assert image.labels.get("lazyaf.control-layer") == "1"

    def test_content_hash_label_stamped(self, docker_client):
        image = docker_client.images.get(BASE_IMAGE)
        assert image.labels.get("lazyaf.content-hash")


class TestStdoutModeAndHomePersistence:
    def test_runs_as_lazyaf_with_workspace_home(self, docker_client, named_volume):
        """CMD passthrough (stock-image degradation): non-root user, HOME on
        the volume — even across the gosu drop."""
        code, logs = _run_in_volume(
            docker_client, named_volume, ["bash", "-c", 'whoami; echo "HOME=$HOME"']
        )
        assert code == 0, logs
        assert "lazyaf" in logs
        assert "HOME=/workspace/home" in logs

    def test_chown_at_entrypoint_makes_fresh_volume_writable(
        self, docker_client, named_volume
    ):
        """A fresh named volume is root-owned; the entrypoint chown must make
        it writable for uid 1000 (the failure_01 non-root-vs-root-volume bug)."""
        code, logs = _run_in_volume(
            docker_client,
            named_volume,
            ["bash", "-c", "touch /workspace/home/proof.txt && echo WROTE-OK"],
        )
        assert code == 0, logs
        assert "WROTE-OK" in logs

    def test_home_persists_across_containers(self, docker_client, named_volume):
        """Cross-step HOME persistence: a file written under /workspace/home
        by one container is present for the next (same volume)."""
        code, logs = _run_in_volume(
            docker_client,
            named_volume,
            ["bash", "-c", "echo step-one-was-here > /workspace/home/marker.txt"],
        )
        assert code == 0, logs

        code, logs = _run_in_volume(
            docker_client, named_volume, ["bash", "-c", "cat /workspace/home/marker.txt"]
        )
        assert code == 0, logs
        assert "step-one-was-here" in logs


class TestDockerSocketAccess:
    def test_dropped_user_can_connect_to_mounted_socket(self, docker_client):
        """Regression for the T2 DooD preflight EACCES: needs:[docker] steps
        run as the lazyaf user, so the entrypoint must join it to the
        socket's owning group before gosu (group_add would not survive the
        privilege drop)."""
        output = docker_client.containers.run(
            BASE_IMAGE,
            command=[
                "bash", "-c",
                "python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); "
                "s.connect('/var/run/docker.sock'); print('SOCK_OK')\" && id",
            ],
            volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
            remove=True,
        ).decode()
        assert "SOCK_OK" in output
        assert "uid=1000" in output  # proves the drop happened before connect


class TestControlModeRoundTrip:
    def test_put_archive_config_control_run_and_consume_once(
        self, docker_client, named_volume, stub_backend
    ):
        """The full §2 delivery + control flow against a stubbed backend:
        create container -> put_archive config -> start -> the runtime POSTs
        running/logs/completed and deletes the config file."""
        step_id = f"exec-{uuid4().hex[:8]}"
        config = {
            "step_id": step_id,
            "step_run_id": "sr-int",
            "execution_key": "run-int:0:sr-int",
            "command": "echo hello-from-control\necho HOME=$HOME",
            # DooD-safe (conftest): the step container is a SIBLING, so the
            # stub (bound 0.0.0.0) is advertised as this container's own IP
            # when the suite runs inside a container (the CI path), else
            # host.docker.internal (Linux-Engine hosts may need
            # --add-host host.docker.internal:host-gateway).
            "backend_url": f"http://{advertise_addr()}:{stub_backend.port}",
            "auth_token": "integration-token",
            "environment": {},
            "timeout_seconds": 60,
            "working_directory": "/workspace/repo",
            "log_batch_interval": 0.2,
        }

        # 1. create (mounts bound at create; command irrelevant in control mode)
        container = docker_client.containers.create(
            BASE_IMAGE,
            volumes={named_volume: {"bind": "/workspace", "mode": "rw"}},
            environment={"LAZYAF_CONTROL": "1"},
        )
        try:
            # 2. config into the volume through the created container
            assert container.put_archive("/workspace", _config_tar(config))
            # 3. start; the entrypoint runs /control/run.py
            container.start()
            result = container.wait(timeout=90)
            logs = container.logs().decode("utf-8", errors="replace")
        finally:
            container.remove(force=True)

        assert result.get("StatusCode") == 0, logs
        assert "hello-from-control" in logs  # stdout echo kept for forensics
        assert "HOME=/workspace/home" in logs

        # Status round trip: running first, completed last
        status_posts = [
            p for path, p in stub_backend.posts
            if path == f"/api/steps/{step_id}/status"
        ]
        assert status_posts, f"no status posts; got {stub_backend.paths()}"
        assert status_posts[0]["status"] == "running"
        assert status_posts[-1]["status"] == "completed"
        assert status_posts[-1]["exit_code"] == 0

        # Shape pin: every /logs body validated against the real LogsRequest
        assert not stub_backend.schema_errors, stub_backend.schema_errors

        # Logs arrived as LogLine objects with trailing newlines
        log_posts = [
            p for path, p in stub_backend.posts
            if path == f"/api/steps/{step_id}/logs"
        ]
        all_lines = [line for p in log_posts for line in p["lines"]]
        assert {"content": "hello-from-control\n", "stream": "stdout"} in all_lines

        # Consume-once: the config (and its token) did not survive the step
        code, logs = _run_in_volume(
            docker_client,
            named_volume,
            ["bash", "-c",
             "test ! -f /workspace/.control/step_config.json && echo CONSUMED"],
        )
        assert code == 0, logs
        assert "CONSUMED" in logs
