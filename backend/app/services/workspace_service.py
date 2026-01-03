"""
Workspace Service for Phase 12.2.

Manages the lifecycle of pipeline workspaces:
- Creation (with Docker volume)
- Acquisition (for step execution)
- Release (after step completion)
- Cleanup (after pipeline completion)
- Orphan detection

Uses WorkspaceStateMachine for state tracking and
WorkspaceLock for concurrency control.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceStatus, PipelineRun, Repo
from app.services.execution.workspace_state import (
    WorkspaceState,
    WorkspaceStateMachine,
)
from app.services.execution.workspace_locking import (
    LockType,
    acquire_workspace_lock,
    release_workspace_lock,
)

if TYPE_CHECKING:
    from app.services.execution.local_executor import LocalExecutor

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Base exception for workspace errors."""
    pass


class WorkspaceCreationError(WorkspaceError):
    """Raised when workspace creation fails."""
    pass


class WorkspaceAcquisitionError(WorkspaceError):
    """Raised when workspace acquisition fails."""
    pass


class WorkspaceService:
    """
    Service for managing pipeline workspaces.

    Coordinates between:
    - Workspace DB model (persistence)
    - WorkspaceStateMachine (state transitions)
    - WorkspaceLock (concurrency control)
    - Docker volumes (actual storage)
    """

    def __init__(self, docker_client=None):
        """
        Initialize workspace service.

        Args:
            docker_client: Optional Docker client (uses aiodocker if None)
        """
        self._docker = docker_client

    async def _get_docker(self):
        """Get or create Docker client."""
        if self._docker is None:
            import aiodocker
            self._docker = aiodocker.Docker()
        return self._docker

    async def close(self):
        """Close the Docker client if it was created."""
        if self._docker is not None:
            await self._docker.close()
            self._docker = None

    async def get_or_create_workspace(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        branch: str | None = None,
        commit_sha: str | None = None,
    ) -> Workspace:
        """
        Get existing workspace or create a new one for a pipeline run.

        This is idempotent - calling multiple times returns the same workspace.

        Args:
            db: Database session
            pipeline_run: The pipeline run needing a workspace
            repo: The repository to clone
            branch: Optional branch to checkout
            commit_sha: Optional commit to checkout

        Returns:
            Workspace model instance

        Raises:
            WorkspaceCreationError: If workspace creation fails
        """
        workspace_id = Workspace.make_workspace_id(pipeline_run.id)

        # Try to get existing workspace
        result = await db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = result.scalar_one_or_none()

        if workspace:
            logger.debug(f"Found existing workspace {workspace_id}")
            return workspace

        # Need to create - acquire exclusive lock
        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
            timeout=30.0,
        )

        try:
            if not lock.acquired:
                raise WorkspaceCreationError(
                    f"Could not acquire lock to create workspace {workspace_id}"
                )

            # Double-check after acquiring lock (another process may have created it)
            result = await db.execute(
                select(Workspace).where(Workspace.id == workspace_id)
            )
            workspace = result.scalar_one_or_none()
            if workspace:
                return workspace

            # Create workspace record
            workspace = Workspace(
                id=workspace_id,
                pipeline_run_id=pipeline_run.id,
                repo_id=repo.id,
                branch=branch,
                commit_sha=commit_sha,
                status=WorkspaceStatus.CREATING.value,
            )
            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)

            logger.info(f"Creating workspace {workspace_id} for pipeline run {pipeline_run.id[:8]}")

            # Create Docker volume
            try:
                await self._create_volume(workspace_id)

                # Update status to READY
                workspace.status = WorkspaceStatus.READY.value
                workspace.last_activity_at = datetime.utcnow()
                await db.commit()

                logger.info(f"Workspace {workspace_id} created and ready")

            except Exception as e:
                # Creation failed
                workspace.status = WorkspaceStatus.FAILED.value
                workspace.error = str(e)
                await db.commit()
                raise WorkspaceCreationError(f"Failed to create volume: {e}") from e

            return workspace

        finally:
            await release_workspace_lock(lock)

    async def _create_volume(self, volume_name: str) -> None:
        """
        Create a Docker volume for the workspace.

        Args:
            volume_name: Name for the volume
        """
        docker = await self._get_docker()

        # Check if volume already exists
        try:
            await docker.volumes.get(volume_name)
            logger.debug(f"Volume {volume_name} already exists")
            return
        except Exception:
            pass  # Volume doesn't exist, create it

        # Create volume with labels
        await docker.volumes.create({
            "Name": volume_name,
            "Labels": {
                "lazyaf.workspace": "true",
                "lazyaf.created_at": datetime.utcnow().isoformat(),
            },
        })
        logger.debug(f"Created Docker volume {volume_name}")

    async def acquire_workspace(
        self,
        db: AsyncSession,
        workspace: Workspace,
        step_info: str = "unknown",
    ) -> bool:
        """
        Acquire a workspace for step execution.

        Increments use_count and transitions to IN_USE if needed.

        Args:
            db: Database session
            workspace: Workspace to acquire
            step_info: Description of acquiring step (for logging)

        Returns:
            True if acquired, False if not available

        Raises:
            WorkspaceAcquisitionError: If workspace is in invalid state
        """
        # Get shared lock for execution
        lock = await acquire_workspace_lock(
            workspace.id,
            lock_type=LockType.SHARED,
            purpose=f"execute:{step_info}",
            timeout=10.0,
        )

        if not lock.acquired:
            logger.warning(f"Could not acquire lock for workspace {workspace.id}")
            return False

        try:
            # Check workspace status
            if workspace.status not in (
                WorkspaceStatus.READY.value,
                WorkspaceStatus.IN_USE.value,
            ):
                raise WorkspaceAcquisitionError(
                    f"Workspace {workspace.id} is in state {workspace.status}, cannot acquire"
                )

            # Increment use count and update status
            workspace.use_count += 1
            workspace.status = WorkspaceStatus.IN_USE.value
            workspace.last_activity_at = datetime.utcnow()
            await db.commit()

            logger.debug(f"Acquired workspace {workspace.id}, use_count={workspace.use_count}")
            return True

        except Exception:
            await release_workspace_lock(lock)
            raise

        # Note: We intentionally don't release the shared lock here
        # It's released in release_workspace()

    async def release_workspace(
        self,
        db: AsyncSession,
        workspace: Workspace,
        step_info: str = "unknown",
    ) -> None:
        """
        Release a workspace after step completion.

        Decrements use_count and transitions to READY if count reaches 0.

        Args:
            db: Database session
            workspace: Workspace to release
            step_info: Description of releasing step (for logging)
        """
        if workspace.use_count <= 0:
            logger.warning(f"Workspace {workspace.id} has use_count={workspace.use_count}, cannot release")
            return

        workspace.use_count -= 1
        workspace.last_activity_at = datetime.utcnow()

        if workspace.use_count == 0:
            workspace.status = WorkspaceStatus.READY.value

        await db.commit()
        logger.debug(f"Released workspace {workspace.id}, use_count={workspace.use_count}")

    async def cleanup_workspace(
        self,
        db: AsyncSession,
        workspace: Workspace,
        force: bool = False,
    ) -> bool:
        """
        Cleanup a workspace after pipeline completion.

        Removes the Docker volume and marks workspace as CLEANED.

        Args:
            db: Database session
            workspace: Workspace to clean up
            force: Force cleanup even if in use (for orphan recovery)

        Returns:
            True if cleaned up, False if not possible
        """
        # Can't cleanup if in use (unless forced)
        if workspace.use_count > 0 and not force:
            logger.warning(f"Cannot cleanup workspace {workspace.id}, use_count={workspace.use_count}")
            return False

        # Acquire exclusive lock for cleanup
        lock = await acquire_workspace_lock(
            workspace.id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=30.0 if not force else 5.0,
        )

        try:
            if not lock.acquired and not force:
                logger.warning(f"Could not acquire lock to cleanup workspace {workspace.id}")
                return False

            # Update status to CLEANING
            workspace.status = WorkspaceStatus.CLEANING.value
            await db.commit()

            logger.info(f"Cleaning up workspace {workspace.id}")

            # Remove Docker volume
            try:
                await self._remove_volume(workspace.volume_name)

                # Update status to CLEANED
                workspace.status = WorkspaceStatus.CLEANED.value
                workspace.cleaned_at = datetime.utcnow()
                await db.commit()

                logger.info(f"Workspace {workspace.id} cleaned up")
                return True

            except Exception as e:
                # Cleanup failed
                workspace.status = WorkspaceStatus.FAILED.value
                workspace.error = f"Cleanup failed: {e}"
                await db.commit()
                logger.error(f"Failed to cleanup workspace {workspace.id}: {e}")
                return False

        finally:
            await release_workspace_lock(lock)

    async def _remove_volume(self, volume_name: str) -> None:
        """
        Remove a Docker volume.

        Args:
            volume_name: Name of volume to remove
        """
        docker = await self._get_docker()

        try:
            volume = await docker.volumes.get(volume_name)
            await volume.delete()
            logger.debug(f"Removed Docker volume {volume_name}")
        except Exception as e:
            # Volume may not exist, that's okay
            logger.debug(f"Could not remove volume {volume_name}: {e}")

    async def find_orphaned_workspaces(
        self,
        db: AsyncSession,
        threshold: timedelta = timedelta(hours=2),
    ) -> list[Workspace]:
        """
        Find workspaces that appear to be orphaned.

        A workspace is orphaned if:
        - It's in READY state with no activity beyond threshold
        - Its pipeline run is complete or doesn't exist

        Args:
            db: Database session
            threshold: Time since last activity to consider orphaned

        Returns:
            List of orphaned workspaces
        """
        cutoff = datetime.utcnow() - threshold

        # Find workspaces that are READY or FAILED with old activity
        result = await db.execute(
            select(Workspace)
            .where(
                Workspace.status.in_([
                    WorkspaceStatus.READY.value,
                    WorkspaceStatus.FAILED.value,
                ])
            )
            .where(Workspace.last_activity_at < cutoff)
        )
        candidates = result.scalars().all()

        orphaned = []
        for workspace in candidates:
            # Check if pipeline run is complete
            if workspace.pipeline_run:
                if workspace.pipeline_run.status in ("passed", "failed", "cancelled"):
                    orphaned.append(workspace)
            else:
                # No pipeline run, definitely orphaned
                orphaned.append(workspace)

        logger.info(f"Found {len(orphaned)} orphaned workspaces")
        return orphaned

    async def cleanup_orphaned_workspaces(
        self,
        db: AsyncSession,
        threshold: timedelta = timedelta(hours=2),
    ) -> int:
        """
        Find and cleanup all orphaned workspaces.

        Args:
            db: Database session
            threshold: Time since last activity to consider orphaned

        Returns:
            Number of workspaces cleaned up
        """
        orphaned = await self.find_orphaned_workspaces(db, threshold)

        cleaned = 0
        for workspace in orphaned:
            if await self.cleanup_workspace(db, workspace, force=True):
                cleaned += 1

        logger.info(f"Cleaned up {cleaned} orphaned workspaces")
        return cleaned


# Global singleton
_workspace_service: WorkspaceService | None = None


def get_workspace_service() -> WorkspaceService:
    """Get or create the global workspace service."""
    global _workspace_service
    if _workspace_service is None:
        _workspace_service = WorkspaceService()
    return _workspace_service
