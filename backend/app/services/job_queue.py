"""
In-memory job queue for managing pending jobs.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class QueuedJob:
    id: str
    card_id: str
    repo_id: str
    repo_url: str
    base_branch: str
    card_title: str
    card_description: str
    runner_type: str = "any"  # Requested runner type (any, claude-code, gemini)
    repo_path: str | None = None  # Deprecated, not used with internal git
    use_internal_git: bool = False  # When True, runner clones from internal git server
    agent_file_ids: list[str] = field(default_factory=list)  # List of agent file IDs to mount
    prompt_template: str | None = None  # Custom prompt template (overrides default)
    # Step type and config (Phase 8.5)
    step_type: str = "agent"  # agent, script, docker
    step_config: dict | None = None  # Config for the step (command, image, etc.)
    # Pipeline context (Phase 9.1)
    continue_in_context: bool = False  # If true, runner preserves workspace for next step
    is_continuation: bool = False  # If true, runner skips cleanup at start (continues from previous step)
    previous_step_logs: str | None = None  # Logs from previous step (for agent context)
    pipeline_run_id: str | None = None  # Pipeline run ID for context tracking
    created_at: datetime = field(default_factory=datetime.utcnow)


class JobQueue:
    def __init__(self):
        self._jobs: list[QueuedJob] = []  # Ordered list of queued jobs
        self._pending: dict[str, QueuedJob] = {}  # job_id -> job (includes jobs being worked on)
        self._lock = asyncio.Lock()
        self._handlers: list[Callable[[QueuedJob], Awaitable[None]]] = []

    async def enqueue(self, job: QueuedJob) -> str:
        """Add a job to the queue."""
        async with self._lock:
            self._pending[job.id] = job
            self._jobs.append(job)
            logger.info(f"Enqueued job {job.id[:8]} (type={job.runner_type!r}) for card {job.card_id[:8]}")
        return job.id

    async def dequeue(self, runner_type: str | None = None) -> QueuedJob | None:
        """
        Get the next job from the queue that matches the runner type.

        Matching logic:
        - If runner_type is None, only return jobs with runner_type="any"
        - If runner_type is specified (e.g., "claude-code"), return jobs that:
          - Have runner_type="any" (any runner can take them), OR
          - Have runner_type matching the runner's type
        """
        async with self._lock:
            logger.debug(f"Dequeue called with runner_type={runner_type!r}, queue has {len(self._jobs)} jobs")
            for i, job in enumerate(self._jobs):
                matches = self._job_matches_runner(job, runner_type)
                logger.debug(f"  Job {job.id[:8]} (type={job.runner_type!r}) matches runner {runner_type!r}: {matches}")
                if matches:
                    self._jobs.pop(i)
                    logger.info(f"Dequeued job {job.id[:8]} (type={job.runner_type!r}) for runner type {runner_type!r}")
                    return job
            return None

    def _job_matches_runner(self, job: QueuedJob, runner_type: str | None) -> bool:
        """Check if a job can be picked up by a runner of the given type."""
        # Normalize to strings to ensure comparison works
        job_type = str(job.runner_type) if job.runner_type else "any"
        runner_type_str = str(runner_type) if runner_type else None

        # If job requests "any" runner, any runner can take it
        if job_type == "any":
            return True

        # If runner has no type (legacy), only take "any" jobs
        if runner_type_str is None:
            return job_type == "any"

        # Otherwise, job type must match runner type exactly
        return job_type == runner_type_str

    async def wait_for_job(self, runner_type: str | None = None, timeout: float = 30.0) -> QueuedJob | None:
        """Wait for a job that matches the runner type."""
        import time
        start = time.time()
        while time.time() - start < timeout:
            job = await self.dequeue(runner_type)
            if job:
                return job
            await asyncio.sleep(0.5)
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
        return len(self._jobs)


# Global job queue instance
job_queue = JobQueue()
