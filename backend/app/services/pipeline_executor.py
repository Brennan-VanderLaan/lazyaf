"""
Pipeline execution service.

Orchestrates multi-step pipeline workflows by:
1. Creating pipeline runs and step runs
2. Enqueuing steps as jobs (via temporary cards for tracking)
3. Handling step completion callbacks
4. Evaluating on_success/on_failure branching logic
5. Broadcasting status via WebSocket
"""

import json
import logging
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

    async def start_pipeline(
        self,
        db: AsyncSession,
        pipeline: Pipeline,
        repo: Repo,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> PipelineRun:
        """
        Start a new pipeline run.

        Creates PipelineRun, then starts executing the first step.
        """
        steps = parse_steps(pipeline.steps)

        # Create the pipeline run
        pipeline_run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            current_step=0,
            steps_completed=0,
            steps_total=len(steps),
            started_at=datetime.utcnow(),
        )
        db.add(pipeline_run)
        await db.commit()
        await db.refresh(pipeline_run)

        logger.info(f"Started pipeline run {pipeline_run.id[:8]} for pipeline {pipeline.name}")

        # Broadcast pipeline run started
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        # Execute the first step
        if steps:
            await self._execute_step(db, pipeline_run, repo, steps, 0, params)
        else:
            # No steps, mark as passed
            pipeline_run.status = RunStatus.PASSED.value
            pipeline_run.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(pipeline_run)
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        return pipeline_run

    async def _execute_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        step_index: int,
        params: dict[str, Any] | None = None,
    ) -> None:
        """
        Execute a single step in the pipeline.

        Creates a StepRun, creates a temporary Card + Job, and enqueues the job.
        """
        if step_index >= len(steps):
            # All steps completed
            pipeline_run.status = RunStatus.PASSED.value
            pipeline_run.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(pipeline_run)
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
            logger.info(f"Pipeline run {pipeline_run.id[:8]} completed successfully")
            return

        step = steps[step_index]
        step_name = step.get("name", f"Step {step_index + 1}")
        step_type = step.get("type", "script")
        step_config = step.get("config", {})
        timeout = step.get("timeout", 300)

        logger.info(f"Executing step {step_index}: {step_name} (type={step_type})")

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
    ) -> None:
        """
        Handle step completion.

        Called from job_callback when a job with step_run_id completes.
        Evaluates on_success/on_failure and proceeds accordingly.
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

        # Handle the action
        await self._handle_action(db, pipeline_run, repo, steps, step_run.step_index, action, step_success)

    async def _handle_action(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        repo: Repo,
        steps: list[dict],
        current_step: int,
        action: str,
        step_success: bool,
    ) -> None:
        """
        Handle on_success/on_failure action.

        Actions:
        - "next": Execute next step
        - "stop": Complete pipeline (status based on step_success)
        - "trigger:{card_id}": Clone card as template and run it
        - "trigger:pipeline:{pipeline_id}": Start another pipeline
        - "merge:{branch}": Merge current branch to target
        """
        logger.info(f"Handling action '{action}' after step {current_step} (success={step_success})")

        if action == "next":
            # Execute next step
            await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)

        elif action == "stop":
            # Complete the pipeline
            pipeline_run.status = RunStatus.PASSED.value if step_success else RunStatus.FAILED.value
            pipeline_run.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(pipeline_run)
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
            logger.info(f"Pipeline run {pipeline_run.id[:8]} stopped with status {pipeline_run.status}")

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
            pipeline_run.status = RunStatus.PASSED.value if step_success else RunStatus.FAILED.value
            pipeline_run.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(pipeline_run)
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

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
            # Continue to next step or complete
            if current_step + 1 < len(steps):
                await self._execute_step(db, pipeline_run, repo, steps, current_step + 1)
            else:
                pipeline_run.status = RunStatus.PASSED.value
                pipeline_run.completed_at = datetime.utcnow()
                await db.commit()
                await db.refresh(pipeline_run)
                await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))
        else:
            logger.error(f"Merge failed: {merge_result}")
            # Mark pipeline as failed
            pipeline_run.status = RunStatus.FAILED.value
            pipeline_run.completed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(pipeline_run)
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

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
pipeline_executor = PipelineExecutor()
