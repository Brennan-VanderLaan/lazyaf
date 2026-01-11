"""
Workspace Locking - Phase 12.2

Provides locking semantics for workspace operations:
- EXCLUSIVE locks for create/cleanup (only one holder)
- SHARED locks for step execution (multiple holders allowed)
- Lock timeout handling
- Context manager for automatic release
"""
import asyncio
from enum import Enum
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from uuid import uuid4
from contextlib import asynccontextmanager


class LockType(str, Enum):
    """Types of workspace locks."""
    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class LockTimeoutError(Exception):
    """Raised when lock acquisition times out."""
    pass


@dataclass(eq=False)
class Lock:
    """Represents a held lock on a workspace."""
    id: str
    workspace_id: str
    lock_type: LockType
    acquired_at: datetime
    reason: str = ""

    @classmethod
    def create(
        cls,
        workspace_id: str,
        lock_type: LockType,
        reason: str = ""
    ) -> "Lock":
        return cls(
            id=str(uuid4()),
            workspace_id=workspace_id,
            lock_type=lock_type,
            acquired_at=datetime.utcnow(),
            reason=reason,
        )

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if not isinstance(other, Lock):
            return False
        return self.id == other.id


class WorkspaceLockManager:
    """
    Manages locks for workspace operations.

    Supports:
    - Exclusive locks (one holder, blocks all others)
    - Shared locks (multiple holders, blocked by exclusive)
    - Lock timeout for non-blocking acquisition
    """

    def __init__(self):
        # workspace_id -> set of Lock objects
        self._locks: Dict[str, Set[Lock]] = {}
        # workspace_id -> asyncio.Lock for synchronization
        self._mutexes: Dict[str, asyncio.Lock] = {}
        # workspace_id -> asyncio.Event for signaling lock release
        self._release_events: Dict[str, asyncio.Event] = {}

    def _get_mutex(self, workspace_id: str) -> asyncio.Lock:
        """Get or create mutex for workspace."""
        if workspace_id not in self._mutexes:
            self._mutexes[workspace_id] = asyncio.Lock()
        return self._mutexes[workspace_id]

    def _get_release_event(self, workspace_id: str) -> asyncio.Event:
        """Get or create release event for workspace."""
        if workspace_id not in self._release_events:
            self._release_events[workspace_id] = asyncio.Event()
            self._release_events[workspace_id].set()  # Initially unlocked
        return self._release_events[workspace_id]

    def _has_exclusive_lock(self, workspace_id: str) -> bool:
        """Check if workspace has an exclusive lock."""
        locks = self._locks.get(workspace_id, set())
        return any(lock.lock_type == LockType.EXCLUSIVE for lock in locks)

    def _has_any_locks(self, workspace_id: str) -> bool:
        """Check if workspace has any locks."""
        return bool(self._locks.get(workspace_id))

    async def acquire(
        self,
        workspace_id: str,
        lock_type: LockType,
        timeout: float = 0,
        reason: str = "",
    ) -> Optional[Lock]:
        """
        Acquire a lock on a workspace.

        Args:
            workspace_id: ID of workspace to lock
            lock_type: EXCLUSIVE or SHARED
            timeout: Seconds to wait (0 = immediate fail if unavailable)
            reason: Optional reason for the lock

        Returns:
            Lock object if acquired, None if timeout
        """
        mutex = self._get_mutex(workspace_id)
        release_event = self._get_release_event(workspace_id)

        start_time = asyncio.get_event_loop().time()
        remaining = timeout

        while True:
            async with mutex:
                can_acquire = False

                if lock_type == LockType.EXCLUSIVE:
                    # Exclusive needs no other locks
                    can_acquire = not self._has_any_locks(workspace_id)
                else:
                    # Shared can coexist with other shared, but not exclusive
                    can_acquire = not self._has_exclusive_lock(workspace_id)

                if can_acquire:
                    lock = Lock.create(workspace_id, lock_type, reason)
                    if workspace_id not in self._locks:
                        self._locks[workspace_id] = set()
                    self._locks[workspace_id].add(lock)
                    release_event.clear()  # Mark as locked
                    return lock

            # Check if we should keep waiting
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = timeout - elapsed

            if remaining <= 0:
                return None

            # Wait for a release signal or timeout
            try:
                await asyncio.wait_for(
                    release_event.wait(),
                    timeout=min(remaining, 0.1)  # Check every 100ms
                )
            except asyncio.TimeoutError:
                pass

    async def release(self, lock: Lock) -> None:
        """
        Release a held lock.

        Args:
            lock: The lock to release
        """
        mutex = self._get_mutex(lock.workspace_id)
        release_event = self._get_release_event(lock.workspace_id)

        async with mutex:
            locks = self._locks.get(lock.workspace_id, set())
            locks.discard(lock)

            if not locks:
                self._locks.pop(lock.workspace_id, None)
                release_event.set()  # Signal that locks are released

    def get_lock_count(self, workspace_id: str) -> int:
        """Get number of active locks on a workspace."""
        return len(self._locks.get(workspace_id, set()))

    def get_active_locks(self, workspace_id: str) -> List[Lock]:
        """Get list of active locks on a workspace."""
        return list(self._locks.get(workspace_id, set()))

    async def force_release(self, workspace_id: str) -> int:
        """
        Force release all locks on a workspace.

        Returns:
            Number of locks released
        """
        mutex = self._get_mutex(workspace_id)
        release_event = self._get_release_event(workspace_id)

        async with mutex:
            locks = self._locks.pop(workspace_id, set())
            count = len(locks)
            release_event.set()
            return count

    @asynccontextmanager
    async def lock(
        self,
        workspace_id: str,
        lock_type: LockType,
        timeout: float = 0,
        reason: str = "",
    ):
        """
        Context manager for automatic lock acquire/release.

        Raises:
            LockTimeoutError: If lock cannot be acquired within timeout

        Usage:
            async with manager.lock(ws_id, LockType.SHARED, timeout=5.0) as lock:
                # do work with workspace
        """
        lock = await self.acquire(workspace_id, lock_type, timeout, reason)
        if lock is None:
            raise LockTimeoutError(
                f"Failed to acquire {lock_type.value} lock on {workspace_id} "
                f"within {timeout}s"
            )

        try:
            yield lock
        finally:
            await self.release(lock)
