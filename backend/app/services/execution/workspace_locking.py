"""
Workspace Locking for Phase 12.2.

Implements shared/exclusive locking for workspace access:
- Exclusive lock for creation: Only one process can create a workspace
- Exclusive lock for cleanup: Only one process can cleanup a workspace
- Shared lock for execution: Multiple steps can execute concurrently

Uses in-memory locking for development, PostgreSQL advisory locks for production.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Set
from contextlib import asynccontextmanager


class LockType(Enum):
    """Type of workspace lock."""
    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class LockAcquisitionError(Exception):
    """Raised when lock acquisition fails."""
    pass


@dataclass
class WorkspaceLock:
    """Represents an acquired workspace lock."""
    workspace_id: str
    lock_type: LockType
    purpose: str
    acquired: bool
    acquired_at: Optional[datetime] = None
    timeout_seconds: Optional[float] = None

    # Internal tracking
    _lock_id: str = field(default="", init=False, compare=False)

    def __post_init__(self):
        if self.acquired and self.acquired_at is None:
            self.acquired_at = datetime.utcnow()
        self._lock_id = f"{self.workspace_id}:{self.lock_type.value}:{id(self)}"

    def __hash__(self) -> int:
        """Make WorkspaceLock hashable for use in sets."""
        return hash(self._lock_id)

    def __eq__(self, other: object) -> bool:
        """Equality based on lock_id."""
        if not isinstance(other, WorkspaceLock):
            return False
        return self._lock_id == other._lock_id

    async def __aenter__(self) -> WorkspaceLock:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await release_workspace_lock(self)
        return False


# Global lock manager (in-memory implementation)
class LockManager:
    """
    In-memory lock manager for workspace locking.

    Production would use PostgreSQL advisory locks or Redis.
    """

    def __init__(self):
        self._exclusive_locks: Dict[str, WorkspaceLock] = {}
        self._shared_locks: Dict[str, Set[WorkspaceLock]] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        workspace_id: str,
        lock_type: LockType,
        purpose: str,
        timeout: float = 5.0,
    ) -> WorkspaceLock:
        """
        Attempt to acquire a workspace lock.

        Args:
            workspace_id: ID of workspace to lock
            lock_type: EXCLUSIVE or SHARED
            purpose: Human-readable purpose (for debugging)
            timeout: Maximum seconds to wait (0 = don't wait)

        Returns:
            WorkspaceLock with acquired=True if successful, False otherwise
        """
        start_time = datetime.utcnow()
        poll_interval = 0.05  # 50ms

        while True:
            async with self._lock:
                can_acquire = await self._can_acquire_unsafe(workspace_id, lock_type)

                if can_acquire:
                    lock = WorkspaceLock(
                        workspace_id=workspace_id,
                        lock_type=lock_type,
                        purpose=purpose,
                        acquired=True,
                        acquired_at=datetime.utcnow(),
                        timeout_seconds=timeout,
                    )

                    if lock_type == LockType.EXCLUSIVE:
                        self._exclusive_locks[workspace_id] = lock
                    else:
                        if workspace_id not in self._shared_locks:
                            self._shared_locks[workspace_id] = set()
                        self._shared_locks[workspace_id].add(lock)

                    return lock

            # Check timeout
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed >= timeout:
                return WorkspaceLock(
                    workspace_id=workspace_id,
                    lock_type=lock_type,
                    purpose=purpose,
                    acquired=False,
                    timeout_seconds=timeout,
                )

            # Wait before retrying
            remaining = timeout - elapsed
            await asyncio.sleep(min(poll_interval, remaining))

    async def _can_acquire_unsafe(
        self,
        workspace_id: str,
        lock_type: LockType,
    ) -> bool:
        """
        Check if lock can be acquired (must hold self._lock).

        Exclusive locks block everything.
        Shared locks block exclusive but allow other shared.
        """
        has_exclusive = workspace_id in self._exclusive_locks
        has_shared = bool(self._shared_locks.get(workspace_id))

        if lock_type == LockType.EXCLUSIVE:
            # Exclusive requires no existing locks
            return not has_exclusive and not has_shared
        else:
            # Shared requires no exclusive lock
            return not has_exclusive

    async def release(self, lock: WorkspaceLock) -> None:
        """Release a previously acquired lock."""
        if not lock.acquired:
            return

        async with self._lock:
            workspace_id = lock.workspace_id

            if lock.lock_type == LockType.EXCLUSIVE:
                if workspace_id in self._exclusive_locks:
                    del self._exclusive_locks[workspace_id]
            else:
                if workspace_id in self._shared_locks:
                    self._shared_locks[workspace_id].discard(lock)
                    if not self._shared_locks[workspace_id]:
                        del self._shared_locks[workspace_id]

            lock.acquired = False


# Global singleton
_lock_manager: Optional[LockManager] = None


def get_lock_manager() -> LockManager:
    """Get or create the global lock manager."""
    global _lock_manager
    if _lock_manager is None:
        _lock_manager = LockManager()
    return _lock_manager


def reset_lock_manager() -> None:
    """Reset the lock manager (for testing)."""
    global _lock_manager
    _lock_manager = None


async def acquire_workspace_lock(
    workspace_id: str,
    lock_type: LockType,
    purpose: str,
    timeout: float = 5.0,
) -> WorkspaceLock:
    """
    Acquire a workspace lock.

    Args:
        workspace_id: ID of workspace to lock
        lock_type: EXCLUSIVE or SHARED
        purpose: Human-readable purpose
        timeout: Maximum seconds to wait (default 5s)

    Returns:
        WorkspaceLock (check .acquired to see if successful)
    """
    manager = get_lock_manager()
    return await manager.acquire(workspace_id, lock_type, purpose, timeout)


async def release_workspace_lock(lock: WorkspaceLock) -> None:
    """Release a workspace lock."""
    manager = get_lock_manager()
    await manager.release(lock)


@asynccontextmanager
async def workspace_lock(
    workspace_id: str,
    lock_type: LockType,
    purpose: str,
    timeout: float = 5.0,
):
    """
    Context manager for workspace locking.

    Usage:
        async with workspace_lock(ws_id, LockType.SHARED, "execute") as lock:
            if lock.acquired:
                # do work
            else:
                # handle lock failure
    """
    lock = await acquire_workspace_lock(workspace_id, lock_type, purpose, timeout)
    try:
        yield lock
    finally:
        await release_workspace_lock(lock)
