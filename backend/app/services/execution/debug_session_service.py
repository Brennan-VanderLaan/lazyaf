"""
Debug Session Service - Core business logic for debug re-run mode.

This service manages:
- Creating debug re-runs from failed pipelines
- Tracking session state transitions
- Managing breakpoints and pausing execution
- Handling CLI connections
- Session timeouts
"""

import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DebugSession, DebugSessionStatus, PipelineRun, Pipeline, RunStatus
from app.services.execution.debug_state import DebugState, DebugStateMachine
from app.services.websocket import manager


# Singleton instance
_debug_session_service: Optional["DebugSessionService"] = None


def get_debug_session_service() -> "DebugSessionService":
    """Get the singleton DebugSessionService instance."""
    global _debug_session_service
    if _debug_session_service is None:
        _debug_session_service = DebugSessionService()
    return _debug_session_service


class DebugSessionService:
    """
    Service for managing debug sessions.

    Responsibilities:
    - Create debug re-runs from failed pipelines
    - Track session state transitions
    - Generate auth tokens
    - Manage breakpoints
    - Handle session timeouts
    """

    def __init__(self):
        """Initialize the debug session service."""
        # Track active sessions for quick lookup
        self._active_sessions: dict[str, DebugStateMachine] = {}

    async def create_debug_rerun(
        self,
        db: AsyncSession,
        pipeline_run_id: str,
        breakpoints: list[int],
        use_original_commit: bool = True,
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> tuple[DebugSession, PipelineRun]:
        """
        Create a new debug re-run session from a failed/cancelled pipeline.

        Args:
            db: Database session
            pipeline_run_id: ID of the original (failed) pipeline run
            breakpoints: List of step indices to pause before
            use_original_commit: Use same commit as original run
            commit_sha: Specific commit (if use_original_commit=False)
            branch: Branch name (if use_original_commit=False)

        Returns:
            Tuple of (DebugSession, new PipelineRun)

        Raises:
            ValueError: If original run not found or not failed/cancelled
        """
        # Get original pipeline run
        original_run = await db.get(PipelineRun, pipeline_run_id)
        if not original_run:
            raise ValueError("Pipeline run not found")

        # Check if original run is failed or cancelled
        if original_run.status not in {RunStatus.FAILED.value, RunStatus.CANCELLED.value, "failed", "cancelled"}:
            raise ValueError("Can only debug re-run failed or cancelled pipelines")

        # Get the pipeline
        pipeline = await db.get(Pipeline, original_run.pipeline_id)
        if not pipeline:
            raise ValueError("Pipeline not found")

        # Determine commit info
        trigger_context = json.loads(original_run.trigger_context or "{}")
        if use_original_commit:
            run_commit_sha = trigger_context.get("sha", original_run.trigger_ref)
            run_branch = trigger_context.get("branch")
        else:
            run_commit_sha = commit_sha
            run_branch = branch

        # Create new pipeline run for debug
        new_run = PipelineRun(
            pipeline_id=original_run.pipeline_id,
            status=RunStatus.PENDING.value,
            trigger_type="debug",
            trigger_ref=run_commit_sha or "",
            trigger_context=json.dumps({
                "branch": run_branch,
                "sha": run_commit_sha,
                "debug_rerun_of": pipeline_run_id,
            }),
            current_step=0,
            steps_completed=0,
            steps_total=original_run.steps_total,
        )
        db.add(new_run)
        await db.flush()  # Get the ID

        # Create debug session
        session = DebugSession(
            pipeline_run_id=new_run.id,
            original_run_id=pipeline_run_id,
            status=DebugSessionStatus.PENDING.value,
            breakpoints=json.dumps(breakpoints),
            token=secrets.token_urlsafe(32),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        await db.refresh(new_run)

        # Initialize state machine
        self._active_sessions[session.id] = DebugStateMachine()

        return session, new_run

    async def get_session(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> Optional[DebugSession]:
        """Get a debug session by ID."""
        return await db.get(DebugSession, session_id)

    async def get_session_info(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> Optional[dict]:
        """
        Get full session info for UI display.

        Returns None if session not found.
        """
        session = await self.get_session(db, session_id)
        if not session:
            return None

        # Check if expired
        if session.expires_at and datetime.utcnow() > session.expires_at:
            await self._transition_to_timeout(db, session)
            raise ValueError("Session expired")

        # Get pipeline run
        run = await db.get(PipelineRun, session.pipeline_run_id)
        if not run:
            return None

        # Get pipeline for step info
        pipeline = await db.get(Pipeline, run.pipeline_id)
        steps = json.loads(pipeline.steps) if pipeline else []

        # Build current step info
        current_step = None
        if session.current_step_index is not None:
            step = steps[session.current_step_index] if session.current_step_index < len(steps) else None
            if step:
                current_step = {
                    "name": session.current_step_name or step.get("name", f"Step {session.current_step_index + 1}"),
                    "index": session.current_step_index,
                    "type": step.get("type", "unknown"),
                }

        # Build commit info
        trigger_context = json.loads(run.trigger_context or "{}")
        commit = {
            "sha": trigger_context.get("sha", run.trigger_ref or "unknown"),
            "message": trigger_context.get("message", ""),
        }

        # Build runtime info (placeholder - will be filled by executor)
        runtime = {
            "host": "localhost",
            "orchestrator": "docker",
            "image": "lazyaf-base:latest",
            "image_sha": None,
        }

        # Get logs
        logs = await self._get_logs_up_to_breakpoint(db, session)

        # Build join command
        join_command = f"lazyaf debug {session.id} --token {session.token}"

        return {
            "id": session.id,
            "status": session.status,
            "current_step": current_step,
            "commit": commit,
            "runtime": runtime,
            "logs": logs,
            "join_command": join_command,
            "token": session.token,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        }

    async def on_breakpoint_hit(
        self,
        db: AsyncSession,
        session_id: str,
        step_index: int,
        step_name: str,
    ) -> None:
        """
        Called when pipeline execution hits a breakpoint.

        Args:
            db: Database session
            session_id: Debug session ID
            step_index: Index of step about to execute
            step_name: Name of the step
        """
        session = await self.get_session(db, session_id)
        if not session:
            return

        # Update session
        session.status = DebugSessionStatus.WAITING_AT_BP.value
        session.current_step_index = step_index
        session.current_step_name = step_name
        session.breakpoint_hit_at = datetime.utcnow()
        session.expires_at = datetime.utcnow() + timedelta(seconds=session.timeout_seconds)

        # Update state machine
        machine = self._get_or_create_machine(session)
        if machine.state != DebugState.WAITING_AT_BP:
            machine.transition(DebugState.WAITING_AT_BP, reason=f"Breakpoint at step {step_index}")

        # Save state history
        session.state_history = json.dumps(machine.to_dict()["history"])

        await db.commit()

        # Broadcast to WebSocket
        await manager.broadcast("debug_breakpoint", {
            "session_id": session_id,
            "step_index": step_index,
            "step_name": step_name,
            "status": "waiting_at_bp",
        })

    async def on_connect(
        self,
        db: AsyncSession,
        session_id: str,
        mode: str,
    ) -> str:
        """
        Called when CLI connects to a debug session.

        Args:
            db: Database session
            session_id: Debug session ID
            mode: Connection mode ("sidecar" or "shell")

        Returns:
            Container ID for terminal connection
        """
        session = await self.get_session(db, session_id)
        if not session:
            raise ValueError("Session not found")

        if session.status != DebugSessionStatus.WAITING_AT_BP.value:
            raise ValueError(f"Cannot connect: session status is {session.status}")

        # Update session
        session.status = DebugSessionStatus.CONNECTED.value
        session.connection_mode = mode
        session.connected_at = datetime.utcnow()

        # Update state machine
        machine = self._get_or_create_machine(session)
        if machine.state != DebugState.CONNECTED:
            machine.transition(DebugState.CONNECTED, reason=f"CLI connected ({mode})")

        session.state_history = json.dumps(machine.to_dict()["history"])
        await db.commit()

        # Broadcast status update
        await manager.broadcast("debug_status", {
            "session_id": session_id,
            "status": "connected",
            "mode": mode,
        })

        # Container ID will be set by terminal service
        return session.sidecar_container_id or ""

    async def resume(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> None:
        """
        Resume pipeline execution from breakpoint.

        Args:
            db: Database session
            session_id: Debug session ID
        """
        session = await self.get_session(db, session_id)
        if not session:
            raise ValueError("Session not found")

        if session.status not in {DebugSessionStatus.CONNECTED.value, DebugSessionStatus.WAITING_AT_BP.value}:
            raise ValueError(f"Can only resume from connected or waiting state, got {session.status}")

        # Update session to ended
        session.status = DebugSessionStatus.ENDED.value
        session.ended_at = datetime.utcnow()

        # Update state machine
        machine = self._get_or_create_machine(session)
        if not machine.is_terminal:
            machine.transition(DebugState.ENDED, reason="User resumed")

        session.state_history = json.dumps(machine.to_dict()["history"])
        await db.commit()

        # Broadcast status update
        await manager.broadcast("debug_status", {
            "session_id": session_id,
            "status": "resumed",
        })

        # Trigger pipeline continuation (will be picked up by executor)
        await self._continue_pipeline_execution(db, session)

    async def abort(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> None:
        """
        Abort debug session and cancel pipeline.

        Args:
            db: Database session
            session_id: Debug session ID
        """
        session = await self.get_session(db, session_id)
        if not session:
            raise ValueError("Session not found")

        # Update session to ended
        session.status = DebugSessionStatus.ENDED.value
        session.ended_at = datetime.utcnow()

        # Update state machine
        machine = self._get_or_create_machine(session)
        if not machine.is_terminal:
            machine.transition(DebugState.ENDED, reason="User aborted")

        session.state_history = json.dumps(machine.to_dict()["history"])
        await db.commit()

        # Cancel the pipeline run
        await self._cancel_pipeline_run(db, session)

        # Broadcast status update
        await manager.broadcast("debug_status", {
            "session_id": session_id,
            "status": "aborted",
        })

        # End the session
        await self._end_session(db, session)

    async def extend_timeout(
        self,
        db: AsyncSession,
        session_id: str,
        additional_seconds: int,
    ) -> datetime:
        """
        Extend session timeout.

        Args:
            db: Database session
            session_id: Debug session ID
            additional_seconds: Seconds to add

        Returns:
            New expiration time
        """
        session = await self.get_session(db, session_id)
        if not session:
            raise ValueError("Session not found")

        # Calculate new expiry
        current_expiry = session.expires_at or datetime.utcnow()
        new_expiry = current_expiry + timedelta(seconds=additional_seconds)

        # Check max timeout
        max_expiry = session.created_at + timedelta(seconds=session.max_timeout_seconds)
        if new_expiry > max_expiry:
            raise ValueError(f"Cannot extend beyond maximum timeout of {session.max_timeout_seconds // 3600} hours")

        session.expires_at = new_expiry
        await db.commit()

        return new_expiry

    async def check_timeouts(
        self,
        db: AsyncSession,
    ) -> list[str]:
        """
        Check for and handle timed-out sessions.

        Returns list of session IDs that were timed out.
        """
        now = datetime.utcnow()

        # Find sessions that have expired
        result = await db.execute(
            select(DebugSession).where(
                DebugSession.status.in_([
                    DebugSessionStatus.WAITING_AT_BP.value,
                    DebugSessionStatus.CONNECTED.value,
                ]),
                DebugSession.expires_at < now,
            )
        )
        expired_sessions = result.scalars().all()

        timed_out_ids = []
        for session in expired_sessions:
            await self._transition_to_timeout(db, session)
            timed_out_ids.append(session.id)

        return timed_out_ids

    async def on_pipeline_complete(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> None:
        """Called when the debug pipeline completes (without hitting more breakpoints)."""
        session = await self.get_session(db, session_id)
        if not session:
            return

        if session.status in {DebugSessionStatus.TIMEOUT.value, DebugSessionStatus.ENDED.value}:
            return

        await self._end_session(db, session, reason="Pipeline completed")

    def validate_token(self, session: DebugSession, token: str) -> bool:
        """Validate session token."""
        if not token or not session.token:
            return False
        return secrets.compare_digest(session.token, token)

    async def _is_session_active(self, session_id: str, db: AsyncSession) -> bool:
        """Check if session is active (not terminal)."""
        session = await self.get_session(db, session_id)
        if not session:
            return False
        return session.status not in {
            DebugSessionStatus.TIMEOUT.value,
            DebugSessionStatus.ENDED.value,
        }

    def _get_or_create_machine(self, session: DebugSession) -> DebugStateMachine:
        """Get or create state machine for session."""
        if session.id not in self._active_sessions:
            # Restore from history or create new
            if session.state_history:
                try:
                    history_data = json.loads(session.state_history)
                    # Create machine at current state
                    machine = DebugStateMachine(
                        initial_state=DebugState(session.status)
                    )
                    self._active_sessions[session.id] = machine
                except (json.JSONDecodeError, ValueError):
                    self._active_sessions[session.id] = DebugStateMachine(
                        initial_state=DebugState(session.status)
                    )
            else:
                self._active_sessions[session.id] = DebugStateMachine(
                    initial_state=DebugState(session.status)
                )
        return self._active_sessions[session.id]

    async def _transition_to_timeout(
        self,
        db: AsyncSession,
        session: DebugSession,
    ) -> None:
        """Transition session to timeout state."""
        session.status = DebugSessionStatus.TIMEOUT.value
        session.ended_at = datetime.utcnow()

        machine = self._get_or_create_machine(session)
        if not machine.is_terminal:
            machine.transition(DebugState.TIMEOUT, reason="Session expired")

        session.state_history = json.dumps(machine.to_dict()["history"])
        await db.commit()

        # Cancel pipeline
        await self._cancel_pipeline_run(db, session)

        # Broadcast
        await manager.broadcast("debug_status", {
            "session_id": session.id,
            "status": "timeout",
        })

    async def _end_session(
        self,
        db: AsyncSession,
        session: DebugSession,
        reason: str = "Session ended",
    ) -> None:
        """End a debug session."""
        if session.status in {DebugSessionStatus.TIMEOUT.value, DebugSessionStatus.ENDED.value}:
            return

        session.status = DebugSessionStatus.ENDED.value
        session.ended_at = datetime.utcnow()

        machine = self._get_or_create_machine(session)
        if not machine.is_terminal:
            machine.transition(DebugState.ENDED, reason=reason)

        session.state_history = json.dumps(machine.to_dict()["history"])
        await db.commit()

        # Clean up from active sessions
        self._active_sessions.pop(session.id, None)

    async def _cancel_pipeline_run(
        self,
        db: AsyncSession,
        session: DebugSession,
    ) -> None:
        """Cancel the associated pipeline run."""
        run = await db.get(PipelineRun, session.pipeline_run_id)
        if run and run.status not in {RunStatus.PASSED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            run.status = RunStatus.CANCELLED.value
            run.completed_at = datetime.utcnow()
            await db.commit()

    async def _continue_pipeline_execution(
        self,
        db: AsyncSession,
        session: DebugSession,
    ) -> None:
        """
        Resume pipeline execution from the breakpoint.

        Calls pipeline_executor.resume_from_breakpoint() to continue execution.
        """
        # Import here to avoid circular imports
        from app.services.pipeline_executor import pipeline_executor

        # Broadcast resume event
        await manager.broadcast("debug_resume", {
            "session_id": session.id,
            "pipeline_run_id": session.pipeline_run_id,
            "continue_from_step": session.current_step_index,
        })

        # Actually resume execution
        await pipeline_executor.resume_from_breakpoint(db, session)

    async def _get_logs_up_to_breakpoint(
        self,
        db: AsyncSession,
        session: DebugSession,
    ) -> str:
        """Get accumulated logs up to the current breakpoint."""
        # TODO: Implement log aggregation from step runs
        # For now, return placeholder
        run = await db.get(PipelineRun, session.pipeline_run_id)
        if not run:
            return ""

        # Collect logs from completed steps
        logs = []
        if session.current_step_index is not None:
            from app.models import StepRun
            result = await db.execute(
                select(StepRun).where(
                    StepRun.pipeline_run_id == session.pipeline_run_id,
                    StepRun.step_index < session.current_step_index,
                ).order_by(StepRun.step_index)
            )
            step_runs = result.scalars().all()
            for step_run in step_runs:
                if step_run.logs:
                    logs.append(f"=== Step {step_run.step_index + 1}: {step_run.step_name} ===")
                    logs.append(step_run.logs)
                    logs.append("")

        return "\n".join(logs)

    async def run_timeout_monitor(self) -> None:
        """
        Background task to monitor session timeouts.

        Should be started in app lifespan.
        """
        import asyncio
        from app.database import async_session_maker

        while True:
            try:
                async with async_session_maker() as db:
                    await self.check_timeouts(db)
            except Exception as e:
                # Log error but continue
                print(f"Error checking debug session timeouts: {e}")

            # Check every 30 seconds
            await asyncio.sleep(30)
