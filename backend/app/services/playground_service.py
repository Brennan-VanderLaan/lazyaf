"""
Playground service for ephemeral agent testing.

Phase 12.5: the playground no longer enqueues a job for a polling runner. It
starts an AD-HOC AGENT RUN (``app.services.agent_run``) - an ephemeral hidden
Pipeline row plus a real PipelineRun with one agent step - so a playground
test gets the workspace volume, StepRun/StepExecution rows, control mode,
streamed logs, and a StepUsage row for free, on exactly the same path a
pipeline agent step takes. The polling queue this once fed is gone
entirely as of 12.6 (``tdd/unit/services/test_no_legacy_code.py``).

Two consequences worth stating, because both replaced a race:

1. LOGS. The runner used to POST log lines to
   ``/api/playground/{id}/internal/log``. Now the step's log lines arrive at
   ``POST /api/steps/{id}/logs`` like every other control-mode step, and the
   WS manager - already the single place every log frame passes through (R3)
   - fans them to a per-run local observer this service registers
   (cross-agent contract #8). The SSE stream is unchanged from the client's
   point of view.

2. DIFFS. The runner used to compute the diff inside its own workspace and
   POST it back. The workspace volume is now deleted the moment the run
   completes, so reading it afterwards is a race the platform loses at
   random. Instead the agent pushes to ``playground/<session_id[:8]>`` on the
   internal git server and the diff is computed SERVER-SIDE from that branch
   (``agent_run._finish_playground_success``), the same call
   ``GET /api/cards/{id}/diff`` uses.

Two rules the ad-hoc run imposes on this service, both of them about a run
whose life does not match the session's:

3. NEVER WRITE "running" BLIND. ``start_pipeline`` can complete a run
   SYNCHRONOUSLY (image preflight failure), and ``on_run_complete`` then
   lands the session terminal from inside the start call. ``start_test``
   re-reads the run before it reports progress, because a "running" written
   over a terminal status leaves ``stream_logs`` looping (it only exits on a
   terminal status), ``get_result`` answering "running" forever, and a log
   observer attached to the process-wide WS manager until the TTL sweep.

4. A CANCEL THAT CANNOT CANCEL IS AN ERROR. ``cancel_test`` raises
   ``PlaygroundCancelError`` rather than swallowing a failed run cancel: the
   container is what spends money, and a session reported "cancelled" is a
   session nobody watches.

The ``/internal/*`` endpoints stay routed for the legacy runner path (R2:
``executor: legacy`` must remain callable until the 12.6 deletion commit).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncGenerator
from uuid import uuid4

logger = logging.getLogger(__name__)


class PlaygroundCancelError(RuntimeError):
    """A cancel that could not stop the agent run behind a session.

    Surfaced, never swallowed: the container is what costs money, and a
    silent failure here answers "cancelled" while the agent keeps working
    with nobody watching. The router maps this to a 5xx naming the run.
    """


def playground_branch(session_id: str) -> str:
    """The branch an ad-hoc playground run pushes to.

    ALWAYS ``playground/<session_id[:8]>``, even when the user asked to save
    to a named branch: the diff has to be computable from a branch that
    certainly exists, and renaming afterwards is one ref write
    (``agent_run._dispose_playground_branch``). A run that pushed straight to
    the user's chosen name would leave that name behind on failure.
    """
    return f"playground/{session_id[:8]}"


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

    # 12.5 ad-hoc run tracking
    pipeline_run_id: str | None = None
    work_branch: str | None = None  # branch the agent pushes to
    save_branch: str | None = None  # user-requested keeper branch, if any
    agent: str | None = None  # resolved agent vocabulary value
    _log_observer: object | None = None  # registered on the WS manager


class PlaygroundService:
    """
    Manages playground test runs as ad-hoc agent runs (12.5).

    Key responsibilities:
    1. Start an ad-hoc single-agent-step PipelineRun per session
    2. Track active sessions in memory
    3. Stream logs via SSE, fed from the WS manager's per-run log observers
    4. Report the server-side diff on completion
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
                session = self._sessions.pop(session_id)
                self._detach_observer(session)
                logger.info(f"Cleaned up expired session {session_id[:8]}")

    async def reset(self):
        """Drop all sessions. Test-mode reset hook: live sessions are marked
        cancelled and subscribers notified so open SSE streams terminate
        instead of pinging forever against a forgotten session object."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            # A forgotten session's log observer would otherwise keep a dead
            # object attached to the WS manager for the life of the process.
            self._detach_observer(session)
            if session.status in ("queued", "running"):
                session.status = "cancelled"
                session.completed_at = datetime.utcnow()
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

        if sessions:
            logger.info(f"Playground service reset ({len(sessions)} sessions dropped)")

    # -------------------------------------------------------------------------
    # Log observers (cross-agent contract #8)
    # -------------------------------------------------------------------------

    def _make_log_observer(self, session_id: str):
        """Callback the WS manager invokes with every log batch of a run.

        Signature is the contract's: ``(step_index, lines)``; the manager
        awaits it and swallows/logs any exception, so a dead session can
        never break the step behind it.
        """

        async def _observe(step_index: int, lines: list[str]) -> None:
            await self.append_logs(session_id, list(lines))

        return _observe

    def attach_run(self, session_id: str, pipeline_run_id: str) -> None:
        """Bind a session to its ad-hoc run and start observing its logs."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        from app.services.websocket import manager

        session.pipeline_run_id = pipeline_run_id
        observer = self._make_log_observer(session_id)
        session._log_observer = observer
        manager.register_run_log_observer(pipeline_run_id, observer)

    def detach_run(self, session_id: str) -> None:
        """Stop observing a session's run (completion / cancellation)."""
        session = self._sessions.get(session_id)
        if session is not None:
            self._detach_observer(session)

    def _detach_observer(self, session: PlaygroundSession) -> None:
        if not session.pipeline_run_id or session._log_observer is None:
            return
        from app.services.websocket import manager

        manager.unregister_run_log_observer(
            session.pipeline_run_id, session._log_observer
        )
        session._log_observer = None

    # -------------------------------------------------------------------------
    # Start
    # -------------------------------------------------------------------------

    async def start_test(
        self,
        db,
        repo,
        branch: str,
        runner_type: str,
        model: str | None = None,
        task_override: str | None = None,
        save_branch: str | None = None,
        prompt_template: str | None = None,
        agent_file_ids: list[str] | None = None,
        mock_config: dict | None = None,
    ) -> str:
        """
        Start a playground test as an ad-hoc agent run.

        Returns session_id for SSE streaming.
        """
        from app.services import agent_run

        session_id = str(uuid4())
        work_branch = playground_branch(session_id)
        agent = agent_run.resolve_agent(runner_type)

        session = PlaygroundSession(
            id=session_id,
            repo_id=repo.id,
            branch=branch,
            runner_type=runner_type,
            work_branch=work_branch,
            save_branch=save_branch,
            agent=agent,
        )

        async with self._lock:
            self._sessions[session_id] = session

        task_description = task_override or "Test agent behavior on this branch"

        try:
            pipeline_run = await agent_run.start_adhoc_agent_run(
                db,
                repo,
                trigger_type=agent_run.TRIGGER_PLAYGROUND,
                trigger_ref=session_id,
                base_branch=branch,
                work_branch=work_branch,
                agent=agent,
                model=model,
                prompt_template=prompt_template,
                task={
                    "title": "Playground Test",
                    "description": task_description,
                },
                agent_file_ids=agent_file_ids,
                mock_config=mock_config,
                commit_enabled=True,
                push_branch=True,
                step_name="Playground agent",
            )
        except Exception as e:
            # A dispatch failure has to reach the SSE stream: a session stuck
            # in "queued" forever is the exact silence R1 forbids.
            logger.exception("Playground session %s failed to start", session_id[:8])
            await self.update_status(session_id, "failed", f"failed to start: {e}")
            return session_id

        # Observe first, THEN check: attaching after the terminal check would
        # leave an observer on the process-wide WS manager for a run nobody
        # will ever detach from.
        self.attach_run(session_id, pipeline_run.id)

        # start_pipeline can complete a run SYNCHRONOUSLY - an image
        # preflight failure is the common one - and on_run_complete has then
        # ALREADY landed this session in its terminal state, from inside the
        # call above. Writing "running" over that would resurrect a dead
        # session: stream_logs only leaves its loop on a terminal status, so
        # the SSE stream would ping until the 30-minute TTL, get_result would
        # keep answering "running", and the observer would leak for that long.
        if not await agent_run.run_is_live(db, pipeline_run.id):
            self.detach_run(session_id)
            session = self._sessions.get(session_id)
            logger.warning(
                "Playground session %s: ad-hoc run %s was already terminal on "
                "return from start_pipeline (session is %s)",
                session_id[:8],
                pipeline_run.id[:8],
                session.status if session else "gone",
            )
            return session_id

        session = self._sessions.get(session_id)
        if session is not None and session.status not in ("queued", "running"):
            # Belt and braces: the run is live but something already ended
            # the session (a reset, an expiry sweep). Do not un-end it.
            self.detach_run(session_id)
            return session_id

        await self.update_status(session_id, "running")
        logger.info(
            f"Started playground session {session_id[:8]} as ad-hoc run "
            f"{pipeline_run.id[:8]} (agent={agent}, branch={work_branch})"
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

    async def cancel_test(self, session_id: str, db=None) -> bool:
        """Cancel a running test.

        Cancelling the SESSION also cancels the RUN behind it (12.5): the
        container is killed and the workspace volume reclaimed. Leaving an
        agent container running against an abandoned session would burn real
        API budget with nobody watching - which is the whole reason this
        method exists.

        A cancel that could not stop the container is a FAILED cancel. It
        used to be swallowed, which meant the endpoint answered 200
        "cancelled" while the agent kept running: the session went quiet, so
        nobody watched the thing that was still spending money. Now the
        session is restored to ``running`` and the caller gets an error, so a
        retry is possible and the truth is visible.

        Raises:
            PlaygroundCancelError: the run behind this session could not be
                cancelled; the session is left as it was.
        """
        session = self._sessions.get(session_id)
        if not session:
            return False

        if session.status not in ("queued", "running"):
            return False

        # Mark the session cancelled BEFORE killing the run, and put it back
        # if the kill fails. Marking first closes the window where a
        # straggler step task completes the run and _complete_playground -
        # which only no-ops on a TERMINAL session - reports a diff for work
        # the user stopped. Restoring on failure is what keeps the session
        # live and retryable when the container could not be killed.
        previous = (session.status, session.completed_at, session.error)
        session.status = "cancelled"
        session.completed_at = datetime.utcnow()
        session.error = "Cancelled by user"
        try:
            await self._cancel_run(session, db)
        except PlaygroundCancelError:
            session.status, session.completed_at, session.error = previous
            raise

        self._detach_observer(session)

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

    async def _cancel_run(self, session: PlaygroundSession, db=None) -> None:
        """Cancel the ad-hoc run behind a session.

        Takes the CALLER's session when it has one (the cancel endpoint
        does): a service that reaches past its caller for a second session on
        the global engine is how a request ends up reading a different
        database than the request that created the row.

        Raises PlaygroundCancelError when the run exists, is live, and could
        not be cancelled. A session with no run, or one whose run is already
        terminal, is not a failure - there is nothing left to kill.
        """
        if not session.pipeline_run_id:
            return
        try:
            if db is None:
                from app.database import async_session

                async with async_session() as owned:
                    await self._cancel_run_with(owned, session)
                return
            await self._cancel_run_with(db, session)
        except Exception as e:
            logger.exception(
                "Could not cancel ad-hoc run %s for playground session %s - "
                "the agent container may STILL BE RUNNING",
                (session.pipeline_run_id or "?")[:8],
                session.id[:8],
            )
            raise PlaygroundCancelError(
                f"could not cancel the agent run behind session "
                f"{session.id[:8]} ({e}); the container may still be running"
            ) from e

    async def _cancel_run_with(self, db, session: PlaygroundSession) -> None:
        """Load the run the way ``cancel_run`` needs it, then cancel it.

        ``pipeline_executor.cancel_run`` walks ``pipeline_run.step_runs`` to
        find the containers to kill. That relationship is lazy, so loading
        the run with a bare ``db.get`` makes the very first thing cancel_run
        does raise MissingGreenlet under asyncio - it killed nothing, and the
        failure used to be swallowed one frame up. The pipelines router
        eager-loads for exactly this reason; so does this.
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.models import PipelineRun, RunStatus
        from app.services.pipeline_executor import pipeline_executor

        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == session.pipeline_run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        run = result.scalar_one_or_none()
        if run is None:
            logger.info(
                "Playground session %s: run %s is gone - nothing to cancel",
                session.id[:8],
                (session.pipeline_run_id or "?")[:8],
            )
            return
        if run.status not in (RunStatus.PENDING.value, RunStatus.RUNNING.value):
            logger.info(
                "Playground session %s: run %s is already %s - nothing to "
                "cancel",
                session.id[:8],
                run.id[:8],
                run.status,
            )
            return
        await pipeline_executor.cancel_run(db, run)

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
