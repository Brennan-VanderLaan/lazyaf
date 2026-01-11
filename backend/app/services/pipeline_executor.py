"""
Pipeline execution service.

Orchestrates multi-step pipeline workflows by:
1. Creating pipeline runs and step runs
2. Enqueuing steps as jobs (via temporary cards for tracking)
3. Handling step completion callbacks
4. Graph-based parallel execution with fan-out/fan-in
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


def parse_steps_graph(steps_graph_str: str | None) -> dict | None:
    """Parse steps_graph from JSON string to dict."""
    if not steps_graph_str:
        return None
    try:
        return json.loads(steps_graph_str)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_json_list(json_str: str | None) -> list:
    """Parse a JSON list string, returning empty list on failure."""
    if not json_str:
        return []
    try:
        result = json.loads(json_str)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def get_upstream_step_ids(graph: dict, step_id: str) -> list[str]:
    """Get all step IDs that have edges pointing TO this step."""
    edges = graph.get("edges", [])
    return [e["from_step"] for e in edges if e.get("to_step") == step_id]


def get_downstream_edges(graph: dict, step_id: str, condition: str) -> list[dict]:
    """Get all edges FROM this step matching the given condition (success/failure/always)."""
    edges = graph.get("edges", [])
    result = []
    for edge in edges:
        if edge.get("from_step") == step_id:
            edge_condition = edge.get("condition", "success")
            # Match condition: success matches success, failure matches failure, always matches both
            if edge_condition == condition or edge_condition == "always":
                result.append(edge)
    return result


def count_total_steps(graph: dict) -> int:
    """Count total steps in a graph."""
    return len(graph.get("steps", {}))


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
        "active_step_ids": parse_json_list(run.active_step_ids),
        "completed_step_ids": parse_json_list(run.completed_step_ids),
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
        "step_id": step_run.step_id,
        "step_name": step_run.step_name,
        "status": step_run.status,
        "job_id": step_run.job_id,
        "error": step_run.error,
        "started_at": step_run.started_at.isoformat() if step_run.started_at else None,
        "completed_at": step_run.completed_at.isoformat() if step_run.completed_at else None,
    }


class PipelineExecutor:
    """Orchestrates pipeline execution."""

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
        """
        pipeline_run.status = RunStatus.PASSED.value if success else RunStatus.FAILED.value
        pipeline_run.completed_at = datetime.utcnow()
        await db.commit()
        await db.refresh(pipeline_run)

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

        For graph-based pipelines (v2): Executes ALL entry points in parallel.
        For legacy pipelines (v1): Executes steps sequentially.

        trigger_context can contain:
        - branch: The branch to work on
        - commit_sha: The specific commit
        - card_id: The card that triggered the pipeline (for card_complete triggers)
        """
        graph = parse_steps_graph(pipeline.steps_graph)

        if graph:
            # Graph-based (v2) pipeline - execute entry points in parallel
            entry_points = graph.get("entry_points", [])
            steps_dict = graph.get("steps", {})
            total_steps = count_total_steps(graph)

            logger.info(f"Using steps_graph with {total_steps} steps, {len(entry_points)} entry points")

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
                steps_total=total_steps,
                active_step_ids=json.dumps([]),
                completed_step_ids=json.dumps([]),
                started_at=datetime.utcnow(),
            )
            db.add(pipeline_run)
            await db.commit()
            await db.refresh(pipeline_run)

            logger.info(f"Started pipeline run {pipeline_run.id[:8]} for pipeline {pipeline.name}")
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

            if not entry_points:
                # No entry points, mark as passed
                await self._complete_pipeline(db, pipeline_run, success=True)
            else:
                # Execute ALL entry points in parallel
                for step_id in entry_points:
                    if step_id in steps_dict:
                        await self._execute_graph_step(
                            db, pipeline_run, pipeline, repo, graph, step_id, params
                        )
                    else:
                        logger.warning(f"Entry point {step_id} not found in steps")

            return pipeline_run
        else:
            # Legacy (v1) pipeline - execute sequentially
            steps = parse_steps(pipeline.steps)
            logger.info(f"Using legacy steps with {len(steps)} steps")

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
            await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

            if steps:
                await self._execute_step(db, pipeline_run, repo, steps, 0, params)
            else:
                await self._complete_pipeline(db, pipeline_run, success=True)

            return pipeline_run

    async def _execute_graph_step(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        graph: dict,
        step_id: str,
        params: dict[str, Any] | None = None,
        previous_runner_id: str | None = None,
    ) -> None:
        """
        Execute a single step in a graph-based pipeline.

        This method:
        1. Creates a StepRun for tracking
        2. Creates a temporary Card + Job for the runner system
        3. Updates active_step_ids to track running steps
        4. Enqueues the job for a runner to pick up
        """
        steps_dict = graph.get("steps", {})
        step = steps_dict.get(step_id)
        if not step:
            logger.error(f"Step {step_id} not found in graph")
            return

        step_name = step.get("name", step_id)
        step_type = step.get("type", "script")
        step_config = step.get("config", {})

        # Get step index for legacy compatibility (use insertion order)
        step_ids = list(steps_dict.keys())
        step_index = step_ids.index(step_id) if step_id in step_ids else 0

        logger.info(f"Executing graph step {step_id}: {step_name} (type={step_type})")

        # Add to active steps
        active_ids = parse_json_list(pipeline_run.active_step_ids)
        if step_id not in active_ids:
            active_ids.append(step_id)
            pipeline_run.active_step_ids = json.dumps(active_ids)

        # Create the step run
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=pipeline_run.id,
            step_index=step_index,
            step_id=step_id,  # Graph step ID
            step_name=step_name,
            status=RunStatus.RUNNING.value,
            started_at=datetime.utcnow(),
        )
        db.add(step_run)
        await db.commit()
        await db.refresh(step_run)
        await db.refresh(pipeline_run)

        # Broadcast updates
        await manager.send_step_run_status(step_run_to_ws_dict(step_run))
        await manager.send_pipeline_run_status(pipeline_run_to_ws_dict(pipeline_run))

        # Create a temporary card for the job/runner infrastructure
        card_title = f"[Pipeline] {step_name}"
        card_description = f"Pipeline: {pipeline.name}\nStep: {step_name}"

        # For agent steps, use description from config
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
            step_run_id=step_run.id,
        )
        db.add(job)

        card.job_id = job_id
        card.branch_name = f"lazyaf/{job_id[:8]}"
        step_run.job_id = job_id

        await db.commit()

        # Queue the job
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
            pipeline_run_id=pipeline_run.id,
            step_id=step_id,
            step_index=step_index,
            step_name=step_name,
            required_runner_id=previous_runner_id,
        )
        await job_queue.enqueue(queued_job)

        logger.info(f"Enqueued job {job_id[:8]} for graph step {step_id}: {step_name}")

        # Broadcast job queued
        await manager.send_job_status({
            "id": job_id,
            "card_id": card.id,
            "status": "queued",
            "error": None,
            "started_at": None,
            "completed_at": None,
        })

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

        Creates a StepRun, creates a temporary Card + Job, and enqueues the job.

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

        For graph-based pipelines:
        - Updates completed_step_ids and active_step_ids
        - Finds all downstream edges based on success/failure
        - Triggers ready downstream steps (fan-out)
        - Handles fan-in by checking all upstream dependencies

        For legacy pipelines:
        - Uses sequential step execution with on_success/on_failure

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

        # Check if this is a graph-based pipeline
        graph = parse_steps_graph(pipeline.steps_graph)

        if graph and step_run.step_id:
            # Graph-based execution with parallel support
            await self._handle_graph_step_complete(
                db, pipeline_run, pipeline, repo, graph, step_run.step_id, step_success, runner_id
            )
        else:
            # Legacy sequential execution
            steps = parse_steps(pipeline.steps)
            if step_run.step_index >= len(steps):
                logger.error(f"Step index {step_run.step_index} out of range")
                return

            step = steps[step_run.step_index]
            action = step.get("on_success" if step_success else "on_failure", "stop" if not step_success else "next")
            await self._handle_action(db, pipeline_run, repo, steps, step_run.step_index, action, step_success, runner_id=runner_id)

    async def _handle_graph_step_complete(
        self,
        db: AsyncSession,
        pipeline_run: PipelineRun,
        pipeline: Pipeline,
        repo: Repo,
        graph: dict,
        completed_step_id: str,
        step_success: bool,
        runner_id: str | None = None,
    ) -> None:
        """
        Handle completion of a graph step with parallel execution support.

        This method:
        1. Updates completed_step_ids and active_step_ids
        2. Finds downstream edges based on success/failure condition
        3. For each downstream step, checks if all upstream dependencies are satisfied (fan-in)
        4. Executes ready downstream steps (fan-out)
        5. Completes pipeline when all steps are done
        """
        steps_dict = graph.get("steps", {})

        # Update tracking sets
        completed_ids = set(parse_json_list(pipeline_run.completed_step_ids))
        active_ids = set(parse_json_list(pipeline_run.active_step_ids))

        # Mark this step as completed
        completed_ids.add(completed_step_id)
        active_ids.discard(completed_step_id)

        pipeline_run.completed_step_ids = json.dumps(list(completed_ids))
        pipeline_run.active_step_ids = json.dumps(list(active_ids))
        await db.commit()
        await db.refresh(pipeline_run)

        logger.info(f"Graph step {completed_step_id} completed. Active: {list(active_ids)}, Completed: {list(completed_ids)}")

        # Find downstream edges based on the step result
        condition = "success" if step_success else "failure"
        downstream_edges = get_downstream_edges(graph, completed_step_id, condition)

        logger.info(f"Found {len(downstream_edges)} downstream edges for condition '{condition}'")

        # Track which steps are ready to execute
        steps_to_execute = []

        for edge in downstream_edges:
            next_step_id = edge.get("to_step")
            if not next_step_id or next_step_id not in steps_dict:
                continue

            # Skip if already completed or currently active
            if next_step_id in completed_ids or next_step_id in active_ids:
                logger.info(f"Skipping {next_step_id} - already completed or active")
                continue

            # Fan-in check: are ALL upstream dependencies satisfied?
            upstream_ids = get_upstream_step_ids(graph, next_step_id)

            if self._all_upstream_satisfied(graph, next_step_id, completed_ids):
                steps_to_execute.append(next_step_id)
                logger.info(f"Step {next_step_id} is ready (all {len(upstream_ids)} upstream deps satisfied)")
            else:
                logger.info(f"Step {next_step_id} waiting for more upstream deps. Has: {upstream_ids}, Completed: {completed_ids}")

        # Execute ready downstream steps (fan-out)
        for step_id in steps_to_execute:
            await self._execute_graph_step(
                db, pipeline_run, pipeline, repo, graph, step_id, None, runner_id
            )

        # Refresh to get latest state after executing new steps
        await db.refresh(pipeline_run)

        # Check if pipeline is complete
        # Complete when: no active steps AND (all steps completed OR we failed with no more to run)
        active_ids = set(parse_json_list(pipeline_run.active_step_ids))
        completed_ids = set(parse_json_list(pipeline_run.completed_step_ids))
        total_steps = count_total_steps(graph)

        if not active_ids:
            # No steps running - check if we're done
            if len(completed_ids) >= total_steps:
                # All steps completed
                all_passed = await self._check_all_steps_passed(db, pipeline_run)
                await self._complete_pipeline(db, pipeline_run, success=all_passed)
            elif not steps_to_execute:
                # No more steps can run (failed branch or dead end)
                # Pipeline is complete, but may have failed
                all_passed = await self._check_all_steps_passed(db, pipeline_run)
                await self._complete_pipeline(db, pipeline_run, success=all_passed)

    def _all_upstream_satisfied(
        self,
        graph: dict,
        step_id: str,
        completed_ids: set[str],
    ) -> bool:
        """
        Check if all upstream dependencies for a step are satisfied.

        A step can execute when ALL its incoming edges come from completed steps
        AND the edge conditions match (success edge requires success, etc).
        """
        edges = graph.get("edges", [])

        # Find all edges pointing to this step
        incoming_edges = [e for e in edges if e.get("to_step") == step_id]

        if not incoming_edges:
            # Entry point or no dependencies - can execute
            return True

        # Check if at least one edge's source is completed (OR semantic for multiple paths)
        # For fan-in, we need ALL sources to be completed
        for edge in incoming_edges:
            from_step = edge.get("from_step")
            if from_step not in completed_ids:
                return False

        return True

    async def _check_all_steps_passed(self, db: AsyncSession, pipeline_run: PipelineRun) -> bool:
        """Check if all completed step runs passed."""
        result = await db.execute(
            select(StepRun).where(StepRun.pipeline_run_id == pipeline_run.id)
        )
        step_runs = result.scalars().all()

        for sr in step_runs:
            if sr.status == RunStatus.FAILED.value:
                return False

        return True

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
pipeline_executor = PipelineExecutor()
