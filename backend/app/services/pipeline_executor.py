"""
Pipeline execution service.

Orchestrates multi-step pipeline workflows by:
1. Creating pipeline runs and step runs
2. Creating workspace for pipeline (Docker volume)
3. Routing steps to LocalExecutor (Phase 12.4) or job queue (legacy/agent)
4. Handling step completion callbacks
5. Evaluating on_success/on_failure branching logic
6. Broadcasting status via WebSocket
7. Cleaning up workspace on completion
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Pipeline, PipelineRun, StepRun, RunStatus, Job, Card, Repo
from app.services.job_queue import job_queue, QueuedJob
from app.services.websocket import manager
from app.services.git_server import git_repo_manager
from app.services.workspace_service import get_workspace_service, WorkspaceError

# Phase 12.4: LocalExecutor integration
from app.services.execution.router import ExecutionRouter, ExecutorType
from app.services.execution.config_builder import build_execution_config
from app.services.execution.local_executor import get_local_executor, ExecutionResult
from app.services.execution.idempotency import ExecutionKey

logger = logging.getLogger(__name__)


def parse_steps(steps_str: str | None) -> list[dict]:
    """Parse steps from JSON string to list."""
    if not steps_str:
        return []
    try:
        return json.loads(steps_str)
    except (json.JSONDecodeError, TypeError):
        return []


def pipeline_run_to_ws_dict(run: PipelineRun) -> dict:
    """Convert a PipelineRun model to a dict for websocket broadcast."""
    return {
        "id": run.id,
        "pipeline_id": run.pipeline_id,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "current_step": run.current_step,
        "steps_completed": run.steps_completed,
        "steps_total": run.steps_total,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def step_run_to_ws_dict(step_run: StepRun) -> dict:
    """Convert a StepRun model to a dict for websocket broadcast."""
    return {
        "id": step_run.id,
        "pipeline_run_id": step_run.pipeline_run_id,
        "step_index": step_run.step_index,
        "step_name": step_run.step_name,
        "status": step_run.status,
        "job_id": step_run.job_id,
        "error": step_run.error,
        "started_at": step_run.started_at.isoformat() if step_run.started_at else None,
        "completed_at": step_run.completed_at.isoformat() if step_run.completed_at else None,
    }


class PipelineExecutor:
    """Orchestrates pipeline execution."""

    def __init__(self, use_local_executor: bool = True):
        """
        Initialize PipelineExecutor with execution router.

        Args:
            use_local_executor: If True, route script/docker steps to LocalExecutor
                              for immediate execution. If False, always use job queue.
                              Set to False in tests or when Docker isn't available.
        """
        self._router = ExecutionRouter()
        self._use_local_executor = use_local_executor

    async def _complete_pipeline(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        success: bool,
    ) -> None:
        """
        Complete a pipeline run and execute trigger actions.

        This handles:
        1. Setting the final status (passed/failed)
        2. Executing on_pass/on_fail actions from trigger_context
        3. Broadcasting the status update
        4. Cleaning up the workspace
        """
        pipeline_run.status = RunStatus.PASSED.value if success else RunStatus.FAILED.value
        pipeline_run.completed_at = datetime.utcnow()
        await db.commit()
        await db.refresh(pipeline_run)

        # Cleanup workspace
        try:
            await self._cleanup_workspace(db, pipeline_run)
        except Exception as e:
            logger.error(f"Failed to cleanup workspace for pipeline run {pipeline_run.id[:8]}: {e}")

        # Execute trigger actions if present in trigger_context
        if pipeline_run.trigger_context:
            try:
                context = json.loads(pipeline_run.trigger_context)
                action = context.get("on_pass") if success else context.get("on_fail")

                if action and action != "nothing":
                    await self._execute_trigger_action(db, pipeline_run, context, action, success)
            except Exception as e:
                logger.error(f"Failed to execute trigger action: {e}")

        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        logger.info(f"Pipeline run {pipeline_run.id[:8]} completed with status {pipeline_run.status}")

    async def _execute_trigger_action(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        context: dict,
        action: str,
        success: bool,
    ) -> None:
        """
        Execute a trigger action after pipeline completion.

        Actions:
        - "merge" or "merge:{branch}": Approve and merge the card
        - "reject": Reject the card back to todo
        """
        card_id = context.get("card_id")
        if not card_id:
            logger.warning(f"No card_id in trigger context, cannot execute action '{action}'")
            return

        # Fetch the card
        result = await db.execute(select(Card).where(Card.id == card_id))
        card = result.scalar_one_or_none()
        if not card:
            logger.warning(f"Card {card_id} not found, cannot execute action '{action}'")
            return

        # Fetch the repo for merge operations
        result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
        repo = result.scalar_one_or_none()

        logger.info(f"Executing trigger action '{action}' for card {card_id[:8]}")

        if action == "merge" or action.startswith("merge:"):
            # Determine target branch
            if action.startswith("merge:"):
                target_branch = action[6:]  # Remove "merge:" prefix
            else:
                target_branch = repo.default_branch if repo else "main"

            # Only merge if card has a branch and is in a mergeable state
            if card.branch_name and card.status in ("in_review", "in_progress"):
                merge_result = git_repo_manager.merge_branch(
                    repo_id=card.repo_id,
                    source_branch=card.branch_name,
                    target_branch=target_branch,
                )

                if merge_result.get("success"):
                    card.status = "done"
                    await db.commit()
                    await db.refresh(card)
                    logger.info(f"Card {card_id[:8]} merged to {target_branch} and marked done")

                    # Broadcast card update
                    await manager.send_card_updated({
                        "id": card.id,
                        "repo_id": card.repo_id,
                        "title": card.title,
                        "status": card.status,
                        "branch_name": card.branch_name,
                    })
                else:
                    logger.error(f"Merge failed for card {card_id[:8]}: {merge_result.get('error')}")
            else:
                logger.warning(
                    f"Cannot merge card {card_id[:8]}: "
                    f"branch={card.branch_name}, status={card.status}"
                )

        elif action == "reject":
            # Reject card back to todo
            if card.status in ("in_review", "failed", "in_progress"):
                card.status = "todo"
                card.branch_name = None
                card.pr_url = None
                await db.commit()
                await db.refresh(card)
                logger.info(f"Card {card_id[:8]} rejected back to todo")

                # Broadcast card update
                await manager.send_card_updated({
                    "id": card.id,
                    "repo_id": card.repo_id,
                    "title": card.title,
                    "status": card.status,
                    "branch_name": card.branch_name,
                })
            else:
                logger.warning(f"Cannot reject card {card_id[:8]}: status={card.status}")

        elif action == "fail":
            # Mark card as failed (user can retry)
            if card.status in ("in_review", "in_progress"):
                card.status = "failed"
                await db.commit()
                await db.refresh(card)
                logger.info(f"Card {card_id[:8]} marked as failed")

                # Broadcast card update
                await manager.send_card_updated({
                    "id": card.id,
                    "repo_id": card.repo_id,
                    "title": card.title,
                    "status": card.status,
                    "branch_name": card.branch_name,
                })
            else:
                logger.warning(f"Cannot fail card {card_id[:8]}: status={card.status}")

        else:
            logger.warning(f"Unknown trigger action: {action}")

    async def start_pipeline(
        self,
        db: AsyncSession,
        pipeline: Pipeline,
        repo: Repo,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        trigger_context: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> PipelineRun:
        """
        Start a new pipeline run.

        Creates PipelineRun, workspace, then starts executing the first step.

        trigger_context can contain:
        - branch: The branch to work on
        - commit_sha: The specific commit
        - card_id: The card that triggered the pipeline (for card_complete triggers)
        """
        steps = parse_steps(pipeline.steps)

        # Create the pipeline run
        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            trigger_context=json.dumps(trigger_context) if trigger_context else None,
            current_step=0,
            steps_completed=0,
            steps_total=len(steps),
            started_at=datetime.utcnow(),
        )
        db.add(pipeline_run)
        await db.commit()
        await db.refresh(pipeline_run)

        logger.info(f"Started pipeline run {pipeline_run.id[:8]} for pipeline {pipeline.name}")

        # Create workspace for the pipeline run
        try:
            branch = trigger_context.get("branch") if trigger_context else None
            commit_sha = trigger_context.get("commit_sha") if trigger_context else None
            workspace_service = get_workspace_service()
            workspace = await workspace_service.get_or_create_workspace(
                db=db,
                pipeline_run=pipeline_run,
                repo=repo,
                branch=branch,
                commit_sha=commit_sha,
            )
            logger.info(f"Created workspace {workspace.id} for pipeline run {pipeline_run.id[:8]}")
        except WorkspaceError as e:
            logger.error(f"Failed to create workspace: {e}")
            # Continue without workspace - jobs will create their own working directories

        # Broadcast pipeline run started
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        # Execute the first step
        if steps:
            await self._execute_step(db, pipeline_run, repo, steps, 0, params)
        else:
            # No steps, mark as passed
            await self._complete_pipeline(db, pipeline_run, success=True)

        return pipeline_run

    async def _cleanup_workspace(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
    ) -> None:
        """
        Cleanup workspace after pipeline completion.

        Args:
            db: Database session
            pipeline_run: Completed pipeline run
        """
        # Refresh to get workspace relationship
        await db.refresh(pipeline_run)

        if not pipeline_run.workspace:
            logger.debug(f"No workspace to cleanup for pipeline run {pipeline_run.id[:8]}")
            return

        workspace_service = get_workspace_service()
        await workspace_service.cleanup_workspace(db, pipeline_run.workspace)

    async def _execute_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        step_index: int,
        params: dict[str, Any] | None = None,
        previous_runner_id: str | None = None,
    ) -> None:
        """
        Execute a single step in the pipeline.

        Phase 12.4: Routes script/docker steps to LocalExecutor for instant execution.
        Agent steps and steps with special requirements use job queue (legacy path).

        Args:
            previous_runner_id: The runner that executed the previous step (for continuation affinity)
        """
        if step_index >= len(steps):
            # All steps completed
            await self._complete_pipeline(db, pipeline_run, success=True)
            return

        step = steps[step_index]
        step_name = step.get("name", f"Step {step_index + 1}")
        step_type = step.get("type", "script")
        step_config = step.get("config", {})
        timeout = step.get("timeout", 300)
        continue_in_context = step.get("continue_in_context", False)
        step_id = step.get("id")  # Optional step ID for context directory naming
        requirements = step.get("requires", {})

        # Extract agent-specific fields from step config (Phase 9.1c)
        agent_file_ids = step_config.get("agent_file_ids", []) if step_type == "agent" else []
        prompt_template = step_config.get("prompt_template") if step_type == "agent" else None

        # Check if this step is a continuation from the previous step
        is_continuation = False
        previous_step_logs = None
        if step_index > 0:
            prev_step_config = steps[step_index - 1]
            is_continuation = prev_step_config.get("continue_in_context", False)

            # Get previous step logs
            prev_step_run = await db.execute(
                select(StepRun)
                .where(StepRun.pipeline_run_id == pipeline_run.id)
                .where(StepRun.step_index == step_index - 1)
            )
            prev_step = prev_step_run.scalar_one_or_none()
            if prev_step and prev_step.logs:
                previous_step_logs = prev_step.logs

        logger.info(f"Executing step {step_index}: {step_name} (type={step_type}, continue_in_context={continue_in_context}, is_continuation={is_continuation})")

        # Create the step run
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=step_index,
            step_name=step_name,
            status=RunStatus.RUNNING.value,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)

        # Update pipeline run's current step
        pipeline_run.current_step = step_index
        await db.commit()
        await db.refresh(step_run)

        # Broadcast step started
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        # Phase 12.4: Route step to appropriate executor
        # Continuation steps need affinity, so they use job queue for remote runners
        routing_decision = self._router.route(
            step_type=step_type,
            step_config=step_config,
            requirements=requirements,
            previous_runner_id=previous_runner_id if is_continuation else None,
        )

        # Use LocalExecutor for script/docker/agent steps without special requirements
        # Only if local execution is enabled (disabled in tests)
        # Phase 12.5: Agent steps now use LocalExecutor
        use_local_executor = (
            self._use_local_executor
            and routing_decision.executor_type == ExecutorType.LOCAL
            and step_type in ("script", "docker", "agent")
            and not is_continuation  # Continuations need job queue for proper workspace handling
        )

        if use_local_executor:
            # Execute locally via LocalExecutor
            await self._execute_step_locally(
                db=db,
                pipeline_run=pipeline_run,
                repo=repo,
                steps=steps,
                step_index=step_index,
                step_run=step_run,
                step_type=step_type,
                step_config=step_config,
                timeout=timeout,
                continue_in_context=continue_in_context,
                # Agent-specific params (Phase 12.5)
                agent_file_ids=agent_file_ids,
                prompt_template=prompt_template,
                previous_step_logs=previous_step_logs,
            )
            return

        # Legacy path: Use job queue for agent steps and remote execution
        await self._execute_step_via_job_queue(
            db=db,
            pipeline_run=pipeline_run,
            repo=repo,
            steps=steps,
            step_index=step_index,
            step_run=step_run,
            step_name=step_name,
            step_type=step_type,
            step_config=step_config,
            step_id=step_id,
            timeout=timeout,
            continue_in_context=continue_in_context,
            is_continuation=is_continuation,
            previous_step_logs=previous_step_logs,
            previous_runner_id=previous_runner_id,
            agent_file_ids=agent_file_ids,
            prompt_template=prompt_template,
        )

    async def _execute_step_locally(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        step_index: int,
        step_run: StepRun,
        step_type: str,
        step_config: dict,
        timeout: int,
        continue_in_context: bool,
        # Agent-specific params (Phase 12.5)
        agent_file_ids: list[str] | None = None,
        prompt_template: str | None = None,
        previous_step_logs: str | None = None,
    ) -> None:
        """
        Execute a step locally using LocalExecutor (Phase 12.4 + 12.5).

        This is the fast path for script/docker/agent steps without special requirements.
        """
        step_name = step_run.step_name
        logger.info(f"Executing step {step_index} locally: {step_name}")

        # Get workspace - explicitly load the workspace relationship
        await db.refresh(pipeline_run, ["workspace"])
        workspace = pipeline_run.workspace

        # For Docker volume-based workspaces, we need to mount by volume name
        # For local execution, the LocalExecutor handles volume mounting
        if workspace and workspace.volume_name:
            # Use Docker volume name - LocalExecutor will mount it
            workspace_path = workspace.volume_name
        else:
            # No workspace - create a temporary directory
            logger.warning(f"No workspace for pipeline run {pipeline_run.id[:8]}, creating temporary workspace")
            workspace_path = f"/tmp/lazyaf-workspace-{pipeline_run.id}"

        try:
            # Build agent_config for agent steps (Phase 12.5)
            agent_config = None
            if step_type == "agent":
                # Backend URL for container to communicate back
                backend_url = "http://host.docker.internal:8000"
                agent_config = {
                    "runner_type": step_config.get("runner_type", "claude-code"),
                    "title": step_config.get("title", step_name),
                    "description": step_config.get("description", ""),
                    "model": step_config.get("model"),
                    "agent_file_ids": agent_file_ids or [],
                    "prompt_template": prompt_template,
                    "previous_step_logs": previous_step_logs,
                    "repo_url": repo.get_internal_git_url(backend_url),
                    "branch_name": f"lazyaf/{step_run.id[:8]}",
                    "base_branch": repo.default_branch,
                    "is_continuation": False,  # Non-continuation steps only in LocalExecutor
                    "pipeline_run_id": pipeline_run.id,
                }

            # Build execution config
            exec_config = build_execution_config(
                step_type=step_type,
                step_config=step_config,
                workspace_path=workspace_path,
                timeout_seconds=timeout,
                use_control_layer=(step_type == "agent"),  # Use control layer for agent steps
                backend_url="http://host.docker.internal:8000" if step_type == "agent" else None,
                agent_config=agent_config,
            )

            # Create execution key for idempotency
            exec_key = ExecutionKey(
                pipeline_run_id=pipeline_run.id,
                step_index=step_index,
                attempt=1,  # TODO: Track attempts for retries
            )

            # Execute via LocalExecutor
            executor = get_local_executor()
            logs_buffer = []

            async for item in executor.execute_step(exec_key, exec_config):
                if isinstance(item, str):
                    logs_buffer.append(item)
                    # Stream logs in real-time
                    logger.debug(f"[step {step_index}] {item}")
                elif isinstance(item, ExecutionResult):
                    # Execution complete
                    step_success = item.success

                    # Update step run
                    step_run.status = RunStatus.PASSED.value if step_success else RunStatus.FAILED.value
                    step_run.completed_at = datetime.utcnow()
                    step_run.logs = "\n".join(logs_buffer)
                    if not step_success:
                        step_run.error = item.error or f"Exit code: {item.exit_code}"

                    if step_success:
                        pipeline_run.steps_completed += 1

                    await db.commit()
                    await db.refresh(step_run)
                    await db.refresh(pipeline_run)

                    # Broadcast completion
                    await manager.send_step_run_status(step_run_to_ws_dict(step_run))
                    await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

                    logger.info(f"Step {step_index} ({step_name}) completed locally: {'success' if step_success else 'failed'}")

                    # Get step definition for action handling
                    step = steps[step_index]
                    action = step.get("on_success" if step_success else "on_failure", "stop" if not step_success else "next")

                    # Handle action (no runner_id for local execution)
                    await self._handle_action(db, pipeline_run, repo, steps, step_index, action, step_success, runner_id=None)
                    return

        except Exception as e:
            logger.error(f"Local execution failed for step {step_index}: {e}")

            # Mark step as failed
            step_run.status = RunStatus.FAILED.value
            step_run.completed_at = datetime.utcnow()
            step_run.error = str(e)

            await db.commit()
            await db.refresh(step_run)

            # Broadcast failure
            await manager.send_step_run_status(step_run_to_ws_dict(step_run))
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

            # Get step definition for action handling
            step = steps[step_index]
            action = step.get("on_failure", "stop")

            await self._handle_action(db, pipeline_run, repo, steps, step_index, action, False, runner_id=None)

    async def _execute_step_via_job_queue(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        step_index: int,
        step_run: StepRun,
        step_name: str,
        step_type: str,
        step_config: dict,
        step_id: str | None,
        timeout: int,
        continue_in_context: bool,
        is_continuation: bool,
        previous_step_logs: str | None,
        previous_runner_id: str | None,
        agent_file_ids: list,
        prompt_template: str | None,
    ) -> None:
        """
        Execute a step via job queue (legacy path for agent steps and remote execution).

        This preserves the existing behavior for agent steps and steps with special requirements.
        """
        # Create a temporary card for tracking this step
        # This allows reuse of the existing job/runner infrastructure
        card_title = f"[Pipeline] {step_name}"
        card_description = f"Pipeline: {pipeline_run.pipeline_id}\nStep {step_index + 1} of {pipeline_run.steps_total}"

        # For agent steps, use the description from config
        if step_type == "agent":
            card_title = step_config.get("title", card_title)
            card_description = step_config.get("description", card_description)

        card = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title=card_title,
            description=card_description,
            status="in_progress",
            runner_type=step_config.get("runner_type", "any"),
            step_type=step_type,
            step_config=json.dumps(step_config) if step_config else None,
            # Agent-specific fields (Phase 9.1c)
            agent_file_ids=json.dumps(agent_file_ids) if agent_file_ids else None,
            prompt_template=prompt_template,
            pipeline_run_id=pipeline_run.id,
            pipeline_step_index=step_index,
        )
        db.add(card)

        # Create the job
        job_id = str(uuid4())
        job = Job(
            id=job_id,
            card_id=card.id,
            status="queued",
            step_type=step_type,
            step_config=json.dumps(step_config) if step_config else None,
            step_run_id=step_run.id,  # Link job to step run
        )
        db.add(job)

        # Update card and step_run with job reference
        card.job_id = job_id
        card.branch_name = f"lazyaf/{job_id[:8]}"
        step_run.job_id = job_id

        await db.commit()

        # Queue the job for a runner
        # If this is a continuation, require the same runner for affinity
        required_runner_id = previous_runner_id if is_continuation else None
        logger.info(f"Step {step_index}: is_continuation={is_continuation}, previous_runner_id={previous_runner_id[:8] if previous_runner_id else None}, required_runner_id={required_runner_id[:8] if required_runner_id else None}")

        queued_job = QueuedJob(
            id=job_id,
            card_id=card.id,
            repo_id=repo.id,
            repo_url=repo.remote_url or "",
            base_branch=repo.default_branch,
            card_title=card_title,
            card_description=card_description,
            runner_type=step_config.get("runner_type", "any"),
            use_internal_git=True,
            step_type=step_type,
            step_config=step_config,
            # Agent-specific fields (Phase 9.1c)
            agent_file_ids=agent_file_ids,
            prompt_template=prompt_template,
            # Pipeline context
            continue_in_context=continue_in_context,
            is_continuation=is_continuation,
            previous_step_logs=previous_step_logs,
            pipeline_run_id=pipeline_run.id,
            # Step metadata for context directory (Phase 9.1d)
            step_id=step_id,
            step_index=step_index,
            step_name=step_name,
            # Runner affinity for continuations
            required_runner_id=required_runner_id,
        )
        await job_queue.enqueue(queued_job)

        logger.info(f"Enqueued job {job_id[:8]} for step {step_index}: {step_name}")

        # Broadcast job queued
        await manager.send_job_status({
            "id": job_id,
            "card_id": card.id,
            "status": "queued",
            "error": None,
            "started_at": None,
            "completed_at": None,
        })

    async def on_step_complete(
        self,
        db: AsyncSession,
        step_run_id: str,
        job: Job,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle step completion.

        Called from job_callback when a job with step_run_id completes.
        Evaluates on_success/on_failure and proceeds accordingly.

        Args:
            runner_id: The runner that executed this step (for continuation affinity)
        """
        # Get the step run
        result = await db.execute(
            select(StepRun).where(StepRun.id == step_run_id)
        )
        step_run = result.scalar_one_or_none()
        if not step_run:
            logger.error(f"StepRun {step_run_id} not found")
            return

        # Get the pipeline run with steps
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == step_run.pipeline_run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        pipeline_run = result.scalar_one_or_none()
        if not pipeline_run:
            logger.error(f"PipelineRun {step_run.pipeline_run_id} not found")
            return

        # Check if pipeline was already cancelled or completed
        if pipeline_run.status not in (RunStatus.RUNNING.value, RunStatus.PENDING.value):
            logger.info(f"Pipeline run {pipeline_run.id[:8]} is {pipeline_run.status}, ignoring step completion")
            return

        # Get the pipeline and repo
        result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_run.pipeline_id))
        pipeline = result.scalar_one_or_none()
        if not pipeline:
            logger.error(f"Pipeline {pipeline_run.pipeline_id} not found")
            return

        result = await db.execute(select(Repo).where(Repo.id == pipeline.repo_id))
        repo = result.scalar_one_or_none()
        if not repo:
            logger.error(f"Repo {pipeline.repo_id} not found")
            return

        # Determine if step succeeded
        step_success = job.status == "completed"

        # Check if tests failed (Phase 8 integration)
        if step_success and job.tests_run and not job.tests_passed:
            step_success = False

        # Update step run status
        step_run.status = RunStatus.PASSED.value if step_success else RunStatus.FAILED.value
        step_run.completed_at = datetime.utcnow()
        step_run.logs = job.logs or ""
        step_run.error = job.error

        if step_success:
            pipeline_run.steps_completed += 1

        await db.commit()
        await db.refresh(step_run)
        await db.refresh(pipeline_run)

        # Broadcast step completion
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        logger.info(f"Step {step_run.step_index} ({step_run.step_name}) completed: {'success' if step_success else 'failed'}")

        # Get step definition to determine next action
        steps = parse_steps(pipeline.steps)
        if step_run.step_index >= len(steps):
            logger.error(f"Step index {step_run.step_index} out of range")
            return

        step = steps[step_run.step_index]
        action = step.get("on_success" if step_success else "on_failure", "stop" if not step_success else "next")

        # Handle the action, passing runner_id for continuation affinity
        await self._handle_action(db, pipeline_run, repo, steps, step_run.step_index, action, step_success, runner_id=runner_id)

    async def _handle_action(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        action: str,
        step_success: bool,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle on_success/on_failure action.

        Actions:
        - "next": Execute next step
        - "stop": Complete pipeline (status based on step_success)
        - "trigger:{card_id}": Clone card as template and run it
        - "trigger:pipeline:{pipeline_id}": Start another pipeline
        - "merge:{branch}": Merge current branch to target

        Args:
            runner_id: The runner that completed the previous step (for continuation affinity)
        """
        logger.info(f"Handling action '{action}' after step {current_step} (success={step_success})")

        if action == "next":
            # Execute next step, passing runner_id for affinity
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1, previous_runner_id=runner_id)

        elif action == "stop":
            # Complete the pipeline
            await self._complete_pipeline(db, pipeline_run, success=step_success)

        elif action.startswith("trigger:pipeline:"):
            # Start another pipeline
            target_pipeline_id = action[17:]  # Remove "trigger:pipeline:" prefix
            await self._trigger_pipeline(db, pipeline_run, repo, steps, current_step, target_pipeline_id)

        elif action.startswith("trigger:"):
            # Clone card as template and run it
            card_id = action[8:]  # Remove "trigger:" prefix
            await self._trigger_card(db, pipeline_run, repo, steps, current_step, card_id)

        elif action.startswith("merge:"):
            # Merge the step's branch to target
            target_branch = action[6:]  # Remove "merge:" prefix
            await self._merge_branch(db, pipeline_run, repo, steps, current_step, target_branch)

        else:
            logger.warning(f"Unknown action '{action}', treating as 'stop'")
            await self._complete_pipeline(db, pipeline_run, success=step_success)

    async def _trigger_card(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        template_card_id: str,
    ) -> None:
        """
        Clone a card as template and run it to fix issues.

        The triggered card runs as an additional step, then continues to next step.
        """
        # Get the template card
        result = await db.execute(select(Card).where(Card.id == template_card_id))
        template_card = result.scalar_one_or_none()
        if not template_card:
            logger.error(f"Template card {template_card_id} not found for trigger action")
            # Continue to next step anyway
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        logger.info(f"Triggering card template {template_card_id} to fix step {current_step}")

        # Create step run for the triggered card
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=current_step,  # Same step index (sub-step)
            step_name=f"[Fix] {template_card.title}",
            status=RunStatus.RUNNING.value,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)

        # Clone the template card
        cloned_card = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title=f"[Pipeline Fix] {template_card.title}",
            description=template_card.description,
            status="in_progress",
            runner_type=template_card.runner_type,
            step_type=template_card.step_type,
            step_config=template_card.step_config,
        )
        db.add(cloned_card)

        # Create job for the cloned card
        job_id = str(uuid4())
        job = Job(
            id=job_id,
            card_id=cloned_card.id,
            status="queued",
            step_type=cloned_card.step_type,
            step_config=cloned_card.step_config,
            step_run_id=step_run.id,
        )
        db.add(job)

        # Update references
        cloned_card.job_id = job_id
        cloned_card.branch_name = f"lazyaf/{job_id[:8]}"
        step_run.job_id = job_id

        await db.commit()

        # Parse step_config for the queued job
        step_config = None
        if cloned_card.step_config:
            try:
                step_config = json.loads(cloned_card.step_config)
            except (json.JSONDecodeError, TypeError):
                pass

        # Queue the job
        queued_job = QueuedJob(
            id=job_id,
            card_id=cloned_card.id,
            repo_id=repo.id,
            repo_url=repo.remote_url or "",
            base_branch=repo.default_branch,
            card_title=cloned_card.title,
            card_description=cloned_card.description,
            runner_type=cloned_card.runner_type,
            use_internal_git=True,
            step_type=cloned_card.step_type,
            step_config=step_config,
        )
        await job_queue.enqueue(queued_job)

        logger.info(f"Enqueued triggered job {job_id[:8]} for fix card")

        # Broadcast updates
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_job_status({
            "id": job_id,
            "card_id": cloned_card.id,
            "status": "queued",
            "error": None,
            "started_at": None,
            "completed_at": None,
        })

    async def _trigger_pipeline(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        target_pipeline_id: str,
    ) -> None:
        """
        Trigger another pipeline and wait for it to complete, then continue.

        The triggered pipeline runs independently, and we continue to the next step
        regardless of its outcome (it's fire-and-forget for now).
        """
        # Get the target pipeline
        result = await db.execute(select(Pipeline).where(Pipeline.id == target_pipeline_id))
        target_pipeline = result.scalar_one_or_none()
        if not target_pipeline:
            logger.error(f"Target pipeline {target_pipeline_id} not found for trigger action")
            # Continue to next step anyway
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        # Get the target repo (may be different from current)
        result = await db.execute(select(Repo).where(Repo.id == target_pipeline.repo_id))
        target_repo = result.scalar_one_or_none()
        if not target_repo:
            logger.error(f"Repo {target_pipeline.repo_id} not found for triggered pipeline")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        if not target_repo.is_ingested:
            logger.error(f"Repo {target_repo.id} is not ingested, cannot run pipeline")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        logger.info(f"Triggering pipeline {target_pipeline.name} (id: {target_pipeline_id})")

        # Start the target pipeline (fire-and-forget for now)
        # The triggered pipeline runs independently
        await self.start_pipeline(
            db=db,
            pipeline=target_pipeline,
            repo=target_repo,
            trigger_type="pipeline",
            trigger_ref=pipeline_run.id,  # Reference to the triggering pipeline run
        )

        # Continue to next step immediately (don't wait for triggered pipeline)
        await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)

    async def _merge_branch(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        target_branch: str,
    ) -> None:
        """
        Merge the current step's branch to target branch, then continue.
        """
        # Get the step run to find its job/card
        result = await db.execute(
            select(StepRun)
            .where(StepRun.pipeline_run_id == pipeline_run.id)
            .where(StepRun.step_index == current_step)
        )
        step_run = result.scalar_one_or_none()
        if not step_run or not step_run.job_id:
            logger.warning(f"No job found for step {current_step}, skipping merge")
            # Continue to next step
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        # Get the job to find the card's branch
        result = await db.execute(select(Job).where(Job.id == step_run.job_id))
        job = result.scalar_one_or_none()
        if not job:
            logger.warning(f"Job {step_run.job_id} not found, skipping merge")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        # Get the card to find the branch name
        result = await db.execute(select(Card).where(Card.id == job.card_id))
        card = result.scalar_one_or_none()
        if not card or not card.branch_name:
            logger.warning(f"Card or branch not found for merge, skipping")
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            return

        logger.info(f"Merging branch {card.branch_name} to {target_branch}")

        # Perform the merge
        merge_result = git_repo_manager.merge_branch(
            repo_id=repo.id,
            source_branch=card.branch_name,
            target_branch=target_branch,
        )

        if merge_result["success"]:
            logger.info(f"Merge successful: {merge_result}")

            # Clean up .lazyaf-context directory from merged branch (Phase 9.1d)
            cleanup_result = git_repo_manager.delete_directory_from_branch(
                repo_id=repo.id,
                branch=target_branch,
                directory=".lazyaf-context",
            )
            if cleanup_result["success"]:
                logger.info(f"Context cleanup: {cleanup_result.get('message', 'done')}")
            else:
                logger.warning(f"Context cleanup failed: {cleanup_result.get('error', 'unknown')}")

            # Continue to next step or complete
            if current_step + 1 < len(steps):
                await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            else:
                await self._complete_pipeline(db, pipeline_run, success=True)
        else:
            logger.error(f"Merge failed: {merge_result}")
            await self._complete_pipeline(db, pipeline_run, success=False)

    async def cancel_run(self, db: AsyncSession, pipeline_run: PipelineRun) -> PipelineRun:
        """
        Cancel a running pipeline.

        Marks the run as cancelled and cancels any running jobs.
        """
        logger.info(f"Cancelling pipeline run {pipeline_run.id[:8]}")

        pipeline_run.status = RunStatus.CANCELLED.value
        pipeline_run.completed_at = datetime.utcnow()

        # Cancel any running step runs
        for step_run in pipeline_run.step_runs:
            if step_run.status == RunStatus.RUNNING.value:
                step_run.status = RunStatus.CANCELLED.value
                step_run.completed_at = datetime.utcnow()
                step_run.error = "Cancelled by user"

                # Cancel the job if it exists
                if step_run.job_id:
                    result = await db.execute(select(Job).where(Job.id == step_run.job_id))
                    job = result.scalar_one_or_none()
                    if job and job.status in ("queued", "running"):
                        job.status = "failed"
                        job.error = "Pipeline cancelled"

        await db.commit()
        await db.refresh(pipeline_run)

        # Broadcast updates
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        for step_run in pipeline_run.step_runs:
            await manager.send_step_run_status(step_run_to_ws_dict(step_run))

        return pipeline_run


# Global pipeline executor instance
# Local executor is disabled by default for safety in tests
# Set LAZYAF_USE_LOCAL_EXECUTOR=1 to enable in production
_use_local_executor = os.environ.get("LAZYAF_USE_LOCAL_EXECUTOR", "0") == "1"
pipeline_executor = PipelineExecutor(use_local_executor=_use_local_executor)
