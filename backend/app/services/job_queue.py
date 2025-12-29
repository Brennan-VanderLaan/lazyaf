"""
In-memory job queue for managing pending jobs.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable
from uuid import uuid4


@dataclass
class QueuedJob:
    id: str
    card_id: str
    repo_id: str
    repo_url: str
    base_branch: str
    card_title: str
    card_description: str
    repo_path: str | None = None  # Deprecated, not used with internal git
    use_internal_git: bool = False  # When True, runner clones from internal git server
    created_at: datetime = field(default_factory=datetime.utcnow)


class JobQueue:
    def __init__(self):
        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue()
        self._pending: dict[str, QueuedJob] = {}  # job_id -> job
        self._handlers: list[Callable[[QueuedJob], Awaitable[None]]] = []

    async def enqueue(self, job: QueuedJob) -> str:
        """Add a job to the queue."""
        self._pending[job.id] = job
        await self._queue.put(job)
        return job.id

    async def dequeue(self) -> QueuedJob | None:
        """Get the next job from the queue (non-blocking)."""
        try:
            job = self._queue.get_nowait()
            return job
        except asyncio.QueueEmpty:
            return None

    async def wait_for_job(self, timeout: float = 30.0) -> QueuedJob | None:
        """Wait for a job from the queue."""
        try:
            job = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return job
        except asyncio.TimeoutError:
            return None

    def remove_pending(self, job_id: str):
        """Remove a job from pending tracking."""
        self._pending.pop(job_id, None)

    def get_pending(self, job_id: str) -> QueuedJob | None:
        """Get a pending job by ID."""
        return self._pending.get(job_id)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()


# Global job queue instance
job_queue = JobQueue()
