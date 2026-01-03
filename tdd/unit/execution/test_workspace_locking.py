"""
Tests for Workspace Locking (Phase 12.2).

These tests DEFINE the workspace locking contract.
Write tests first, then implement to make them pass.

Locking Semantics:
- Exclusive lock for creation: Only one process can create a workspace
- Exclusive lock for cleanup: Only one process can cleanup a workspace
- Shared lock for execution: Multiple steps can execute concurrently
- Lock timeout: Don't block forever, return False on timeout

Implementation uses PostgreSQL advisory locks (SQLite file locking in dev).
"""
import sys
import asyncio
from pathlib import Path
from datetime import datetime

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.workspace_locking import (
        WorkspaceLock,
        LockType,
        LockAcquisitionError,
        acquire_workspace_lock,
        release_workspace_lock,
    )
    LOCKING_MODULE_AVAILABLE = True
except ImportError:
    LOCKING_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    from enum import Enum
    LockType = Enum("LockType", ["EXCLUSIVE", "SHARED"])
    WorkspaceLock = None
    LockAcquisitionError = Exception
    acquire_workspace_lock = None
    release_workspace_lock = None


pytestmark = pytest.mark.skipif(
    not LOCKING_MODULE_AVAILABLE,
    reason="workspace locking module not yet implemented"
)


class TestLockTypes:
    """Tests for lock type definitions."""

    def test_has_exclusive_lock_type(self):
        """LockType has EXCLUSIVE value."""
        assert LockType.EXCLUSIVE is not None
        assert LockType.EXCLUSIVE.value == "exclusive"

    def test_has_shared_lock_type(self):
        """LockType has SHARED value."""
        assert LockType.SHARED is not None
        assert LockType.SHARED.value == "shared"


class TestExclusiveLockForCreate:
    """Tests for exclusive lock during workspace creation."""

    @pytest.mark.asyncio
    async def test_exclusive_lock_for_create(self):
        """Only one process can hold exclusive lock for creation."""
        workspace_id = "lazyaf-ws-test-123"

        # First lock succeeds
        lock1 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
        )
        assert lock1 is not None
        assert lock1.acquired is True

        # Second exclusive lock fails (with short timeout)
        lock2 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
            timeout=0.1,
        )
        assert lock2.acquired is False

        # Release first lock
        await release_workspace_lock(lock1)

        # Now second attempt succeeds
        lock3 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
        )
        assert lock3.acquired is True
        await release_workspace_lock(lock3)

    @pytest.mark.asyncio
    async def test_exclusive_lock_blocks_shared(self):
        """Exclusive lock blocks shared lock acquisition."""
        workspace_id = "lazyaf-ws-test-456"

        # Acquire exclusive lock
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="create",
        )
        assert exclusive.acquired is True

        # Shared lock fails
        shared = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
            timeout=0.1,
        )
        assert shared.acquired is False

        await release_workspace_lock(exclusive)


class TestExclusiveLockForCleanup:
    """Tests for exclusive lock during workspace cleanup."""

    @pytest.mark.asyncio
    async def test_exclusive_lock_for_cleanup(self):
        """Only one process can hold exclusive lock for cleanup."""
        workspace_id = "lazyaf-ws-test-789"

        # First cleanup lock succeeds
        lock1 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
        )
        assert lock1 is not None
        assert lock1.acquired is True
        assert lock1.purpose == "cleanup"

        # Second cleanup lock fails
        lock2 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert lock2.acquired is False

        await release_workspace_lock(lock1)

    @pytest.mark.asyncio
    async def test_cleanup_waits_for_execution(self):
        """Cleanup must wait for all executions to complete."""
        workspace_id = "lazyaf-ws-test-abc"

        # Acquire shared lock for execution
        shared = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
        )
        assert shared.acquired is True

        # Exclusive lock for cleanup fails (execution in progress)
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert exclusive.acquired is False

        # Release execution lock
        await release_workspace_lock(shared)

        # Now cleanup can proceed
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
        )
        assert exclusive.acquired is True
        await release_workspace_lock(exclusive)


class TestSharedLockForExecution:
    """Tests for shared lock during step execution."""

    @pytest.mark.asyncio
    async def test_shared_lock_for_execution(self):
        """Multiple steps can run with shared locks."""
        workspace_id = "lazyaf-ws-test-def"

        # First shared lock succeeds
        lock1 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute_step_1",
        )
        assert lock1.acquired is True

        # Second shared lock also succeeds (concurrent execution)
        lock2 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute_step_2",
        )
        assert lock2.acquired is True

        # Third shared lock also succeeds
        lock3 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute_step_3",
        )
        assert lock3.acquired is True

        # Cleanup all
        await release_workspace_lock(lock1)
        await release_workspace_lock(lock2)
        await release_workspace_lock(lock3)

    @pytest.mark.asyncio
    async def test_shared_lock_blocks_exclusive(self):
        """Shared locks block exclusive lock acquisition."""
        workspace_id = "lazyaf-ws-test-ghi"

        # Acquire shared locks
        shared1 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
        )
        shared2 = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute",
        )

        # Exclusive lock fails while any shared locks held
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert exclusive.acquired is False

        # Release one shared lock - exclusive still fails
        await release_workspace_lock(shared1)
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
            timeout=0.1,
        )
        assert exclusive.acquired is False

        # Release all shared locks - now exclusive succeeds
        await release_workspace_lock(shared2)
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="cleanup",
        )
        assert exclusive.acquired is True
        await release_workspace_lock(exclusive)


class TestLockTimeout:
    """Tests for lock acquisition timeout."""

    @pytest.mark.asyncio
    async def test_lock_timeout_returns_false(self):
        """Lock acquisition returns False on timeout, doesn't raise."""
        workspace_id = "lazyaf-ws-test-timeout"

        # Acquire exclusive lock
        blocker = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="blocking",
        )
        assert blocker.acquired is True

        # Try to acquire with very short timeout
        start = datetime.utcnow()
        result = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="waiting",
            timeout=0.2,  # 200ms timeout
        )
        elapsed = (datetime.utcnow() - start).total_seconds()

        # Should return False, not raise exception
        assert result.acquired is False
        # Should have waited approximately the timeout duration
        assert 0.1 <= elapsed <= 0.5

        await release_workspace_lock(blocker)

    @pytest.mark.asyncio
    async def test_zero_timeout_returns_immediately(self):
        """Zero timeout returns immediately without waiting."""
        workspace_id = "lazyaf-ws-test-zero-timeout"

        # Acquire exclusive lock
        blocker = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="blocking",
        )

        # Try with zero timeout
        start = datetime.utcnow()
        result = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="try",
            timeout=0,
        )
        elapsed = (datetime.utcnow() - start).total_seconds()

        assert result.acquired is False
        assert elapsed < 0.1  # Should be nearly instant

        await release_workspace_lock(blocker)

    @pytest.mark.asyncio
    async def test_default_timeout_is_reasonable(self):
        """Default timeout should be a reasonable value (not infinite)."""
        # This test just verifies the default is set
        workspace_id = "lazyaf-ws-test-default"

        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="test",
            # No timeout specified - uses default
        )

        # Should succeed (no blocking)
        assert lock.acquired is True
        # Lock should have a default timeout recorded
        assert lock.timeout_seconds is not None
        assert lock.timeout_seconds > 0
        assert lock.timeout_seconds <= 30  # Reasonable upper bound

        await release_workspace_lock(lock)


class TestLockMetadata:
    """Tests for lock metadata and tracking."""

    @pytest.mark.asyncio
    async def test_lock_has_workspace_id(self):
        """Lock object includes workspace ID."""
        workspace_id = "lazyaf-ws-test-meta-1"
        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="test",
        )
        assert lock.workspace_id == workspace_id
        await release_workspace_lock(lock)

    @pytest.mark.asyncio
    async def test_lock_has_lock_type(self):
        """Lock object includes lock type."""
        workspace_id = "lazyaf-ws-test-meta-2"
        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="test",
        )
        assert lock.lock_type == LockType.EXCLUSIVE
        await release_workspace_lock(lock)

    @pytest.mark.asyncio
    async def test_lock_has_purpose(self):
        """Lock object includes purpose."""
        workspace_id = "lazyaf-ws-test-meta-3"
        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="execute_step_5",
        )
        assert lock.purpose == "execute_step_5"
        await release_workspace_lock(lock)

    @pytest.mark.asyncio
    async def test_lock_has_acquired_at_timestamp(self):
        """Lock object includes acquisition timestamp."""
        workspace_id = "lazyaf-ws-test-meta-4"
        before = datetime.utcnow()
        lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="test",
        )
        after = datetime.utcnow()

        assert lock.acquired_at is not None
        assert before <= lock.acquired_at <= after
        await release_workspace_lock(lock)


class TestContextManager:
    """Tests for async context manager interface."""

    @pytest.mark.asyncio
    async def test_lock_as_context_manager(self):
        """Lock can be used as async context manager."""
        from app.services.execution.workspace_locking import workspace_lock

        workspace_id = "lazyaf-ws-test-ctx"

        async with workspace_lock(
            workspace_id,
            lock_type=LockType.SHARED,
            purpose="context_test",
        ) as lock:
            assert lock.acquired is True
            # Lock is held here

        # After exiting context, lock is released
        # New exclusive lock should succeed
        exclusive = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="after_context",
            timeout=0.1,
        )
        assert exclusive.acquired is True
        await release_workspace_lock(exclusive)

    @pytest.mark.asyncio
    async def test_context_manager_releases_on_exception(self):
        """Context manager releases lock even on exception."""
        from app.services.execution.workspace_locking import workspace_lock

        workspace_id = "lazyaf-ws-test-ctx-exc"

        try:
            async with workspace_lock(
                workspace_id,
                lock_type=LockType.EXCLUSIVE,
                purpose="will_fail",
            ) as lock:
                assert lock.acquired is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Lock should be released despite exception
        new_lock = await acquire_workspace_lock(
            workspace_id,
            lock_type=LockType.EXCLUSIVE,
            purpose="after_exception",
            timeout=0.1,
        )
        assert new_lock.acquired is True
        await release_workspace_lock(new_lock)


class TestDifferentWorkspaces:
    """Tests for lock isolation between workspaces."""

    @pytest.mark.asyncio
    async def test_different_workspaces_independent(self):
        """Locks on different workspaces don't interfere."""
        workspace_a = "lazyaf-ws-workspace-a"
        workspace_b = "lazyaf-ws-workspace-b"

        # Lock workspace A exclusively
        lock_a = await acquire_workspace_lock(
            workspace_a,
            lock_type=LockType.EXCLUSIVE,
            purpose="test_a",
        )
        assert lock_a.acquired is True

        # Lock workspace B exclusively - should succeed
        lock_b = await acquire_workspace_lock(
            workspace_b,
            lock_type=LockType.EXCLUSIVE,
            purpose="test_b",
        )
        assert lock_b.acquired is True

        await release_workspace_lock(lock_a)
        await release_workspace_lock(lock_b)
