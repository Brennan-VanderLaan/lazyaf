"""
Integration tests for workspace population (Phase 12.2-INT).

The subject is app.services.workspace.population.populate_workspace: a
short-lived helper container on settings.container_network clones the repo
into /workspace/repo on a NAMED docker volume (R6: named volumes, not
tmp_path bind mounts, are the workspace medium - and contents are verified
by a SECOND container reading the volume, not by peeking at host paths).

TEST SEAM (documented in population.py): the clone URL template on the
cached settings instance is monkeypatched to a file:// URL served from a
read-only NAMED-VOLUME mount passed via `extra_mounts`. The seed repo is
built INSIDE that volume by a helper container - never on a host path -
because this suite must pass identically on the host and inside dogfood
CI's DooD step containers, where tmp_path bind sources do not exist on
the daemon that interprets them (the exact landmine-2 seam this suite
guards; run cccae257 caught the bind variant failing under DooD).

The real http URL through the backend is covered by the e2e-lane test at
the bottom, gated on ENV PRESENCE (E2E_BACKEND_URL / BACKEND_URL), never on
availability: if the env is set and the backend is unreachable, the test
fails loudly.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Docker Availability Check (mirrors sibling real-docker suites; Docker is
# expected to be present - the T2 tier floor makes wholesale skipping a
# hard CI failure)
# -----------------------------------------------------------------------------

def docker_available() -> bool:
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not docker_available(),
    reason="Docker not available",
)


# -----------------------------------------------------------------------------
# Helpers / fixtures
# -----------------------------------------------------------------------------

def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4 - this file's old copy had
# drifted ping-less).


@pytest.fixture
def volume_name(docker_client):
    """A real NAMED docker volume, cleaned up afterwards."""
    name = f"lazyaf-test-pop-{uuid.uuid4().hex[:8]}"
    docker_client.volumes.create(name=name)
    yield name
    try:
        docker_client.volumes.get(name).remove(force=True)
    except Exception:
        pass


_SEED_BUILD_SCRIPT = """
set -e
mkdir -p /tmp/work && cd /tmp/work
git init -q -b main .
git config user.email test@lazyaf.local
git config user.name "LazyAF Test"
printf 'first commit content\\n' > hello.txt
git add . && git commit -qm first
echo "SHA1=$(git rev-parse HEAD)"
printf 'second commit content\\n' > hello.txt
printf 'added later\\n' > extra.txt
git add . && git commit -qm second
echo "SHA2=$(git rev-parse HEAD)"
git clone -q --bare /tmp/work "/seed/{repo_id}.git"
"""


@pytest.fixture
def seed_repo(docker_client):
    """A bare seed repo built INSIDE a named volume: (seed_volume, repo_id,
    [sha1, sha2]).

    A helper container (the clone image - it has git) writes
    /seed/<repo_id>.git with two commits on main. No host paths are
    involved anywhere, so the fixture behaves identically on the host and
    under DooD, where the daemon cannot see this container's tmp_path.
    """
    from app.config import get_settings

    repo_id = f"seed-{uuid.uuid4().hex[:8]}"
    seed_volume = f"lazyaf-test-seed-{uuid.uuid4().hex[:8]}"
    docker_client.volumes.create(name=seed_volume)
    try:
        output = docker_client.containers.run(
            get_settings().workspace_clone_image,
            command=["bash", "-c", _SEED_BUILD_SCRIPT.format(repo_id=repo_id)],
            volumes={seed_volume: {"bind": "/seed", "mode": "rw"}},
            remove=True,
        ).decode("utf-8", errors="replace")
        shas = {
            key: value
            for key, _, value in (
                line.partition("=") for line in output.splitlines() if "=" in line
            )
        }
        yield seed_volume, repo_id, [shas["SHA1"], shas["SHA2"]]
    finally:
        try:
            docker_client.volumes.get(seed_volume).remove(force=True)
        except Exception:
            pass


@pytest.fixture
def file_url_template(seed_repo, monkeypatch):
    """TEST SEAM: point the clone template at the volume-mounted seed dir."""
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "container_git_url_template", "file:///seed/{repo_id}.git"
    )
    return seed_repo


def seed_mount(seed_volume: str):
    from app.services.execution.local_executor import MountAddressing, MountSpec

    # Explicit VOLUME addressing: the seed lives in a named volume, which the
    # host daemon resolves identically whether this test runs on the host or
    # inside a DooD step container. (BIND-with-Windows-path addressing is
    # covered in the local_executor hardening unit tests.)
    return MountSpec(
        addressing=MountAddressing.VOLUME,
        source=seed_volume,
        target="/seed",
        mode="ro",
    )


def read_volume(docker_client, volume_name: str, command: str) -> str:
    """Verify volume contents through a SECOND container (never via host paths)."""
    output = docker_client.containers.run(
        "alpine:latest",
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        remove=True,
    )
    return output.decode("utf-8", errors="replace")


# -----------------------------------------------------------------------------
# Named-volume population through the file:// seam
# -----------------------------------------------------------------------------

class TestPopulateNamedVolume:
    async def test_clones_repo_into_named_volume(
        self, docker_client, volume_name, file_url_template
    ):
        """The R6 landmine test: clone lands on a NAMED volume and a second
        container sees /workspace/repo."""
        from app.services.workspace.population import populate_workspace

        seed_vol, repo_id, _ = file_url_template

        await populate_workspace(
            volume_name, repo_id, "main", None,
            extra_mounts=[seed_mount(seed_vol)],
        )

        listing = read_volume(docker_client, volume_name, "ls -1a /workspace/repo")
        assert "hello.txt" in listing
        assert "extra.txt" in listing
        assert ".git" in listing

        content = read_volume(docker_client, volume_name, "cat /workspace/repo/hello.txt")
        assert "second commit content" in content  # branch head by default

    async def test_repo_is_writable_by_step_uid(
        self, docker_client, volume_name, file_url_template
    ):
        """Regression for dogfood run 9b52ebbe: the lazyaf images run steps as
        uid 1000, so the root-run clone helper must hand the repo tree over -
        otherwise control-mode steps EACCES on their first build artifact."""
        from app.services.workspace.population import populate_workspace

        seed_vol, repo_id, _ = file_url_template

        await populate_workspace(
            volume_name, repo_id, "main", None,
            extra_mounts=[seed_mount(seed_vol)],
        )

        output = docker_client.containers.run(
            "alpine:latest",
            command=["sh", "-c", "touch /workspace/repo/write-probe && echo WRITABLE"],
            volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
            user="1000:1000",
            remove=True,
        ).decode()
        assert "WRITABLE" in output

    async def test_checks_out_requested_commit_sha(
        self, docker_client, volume_name, file_url_template
    ):
        from app.services.workspace.population import populate_workspace

        seed_vol, repo_id, (sha1, _sha2) = file_url_template

        await populate_workspace(
            volume_name, repo_id, "main", sha1,
            extra_mounts=[seed_mount(seed_vol)],
        )

        content = read_volume(docker_client, volume_name, "cat /workspace/repo/hello.txt")
        assert "first commit content" in content
        listing = read_volume(docker_client, volume_name, "ls -1 /workspace/repo")
        assert "extra.txt" not in listing  # second commit not checked out

    async def test_failure_raises_with_log_tail(
        self, docker_client, volume_name, file_url_template
    ):
        """A bad repo id fails loudly and the error carries the helper's logs."""
        from app.services.workspace.population import (
            WorkspacePopulationError,
            populate_workspace,
        )

        seed_vol, _repo_id, _ = file_url_template

        with pytest.raises(WorkspacePopulationError) as excinfo:
            await populate_workspace(
                volume_name, "no-such-repo", "main", None,
                extra_mounts=[seed_mount(seed_vol)],
            )

        message = str(excinfo.value)
        assert "helper log tail" in message
        assert "fatal" in message.lower()  # git's own error is surfaced
        assert "no-such-repo" in message

    async def test_helper_container_is_removed_after_run(
        self, docker_client, volume_name, file_url_template
    ):
        """Success or failure, no populate helper containers are left behind."""
        from app.services.workspace.population import (
            WorkspacePopulationError,
            populate_workspace,
        )

        seed_vol, repo_id, _ = file_url_template

        await populate_workspace(
            volume_name, repo_id, "main", None,
            extra_mounts=[seed_mount(seed_vol)],
        )
        with pytest.raises(WorkspacePopulationError):
            await populate_workspace(
                volume_name, "no-such-repo", "main", None,
                extra_mounts=[seed_mount(seed_vol)],
            )

        leftovers = docker_client.containers.list(
            all=True, filters={"label": f"lazyaf.volume={volume_name}"}
        )
        assert leftovers == []


# -----------------------------------------------------------------------------
# E2E lane: the real http URL through the backend's git server.
#
# Gated on ENV PRESENCE, not availability (R4): unset -> skip (baselined,
# reason prefix "e2e-lane:"); set but unreachable -> loud failure.
# BACKEND_URL must be reachable BOTH from this test process AND from
# containers on settings.container_network (e.g. http://backend-e2e:8000
# inside the compose e2e stack, or http://host.docker.internal:8765 from a
# host run against it - in that case set E2E_POPULATION_GIT_BASE for the
# in-container base if it differs from BACKEND_URL).
# -----------------------------------------------------------------------------

_E2E_BACKEND = os.environ.get("E2E_BACKEND_URL") or os.environ.get("BACKEND_URL")


@pytest.mark.skipif(
    _E2E_BACKEND is None,
    reason="e2e-lane: E2E_BACKEND_URL/BACKEND_URL not set - real-http population clone runs in the e2e lane",
)
class TestPopulateViaBackendHttp:
    async def test_clones_via_backend_git_http(
        self, docker_client, volume_name, tmp_path, monkeypatch
    ):
        import httpx

        from app.config import get_settings
        from app.services.workspace.population import populate_workspace

        backend_url = _E2E_BACKEND.rstrip("/")
        container_base = os.environ.get("E2E_POPULATION_GIT_BASE", backend_url).rstrip("/")

        # 1. Create + ingest a repo through the real backend API. No
        #    availability probe: if the backend is down this fails loudly.
        async with httpx.AsyncClient(base_url=backend_url, timeout=30.0) as client:
            response = await client.post(
                "/api/repos/ingest",
                json={"name": f"pop-e2e-{uuid.uuid4().hex[:8]}", "default_branch": "main"},
            )
            assert response.status_code == 201, response.text
            repo_id = response.json()["id"]

        # 2. Push real content over git http.
        work = tmp_path / "work"
        work.mkdir()
        _git("init", "-b", "main", cwd=work)
        _git("config", "user.email", "test@lazyaf.local", cwd=work)
        _git("config", "user.name", "LazyAF Test", cwd=work)
        (work / "e2e.txt").write_text("populated over http\n")
        _git("add", ".", cwd=work)
        _git("commit", "-m", "e2e", cwd=work)
        _git("push", f"{backend_url}/git/{repo_id}.git", "HEAD:main", cwd=work)

        # 3. Populate through the REAL http template on the container network.
        monkeypatch.setattr(
            get_settings(),
            "container_git_url_template",
            container_base + "/git/{repo_id}.git",
        )
        await populate_workspace(volume_name, repo_id, "main", None)

        content = read_volume(docker_client, volume_name, "cat /workspace/repo/e2e.txt")
        assert "populated over http" in content
