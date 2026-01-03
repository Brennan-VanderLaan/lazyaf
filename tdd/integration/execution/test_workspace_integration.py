"""
Integration tests for Workspace lifecycle (Phase 12.2).

These tests verify the complete workspace lifecycle with real Docker volumes:
- Workspace creation with pipeline run
- Step execution using shared workspace
- Workspace cleanup after completion
- Orphan detection and cleanup
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import asyncio

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Skip all tests if Docker is not available
try:
    import aiodocker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason="aiodocker not available"),
    pytest.mark.integration,
]


class TestWorkspaceCreation:
    """Tests for workspace creation."""

    @pytest.mark.asyncio
    async def test_workspace_volume_created(self):
        """Workspace creation creates a Docker volume."""
        from app.services.workspace_service import WorkspaceService
        from app.models import Workspace, WorkspaceStatus

        service = WorkspaceService()

        # Create a mock pipeline run and repo
        class MockPipelineRun:
            id = "test-run-12345678"

        class MockRepo:
            id = "test-repo-1"

        # We'll test the volume creation directly
        workspace_id = Workspace.make_workspace_id(MockPipelineRun.id)
        assert workspace_id == "lazyaf-ws-test-run"

        # Create volume
        try:
            await service._create_volume(workspace_id)

            # Verify volume exists
            docker = await service._get_docker()
            volume = await docker.volumes.get(workspace_id)
            assert volume is not None

            # Get volume info
            info = await volume.show()
            assert info["Name"] == workspace_id
            assert info["Labels"].get("lazyaf.workspace") == "true"

        finally:
            # Cleanup
            try:
                await service._remove_volume(workspace_id)
            except Exception:
                pass
            await service.close()

    @pytest.mark.asyncio
    async def test_workspace_id_format(self):
        """Workspace IDs follow expected format."""
        from app.models import Workspace

        # Standard format
        ws_id = Workspace.make_workspace_id("abcd1234-5678-9012-3456-789012345678")
        assert ws_id == "lazyaf-ws-abcd1234"

        # Short ID
        ws_id = Workspace.make_workspace_id("short")
        assert ws_id == "lazyaf-ws-short"


class TestWorkspaceStateMachineIntegration:
    """Tests for workspace state machine with real operations."""

    @pytest.mark.asyncio
    async def test_workspace_lifecycle(self):
        """Workspace goes through complete lifecycle."""
        from app.services.execution.workspace_state import (
            WorkspaceState,
            WorkspaceStateMachine,
        )

        # Create state machine
        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-lifecycle-test",
            initial_state=WorkspaceState.CREATING,
        )

        # CREATING -> READY
        machine.transition(WorkspaceState.READY)
        assert machine.state == WorkspaceState.READY
        assert machine.use_count == 0

        # Acquire for first step
        machine.acquire()
        assert machine.state == WorkspaceState.IN_USE
        assert machine.use_count == 1

        # Acquire for concurrent second step
        machine.acquire()
        assert machine.state == WorkspaceState.IN_USE
        assert machine.use_count == 2

        # Release first step
        machine.release()
        assert machine.state == WorkspaceState.IN_USE
        assert machine.use_count == 1

        # Release second step
        machine.release()
        assert machine.state == WorkspaceState.READY
        assert machine.use_count == 0

        # Cleanup
        machine.transition(WorkspaceState.CLEANING)
        assert machine.state == WorkspaceState.CLEANING

        machine.transition(WorkspaceState.CLEANED)
        assert machine.state == WorkspaceState.CLEANED
        assert machine.is_terminal


class TestWorkspaceLockingIntegration:
    """Tests for workspace locking with concurrent access."""

    @pytest.mark.asyncio
    async def test_concurrent_shared_locks(self):
        """Multiple shared locks can be held concurrently."""
        from app.services.execution.workspace_locking import (
            LockType,
            acquire_workspace_lock,
            release_workspace_lock,
            reset_lock_manager,
        )

        # Reset lock manager for clean test
        reset_lock_manager()

        workspace_id = "lazyaf-ws-concurrent-test"

        # Acquire multiple shared locks
        locks = []
        for i in range(5):
            lock = await acquire_workspace_lock(
                workspace_id,
                lock_type=LockType.SHARED,
                purpose=f"step_{i}",
            )
            assert lock.acquired is True
            locks.append(lock)

        # All 5 locks held
        assert len(locks) == 5

        # Exclusive lock should fail
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert exclusive.acquired is False

        # Release all shared locks
        for lock in locks:
            await release_workspace_lock(lock)

        # Now exclusive should succeed
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
        )
        assert exclusive.acquired is True
        await release_workspace_lock(exclusive)

    @pytest.mark.asyncio
    async def test_exclusive_lock_blocks(self):
        """Exclusive lock blocks other locks."""
        from app.services.execution.workspace_locking import (
            LockType,
            acquire_workspace_lock,
            release_workspace_lock,
            reset_lock_manager,
        )

        reset_lock_manager()
        workspace_id = "lazyaf-ws-exclusive-test"

        # Acquire exclusive lock
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
        )
        assert exclusive.acquired is True

        # Shared lock should fail
        shared = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
            timeout=0.1,
        )
        assert shared.acquired is False

        # Another exclusive should fail
        exclusive2 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert exclusive2.acquired is False

        # Release first exclusive
        await release_workspace_lock(exclusive)

        # Now both should succeed
        shared = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
        )
        assert shared.acquired is True
        await release_workspace_lock(shared)


class TestMultiStepPipeline:
    """Tests for multi-step pipeline workspace sharing."""

    @pytest.mark.asyncio
    async def test_steps_share_workspace(self):
        """Multiple steps share the same workspace volume."""
        from app.services.execution.workspace_state import (
            WorkspaceState,
            WorkspaceStateMachine,
        )
        from app.services.workspace_service import WorkspaceService

        # Simulate multi-step execution
        workspace_id = "lazyaf-ws-multi-step-test"
        machine = WorkspaceStateMachine(
            workspace_id=workspace_id,
            initial_state=WorkspaceState.CREATING,
        )

        service = WorkspaceService()

        try:
            # Create volume
            await service._create_volume(workspace_id)
            machine.transition(WorkspaceState.READY)

            # Step 1 starts
            machine.acquire()
            assert machine.use_count == 1

            # Step 1 creates a file (simulated - just verify volume exists)
            docker = await service._get_docker()
            volume = await docker.volumes.get(workspace_id)
            assert volume is not None

            # Step 1 completes
            machine.release()
            assert machine.state == WorkspaceState.READY

            # Step 2 starts
            machine.acquire()
            assert machine.use_count == 1

            # Step 2 can see the same volume
            volume = await docker.volumes.get(workspace_id)
            assert volume is not None

            # Step 2 completes
            machine.release()
            assert machine.state == WorkspaceState.READY

            # Cleanup
            machine.transition(WorkspaceState.CLEANING)
            await service._remove_volume(workspace_id)
            machine.transition(WorkspaceState.CLEANED)

        finally:
            try:
                await service._remove_volume(workspace_id)
            except Exception:
                pass
            await service.close()


class TestOrphanDetection:
    """Tests for orphan workspace detection and cleanup."""

    @pytest.mark.asyncio
    async def test_orphan_detection_threshold(self):
        """Orphan detection respects time threshold."""
        from app.services.execution.workspace_state import (
            WorkspaceState,
            WorkspaceStateMachine,
        )

        machine = WorkspaceStateMachine(
            workspace_id="lazyaf-ws-orphan-test",
            initial_state=WorkspaceState.READY,
        )

        # Recent activity - not orphaned
        machine._last_activity = datetime.utcnow()
        assert machine.is_orphaned(threshold=timedelta(hours=1)) is False

        # Old activity - orphaned
        machine._last_activity = datetime.utcnow() - timedelta(hours=2)
        assert machine.is_orphaned(threshold=timedelta(hours=1)) is True

        # In use - never orphaned
        machine.acquire()
        machine._last_activity = datetime.utcnow() - timedelta(hours=24)
        assert machine.is_orphaned(threshold=timedelta(hours=1)) is False


class TestWorkspaceCleanup:
    """Tests for workspace cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_volume(self):
        """Cleanup removes the Docker volume."""
        from app.services.workspace_service import WorkspaceService

        service = WorkspaceService()
        workspace_id = "lazyaf-ws-cleanup-test"

        try:
            # Create volume
            await service._create_volume(workspace_id)

            # Verify it exists
            docker = await service._get_docker()
            volume = await docker.volumes.get(workspace_id)
            assert volume is not None

            # Remove it
            await service._remove_volume(workspace_id)

            # Verify it's gone
            with pytest.raises(Exception):
                await docker.volumes.get(workspace_id)
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self):
        """Cleanup is idempotent - can be called multiple times."""
        from app.services.workspace_service import WorkspaceService

        service = WorkspaceService()
        workspace_id = "lazyaf-ws-idempotent-test"

        try:
            # Create and remove
            await service._create_volume(workspace_id)
            await service._remove_volume(workspace_id)

            # Remove again - should not error
            await service._remove_volume(workspace_id)
        finally:
            await service.close()
