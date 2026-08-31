"""
Unit tests for WorkspaceService (Phase 12.2-INT).

Docker is faked at the client seam (the service's injected docker_client);
population is monkeypatched at the service module's seam. Real Docker
volume behavior is covered by tdd/integration/services/test_workspace_lifecycle.py.

Covers the four failure_01 defects the salvage audit flagged:
- no lock leak across acquire/release (locks are per-call)
- no lazy-loads (model has no relationships; audit queries explicitly)
- documented commits (persisted state visible from a fresh query)
- stranded CREATING rows recovered by the orphan audit
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import Base
from app.models.pipeline import Pipeline, PipelineRun
from app.models.workspace import Workspace
from app.services.workspace.state_machine import WorkspaceStatus, generate_volume_name
from app.services.workspace.worker_key import DEFAULT_WORKER_KEY
from app.services import workspace_service as ws_module
from app.services.workspace_service import (
    WorkspaceAcquisitionError,
    WorkspaceCleanupError,
    WorkspaceCreationError,
    WorkspaceService,
    start_orphan_audit,
    workspace_service,
)


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeVolume:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name

    @property
    def attrs(self):
        return {"Labels": self._store[self.name]["labels"]}

    def remove(self, force=False):
        del self._store[self.name]


class FakeVolumesAPI:
    def __init__(self, store: dict):
        self._store = store

    def create(self, name: str, labels=None):
        self._store[name] = {"labels": labels or {}}
        return FakeVolume(self._store, name)

    def get(self, name: str):
        import docker.errors

        if name not in self._store:
            raise docker.errors.NotFound(f"no such volume: {name}")
        return FakeVolume(self._store, name)

    def list(self, filters=None):
        names = list(self._store)
        if filters and "label" in filters:
            key, _, value = filters["label"].partition("=")
            names = [
                n for n in names if self._store[n]["labels"].get(key) == value
            ]
        return [FakeVolume(self._store, n) for n in names]


class FakeLowLevelAPI:
    """The docker APIClient surface the service touches (remove_volume)."""

    def __init__(self, store: dict):
        self._store = store

    def remove_volume(self, name: str, force: bool = False):
        import docker.errors

        if name not in self._store:
            raise docker.errors.NotFound(f"no such volume: {name}")
        del self._store[name]


class FakeDockerClient:
    def __init__(self):
        self.store: dict = {}
        self.volumes = FakeVolumesAPI(self.store)
        self.api = FakeLowLevelAPI(self.store)


class PopulateRecorder:
    """Async populate_workspace stand-in that records calls."""

    def __init__(self, fail_with: Exception | None = None):
        self.calls: list[tuple] = []
        self.clients: list = []
        self.fail_with = fail_with

    async def __call__(self, volume_name, repo_id, branch, commit_sha, *, client=None, **kwargs):
        self.calls.append((volume_name, repo_id, branch, commit_sha))
        self.clients.append(client)
        if self.fail_with is not None:
            raise self.fail_with


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def fake_docker():
    return FakeDockerClient()


@pytest.fixture
def service(fake_docker, async_engine):
    # session_factory: cleanup() owns its sessions and must open them on the
    # TEST engine, never on app.database's real one.
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return WorkspaceService(docker_client=fake_docker, session_factory=factory)


@pytest.fixture
def populate(monkeypatch):
    recorder = PopulateRecorder()
    monkeypatch.setattr(ws_module, "populate_workspace", recorder)
    return recorder


@pytest.fixture
def run_id():
    return str(uuid4())


async def _fetch(db: AsyncSession, run_id: str) -> Workspace | None:
    # populate_existing: some service methods (cleanup) commit through their
    # OWN session; a fresh SELECT must reflect the database, not this
    # session's stale identity-map copy.
    result = await db.execute(
        select(Workspace)
        .where(Workspace.pipeline_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _fetch_lane(db: AsyncSession, run_id: str, worker_key: str) -> Workspace | None:
    """_fetch, scoped to one LANE (a run may own several — M13-1)."""
    result = await db.execute(
        select(Workspace)
        .where(
            Workspace.pipeline_run_id == run_id, Workspace.worker_key == worker_key
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


# -----------------------------------------------------------------------------
# get_or_create
# -----------------------------------------------------------------------------


class TestGetOrCreate:
    async def test_creates_ready_workspace_with_volume_and_population(
        self, db_session, service, fake_docker, populate, run_id
    ):
        ws = await service.get_or_create(
            db_session, run_id, repo_id="repo-1", branch="main", commit_sha="a" * 40
        )

        assert ws.status == WorkspaceStatus.READY.value
        assert ws.use_count == 0
        assert ws.pipeline_run_id == run_id
        # Full run id in the volume name (failure_01 truncated to 8 chars).
        assert ws.volume_name == f"lazyaf-ws-{run_id}"
        assert ws.volume_name == generate_volume_name(run_id)
        assert ws.volume_name in fake_docker.store
        assert (
            fake_docker.store[ws.volume_name]["labels"]["lazyaf.workspace"] == "true"
        )
        assert populate.calls == [(ws.volume_name, "repo-1", "main", "a" * 40)]

    async def test_persists_row_visible_from_fresh_query(
        self, db_session, service, populate, run_id
    ):
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        row = await _fetch(db_session, run_id)
        assert row is not None
        assert row.status == WorkspaceStatus.READY.value
        assert row.created_at is not None
        assert row.updated_at is not None
        assert row.cleaned_at is None

    async def test_idempotent_second_call_returns_same_row(
        self, db_session, service, populate, run_id
    ):
        first = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        second = await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        assert second.id == first.id
        assert len(populate.calls) == 1

    async def test_returns_in_use_workspace_untouched(
        self, db_session, service, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.acquire(db_session, ws.id)

        again = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        assert again.id == ws.id
        assert again.status == WorkspaceStatus.IN_USE.value
        assert len(populate.calls) == 1

    async def test_concurrent_calls_create_one_workspace(
        self, tmp_path, service, populate, run_id
    ):
        """Two gathered calls (own sessions, shared service) -> one create."""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'ws.db').as_posix()}", echo=False
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def call():
            async with factory() as session:
                ws = await service.get_or_create(session, run_id, "repo-1", "main", None)
                return ws.id

        try:
            ids = await asyncio.gather(call(), call())
            assert ids[0] == ids[1]
            assert len(populate.calls) == 1
            async with factory() as session:
                rows = (await session.execute(select(Workspace))).scalars().all()
                assert len(rows) == 1
                assert rows[0].status == WorkspaceStatus.READY.value
        finally:
            await engine.dispose()

    async def test_passes_its_docker_client_down_to_population(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """Contract #4: the whole lifecycle rides ONE docker seam — the
        service's injected client reaches populate_workspace."""
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        assert populate.clients == [fake_docker]

    async def test_concurrent_calls_do_not_hit_database_is_locked(
        self, tmp_path, fake_docker, populate
    ):
        """Regression (session discipline): the loser's pre-lock fast-path
        SELECT must not hold a read transaction across the in-process lock —
        on file-backed sqlite that deadlocks the winner's commit into
        'database is locked'."""
        run_id = str(uuid4())
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'locked.db').as_posix()}", echo=False
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        service = WorkspaceService(docker_client=fake_docker, session_factory=factory)

        async def call():
            async with factory() as session:
                ws = await service.get_or_create(session, run_id, "repo-1", "main", None)
                return ws.id

        try:
            ids = await asyncio.gather(call(), call())  # raises on 'database is locked'
            assert ids[0] == ids[1]
        finally:
            await engine.dispose()

    async def test_volume_creation_failure_marks_failed_and_raises(
        self, db_session, service, fake_docker, populate, run_id, monkeypatch
    ):
        def boom(name):
            raise RuntimeError("docker daemon exploded")

        monkeypatch.setattr(service, "_sync_create_volume", boom)

        with pytest.raises(WorkspaceCreationError, match="docker daemon exploded"):
            await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.FAILED.value
        assert "docker daemon exploded" in row.error
        assert populate.calls == []

    async def test_population_failure_removes_volume_and_marks_failed(
        self, db_session, service, fake_docker, run_id, monkeypatch
    ):
        recorder = PopulateRecorder(fail_with=RuntimeError("clone failed: exit 128"))
        monkeypatch.setattr(ws_module, "populate_workspace", recorder)

        with pytest.raises(WorkspaceCreationError, match="clone failed"):
            await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.FAILED.value
        assert "clone failed" in row.error
        # The just-created volume must NOT leak: the failure path removes it
        # before marking the row FAILED.
        assert row.volume_name not in fake_docker.store

        # cleanup on a FAILED row (executor calls it on failure too) still
        # drives the row to CLEANED and tolerates the already-gone volume.
        await service.cleanup(db_session, run_id)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert row.cleaned_at is not None
        assert row.volume_name not in fake_docker.store

    async def test_population_failure_volume_removal_is_best_effort(
        self, db_session, service, fake_docker, run_id, monkeypatch, caplog
    ):
        """A broken volume removal must not mask the creation failure."""
        recorder = PopulateRecorder(fail_with=RuntimeError("clone failed"))
        monkeypatch.setattr(ws_module, "populate_workspace", recorder)

        def broken_remove(name):
            raise RuntimeError("daemon went away")

        monkeypatch.setattr(service, "_sync_remove_volume", broken_remove)

        with caplog.at_level("ERROR"):
            with pytest.raises(WorkspaceCreationError, match="clone failed"):
                await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.FAILED.value
        assert any(
            "after failed creation" in r.message for r in caplog.records
        )  # logged, not swallowed silently

    async def test_failed_row_is_replaced_on_retry(
        self, db_session, service, fake_docker, run_id, monkeypatch
    ):
        recorder = PopulateRecorder(fail_with=RuntimeError("transient"))
        monkeypatch.setattr(ws_module, "populate_workspace", recorder)
        with pytest.raises(WorkspaceCreationError):
            await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        failed_row = await _fetch(db_session, run_id)

        recorder.fail_with = None  # next attempt succeeds
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        assert ws.status == WorkspaceStatus.READY.value
        assert ws.id != failed_row.id  # fresh lifecycle, same run id
        rows = (await db_session.execute(select(Workspace))).scalars().all()
        assert len(rows) == 1


# -----------------------------------------------------------------------------
# acquire / release
# -----------------------------------------------------------------------------


class TestAcquireRelease:
    async def test_acquire_transitions_and_persists(
        self, db_session, service, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        await service.acquire(db_session, ws.id)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.IN_USE.value
        assert row.use_count == 1

        await service.acquire(db_session, ws.id)
        row = await _fetch(db_session, run_id)
        assert row.use_count == 2

    async def test_release_returns_to_ready_at_zero(
        self, db_session, service, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.acquire(db_session, ws.id)
        await service.acquire(db_session, ws.id)

        await service.release(db_session, ws.id)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.IN_USE.value
        assert row.use_count == 1

        await service.release(db_session, ws.id)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.READY.value
        assert row.use_count == 0

    async def test_unbalanced_release_raises_loudly(
        self, db_session, service, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        with pytest.raises(WorkspaceAcquisitionError, match="Unbalanced release"):
            await service.release(db_session, ws.id)

    async def test_acquire_missing_workspace_raises(self, db_session, service):
        with pytest.raises(WorkspaceAcquisitionError, match="does not exist"):
            await service.acquire(db_session, "no-such-id")

    async def test_acquire_cleaned_workspace_raises(
        self, db_session, service, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.cleanup(db_session, run_id)
        with pytest.raises(WorkspaceAcquisitionError, match="cleaned"):
            await service.acquire(db_session, ws.id)

    async def test_no_lock_survives_a_call(self, db_session, service, populate, run_id):
        """Regression for failure_01's lock leak: after any sequence of
        service calls, the per-workspace lock table is empty."""
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.acquire(db_session, ws.id)
        await service.release(db_session, ws.id)
        await service.cleanup(db_session, run_id)
        assert service._locks.get_lock_count(ws.volume_name) == 0


# -----------------------------------------------------------------------------
# cleanup
# -----------------------------------------------------------------------------


class TestCleanup:
    async def test_cleanup_removes_volume_and_marks_cleaned(
        self, db_session, service, fake_docker, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        assert ws.volume_name in fake_docker.store

        await service.cleanup(db_session, run_id)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert row.cleaned_at is not None
        assert ws.volume_name not in fake_docker.store

    async def test_cleanup_is_idempotent(
        self, db_session, service, populate, run_id
    ):
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.cleanup(db_session, run_id)
        await service.cleanup(db_session, run_id)  # second call: no-op
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_cleanup_missing_row_is_noop(self, db_session, service):
        await service.cleanup(db_session, str(uuid4()))  # must not raise

    async def test_cleanup_tolerates_missing_volume(
        self, db_session, service, fake_docker, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        del fake_docker.store[ws.volume_name]  # volume vanished out-of-band

        await service.cleanup(db_session, run_id)
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_cleanup_force_releases_leaked_use_count(
        self, db_session, service, fake_docker, populate, run_id, caplog
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.acquire(db_session, ws.id)  # leaked by a buggy caller

        with caplog.at_level("WARNING"):
            await service.cleanup(db_session, run_id)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert row.use_count == 0
        assert any("leaked use_count" in r.message for r in caplog.records)

    async def test_cleanup_without_injected_factory_targets_callers_database(
        self, db_session, fake_docker, populate, run_id
    ):
        """With no session_factory (production singleton under test engines),
        cleanup's own session derives from the CALLER's bind — never from
        app.database's real engine."""
        service = WorkspaceService(docker_client=fake_docker)

        await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.cleanup(db_session, run_id)

        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert row.volume_name not in fake_docker.store

    async def test_cleanup_never_commits_the_callers_session(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """Session discipline: cleanup owns its session. Unrelated pending
        state on the caller's session must survive uncommitted (a rollback
        afterwards discards it)."""
        await service.get_or_create(db_session, run_id, "repo-1", "main", None)

        stray_run_id = str(uuid4())
        db_session.add(
            Workspace(
                pipeline_run_id=stray_run_id,
                repo_id="repo-x",
                volume_name=generate_volume_name(stray_run_id),
                status=WorkspaceStatus.CREATING.value,
                use_count=0,
            )
        )  # pending, deliberately NOT committed

        await service.cleanup(db_session, run_id)

        await db_session.rollback()  # discards the pending stray if uncommitted
        assert await _fetch(db_session, stray_run_id) is None  # never committed
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value  # cleanup still landed


# -----------------------------------------------------------------------------
# Orphan audit
# -----------------------------------------------------------------------------


def _old(minutes: int) -> datetime:
    return datetime.utcnow() - timedelta(minutes=minutes)


async def _plant_row(
    db, run_id, status, updated_minutes_ago, use_count=0, worker_key=DEFAULT_WORKER_KEY
):
    ws = Workspace(
        pipeline_run_id=run_id,
        worker_key=worker_key,
        repo_id="repo-1",
        volume_name=generate_volume_name(run_id, worker_key),
        status=status.value,
        use_count=use_count,
        updated_at=_old(updated_minutes_ago),
        created_at=_old(updated_minutes_ago),
    )
    db.add(ws)
    await db.commit()
    return ws


class TestOrphanAudit:
    async def test_recovers_stranded_creating_row(
        self, db_session, service, fake_docker, run_id
    ):
        """Backend crashed mid-create: row stuck CREATING, volume exists."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.CREATING, 60)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert ws.volume_name in cleaned
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert ws.volume_name not in fake_docker.store

    async def test_recovers_stuck_cleaning_row(
        self, db_session, service, fake_docker, run_id
    ):
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.CLEANING, 60)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert ws.volume_name in cleaned
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_leaves_recent_creating_row_alone(
        self, db_session, service, fake_docker, run_id
    ):
        """A create in progress right now must not be swept."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.CREATING, 0)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CREATING.value
        assert ws.volume_name in fake_docker.store

    async def test_cleans_ready_row_for_finished_pipeline(
        self, db_session, service, fake_docker
    ):
        pipeline = Pipeline(repo_id="repo-1", name="p")
        db_session.add(pipeline)
        await db_session.commit()
        run = PipelineRun(pipeline_id=pipeline.id, status="passed")
        db_session.add(run)
        await db_session.commit()

        ws = await _plant_row(db_session, run.id, WorkspaceStatus.READY, 60)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert ws.volume_name in cleaned
        row = await _fetch(db_session, run.id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_leaves_ready_row_for_running_pipeline(
        self, db_session, service, fake_docker
    ):
        pipeline = Pipeline(repo_id="repo-1", name="p")
        db_session.add(pipeline)
        await db_session.commit()
        run = PipelineRun(pipeline_id=pipeline.id, status="running")
        db_session.add(run)
        await db_session.commit()

        ws = await _plant_row(db_session, run.id, WorkspaceStatus.READY, 60)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        row = await _fetch(db_session, run.id)
        assert row.status == WorkspaceStatus.READY.value

    async def test_cleans_old_failed_row_and_removes_its_volume(
        self, db_session, service, fake_docker, run_id
    ):
        """A failed create nobody retried or cleaned is swept: volume
        removed (if present), row marked CLEANED."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.FAILED, 60)
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        cleaned = await service.audit_orphans(db_session)

        assert ws.volume_name in cleaned
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value
        assert ws.volume_name not in fake_docker.store

    async def test_cleans_old_failed_row_without_volume(
        self, db_session, service, fake_docker, run_id
    ):
        """FAILED rows whose volume is already gone (failure path removed
        it) still get their row driven to CLEANED."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.FAILED, 60)

        cleaned = await service.audit_orphans(db_session)

        assert ws.volume_name in cleaned
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_leaves_recent_failed_row_alone(
        self, db_session, service, fake_docker, run_id
    ):
        """A fresh FAILED row may still be retried by get_or_create."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.FAILED, 0)
        fake_docker.volumes.create(
            ws.volume_name,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": datetime.utcnow().isoformat(),
            },
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.FAILED.value
        assert ws.volume_name in fake_docker.store

    async def test_failed_row_is_not_live_for_the_volume_sweep(
        self, db_session, service, fake_docker, run_id
    ):
        """Sweep 3 must not treat a FAILED row as protecting its volume: an
        OLD volume behind a fresh FAILED row is removable garbage even
        though sweep 1's threshold spares the row itself."""
        ws = await _plant_row(db_session, run_id, WorkspaceStatus.FAILED, 0)
        fake_docker.volumes.create(
            ws.volume_name,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": _old(60).isoformat(),
            },
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == [ws.volume_name]
        assert ws.volume_name not in fake_docker.store
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.FAILED.value  # row untouched

    async def test_volume_sweep_is_capped_per_sweep(self, db_session, service, fake_docker):
        """A pathological backlog is worked off across intervals, not in
        one giant destructive sweep."""
        from app.services.workspace_service import ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP

        total = ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP + 5
        for i in range(total):
            fake_docker.volumes.create(
                f"lazyaf-ws-cap-{i}",
                labels={
                    "lazyaf.workspace": "true",
                    "lazyaf.created_at": _old(60).isoformat(),
                },
            )

        cleaned = await service.audit_orphans(db_session)
        assert len(cleaned) == ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP

        # The next interval finishes the job.
        cleaned = await service.audit_orphans(db_session)
        assert len(cleaned) == 5
        assert not fake_docker.store

    async def test_removes_unmatched_old_volume(self, db_session, service, fake_docker):
        name = f"lazyaf-ws-{uuid4()}"
        fake_docker.volumes.create(
            name,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": _old(60).isoformat(),
            },
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == [name]
        assert name not in fake_docker.store

    async def test_keeps_unmatched_young_volume(self, db_session, service, fake_docker):
        name = f"lazyaf-ws-{uuid4()}"
        fake_docker.volumes.create(
            name,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": datetime.utcnow().isoformat(),
            },
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        assert name in fake_docker.store

    async def test_keeps_volume_with_live_row(
        self, db_session, service, fake_docker, populate, run_id
    ):
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        # Make the volume look ancient; the live READY row (with a missing
        # pipeline run) is under sweep 2's grace period, so nothing happens.
        fake_docker.store[ws.volume_name]["labels"]["lazyaf.created_at"] = _old(
            600
        ).isoformat()

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        assert ws.volume_name in fake_docker.store

    async def test_ignores_foreign_prefix_volumes(
        self, db_session, service, fake_docker
    ):
        fake_docker.volumes.create(
            "someone-elses-volume",
            labels={"lazyaf.workspace": "true", "lazyaf.created_at": _old(60).isoformat()},
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == []
        assert "someone-elses-volume" in fake_docker.store


# -----------------------------------------------------------------------------
# start_orphan_audit task factory
# -----------------------------------------------------------------------------


class TestOrphanAuditTask:
    async def test_factory_runs_immediate_sweep_and_cancels_cleanly(
        self, async_engine, service, fake_docker, run_id
    ):
        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            ws = await _plant_row(session, run_id, WorkspaceStatus.CREATING, 60)
            fake_docker.volumes.create(
                ws.volume_name, labels={"lazyaf.workspace": "true"}
            )

        task_factory = start_orphan_audit(
            interval_seconds=3600, service=service, session_factory=factory
        )
        task = task_factory()
        assert isinstance(task, asyncio.Task)

        try:
            # First sweep runs immediately; poll until it lands.
            for _ in range(100):
                async with factory() as session:
                    row = await _fetch(session, run_id)
                    if row.status == WorkspaceStatus.CLEANED.value:
                        break
                await asyncio.sleep(0.05)
            assert row.status == WorkspaceStatus.CLEANED.value
            assert ws.volume_name not in fake_docker.store
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_loop_survives_sweep_errors(self, service, fake_docker):
        """A failing sweep is logged and retried, never kills the task."""
        calls = []

        def broken_factory():
            calls.append(1)
            raise RuntimeError("db exploded")

        task = start_orphan_audit(
            interval_seconds=0.01, service=service, session_factory=broken_factory
        )()
        try:
            await asyncio.sleep(0.15)
            assert not task.done()
            assert len(calls) >= 2  # kept retrying
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# -----------------------------------------------------------------------------
# Module contract
# -----------------------------------------------------------------------------


class TestModuleContract:
    def test_singleton_exists(self):
        assert isinstance(workspace_service, WorkspaceService)

    def test_volume_names_use_full_run_id(self):
        run_id = "abcd1234-5678-9012-3456-789012345678"
        assert generate_volume_name(run_id) == f"lazyaf-ws-{run_id}"


# -----------------------------------------------------------------------------
# Per-worker LANES (M13-1)
#
# A run owns one workspace PER LANE. Without this, K parallel agent steps in
# a fan-out all mount ONE working tree and any conflict rate a benchmark
# reports is a property of the schema rather than of the strategy under
# test. The lane axis is what lets K workers hold K independent checkouts
# and integrate through git instead of through a shared directory.
# -----------------------------------------------------------------------------


class TestLanes:
    async def test_two_lanes_get_two_rows_and_two_volumes(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """THE enabling behavior: one run, two independent checkouts."""
        a = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        b = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w2"
        )

        assert a.id != b.id
        assert a.volume_name != b.volume_name
        assert a.volume_name == f"lazyaf-ws-{run_id}-w1"
        assert b.volume_name == f"lazyaf-ws-{run_id}-w2"
        # Two REAL volumes, and two clones into them.
        assert a.volume_name in fake_docker.store
        assert b.volume_name in fake_docker.store
        assert len(populate.calls) == 2

        rows = (await db_session.execute(select(Workspace))).scalars().all()
        assert {r.worker_key for r in rows} == {"w1", "w2"}

    async def test_same_lane_is_idempotent(
        self, db_session, service, populate, run_id
    ):
        first = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        second = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )

        assert second.id == first.id
        assert len(populate.calls) == 1

    async def test_default_key_and_no_key_are_the_same_workspace(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """The single-worker guarantee at the service level: passing the
        sentinel explicitly must not fork a second checkout."""
        implicit = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        explicit = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key=DEFAULT_WORKER_KEY
        )

        assert explicit.id == implicit.id
        assert implicit.worker_key == DEFAULT_WORKER_KEY
        assert implicit.volume_name == f"lazyaf-ws-{run_id}"
        assert len(populate.calls) == 1
        assert len(fake_docker.store) == 1

    async def test_a_single_worker_run_creates_exactly_one_object_of_each_kind(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """The non-negotiable: the overwhelmingly common case is byte-for-byte
        what it was before lanes existed — one row, one volume with the
        unsuffixed name, one populate container, one lock key, and nothing
        left behind after cleanup."""
        ws = await service.get_or_create(db_session, run_id, "repo-1", "main", None)
        await service.acquire(db_session, ws.id)
        await service.release(db_session, ws.id)

        assert ws.volume_name == generate_volume_name(run_id)
        assert set(fake_docker.store) == {f"lazyaf-ws-{run_id}"}
        assert len(populate.calls) == 1
        rows = (await db_session.execute(select(Workspace))).scalars().all()
        assert len(rows) == 1

        await service.cleanup(db_session, run_id)

        assert fake_docker.store == {}
        assert service._locks.get_lock_count(ws.volume_name) == 0
        row = await _fetch(db_session, run_id)
        assert row.status == WorkspaceStatus.CLEANED.value

    async def test_invalid_lane_key_is_refused_loudly(
        self, db_session, service, populate, run_id
    ):
        with pytest.raises(ValueError, match="non-empty"):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=""
            )
        with pytest.raises(ValueError, match="must be a string"):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=7
            )
        assert populate.calls == []

    async def test_duplicate_row_for_one_lane_is_refused_by_the_database(
        self, db_session, service, populate, run_id
    ):
        """The in-process lock is single-process by design, so the DATABASE
        has to be the backstop. Without the composite unique index a second
        row for one lane means a second volume and a second clone that
        nothing will ever find again, release, or clean."""
        await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )

        db_session.add(
            Workspace(
                pipeline_run_id=run_id,
                worker_key="w1",
                repo_id="repo-1",
                volume_name="lazyaf-ws-some-other-name",
                status=WorkspaceStatus.CREATING.value,
                use_count=0,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_two_runs_may_share_a_lane_name(
        self, db_session, service, populate
    ):
        """Uniqueness is (run, lane), not lane: every run has a "w1"."""
        run_a, run_b = str(uuid4()), str(uuid4())
        a = await service.get_or_create(
            db_session, run_a, "repo-1", "main", None, worker_key="w1"
        )
        b = await service.get_or_create(
            db_session, run_b, "repo-1", "main", None, worker_key="w1"
        )
        assert a.id != b.id
        assert a.volume_name != b.volume_name

    async def test_list_for_run_enumerates_every_lane(
        self, db_session, service, populate, run_id
    ):
        for key in ("w2", "w1", None):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )

        rows = await service.list_for_run(db_session, run_id)

        assert [r.worker_key for r in rows] == [DEFAULT_WORKER_KEY, "w1", "w2"]

    async def test_use_count_is_per_lane(
        self, db_session, service, populate, run_id
    ):
        """use_count survives lanes as an INTEGER because a single lane still
        has concurrent holders (parallel entry points, the debug gate's pin
        alongside a paused step). What lanes change is that lane B's count is
        not lane A's."""
        a = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        b = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w2"
        )

        await service.acquire(db_session, a.id)
        await service.acquire(db_session, a.id)
        await service.acquire(db_session, b.id)

        assert (await _fetch_lane(db_session, run_id, "w1")).use_count == 2
        assert (await _fetch_lane(db_session, run_id, "w2")).use_count == 1

    async def test_lanes_do_not_contend_on_one_lock(
        self, db_session, service, populate, run_id
    ):
        """Lock keys are volume names and volume names are lane-scoped, so
        K workers no longer serialize through one asyncio.Lock the way every
        step of a run used to."""
        a = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        b = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w2"
        )
        assert a.volume_name != b.volume_name

    async def test_concurrent_calls_for_one_lane_create_one_workspace(
        self, tmp_path, service, populate, run_id
    ):
        """The existing single-workspace race, re-run at lane grain."""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'lanes.db').as_posix()}", echo=False
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def call(key):
            async with factory() as session:
                ws = await service.get_or_create(
                    session, run_id, "repo-1", "main", None, worker_key=key
                )
                return ws.id

        try:
            ids = await asyncio.gather(call("w1"), call("w1"), call("w2"))
            assert ids[0] == ids[1]
            assert ids[2] != ids[0]
            assert len(populate.calls) == 2
            async with factory() as session:
                rows = (await session.execute(select(Workspace))).scalars().all()
                assert len(rows) == 2
        finally:
            await engine.dispose()

    async def test_branch_mismatch_on_an_existing_lane_raises(
        self, db_session, service, populate, run_id
    ):
        """A lane IS one checkout. Returning the existing row for a different
        base would hand the caller a working tree at the wrong commit — a
        green run measuring nothing (R1)."""
        await service.get_or_create(
            db_session, run_id, "repo-1", "main", "a" * 40, worker_key="w1"
        )

        with pytest.raises(WorkspaceCreationError) as exc:
            await service.get_or_create(
                db_session, run_id, "repo-1", "feature", "a" * 40, worker_key="w1"
            )
        assert "main" in str(exc.value) and "feature" in str(exc.value)

    async def test_commit_mismatch_on_an_existing_lane_raises(
        self, db_session, service, populate, run_id
    ):
        await service.get_or_create(
            db_session, run_id, "repo-1", "main", "a" * 40, worker_key="w1"
        )

        with pytest.raises(WorkspaceCreationError, match="b" * 40):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", "b" * 40, worker_key="w1"
            )

    async def test_matching_checkout_still_returns_the_same_row(
        self, db_session, service, populate, run_id
    ):
        """The mismatch guard must not fire on the path every caller takes:
        the executor and the debug gate both pass the run's trigger_context."""
        first = await service.get_or_create(
            db_session, run_id, "repo-1", "main", "a" * 40
        )
        second = await service.get_or_create(
            db_session, run_id, "repo-1", "main", "a" * 40
        )
        assert second.id == first.id


class TestLaneCleanup:
    async def test_cleanup_without_a_lane_cleans_every_lane(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """Pipeline teardown passes a run id and nothing else — which is why
        cleanup absorbs "all lanes" instead of the executor learning about
        them."""
        keys = [None, "w1", "w2", "w3"]
        for key in keys:
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )
        assert len(fake_docker.store) == 4

        await service.cleanup(db_session, run_id)

        assert fake_docker.store == {}
        rows = await service.list_for_run(db_session, run_id)
        assert len(rows) == 4
        for row in rows:
            await db_session.refresh(row)
            assert row.status == WorkspaceStatus.CLEANED.value
            assert row.cleaned_at is not None

    async def test_cleanup_of_one_lane_leaves_its_siblings_alone(
        self, db_session, service, fake_docker, populate, run_id
    ):
        keep = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        drop = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w2"
        )

        await service.cleanup(db_session, run_id, worker_key="w2")

        assert keep.volume_name in fake_docker.store
        assert drop.volume_name not in fake_docker.store
        assert (
            await _fetch_lane(db_session, run_id, "w1")
        ).status == WorkspaceStatus.READY.value
        assert (
            await _fetch_lane(db_session, run_id, "w2")
        ).status == WorkspaceStatus.CLEANED.value

    async def test_multi_lane_cleanup_is_idempotent(
        self, db_session, service, fake_docker, populate, run_id
    ):
        for key in (None, "w1"):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )

        await service.cleanup(db_session, run_id)
        await service.cleanup(db_session, run_id)  # second call: no-op

        assert fake_docker.store == {}

    async def test_cleanup_reports_all_lane_failures_not_just_the_first(
        self, db_session, service, fake_docker, populate, run_id, monkeypatch
    ):
        """Stopping at the first failed lane would leak every volume behind
        it, and the operator would only ever hear about one of them."""
        for key in ("w1", "w2", "w3"):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )

        def broken_remove(name):
            raise RuntimeError(f"daemon refused {name}")

        monkeypatch.setattr(service, "_sync_remove_volume", broken_remove)

        with pytest.raises(WorkspaceCleanupError) as exc:
            await service.cleanup(db_session, run_id)

        message = str(exc.value)
        assert "3 of 3" in message
        for key in ("w1", "w2", "w3"):
            assert key in message
            # Every lane was ATTEMPTED, not just the first.
            row = await _fetch_lane(db_session, run_id, key)
            assert row.status == WorkspaceStatus.FAILED.value

    async def test_one_failing_lane_does_not_block_its_siblings(
        self, db_session, service, fake_docker, populate, run_id, monkeypatch
    ):
        for key in ("w1", "w2"):
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )
        doomed = generate_volume_name(run_id, "w1")
        real_remove = service._sync_remove_volume

        def flaky_remove(name):
            if name == doomed:
                raise RuntimeError("daemon refused")
            return real_remove(name)

        monkeypatch.setattr(service, "_sync_remove_volume", flaky_remove)

        with pytest.raises(WorkspaceCleanupError, match="w1"):
            await service.cleanup(db_session, run_id)

        # w2 was cleaned even though w1 blew up first.
        assert generate_volume_name(run_id, "w2") not in fake_docker.store
        assert (
            await _fetch_lane(db_session, run_id, "w2")
        ).status == WorkspaceStatus.CLEANED.value

    async def test_leak_warning_names_the_lane(
        self, db_session, service, fake_docker, populate, run_id, caplog
    ):
        """In an 8-way fan-out an unattributed leak report says nothing about
        WHICH worker failed to release."""
        ws = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w3"
        )
        await service.acquire(db_session, ws.id)

        with caplog.at_level("WARNING"):
            await service.cleanup(db_session, run_id)

        leak = [r for r in caplog.records if "leaked use_count" in r.message]
        assert leak and "w3" in leak[0].getMessage()


class TestLaneOrphanAudit:
    async def test_sweep_keeps_every_live_lane_of_one_run(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """Sweep 3 compares against live ROWS, not runs. Seven lanes of one
        run must all survive while a genuinely unmatched volume goes."""
        lanes = [None, "w1", "w2", "w3", "w4", "w5", "integrate"]
        for key in lanes:
            await service.get_or_create(
                db_session, run_id, "repo-1", "main", None, worker_key=key
            )
        orphan = generate_volume_name(str(uuid4()), "w1")
        fake_docker.volumes.create(
            orphan,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": _old(60).isoformat(),
            },
        )

        cleaned = await service.audit_orphans(db_session)

        assert cleaned == [orphan]
        assert orphan not in fake_docker.store
        for key in lanes:
            assert generate_volume_name(run_id, key) in fake_docker.store

    async def test_sweep_reaps_an_orphaned_lane_and_keeps_its_siblings(
        self, db_session, service, fake_docker, populate, run_id
    ):
        """A crash between the row commit and the volume create leaves a
        stranded lane. Sweep 1 finishes it without touching the run's other
        checkouts."""
        live = await service.get_or_create(
            db_session, run_id, "repo-1", "main", None, worker_key="w1"
        )
        stranded = await _plant_row(
            db_session, run_id, WorkspaceStatus.CREATING, 60, worker_key="w2"
        )
        fake_docker.volumes.create(
            stranded.volume_name, labels={"lazyaf.workspace": "true"}
        )

        cleaned = await service.audit_orphans(db_session)

        assert stranded.volume_name in cleaned
        assert stranded.volume_name not in fake_docker.store
        assert live.volume_name in fake_docker.store
        assert (
            await _fetch_lane(db_session, run_id, "w1")
        ).status == WorkspaceStatus.READY.value

    async def test_audit_clean_log_names_the_lane(
        self, db_session, service, fake_docker, run_id, caplog
    ):
        ws = await _plant_row(
            db_session, run_id, WorkspaceStatus.CREATING, 60, worker_key="w7"
        )
        fake_docker.volumes.create(ws.volume_name, labels={"lazyaf.workspace": "true"})

        with caplog.at_level("WARNING"):
            await service.audit_orphans(db_session)

        cleaning = [
            r for r in caplog.records if "Orphan audit cleaning workspace" in r.message
        ]
        assert cleaning and "w7" in cleaning[0].getMessage()

    async def test_unmatched_volume_log_names_the_lane(
        self, db_session, service, fake_docker, caplog
    ):
        """A fan-out orphan report is unreadable if the lines only say
        "removed <44 random characters>"."""
        orphan = generate_volume_name(str(uuid4()), "w4")
        fake_docker.volumes.create(
            orphan,
            labels={
                "lazyaf.workspace": "true",
                "lazyaf.created_at": _old(60).isoformat(),
            },
        )

        with caplog.at_level("WARNING"):
            await service.audit_orphans(db_session)

        removed = [
            r for r in caplog.records if "removed unmatched volume" in r.message
        ]
        assert removed and "lane w4" in removed[0].getMessage()
