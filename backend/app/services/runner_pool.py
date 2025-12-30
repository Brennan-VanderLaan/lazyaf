"""
Runner pool manager for external runner registration and job assignment.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.services.job_queue import JobQueue, QueuedJob, job_queue

logger = logging.getLogger(__name__)


class RunnerInfo:
    def __init__(self, id: str, name: str | None = None):
        self.id = id
        self.name = name or f"runner-{id[:8]}"
        self.status: str = "idle"  # idle, busy, offline
        self.current_job: QueuedJob | None = None
        self.last_heartbeat: datetime = datetime.utcnow()
        self.logs: list[str] = []
        self.registered_at: datetime = datetime.utcnow()

    def is_alive(self, timeout_seconds: int = 30) -> bool:
        return datetime.utcnow() - self.last_heartbeat < timedelta(seconds=timeout_seconds)


class RunnerPool:
    RUNNER_IMAGE = "lazyaf-runner:latest"
    HEARTBEAT_TIMEOUT = 60  # seconds - allow for some latency in heartbeat delivery

    def __init__(self):
        self._runners: dict[str, RunnerInfo] = {}
        self._running = False
        self._cleanup_task: asyncio.Task | None = None
        self._settings = get_settings()

    async def start(self):
        """Start the runner pool background tasks."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Runner pool started")

    async def stop(self):
        """Stop the runner pool background tasks."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Runner pool stopped")

    async def _cleanup_loop(self):
        """Periodically clean up dead runners."""
        while self._running:
            try:
                await asyncio.sleep(10)
                self._cleanup_dead_runners()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

    def _cleanup_dead_runners(self):
        """Mark runners as offline if they haven't sent a heartbeat."""
        now = datetime.utcnow()
        for runner in self._runners.values():
            if runner.status != "offline" and not runner.is_alive(self.HEARTBEAT_TIMEOUT):
                logger.warning(f"Runner {runner.id} timed out, marking offline")
                runner.status = "offline"
                # If it had a job, put it back in the queue
                if runner.current_job:
                    asyncio.create_task(job_queue.enqueue(runner.current_job))
                    runner.current_job = None

    def register(self, runner_id: str | None = None, name: str | None = None) -> RunnerInfo:
        """Register a runner. If runner_id is provided and exists, reactivate it."""
        # Use provided ID or generate a new one
        if runner_id is None:
            runner_id = str(uuid4())

        # Check if runner already exists (reconnection case)
        if runner_id in self._runners:
            runner = self._runners[runner_id]
            runner.last_heartbeat = datetime.utcnow()
            runner.status = "idle"
            # Update name if provided
            if name:
                runner.name = name
            logger.info(f"Runner {runner_id} ({runner.name}) reconnected")
            return runner

        # Create new runner
        runner = RunnerInfo(id=runner_id, name=name)
        self._runners[runner_id] = runner
        logger.info(f"Runner {runner_id} ({runner.name}) registered")
        return runner

    def unregister(self, runner_id: str) -> bool:
        """Unregister a runner."""
        if runner_id in self._runners:
            runner = self._runners.pop(runner_id)
            # If it had a job, put it back in the queue
            if runner.current_job:
                asyncio.create_task(job_queue.enqueue(runner.current_job))
            logger.info(f"Runner {runner_id} unregistered")
            return True
        return False

    def heartbeat(self, runner_id: str) -> bool:
        """Update runner heartbeat."""
        if runner_id in self._runners:
            runner = self._runners[runner_id]
            runner.last_heartbeat = datetime.utcnow()
            if runner.status == "offline":
                runner.status = "idle"
            return True
        return False

    async def get_job(self, runner_id: str) -> QueuedJob | None:
        """Get a job for a runner if one is available."""
        if runner_id not in self._runners:
            return None

        runner = self._runners[runner_id]
        if runner.status != "idle":
            return None

        job = await job_queue.dequeue()
        if job:
            runner.status = "busy"
            runner.current_job = job
            runner.logs = []  # Clear logs for new job
            logger.info(f"Assigned job {job.id} to runner {runner_id}")
        return job

    def complete_job(self, runner_id: str, success: bool, error: str | None = None) -> QueuedJob | None:
        """Mark a job as complete."""
        if runner_id not in self._runners:
            return None

        runner = self._runners[runner_id]
        job = runner.current_job
        if job:
            job_queue.remove_pending(job.id)
            runner.current_job = None
            runner.status = "idle"
            logger.info(f"Runner {runner_id} completed job {job.id} (success={success})")
        return job

    def append_log(self, runner_id: str, log_line: str):
        """Append a log line for a runner."""
        if runner_id in self._runners:
            runner = self._runners[runner_id]
            runner.logs.append(log_line)
            # Keep only last 1000 lines
            if len(runner.logs) > 1000:
                runner.logs = runner.logs[-1000:]

    def get_logs(self, runner_id: str) -> list[str]:
        """Get logs for a runner."""
        if runner_id in self._runners:
            return self._runners[runner_id].logs
        return []

    def get_runner(self, runner_id: str) -> RunnerInfo | None:
        """Get a specific runner."""
        return self._runners.get(runner_id)

    def get_runners(self) -> list[dict[str, Any]]:
        """Get status of all runners."""
        return [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "current_job_id": r.current_job.id if r.current_job else None,
                "last_heartbeat": r.last_heartbeat.isoformat(),
                "registered_at": r.registered_at.isoformat(),
                "log_count": len(r.logs),
            }
            for r in self._runners.values()
        ]

    @property
    def runner_count(self) -> int:
        return len(self._runners)

    @property
    def idle_count(self) -> int:
        return len([r for r in self._runners.values() if r.status == "idle"])

    @property
    def busy_count(self) -> int:
        return len([r for r in self._runners.values() if r.status == "busy"])

    @property
    def offline_count(self) -> int:
        return len([r for r in self._runners.values() if r.status == "offline"])


# Global runner pool instance
runner_pool = RunnerPool()
