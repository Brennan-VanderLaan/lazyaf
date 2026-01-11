"""
Unit tests for Workspace Locking.

These tests define the contract for workspace locking semantics:
- Exclusive locks for create and cleanup operations
- Shared locks for step execution (multiple steps can run)
- Lock timeout handling
- Lock release on completion

Write these tests BEFORE implementing workspace locking.
"""
import sys
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

# Tests enabled - workspace locking implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Lock Types
# -----------------------------------------------------------------------------

class TestLockTypes:
    """Tests that verify lock types exist and work correctly."""

    def test_exclusive_lock_type_exists(self):
        """EXCLUSIVE lock type exists for create/cleanup."""
        from app.services.workspace.locking import LockType
        assert LockType.EXCLUSIVE.value == "exclusive"

    def test_shared_lock_type_exists(self):
        """SHARED lock type exists for step execution."""
        from app.services.workspace.locking import LockType
        assert LockType.SHARED.value == "shared"


# -----------------------------------------------------------------------------
# Contract: Exclusive Locks
# -----------------------------------------------------------------------------

class TestExclusiveLocks:
    """Tests that verify exclusive lock behavior."""

    async def test_exclusive_lock_for_create(self):
        """Only one creator can hold exclusive lock."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # First acquire succeeds
        lock1 = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert lock1 is not None
        assert lock1.lock_type == LockType.EXCLUSIVE

        # Second acquire should fail (timeout)
        lock2 = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=0.1)
        assert lock2 is None

        # Release first lock
        await manager.release(lock1)

        # Now can acquire again
        lock3 = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert lock3 is not None

    async def test_exclusive_lock_for_cleanup(self):
        """Cleanup requires exclusive lock (no concurrent cleanups)."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Acquire for cleanup
        lock = await manager.acquire(
            workspace_id,
            LockType.EXCLUSIVE,
            timeout=1.0,
            reason="cleanup"
        )
        assert lock is not None
        assert lock.reason == "cleanup"

    async def test_exclusive_blocks_shared(self):
        """Exclusive lock blocks shared lock acquisition."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Take exclusive lock
        exclusive = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert exclusive is not None

        # Shared lock should fail
        shared = await manager.acquire(workspace_id, LockType.SHARED, timeout=0.1)
        assert shared is None

        await manager.release(exclusive)

    async def test_shared_blocks_exclusive(self):
        """Shared locks block exclusive lock acquisition."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Take shared lock
        shared = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        assert shared is not None

        # Exclusive lock should fail
        exclusive = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=0.1)
        assert exclusive is None

        await manager.release(shared)


# -----------------------------------------------------------------------------
# Contract: Shared Locks
# -----------------------------------------------------------------------------

class TestSharedLocks:
    """Tests that verify shared lock behavior for concurrent execution."""

    async def test_multiple_shared_locks_allowed(self):
        """Multiple shared locks can be held simultaneously."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Multiple shared locks should succeed
        lock1 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        lock2 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        lock3 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)

        assert lock1 is not None
        assert lock2 is not None
        assert lock3 is not None

        # All different lock objects
        assert lock1.id != lock2.id != lock3.id

        # All can be released
        await manager.release(lock1)
        await manager.release(lock2)
        await manager.release(lock3)

    async def test_shared_lock_for_step_execution(self):
        """Step execution uses shared lock."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Acquire for step execution
        lock = await manager.acquire(
            workspace_id,
            LockType.SHARED,
            timeout=1.0,
            reason="step:test-step-1"
        )
        assert lock is not None
        assert lock.lock_type == LockType.SHARED
        assert "step" in lock.reason

    async def test_shared_lock_count_tracked(self):
        """Manager tracks how many shared locks are held."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        assert manager.get_lock_count(workspace_id) == 0

        lock1 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        assert manager.get_lock_count(workspace_id) == 1

        lock2 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        assert manager.get_lock_count(workspace_id) == 2

        await manager.release(lock1)
        assert manager.get_lock_count(workspace_id) == 1

        await manager.release(lock2)
        assert manager.get_lock_count(workspace_id) == 0


# -----------------------------------------------------------------------------
# Contract: Lock Timeout
# -----------------------------------------------------------------------------

class TestLockTimeout:
    """Tests that verify lock timeout behavior."""

    async def test_timeout_returns_none(self):
        """Lock acquisition timeout returns None instead of blocking forever."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Hold exclusive lock
        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=10.0)

        # Try to acquire with short timeout
        start = asyncio.get_event_loop().time()
        result = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=0.1)
        elapsed = asyncio.get_event_loop().time() - start

        assert result is None
        assert elapsed < 0.5  # Should return quickly after timeout

        await manager.release(lock)

    async def test_zero_timeout_immediate_failure(self):
        """Zero timeout fails immediately if lock unavailable."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=10.0)

        start = asyncio.get_event_loop().time()
        result = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=0)
        elapsed = asyncio.get_event_loop().time() - start

        assert result is None
        assert elapsed < 0.05  # Should be nearly instant

        await manager.release(lock)

    async def test_lock_acquired_before_timeout(self):
        """Lock can be acquired if released before timeout."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=10.0)

        async def release_after_delay():
            await asyncio.sleep(0.1)
            await manager.release(lock)

        # Start release in background
        asyncio.create_task(release_after_delay())

        # Try to acquire with longer timeout
        result = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert result is not None


# -----------------------------------------------------------------------------
# Contract: Lock Context Manager
# -----------------------------------------------------------------------------

class TestLockContextManager:
    """Tests that verify lock context manager for automatic release."""

    async def test_context_manager_acquires_and_releases(self):
        """Context manager automatically acquires and releases lock."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        async with manager.lock(workspace_id, LockType.EXCLUSIVE, timeout=1.0) as lock:
            assert lock is not None
            assert manager.get_lock_count(workspace_id) == 1

        # Lock should be released after context
        assert manager.get_lock_count(workspace_id) == 0

    async def test_context_manager_releases_on_exception(self):
        """Context manager releases lock even if exception occurs."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        with pytest.raises(RuntimeError):
            async with manager.lock(workspace_id, LockType.EXCLUSIVE, timeout=1.0) as lock:
                assert lock is not None
                raise RuntimeError("Test exception")

        # Lock should still be released
        assert manager.get_lock_count(workspace_id) == 0

    async def test_context_manager_timeout_raises(self):
        """Context manager raises if lock acquisition times out."""
        from app.services.workspace.locking import (
            WorkspaceLockManager, LockType, LockTimeoutError
        )

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        # Hold lock
        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=10.0)

        # Context manager should raise on timeout
        with pytest.raises(LockTimeoutError):
            async with manager.lock(workspace_id, LockType.EXCLUSIVE, timeout=0.1):
                pass

        await manager.release(lock)


# -----------------------------------------------------------------------------
# Contract: Lock Info
# -----------------------------------------------------------------------------

class TestLockInfo:
    """Tests that verify lock metadata tracking."""

    async def test_lock_has_id(self):
        """Each lock has a unique ID."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert lock.id is not None
        assert len(lock.id) > 0

        await manager.release(lock)

    async def test_lock_tracks_workspace_id(self):
        """Lock tracks which workspace it belongs to."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert lock.workspace_id == workspace_id

        await manager.release(lock)

    async def test_lock_tracks_acquired_at(self):
        """Lock tracks when it was acquired."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType
        from datetime import datetime

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        before = datetime.utcnow()
        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        after = datetime.utcnow()

        assert before <= lock.acquired_at <= after

        await manager.release(lock)

    async def test_get_active_locks(self):
        """Can list all active locks for a workspace."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        lock1 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        lock2 = await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)

        active = manager.get_active_locks(workspace_id)
        assert len(active) == 2
        assert lock1.id in [l.id for l in active]
        assert lock2.id in [l.id for l in active]

        await manager.release(lock1)
        await manager.release(lock2)


# -----------------------------------------------------------------------------
# Contract: Force Release
# -----------------------------------------------------------------------------

class TestForceRelease:
    """Tests that verify force release for stuck locks."""

    async def test_force_release_clears_all_locks(self):
        """force_release() clears all locks for a workspace."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)
        await manager.acquire(workspace_id, LockType.SHARED, timeout=1.0)

        assert manager.get_lock_count(workspace_id) == 2

        # Force release all
        released_count = await manager.force_release(workspace_id)
        assert released_count == 2
        assert manager.get_lock_count(workspace_id) == 0

    async def test_force_release_allows_new_locks(self):
        """After force_release(), new locks can be acquired."""
        from app.services.workspace.locking import WorkspaceLockManager, LockType

        manager = WorkspaceLockManager()
        workspace_id = f"lazyaf-ws-{uuid4()}"

        await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        await manager.force_release(workspace_id)

        # Should be able to acquire now
        lock = await manager.acquire(workspace_id, LockType.EXCLUSIVE, timeout=1.0)
        assert lock is not None

        await manager.release(lock)
