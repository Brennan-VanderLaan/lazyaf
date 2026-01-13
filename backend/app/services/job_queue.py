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
    model: str | None = None  # Specific model (claude-sonnet-4-20250514, claude-opus-4-20250514, gemini-2.5-pro, etc.)
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
    # Step metadata for context directory (Phase 9.1d)
    step_id: str | None = None  # Optional step ID from pipeline definition
    step_index: int = 0  # Step index in the pipeline
    step_name: str = "unnamed"  # Step name for logging
    created_at: datetime = field(default_factory=datetime.utcnow)
    # Runner affinity (for pipeline continuations)
    required_runner_id: str | None = None  # If set, only this runner can pick up the job
    # Playground fields (Phase 11)
    is_playground: bool = False  # True = ephemeral run, no card updates
    playground_session_id: str | None = None  # Links to SSE stream
    playground_save_branch: str | None = None  # If set, push changes to this branch


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

    async def dequeue(self, runner_type: str | None = None, runner_id: str | None = None) -> QueuedJob | None:
        """
        Get the next job from the queue that matches the runner type and affinity.

        Matching logic:
        - If job has required_runner_id, only that runner can pick it up
        - If runner_type is None, only return jobs with runner_type="any"
        - If runner_type is specified (e.g., "claude-code"), return jobs that:
          - Have runner_type="any" (any runner can take them), OR
          - Have runner_type matching the runner's type
        """
        async with self._lock:
            runner_short = runner_id[:8] if runner_id else None
            logger.info(f"Dequeue: runner_type={runner_type!r}, runner_id={runner_short}, queue_size={len(self._jobs)}")
            for i, job in enumerate(self._jobs):
                req_runner = job.required_runner_id[:8] if job.required_runner_id else None
                matches = self._job_matches_runner(job, runner_type, runner_id)
                logger.info(f"  Job {job.id[:8]}: type={job.runner_type!r}, required_runner={req_runner}, is_continuation={job.is_continuation}, matches={matches}")
                if matches:
                    self._jobs.pop(i)
                    logger.info(f"  -> Assigned to runner {runner_short}")
                    return job
            return None

    def _job_matches_runner(self, job: QueuedJob, runner_type: str | None, runner_id: str | None = None) -> bool:
        """Check if a job can be picked up by a runner of the given type and ID."""
        # Check runner affinity first (for pipeline continuations)
        if job.required_runner_id:
            if runner_id != job.required_runner_id:
                return False
            # If runner matches, still need to check type

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

    async def clear(self):
        """Clear all jobs from the queue. Used for testing cleanup."""
        async with self._lock:
            count = len(self._jobs)
            self._jobs.clear()
            self._pending.clear()
            logger.info(f"Cleared job queue ({count} jobs removed)")
            return count


# Global job queue instance
job_queue = JobQueue()
