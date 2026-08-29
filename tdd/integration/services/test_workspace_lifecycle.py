"""
Integration tests for the workspace lifecycle with REAL Docker named volumes
(Phase 12.2-INT).

R6: named volumes are the SUBJECT here — no tmp_path bind mounts.
R4: Docker is required in this environment; there is no availability skip.

Population is monkeypatched for the volume-lifecycle tests (the subject is
volume create/cleanup/leak-balance/orphan-sweep); the final test exercises
the REAL populate_workspace clone path through a docker network against a
hermetic in-container git daemon.

Safety: this host carries real leftover lazyaf-ws-* volumes (failure_01
era). Orphan-audit tests therefore pass an exact ``volume_prefix`` so
sweep 3 can never touch volumes that are not planted by this test run.
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

import docker
import docker.errors
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.workspace import Workspace
from app.services import workspace_service as ws_module
from app.services.workspace.state_machine import WorkspaceStatus, generate_volume_name
from app.services.workspace_service import (
    WORKSPACE_VOLUME_LABEL,
    WorkspaceCreationError,
    WorkspaceService,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4).


@pytest.fixture
def volume_tracker(docker_client):
    """Force-remove every tracked volume after the test (leak guard)."""
    names: list[str] = []
    yield names
    for name in names:
        try:
            docker_client.volumes.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass


@pytest.fixture
def session_factory(async_engine):
    """cleanup() owns its sessions: bind them to the TEST engine."""
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def service(docker_client, session_factory):
    return WorkspaceService(docker_client=docker_client, session_factory=session_factory)


@pytest.fixture
def populate(monkeypatch):
    """Recorder stand-in for populate_workspace (volume tests' seam)."""
    calls: list[tuple] = []

    async def fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        calls.append((volume_name, repo_id, branch, commit_sha))

    monkeypatch.setattr(ws_module, "populate_workspace", fake_populate)
    return calls


def _volume_exists(docker_client, name: str) -> bool:
    try:
        docker_client.volumes.get(name)
        return True
    except docker.errors.NotFound:
        return False


async def _fetch(db, run_id: str) -> Workspace | None:
    # populate_existing: cleanup() commits through its OWN session; a fresh
    # SELECT must reflect the database, not this session's stale copy.
    result = await db.execute(
        select(Workspace)
        .where(Workspace.pipeline_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


# -----------------------------------------------------------------------------
# Volume lifecycle
# -----------------------------------------------------------------------------


class TestRealVolumeLifecycle:
    async def test_get_or_create_creates_named_volume(
        self, db_session, service, docker_client, populate, volume_tracker
    ):
        run_id = str(uuid4())
        expected_name = generate_volume_name(run_id)
        volume_tracker.append(expected_name)

        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        assert ws.status == WorkspaceStatus.READY.value
        assert ws.volume_name == expected_name  # full run id, no truncation
        volume = docker_client.volumes.get(expected_name)  # raises if absent
        labels = volume.attrs["Labels"]
        assert labels[WORKSPACE_VOLUME_LABEL] == "true"
        assert "lazyaf.created_at" in labels
        assert populate == [(expected_name, "repo-1", "main", None)]

    async def test_cleanup_removes_the_volume(
        self, db_session, service, docker_client, populate, volume_tracker
    ):
        run_id = str(uuid4())
        volume_tracker.append(generate_volume_name(run_id))
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        assert _volume_exists(docker_client, ws.volume_name)

        await service.cleanup(db_session, run_id)

        assert not _volume_exists(docker_client, ws.volume_name)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert row.cleaned_at is not None

    async def test_cleanup_is_idempotent_against_real_docker(
        self, db_session, service, docker_client, populate, volume_tracker
    ):
        run_id = str(uuid4())
        volume_tracker.append(generate_volume_name(run_id))
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        await service.cleanup(db_session, run_id)
        await service.cleanup(db_session, run_id)  # volume already gone: no-op

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_leak_balance_across_success_and_failure(
        self, db_session, service, docker_client, volume_tracker, monkeypatch
    ):
        """Every created volume is gone by the end — the failed create's
        volume is removed IMMEDIATELY by the failure path (no leak window),
        the successful ones by cleanup."""
        run_ids = [str(uuid4()) for _ in range(3)]
        for rid in run_ids:
            volume_tracker.append(generate_volume_name(rid))

        fail_for = {generate_volume_name(run_ids[1])}

        async def flaky_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
            if volume_name in fail_for:
                raise RuntimeError("simulated clone failure")

        monkeypatch.setattr(ws_module, "populate_workspace", flaky_populate)

        # Two successful creates, one failed create.
        await service.get_or_create(db_session, run_ids[0], "repo-1", "main", None)
        with pytest.raises(WorkspaceCreationError):
            await service.get_or_create(db_session, run_ids[1], "repo-1", "main", None)
        await service.get_or_create(db_session, run_ids[2], "repo-1", "main", None)

        # The successful runs' volumes exist; the failed run's volume was
        # removed on the way out of the failure path.
        assert _volume_exists(docker_client, generate_volume_name(run_ids[0]))
        assert not _volume_exists(docker_client, generate_volume_name(run_ids[1]))
        assert _volume_exists(docker_client, generate_volume_name(run_ids[2]))

        # Cleanup runs on completion AND failure (idempotent for the failed
        # run whose volume is already gone).
        for rid in run_ids:
            await service.cleanup(db_session, rid)

        for rid in run_ids:
            assert not _volume_exists(docker_client, generate_volume_name(rid))
            row = await _fetch(db_session, rid)
            assert row.status == WorkspaceStatus.CLEANED.value


# -----------------------------------------------------------------------------
# Orphan sweep (simulated backend crash)
# -----------------------------------------------------------------------------


class TestOrphanSweepRealDocker:
    async def test_sweep_recovers_stranded_creating_row_and_volume(
        self, db_session, service, docker_client, volume_tracker
    ):
        """Simulated crash mid-create: the CREATING row was committed and the
        volume created, then the backend died. The audit must finish the job."""
        run_id = str(uuid4())
        volume_name = generate_volume_name(run_id)
        volume_tracker.append(volume_name)

        old = datetime.utcnow() - timedelta(hours=1)
        db_session.add(
            Workspace(
                pipeline_run_id=run_id,
                repo_id="repo-1",
                volume_name=volume_name,
                status=WorkspaceStatus.CREATING.value,
                use_count=0,
                created_at=old,
                updated_at=old,
            )
        )
        await db_session.commit()
        docker_client.volumes.create(
            name=volume_name, labels={WORKSPACE_VOLUME_LABEL: "true"}
        )

        cleaned = await service.audit_orphans(
            db_session, volume_prefix=volume_name  # exact match: host safety
        )

        assert volume_name in cleaned
        assert not _volume_exists(docker_client, volume_name)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_sweep_recovers_old_failed_row_and_volume(
        self, db_session, service, docker_client, volume_tracker
    ):
        """A failed create that nobody retried or cleaned: the audit removes
        the real volume and drives the row to CLEANED."""
        run_id = str(uuid4())
        volume_name = generate_volume_name(run_id)
        volume_tracker.append(volume_name)

        old = datetime.utcnow() - timedelta(hours=1)
        db_session.add(
            Workspace(
                pipeline_run_id=run_id,
                repo_id="repo-1",
                volume_name=volume_name,
                status=WorkspaceStatus.FAILED.value,
                use_count=0,
                error="creation failed (planted)",
                created_at=old,
                updated_at=old,
            )
        )
        await db_session.commit()
        docker_client.volumes.create(
            name=volume_name, labels={WORKSPACE_VOLUME_LABEL: "true"}
        )

        cleaned = await service.audit_orphans(
            db_session, volume_prefix=volume_name  # exact match: host safety
        )

        assert volume_name in cleaned
        assert not _volume_exists(docker_client, volume_name)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_sweep_removes_unmatched_volume_with_no_row(
        self, db_session, service, docker_client, volume_tracker
    ):
        """A volume left behind with no DB row at all (row deleted or DB
        reset after a crash) is swept once it is old enough."""
        volume_name = generate_volume_name(str(uuid4()))
        volume_tracker.append(volume_name)
        old_label = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        docker_client.volumes.create(
            name=volume_name,
            labels={WORKSPACE_VOLUME_LABEL: "true", "lazyaf.created_at": old_label},
        )

        cleaned = await service.audit_orphans(
            db_session, volume_prefix=volume_name  # exact match: host safety
        )

        assert cleaned == [volume_name]
        assert not _volume_exists(docker_client, volume_name)

    async def test_sweep_keeps_fresh_volume_backing_a_live_row(
        self, db_session, service, docker_client, populate, volume_tracker
    ):
        run_id = str(uuid4())
        volume_name = generate_volume_name(run_id)
        volume_tracker.append(volume_name)
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        cleaned = await service.audit_orphans(db_session, volume_prefix=volume_name)

        assert cleaned == []
        assert _volume_exists(docker_client, volume_name)


# -----------------------------------------------------------------------------
# Real population through the network path
# -----------------------------------------------------------------------------


class TestRealPopulation:
    async def test_populate_clones_repo_into_named_volume_over_network(
        self, db_session, docker_client, session_factory, volume_tracker, monkeypatch
    ):
        """The named-volume clone test through the network path (12.2-INT
        exit gate): a hermetic git daemon on a docker network serves a seed
        repo; the REAL populate_workspace clones it into the workspace
        volume at /workspace/repo, driven through get_or_create."""
        # R4: population and its settings have landed — imports are strict
        # and loud (the 12.2-INT parallel-agent skip guards are gone).
        import app.services.workspace.population  # noqa: F401
        from app.config import get_settings

        settings = get_settings()

        suffix = uuid4().hex[:8]
        net_name = f"lazyaf-test-popnet-{suffix}"
        seed_volume_name = f"lazyaf-test-seed-{suffix}"
        daemon_name = f"lazyaf-test-gitsrv-{suffix}"
        repo_id = "poptestrepo"
        run_id = str(uuid4())
        volume_tracker.append(generate_volume_name(run_id))
        volume_tracker.append(seed_volume_name)

        network = docker_client.networks.create(net_name)
        daemon = None
        try:
            # Seed a bare repo with one commit on main inside a volume.
            seed_script = (
                "set -e; "
                f"git init --bare --initial-branch=main /srv/{repo_id}.git; "
                f"git clone /srv/{repo_id}.git /tmp/w 2>/dev/null; "
                "cd /tmp/w; echo workspace-population-proof > README.md; "
                "git add README.md; "
                "git -c user.email=t@t -c user.name=t commit -qm init; "
                "git push -q origin main"
            )
            docker_client.containers.run(
                "python:3.12",
                ["bash", "-c", seed_script],
                volumes={seed_volume_name: {"bind": "/srv", "mode": "rw"}},
                remove=True,
            )

            # Serve it over git:// on the test network.
            daemon = docker_client.containers.run(
                "python:3.12",
                [
                    "git", "daemon", "--export-all", "--base-path=/srv",
                    "--reuseaddr", "--verbose",
                ],
                name=daemon_name,
                network=net_name,
                volumes={seed_volume_name: {"bind": "/srv", "mode": "ro"}},
                detach=True,
            )
            deadline = time.time() + 30
            while time.time() < deadline:
                if b"Ready to rumble" in daemon.logs():
                    break
                daemon.reload()
                if daemon.status == "exited":
                    pytest.fail(f"git daemon died: {daemon.logs().decode(errors='replace')}")
                time.sleep(0.25)
            else:
                pytest.fail("git daemon never became ready")

            # Point population at the hermetic server (settings-driven seam).
            monkeypatch.setattr(settings, "container_network", net_name)
            monkeypatch.setattr(
                settings,
                "container_git_url_template",
                f"git://{daemon_name}/{{repo_id}}.git",
            )

            service = WorkspaceService(
                docker_client=docker_client, session_factory=session_factory
            )
            ws = await service.get_or_create(db_session, run_id, repo_id, "main", None)
            assert ws.status == WorkspaceStatus.READY.value

            # The clone landed inside the NAMED VOLUME at /workspace/repo.
            output = docker_client.containers.run(
                "python:3.12",
                ["bash", "-c", "cat /workspace/repo/README.md && git -C /workspace/repo rev-parse --abbrev-ref HEAD"],
                volumes={ws.volume_name: {"bind": "/workspace", "mode": "ro"}},
                remove=True,
            )
            assert b"workspace-population-proof" in output
            assert b"main" in output

            await service.cleanup(db_session, run_id)
            assert not _volume_exists(docker_client, ws.volume_name)
        finally:
            if daemon is not None:
                try:
                    daemon.remove(force=True)
                except docker.errors.NotFound:
                    pass
            try:
                network.remove()
            except docker.errors.APIError:
                pass
