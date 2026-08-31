"""
WorkspaceService — pipeline workspace lifecycle (Phase 12.2-INT).

Rebuilds the SHAPE of failure_01's workspace service on main's tested
primitives (WorkspaceStateMachine + WorkspaceLockManager), explicitly
avoiding the four defects the salvage audit found in the original:

1. Lock leak in acquire/release: failure_01 acquired a SHARED lock in
   acquire_workspace and "released it in release_workspace" — which had no
   reference to it. Here, locks guard only the critical section INSIDE a
   single service call (always via the manager's context manager); the
   persisted use_count — driven through WorkspaceStateMachine — is the
   cross-call accounting. No lock ever outlives a method call.
2. MissingGreenlet lazy-loads: the Workspace model has NO relationships;
   related rows (PipelineRun in the orphan audit) are fetched with
   explicit queries. Nothing can lazy-load.
3. Mid-op commits of the caller's session: see "Session ownership" below —
   commits are part of the documented contract, not a surprise.
4. Stranded CREATING rows: the row is committed in CREATING *before* any
   Docker work (crash-safe marker), and audit_orphans recovers rows stuck
   in CREATING/CLEANING past a threshold.

Session ownership
-----------------
Every public method that takes ``db`` COMMITS that session (workspace
state changes must be durable at each lifecycle step so a crash is
recoverable by the orphan audit). Callers must invoke these methods at a
transaction boundary — never with unrelated pending state on the session.
The periodic orphan-audit task owns its sessions entirely (one per sweep,
from app.database.async_session or an injected factory).

Lanes (M13-1)
-------------
A pipeline run owns one workspace PER LANE — ``(pipeline_run_id,
worker_key)`` is unique in the database. ``worker_key`` defaults to
``DEFAULT_WORKER_KEY`` ("default"), and every entry point here takes it as a
keyword argument that nobody is forced to pass, so a single-worker run
behaves byte-identically to the pre-M13-1 service: same volume name, same
lock key, same number of docker objects, same lifecycle.

Locking
-------
All locks are keyed by the workspace's VOLUME NAME (deterministic from
``(pipeline_run_id, worker_key)`` via generate_volume_name), so creation,
acquire/release, cleanup, and the audit all contend on the same key. Locks
are in-process (single backend process assumption, as on main today).

Because the lock key is now LANE-scoped, two lanes of one run never contend
on acquire/release — where every step of a run previously serialized through
one asyncio.Lock. The database's composite unique index, not the lock, is
what makes concurrent creation of one lane safe across a crash.

Blocking work
-------------
All docker SDK calls are sync (docker-py) and run via
starlette.concurrency.run_in_threadpool — nothing blocks the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models.workspace import Workspace
from app.services.workspace.locking import LockTimeoutError, LockType, WorkspaceLockManager
from app.services.workspace.state_machine import (
    WorkspaceStateMachine,
    WorkspaceStatus,
    generate_volume_name,
    is_orphaned,
    parse_volume_name_parts,
)
from app.services.workspace.worker_key import DEFAULT_WORKER_KEY, validate_worker_key

logger = logging.getLogger(__name__)

WORKSPACE_VOLUME_PREFIX = "lazyaf-ws-"
WORKSPACE_VOLUME_LABEL = "lazyaf.workspace"
CREATED_AT_LABEL = "lazyaf.created_at"


class WorkspaceError(Exception):
    """Base exception for workspace lifecycle errors."""


class WorkspaceCreationError(WorkspaceError):
    """Workspace volume creation or population failed."""


class WorkspaceAcquisitionError(WorkspaceError):
    """Workspace cannot be acquired/released in its current state."""


class WorkspaceCleanupError(WorkspaceError):
    """Workspace cleanup failed (volume removal error)."""


try:
    from app.services.workspace.population import default_docker_client, populate_workspace
except ImportError:  # pragma: no cover - transient while 12.2-INT lands in parallel
    logger.warning(
        "app.services.workspace.population is not importable; workspace "
        "population will FAIL LOUDLY until it lands (R1: no silent fallbacks)."
    )

    async def populate_workspace(
        volume_name: str, repo_id: str, branch: str, commit_sha: str | None, **_kwargs
    ) -> None:
        raise WorkspaceCreationError(
            "app.services.workspace.population is missing: cannot populate "
            f"volume {volume_name} for repo {repo_id}. This is a wiring error "
            "(population.py has not landed), not a recoverable condition."
        )

    def default_docker_client():
        import docker

        return docker.from_env()


# Ceiling on destructive actions per audit sweep: a single pathological
# sweep (e.g. after a mass DB reset) must not spend minutes deleting
# hundreds of volumes in one go; the next interval continues the job.
#
# M13-1 changed what this counts, not whether it is right: with per-worker
# lanes, 25 removals covers ~3 runs of a K=8 fan-out rather than 25 runs.
# A capped sweep is a DEFERRAL (the loop re-runs every 300s and logs the
# cap hit), not a leak, so the constant stays put until a test shows real
# starvation — raising it on a hunch just makes the pathological sweep the
# long one again.
ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP = 25


def _hydrate_machine(workspace: Workspace) -> WorkspaceStateMachine:
    """Build a state machine positioned at the row's persisted state."""
    return WorkspaceStateMachine(
        initial_status=WorkspaceStatus(workspace.status),
        use_count=workspace.use_count,
    )


def _persist_machine(workspace: Workspace, machine: WorkspaceStateMachine) -> None:
    """Write the machine's state back onto the row (caller commits)."""
    workspace.status = machine.current_status.value
    workspace.use_count = machine.use_count
    workspace.updated_at = datetime.utcnow()


class WorkspaceService:
    """Manages workspace rows and their backing named Docker volumes.

    ``docker_client`` may be injected (tests); otherwise the default client
    (population.default_docker_client — contract #1, honors
    settings.docker_host) is created lazily inside the threadpool on first
    use. ``session_factory`` is used by methods that own their sessions
    (cleanup); when absent, such sessions are derived from the caller's
    session bind (see _own_session).
    """

    def __init__(
        self,
        docker_client=None,
        lock_manager: WorkspaceLockManager | None = None,
        session_factory: Callable[[], AsyncSession] | None = None,
    ):
        self._docker = docker_client
        self._locks = lock_manager or WorkspaceLockManager()
        self._session_factory = session_factory

    def _own_session(self, db: AsyncSession) -> AsyncSession:
        """A session THIS service owns, for methods that must not commit the
        caller's session. Prefers the injected factory (tests); otherwise
        derives from the caller's bind so it always targets the same
        database (the module singleton serves many engines under test)."""
        if self._session_factory is not None:
            return self._session_factory()
        # get_bind() hands back the caller's SYNC engine; re-wrap it for a
        # new AsyncSession (callers bind sessions to engines, not connections).
        from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: PLC0415

        return AsyncSession(bind=AsyncEngine(db.get_bind()), expire_on_commit=False)

    def reset(self) -> None:
        """Test-mode reset hook (see routers/test_api.py registry): fresh lock
        manager. No lock outlives a public method call, so any state in the
        manager at reset time is bookkeeping for rows the DB reset deletes."""
        self._locks = WorkspaceLockManager()

    # ------------------------------------------------------------------
    # Sync docker helpers — ALWAYS called via run_in_threadpool
    # ------------------------------------------------------------------

    def _client(self):
        if self._docker is None:
            self._docker = default_docker_client()
        return self._docker

    def _sync_create_volume(self, volume_name: str) -> None:
        """Create the named volume (idempotent)."""
        import docker.errors

        client = self._client()
        try:
            client.volumes.get(volume_name)
            logger.debug("Volume %s already exists", volume_name)
            return
        except docker.errors.NotFound:
            pass
        client.volumes.create(
            name=volume_name,
            labels={
                WORKSPACE_VOLUME_LABEL: "true",
                CREATED_AT_LABEL: datetime.utcnow().isoformat(),
            },
        )
        logger.info("Created workspace volume %s", volume_name)

    def _sync_remove_volume(self, volume_name: str) -> None:
        """Remove the named volume; a missing volume is fine (idempotent).

        One API call (low-level remove_volume + swallowed NotFound) instead
        of a redundant get-then-remove round trip.
        """
        import docker.errors

        client = self._client()
        try:
            client.api.remove_volume(volume_name, force=True)
        except docker.errors.NotFound:
            logger.debug("Volume %s already gone", volume_name)
            return
        logger.info("Removed workspace volume %s", volume_name)

    def _sync_volume_exists(self, volume_name: str) -> bool:
        import docker.errors

        client = self._client()
        try:
            client.volumes.get(volume_name)
            return True
        except docker.errors.NotFound:
            return False

    def _sync_list_workspace_volumes(self, prefix: str) -> list[tuple[str, str | None]]:
        """List (name, created_at_label) for our volumes under ``prefix``."""
        client = self._client()
        volumes = client.volumes.list(filters={"label": f"{WORKSPACE_VOLUME_LABEL}=true"})
        out: list[tuple[str, str | None]] = []
        for volume in volumes:
            name = volume.name
            if not name.startswith(prefix):
                continue
            labels = (volume.attrs or {}).get("Labels") or {}
            out.append((name, labels.get(CREATED_AT_LABEL)))
        return out

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def _get_lane(
        self, db: AsyncSession, pipeline_run_id: str, worker_key: str
    ) -> Workspace | None:
        """The ONE workspace row for a (run, lane), or None.

        scalar_one_or_none is kept on purpose: with the composite unique
        index in place, two rows for one lane is a corrupted database and
        must raise, not be silently picked from.

        ``populate_existing`` is load-bearing and is NOT an optimisation
        knob. The session factory is ``expire_on_commit=False``
        (``database.py``), so a row already in this session's identity map
        comes back from cache and is never refreshed from the database. Two
        callers racing for one lane therefore see this:

            A: creates the row, populates the volume, commits status=READY
            B: re-reads, gets its OWN CACHED COPY still saying 'creating',
               concludes the row is stranded, and DELETES A's populated
               volume out from under it

        A is then left holding a workspace id that no longer exists and
        ``acquire()`` raises. Measured at ~5% in a unit test whose populate
        is instant; a real clone takes seconds, so the window is far wider
        in production. Refreshing here closes it (60/60 green with, ~5%
        failures without).
        """
        result = await db.execute(
            select(Workspace)
            .where(
                Workspace.pipeline_run_id == pipeline_run_id,
                Workspace.worker_key == worker_key,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_for_run(
        self, db: AsyncSession, pipeline_run_id: str
    ) -> list[Workspace]:
        """Every lane of a run, ordered by lane key.

        The ONLY supported way to enumerate a run's workspaces. It exists so
        no future caller reaches for ``scalar_one_or_none()`` on a
        run-scoped query — which is exactly what breaks the instant a run
        fans out (MultipleResultsFound, mid-pipeline).
        """
        result = await db.execute(
            select(Workspace)
            .where(Workspace.pipeline_run_id == pipeline_run_id)
            .order_by(Workspace.worker_key)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        db: AsyncSession,
        pipeline_run_id: str,
        repo_id: str,
        branch: str,
        commit_sha: str | None = None,
        *,
        worker_key: str | None = None,
    ) -> Workspace:
        """Idempotently get or create ONE LANE's workspace for a pipeline run.

        ``worker_key`` names the lane (an independent checkout); ``None``
        means the default lane, which produces exactly the volume name,
        lock key and lifecycle the pre-M13-1 service produced. K parallel
        workers pass K distinct keys and get K independent working trees.

        Returns the row in READY (or IN_USE) status. On first creation:
        commits a CREATING row (crash marker), creates the named volume,
        populates it (clone into /workspace/repo via populate_workspace),
        then commits READY. Concurrent calls for the same (run, lane) are
        serialized on an exclusive lock; losers return the winner's row.
        Calls for DIFFERENT lanes of one run do not contend at all.

        A leftover CREATING (crashed create), FAILED, or CLEANED row for
        the same lane is replaced: its volume is removed and a fresh
        lifecycle starts (new row id, same run + lane).

        Session ownership: COMMITS ``db`` (multiple times). Raises
        WorkspaceCreationError on volume/population failure, leaving the
        row FAILED with ``error`` set; the just-created volume is removed
        best-effort on the way out (no leak), and the orphan audit backstops
        anything that survives a crash. Also raises WorkspaceCreationError
        if an existing lane was checked out at a different branch/commit
        than the one requested (see _assert_checkout_matches).
        """
        key = DEFAULT_WORKER_KEY if worker_key is None else validate_worker_key(worker_key)
        volume_name = generate_volume_name(pipeline_run_id, key)

        # Fast path: no lock for the common re-read.
        workspace = await self._get_lane(db, pipeline_run_id, key)
        if workspace is not None and workspace.status in (
            WorkspaceStatus.READY.value,
            WorkspaceStatus.IN_USE.value,
        ):
            self._assert_checkout_matches(workspace, branch, commit_sha)
            return workspace
        # End the fast-path's READ transaction before awaiting the lock:
        # holding it while a concurrent creator commits deadlocks file-backed
        # sqlite ("database is locked"). Commit (not rollback): this method's
        # contract already commits ``db``, and rollback would expire the
        # caller's loaded objects (executor holds run/step rows across this
        # call).
        await db.commit()

        async with self._locks.lock(
            volume_name, LockType.EXCLUSIVE, timeout=60.0, reason="get_or_create"
        ):
            # Re-check under the lock: a concurrent creator may have won.
            workspace = await self._get_lane(db, pipeline_run_id, key)
            if workspace is not None:
                if workspace.status in (
                    WorkspaceStatus.READY.value,
                    WorkspaceStatus.IN_USE.value,
                ):
                    self._assert_checkout_matches(workspace, branch, commit_sha)
                    return workspace
                if workspace.status == WorkspaceStatus.CLEANING.value:
                    raise WorkspaceCreationError(
                        f"Workspace for run {pipeline_run_id} lane {key!r} is "
                        "being cleaned; cannot recreate mid-cleanup."
                    )
                # CREATING (stranded), FAILED, or CLEANED: replace the row.
                logger.warning(
                    "Replacing stale workspace row %s (status=%s) for run %s lane %s",
                    workspace.id,
                    workspace.status,
                    pipeline_run_id,
                    key,
                )
                await run_in_threadpool(self._sync_remove_volume, volume_name)
                await db.delete(workspace)
                await db.commit()

            # Crash-safe marker: row exists BEFORE any Docker work, so the
            # orphan audit can recover if we die mid-create.
            workspace = Workspace(
                pipeline_run_id=pipeline_run_id,
                worker_key=key,
                repo_id=repo_id,
                volume_name=volume_name,
                branch=branch,
                commit_sha=commit_sha,
                status=WorkspaceStatus.CREATING.value,
                use_count=0,
            )
            db.add(workspace)
            await db.commit()

            machine = _hydrate_machine(workspace)
            try:
                await run_in_threadpool(self._sync_create_volume, volume_name)
                # The service's own client rides down into population so the
                # whole lifecycle uses one docker seam (contract #4).
                client = await run_in_threadpool(self._client)
                await populate_workspace(
                    volume_name, repo_id, branch, commit_sha, client=client
                )
            except Exception as exc:
                # Best effort: do not leak the just-created volume. The row
                # goes FAILED either way; the orphan audit backstops a failed
                # removal.
                try:
                    await run_in_threadpool(self._sync_remove_volume, volume_name)
                except Exception:
                    logger.exception(
                        "Could not remove volume %s after failed creation "
                        "(orphan audit will retry)",
                        volume_name,
                    )
                machine.transition_to(WorkspaceStatus.FAILED)
                workspace.error = f"Workspace creation failed: {exc}"
                _persist_machine(workspace, machine)
                await db.commit()
                logger.error(
                    "Workspace creation failed for run %s lane %s: %s",
                    pipeline_run_id,
                    key,
                    exc,
                )
                raise WorkspaceCreationError(
                    f"Failed to create workspace for run {pipeline_run_id} "
                    f"lane {key!r}: {exc}"
                ) from exc

            machine.transition_to(WorkspaceStatus.READY)
            _persist_machine(workspace, machine)
            await db.commit()
            await db.refresh(workspace)
            logger.info(
                "Workspace %s ready for run %s lane %s (volume %s)",
                workspace.id,
                pipeline_run_id,
                key,
                volume_name,
            )
            return workspace

    @staticmethod
    def _assert_checkout_matches(
        workspace: Workspace, branch: str, commit_sha: str | None
    ) -> None:
        """Refuse to hand back a lane checked out at something else (R1).

        Harmless and unreachable while a run has one lane on one branch —
        every caller passes the same values out of the run's
        trigger_context. It stops being harmless the moment lanes carry
        different bases (a worker branched off ``case.base_commit_sha``,
        the trunk on the run's branch): returning the existing row would
        silently give the caller a working tree at the WRONG commit, and the
        run would look green while measuring nothing.
        """
        if workspace.branch == branch and workspace.commit_sha == commit_sha:
            return
        raise WorkspaceCreationError(
            f"Workspace {workspace.id} (run {workspace.pipeline_run_id} lane "
            f"{workspace.worker_key!r}) is checked out at branch "
            f"{workspace.branch!r} commit {workspace.commit_sha!r}, but was "
            f"requested at branch {branch!r} commit {commit_sha!r}. A lane is "
            "one checkout: use a different worker_key for a different base."
        )

    async def acquire(self, db: AsyncSession, workspace_id: str) -> None:
        """Acquire the workspace for a step: use_count += 1, READY -> IN_USE.

        Session ownership: COMMITS ``db``. Raises WorkspaceAcquisitionError
        if the workspace does not exist or is not READY/IN_USE.
        """
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            raise WorkspaceAcquisitionError(f"Workspace {workspace_id} does not exist")

        async with self._locks.lock(
            workspace.volume_name, LockType.EXCLUSIVE, timeout=30.0, reason="acquire"
        ):
            await db.refresh(workspace)
            if workspace.status not in (
                WorkspaceStatus.READY.value,
                WorkspaceStatus.IN_USE.value,
            ):
                raise WorkspaceAcquisitionError(
                    f"Workspace {workspace_id} is {workspace.status}; cannot acquire"
                )
            machine = _hydrate_machine(workspace)
            machine.acquire()
            _persist_machine(workspace, machine)
            await db.commit()
        logger.debug(
            "Acquired workspace %s (use_count=%d)", workspace_id, workspace.use_count
        )

    async def release(self, db: AsyncSession, workspace_id: str) -> None:
        """Release the workspace after a step: use_count -= 1, IN_USE -> READY at 0.

        Session ownership: COMMITS ``db``. Raises WorkspaceAcquisitionError
        if the workspace does not exist or use_count would go negative
        (a paired-release bug — loud, never swallowed).
        """
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            raise WorkspaceAcquisitionError(f"Workspace {workspace_id} does not exist")

        async with self._locks.lock(
            workspace.volume_name, LockType.EXCLUSIVE, timeout=30.0, reason="release"
        ):
            await db.refresh(workspace)
            machine = _hydrate_machine(workspace)
            try:
                machine.release()
            except ValueError as exc:
                raise WorkspaceAcquisitionError(
                    f"Unbalanced release on workspace {workspace_id}: {exc}"
                ) from exc
            _persist_machine(workspace, machine)
            await db.commit()
        logger.debug(
            "Released workspace %s (use_count=%d)", workspace_id, workspace.use_count
        )

    async def cleanup(
        self,
        db: AsyncSession,
        pipeline_run_id: str,
        *,
        worker_key: str | None = None,
    ) -> None:
        """Remove a run's workspace volume(s) and mark the row(s) CLEANED.

        ``worker_key=None`` — the pipeline-teardown case, and what every
        caller passes today — cleans EVERY lane of the run. For a
        single-worker run that is a one-element list and the behavior is
        exactly what it was before lanes existed. Pass a key to clean one
        lane.

        MUST be called on pipeline completion AND failure. Idempotent:
        a missing row, an already-CLEANED row, and a missing volume are
        all no-ops. A leaked use_count is force-released with a WARNING
        (cleanup at pipeline end means no step can legitimately hold it).

        Lanes are cleaned one at a time, each under its OWN volume lock —
        never nested, so a slow lane cannot deadlock against a concurrent
        acquire on another. A lane that fails does not abort the rest: all
        lanes are attempted and the failures are re-raised together, because
        stopping at the first would leak every volume behind it.

        Session ownership: ``db`` is only used to locate the database (its
        bind); cleanup NEVER reads from, commits, or otherwise touches the
        caller's session. Cleanup runs at pipeline teardown, where callers
        may still carry unrelated pending state — committing their session
        here would flush it mid-flight — so it opens and commits its OWN
        session (injected session_factory, or one bound to ``db``'s engine).

        Raises WorkspaceCleanupError if any lane's volume removal fails
        (those rows left FAILED with ``error`` set; cleanup may be retried —
        FAILED -> CLEANING is a valid transition).
        """
        async with self._own_session(db) as session:
            if worker_key is None:
                rows = await self.list_for_run(session, pipeline_run_id)
            else:
                key = validate_worker_key(worker_key)
                one = await self._get_lane(session, pipeline_run_id, key)
                rows = [one] if one is not None else []

            if not rows:
                logger.debug("cleanup: no workspace row for run %s", pipeline_run_id)
                return

            # Same discipline as get_or_create's fast path: never hold a
            # read transaction across an await on the lock. (Capture the
            # lock keys first — rollback expires the instances, and expired
            # attribute access would lazy-load.)
            targets = [
                (row.id, row.volume_name, row.worker_key)
                for row in rows
                if row.status != WorkspaceStatus.CLEANED.value
            ]
            if not targets:
                logger.debug(
                    "cleanup: every lane of run %s is already cleaned", pipeline_run_id
                )
                return
            await session.rollback()

            failures: list[str] = []
            for ws_id, volume_name, lane in targets:
                try:
                    async with self._locks.lock(
                        volume_name, LockType.EXCLUSIVE, timeout=60.0, reason="cleanup"
                    ):
                        workspace = await self._reload(session, ws_id)
                        if (
                            workspace is None
                            or workspace.status == WorkspaceStatus.CLEANED.value
                        ):
                            continue
                        await self._clean_row(session, workspace)
                except WorkspaceCleanupError as exc:
                    # Collected, not raised: the remaining lanes' volumes
                    # would otherwise leak behind the first failure.
                    failures.append(f"lane {lane!r} (volume {volume_name}): {exc}")

            if failures:
                raise WorkspaceCleanupError(
                    f"Cleanup failed for {len(failures)} of {len(targets)} lane(s) "
                    f"of run {pipeline_run_id}: " + "; ".join(failures)
                )

    @staticmethod
    async def _reload(db: AsyncSession, workspace_id: str) -> Workspace | None:
        """Re-read one row from the DATABASE, past the identity map."""
        result = await db.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def _clean_row(self, db: AsyncSession, workspace: Workspace) -> None:
        """Drive one row to CLEANED through valid machine transitions.

        Caller holds the exclusive lock for the row's volume and owns the
        session boundary (this commits). Handles every recoverable state:
        CREATING -> FAILED -> CLEANING, IN_USE -> (release*) -> READY ->
        CLEANING, READY/FAILED -> CLEANING, stuck CLEANING -> continue.
        """
        machine = _hydrate_machine(workspace)

        if machine.use_count > 0:
            # The lane is named: in an 8-way fan-out an unattributed leak
            # report tells you nothing about WHICH worker failed to release.
            logger.warning(
                "Workspace %s (run %s lane %s) cleaned with leaked "
                "use_count=%d; force-releasing",
                workspace.id,
                workspace.pipeline_run_id,
                workspace.worker_key,
                machine.use_count,
            )
            while machine.use_count > 0:
                machine.release()

        if machine.current_status == WorkspaceStatus.CREATING:
            machine.transition_to(WorkspaceStatus.FAILED)
            workspace.error = workspace.error or "Recovered from stranded CREATING state"

        if machine.current_status != WorkspaceStatus.CLEANING:
            machine.transition_to(WorkspaceStatus.CLEANING)
        _persist_machine(workspace, machine)
        await db.commit()  # crash-safe marker: CLEANING is persisted

        try:
            await run_in_threadpool(self._sync_remove_volume, workspace.volume_name)
        except Exception as exc:
            machine.transition_to(WorkspaceStatus.FAILED)
            workspace.error = f"Cleanup failed: {exc}"
            _persist_machine(workspace, machine)
            await db.commit()
            logger.error(
                "Workspace %s (lane %s) cleanup failed: %s",
                workspace.id,
                workspace.worker_key,
                exc,
            )
            raise WorkspaceCleanupError(
                f"Failed to remove volume {workspace.volume_name}: {exc}"
            ) from exc

        machine.transition_to(WorkspaceStatus.CLEANED)
        _persist_machine(workspace, machine)
        workspace.cleaned_at = datetime.utcnow()
        await db.commit()
        logger.info(
            "Workspace %s cleaned (lane %s, volume %s)",
            workspace.id,
            workspace.worker_key,
            workspace.volume_name,
        )

    # ------------------------------------------------------------------
    # Orphan audit
    # ------------------------------------------------------------------

    async def audit_orphans(
        self,
        db: AsyncSession,
        *,
        stuck_threshold: timedelta = timedelta(minutes=15),
        grace_period_minutes: int = 5,
        volume_prefix: str = WORKSPACE_VOLUME_PREFIX,
    ) -> list[str]:
        """Find and clean orphaned workspaces; returns cleaned volume names.

        Three sweeps (each capped at ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP
        removals; the next interval continues where a capped sweep stopped):
        1. Rows stuck in CREATING/CLEANING older than ``stuck_threshold``
           (a crashed create/cleanup) — force-cleaned via valid transitions
           — plus FAILED rows older than the same threshold (a failed
           create nobody retried or cleaned): volume removed if present,
           row marked CLEANED.
        2. READY/IN_USE rows whose pipeline run is finished or missing
           (per main's is_orphaned, with ``grace_period_minutes``), fetched
           with their run status in ONE outer-joined query.
        3. Docker volumes labeled ours under ``volume_prefix`` with no
           live (non-CLEANED, non-FAILED) row and older than
           ``stuck_threshold`` (rows are always committed before volumes
           are created, so an unmatched old volume is garbage; a FAILED
           row's volume is likewise removable — the failure path already
           tried to remove it).

        Lanes (M13-1). None of the three sweeps assumes one workspace per
        run and none needed structural change when that stopped being true.
        Sweep 1 is row-scoped; sweep 2's outer join is many-to-one and
        already yields one result row per workspace; sweep 3 compares
        against a set of live ROWS, not of runs. The safety property that
        makes sweep 3 sound is not "one volume per run", it is **the row is
        committed before the volume exists** (see get_or_create: the
        CREATING row is committed, and only then is the volume created).
        That invariant is per-row and holds for the Kth lane exactly as for
        the first, so more volumes per run does not raise the chance of
        reaping a live one.

        What lanes DO change is the arithmetic of the cap: a K=8 fan-out
        turns 25 removals into ~3 runs' worth instead of 25. That is a
        DEFERRAL, not a leak — the loop re-runs every interval and logs
        when it caps out — so the constant is deliberately left alone.

        Session ownership: COMMITS ``db``. Rows whose volume lock cannot be
        taken quickly are skipped (an active operation owns them).
        """
        # Import here: pipeline models are queried only by the audit, and
        # keeping this out of module import keeps coupling one-way.
        from app.models.pipeline import PipelineRun

        cleaned: list[str] = []
        now = datetime.utcnow()
        cap = ORPHAN_AUDIT_MAX_REMOVALS_PER_SWEEP

        # Sweep 1: stuck CREATING/CLEANING rows + stale FAILED rows.
        result = await db.execute(
            select(Workspace).where(
                Workspace.status.in_(
                    [
                        WorkspaceStatus.CREATING.value,
                        WorkspaceStatus.CLEANING.value,
                        WorkspaceStatus.FAILED.value,
                    ]
                ),
                Workspace.updated_at < now - stuck_threshold,
            )
        )
        removed = 0
        for workspace in result.scalars().all():
            if removed >= cap:
                logger.warning("Orphan audit sweep 1 hit removal cap (%d); deferring rest", cap)
                break
            why = (
                "failed and never retried or cleaned"
                if workspace.status == WorkspaceStatus.FAILED.value
                else "stuck in transitional state"
            )
            if await self._audit_clean(db, workspace, why):
                cleaned.append(workspace.volume_name)
                removed += 1

        # Sweep 2: READY/IN_USE rows whose pipeline run is done or gone
        # (one outer-joined query — no per-row run lookup).
        result = await db.execute(
            select(Workspace, PipelineRun.status)
            .outerjoin(PipelineRun, PipelineRun.id == Workspace.pipeline_run_id)
            .where(
                Workspace.status.in_(
                    [WorkspaceStatus.READY.value, WorkspaceStatus.IN_USE.value]
                )
            )
        )
        removed = 0
        for workspace, run_status in result.all():
            if removed >= cap:
                logger.warning("Orphan audit sweep 2 hit removal cap (%d); deferring rest", cap)
                break
            if is_orphaned(
                WorkspaceStatus(workspace.status),
                run_status,
                workspace.updated_at,
                grace_period_minutes=grace_period_minutes,
            ):
                if await self._audit_clean(db, workspace, "pipeline run finished or missing"):
                    cleaned.append(workspace.volume_name)
                    removed += 1

        # Sweep 3: our volumes with no live row. FAILED rows are NOT live —
        # their volumes are removable garbage (see docstring).
        result = await db.execute(
            select(Workspace.volume_name).where(
                Workspace.status.not_in(
                    [WorkspaceStatus.CLEANED.value, WorkspaceStatus.FAILED.value]
                )
            )
        )
        live_volumes = set(result.scalars().all())
        volumes = await run_in_threadpool(self._sync_list_workspace_volumes, volume_prefix)
        removed = 0
        for name, created_label in volumes:
            if name in live_volumes:
                continue
            if not self._volume_is_old(created_label, now, stuck_threshold):
                continue
            if removed >= cap:
                logger.warning("Orphan audit sweep 3 hit removal cap (%d); deferring rest", cap)
                break
            try:
                async with self._locks.lock(
                    name, LockType.EXCLUSIVE, timeout=1.0, reason="orphan-audit"
                ):
                    await run_in_threadpool(self._sync_remove_volume, name)
                run_id, lane_slug = parse_volume_name_parts(name)
                logger.warning(
                    "Orphan audit removed unmatched volume %s (run %s lane %s)",
                    name,
                    run_id,
                    lane_slug or DEFAULT_WORKER_KEY,
                )
                cleaned.append(name)
                removed += 1
            except LockTimeoutError:
                logger.debug("Orphan audit: volume %s busy, skipping", name)
            except Exception:
                logger.exception("Orphan audit: failed to remove volume %s", name)

        if cleaned:
            logger.info("Orphan audit cleaned %d workspace(s): %s", len(cleaned), cleaned)
        return cleaned

    async def _audit_clean(self, db: AsyncSession, workspace: Workspace, why: str) -> bool:
        """Force-clean one orphaned row; returns True if it reached CLEANED."""
        try:
            async with self._locks.lock(
                workspace.volume_name, LockType.EXCLUSIVE, timeout=1.0, reason="orphan-audit"
            ):
                await db.refresh(workspace)
                if workspace.status == WorkspaceStatus.CLEANED.value:
                    return False
                logger.warning(
                    "Orphan audit cleaning workspace %s (run %s, lane %s, "
                    "status=%s): %s",
                    workspace.id,
                    workspace.pipeline_run_id,
                    workspace.worker_key,
                    workspace.status,
                    why,
                )
                workspace.error = workspace.error or f"Orphan audit: {why}"
                await self._clean_row(db, workspace)
                return True
        except LockTimeoutError:
            logger.debug(
                "Orphan audit: workspace %s busy (lock held), skipping", workspace.id
            )
            return False
        except WorkspaceCleanupError:
            # _clean_row already logged and persisted FAILED; next sweep retries.
            return False

    @staticmethod
    def _volume_is_old(
        created_label: str | None, now: datetime, threshold: timedelta
    ) -> bool:
        """True if the volume's creation label is older than threshold.

        An unparseable/missing label counts as old: such a volume was not
        created by this service's current code path and has no row backing
        it, so keeping it forever would be the silent-leak failure mode.
        """
        if created_label:
            try:
                created = datetime.fromisoformat(created_label)
                return now - created > threshold
            except ValueError:
                pass
        return True


# Module singleton (interface contract #1).
workspace_service = WorkspaceService()


async def _orphan_audit_loop(
    service: WorkspaceService,
    interval_seconds: float,
    session_factory: Callable[[], AsyncSession] | None,
) -> None:
    """Run audit_orphans forever, one owned session per sweep."""
    if session_factory is None:
        from app.database import async_session as session_factory  # noqa: PLC0415

    while True:
        try:
            async with session_factory() as session:
                await service.audit_orphans(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Workspace orphan audit sweep failed")
        await asyncio.sleep(interval_seconds)


def start_orphan_audit(
    interval_seconds: float = 300.0,
    *,
    service: WorkspaceService | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> Callable[[], "asyncio.Task[None]"]:
    """Return an asyncio task factory for the periodic orphan audit.

    The integrator wires this into main.py's lifespan:

        _audit_task = start_orphan_audit(300)()   # on startup
        _audit_task.cancel()                      # on shutdown

    The first sweep runs immediately (startup crash recovery), then every
    ``interval_seconds``. The loop owns its DB sessions (one per sweep,
    from app.database.async_session unless ``session_factory`` is given)
    and never dies on sweep errors (logged, retried next interval).
    """
    svc = service or workspace_service

    def factory() -> "asyncio.Task[None]":
        return asyncio.create_task(
            _orphan_audit_loop(svc, interval_seconds, session_factory),
            name="workspace-orphan-audit",
        )

    return factory
