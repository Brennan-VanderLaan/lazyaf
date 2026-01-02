"""
Playground service for ephemeral agent testing.

Manages test runs without creating persistent cards/jobs.
Uses existing runner infrastructure with is_playground=True flag.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncGenerator
from uuid import uuid4

from app.services.job_queue import job_queue, QueuedJob

logger = logging.getLogger(__name__)


@dataclass
class PlaygroundSession:
    """Tracks an active playground test session."""

    id: str
    repo_id: str
    branch: str
    runner_type: str
    status: str = "queued"  # queued, running, completed, failed, cancelled
    logs: list[str] = field(default_factory=list)
    diff: str | None = None
    files_changed: list[str] = field(default_factory=list)
    branch_saved: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Internal tracking
    job_id: str | None = None
    runner_id: str | None = None  # For cancellation
    log_subscribers: list[asyncio.Queue] = field(default_factory=list)


class PlaygroundService:
    """
    Manages playground test runs using existing runner infrastructure.

    Key responsibilities:
    1. Create ephemeral jobs with is_playground=True
    2. Track active sessions in memory
    3. Stream logs via SSE
    4. Capture diffs on completion
    5. Cleanup expired sessions
    """

    SESSION_TTL_MINUTES = 30  # Sessions expire after 30 minutes

    def __init__(self):
        self._sessions: dict[str, PlaygroundSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start background cleanup task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Playground service started")

    async def stop(self):
        """Stop background tasks."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Playground service stopped")

    async def _cleanup_loop(self):
        """Periodically clean up expired sessions."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

    async def _cleanup_expired_sessions(self):
        """Remove sessions older than TTL."""
        now = datetime.utcnow()
        expired = []

        async with self._lock:
            for session_id, session in self._sessions.items():
                age = now - session.created_at
                if age > timedelta(minutes=self.SESSION_TTL_MINUTES):
                    expired.append(session_id)

            for session_id in expired:
                del self._sessions[session_id]
                logger.info(f"Cleaned up expired session {session_id[:8]}")

    async def start_test(
        self,
        repo_id: str,
        branch: str,
        runner_type: str,
        model: str | None = None,
        task_override: str | None = None,
        save_branch: str | None = None,
        prompt_template: str | None = None,
        agent_file_ids: list[str] | None = None,
    ) -> str:
        """
        Start a playground test.

        Returns session_id for SSE streaming.
        """
        session_id = str(uuid4())
        job_id = str(uuid4())

        # Create session
        session = PlaygroundSession(
            id=session_id,
            repo_id=repo_id,
            branch=branch,
            runner_type=runner_type,
            job_id=job_id,
        )

        async with self._lock:
            self._sessions[session_id] = session

        # Build task description
        task_description = task_override or "Test agent behavior on this branch"

        # Create ephemeral job
        queued_job = QueuedJob(
            id=job_id,
            card_id=f"playground-{session_id}",  # Fake card ID for tracking
            repo_id=repo_id,
            repo_url="",  # Not used for internal git
            base_branch=branch,
            card_title="Playground Test",
            card_description=task_description,
            runner_type=runner_type,
            model=model,
            use_internal_git=True,
            agent_file_ids=agent_file_ids or [],
            prompt_template=prompt_template,
            step_type="agent",
            # Playground-specific fields
            is_playground=True,
            playground_session_id=session_id,
            playground_save_branch=save_branch,
        )

        await job_queue.enqueue(queued_job)
        logger.info(
            f"Started playground session {session_id[:8]} with job {job_id[:8]}"
        )

        return session_id

    def get_session(self, session_id: str) -> PlaygroundSession | None:
        """Get a session by ID."""
        return self._sessions.get(session_id)

    async def append_log(self, session_id: str, log_line: str):
        """Append a log line and notify subscribers."""
        session = self._sessions.get(session_id)
        if not session:
            return

        session.logs.append(log_line)

        # Notify all subscribers
        event = {
            "type": "log",
            "data": log_line,
            "timestamp": datetime.utcnow().isoformat(),
        }
        for queue in session.log_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if queue is full

    async def append_logs(self, session_id: str, log_lines: list[str]):
        """Append multiple log lines."""
        for line in log_lines:
            await self.append_log(session_id, line)

    async def stream_logs(self, session_id: str) -> AsyncGenerator[dict, None]:
        """
        SSE generator yielding logs as they arrive.

        Yields dict with {type, data, timestamp}.
        """
        session = self._sessions.get(session_id)
        if not session:
            yield {
                "type": "error",
                "data": "Session not found",
                "timestamp": datetime.utcnow().isoformat(),
            }
            return

        # Create queue for this subscriber
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        session.log_subscribers.append(queue)

        try:
            # First, send all existing logs as a batch
            if session.logs:
                yield {
                    "type": "logs_batch",
                    "data": session.logs.copy(),
                    "timestamp": datetime.utcnow().isoformat(),
                }

            # Then stream new logs
            while session.status in ("queued", "running"):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {
                        "type": "ping",
                        "data": "",
                        "timestamp": datetime.utcnow().isoformat(),
                    }

            # Session completed, send final status
            yield {
                "type": "complete",
                "data": session.status,
                "timestamp": datetime.utcnow().isoformat(),
            }

        finally:
            # Remove subscriber
            if queue in session.log_subscribers:
                session.log_subscribers.remove(queue)

    async def update_status(
        self, session_id: str, status: str, error: str | None = None
    ):
        """Update session status."""
        session = self._sessions.get(session_id)
        if not session:
            return

        session.status = status
        if status == "running" and not session.started_at:
            session.started_at = datetime.utcnow()
        if status in ("completed", "failed", "cancelled"):
            session.completed_at = datetime.utcnow()
        if error:
            session.error = error

        # Notify subscribers
        event = {
            "type": "status",
            "data": status,
            "timestamp": datetime.utcnow().isoformat(),
        }
        for queue in session.log_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        logger.info(f"Playground session {session_id[:8]} status: {status}")

    async def set_runner(self, session_id: str, runner_id: str):
        """Set the runner ID for a session (for cancellation)."""
        session = self._sessions.get(session_id)
        if session:
            session.runner_id = runner_id

    async def set_result(
        self,
        session_id: str,
        diff: str | None = None,
        files_changed: list[str] | None = None,
        branch_saved: str | None = None,
    ):
        """Set the result of a completed test."""
        session = self._sessions.get(session_id)
        if not session:
            return

        session.diff = diff
        session.files_changed = files_changed or []
        session.branch_saved = branch_saved

    async def cancel_test(self, session_id: str) -> bool:
        """Cancel a running test."""
        session = self._sessions.get(session_id)
        if not session:
            return False

        if session.status not in ("queued", "running"):
            return False

        session.status = "cancelled"
        session.completed_at = datetime.utcnow()
        session.error = "Cancelled by user"

        # Notify subscribers
        event = {
            "type": "status",
            "data": "cancelled",
            "timestamp": datetime.utcnow().isoformat(),
        }
        for queue in session.log_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        logger.info(f"Cancelled playground session {session_id[:8]}")
        return True

    def get_result(self, session_id: str) -> dict | None:
        """Get the result of a completed test."""
        session = self._sessions.get(session_id)
        if not session:
            return None

        duration = None
        if session.started_at and session.completed_at:
            duration = (session.completed_at - session.started_at).total_seconds()

        return {
            "session_id": session.id,
            "status": session.status,
            "diff": session.diff,
            "files_changed": session.files_changed,
            "branch_saved": session.branch_saved,
            "error": session.error,
            "logs": "\n".join(session.logs),
            "duration_seconds": duration,
        }


# Global instance
playground_service = PlaygroundService()
