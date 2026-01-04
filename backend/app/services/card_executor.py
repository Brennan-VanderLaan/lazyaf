"""
Card Executor Service (Phase 12.5).

Handles local execution of standalone cards (not in pipelines) via LocalExecutor.
This bypasses the job queue when LAZYAF_USE_LOCAL_EXECUTOR=1.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Repo, Job
from app.services.websocket import manager
from app.services.execution.config_builder import build_execution_config
from app.services.execution.local_executor import (
    LocalExecutor,
    ExecutionResult,
)
from app.services.execution.idempotency import ExecutionKey

logger = logging.getLogger(__name__)

# Check if local executor is enabled
_use_local_executor = os.environ.get("LAZYAF_USE_LOCAL_EXECUTOR", "0") == "1"


def is_local_executor_enabled() -> bool:
    """Check if local executor is enabled."""
    return _use_local_executor


# Singleton executor instance
_local_executor: Optional[LocalExecutor] = None


def get_local_executor() -> LocalExecutor:
    """Get or create the LocalExecutor singleton."""
    global _local_executor
    if _local_executor is None:
        _local_executor = LocalExecutor()
    return _local_executor


def card_to_ws_dict(card: Card) -> dict:
    """Convert a Card model to a dict for websocket broadcast."""
    import json

    def parse_json(s):
        if not s:
            return None
        try:
            return json.loads(s)
        except:
            return None

    return {
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "runner_type": card.runner_type,
        "step_type": card.step_type,
        "step_config": parse_json(card.step_config),
        "prompt_template": card.prompt_template,
        "agent_file_ids": parse_json(card.agent_file_ids),
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
        "completed_runner_type": card.completed_runner_type,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


async def execute_card_locally(
    card: Card,
    repo: Repo,
    job: Job,
    db: AsyncSession,
    agent_file_ids: Optional[list[str]] = None,
    prompt_template: Optional[str] = None,
) -> None:
    """
    Execute a card locally via LocalExecutor.

    This is the fast path for agent step execution without polling.
    Runs in a background task so the HTTP request can return immediately.

    Args:
        card: The card to execute
        repo: The repository for the card
        job: The job record for tracking
        db: Database session
        agent_file_ids: Optional list of agent file IDs
        prompt_template: Optional custom prompt template
    """
    logger.info(f"Executing card {card.id} locally via LocalExecutor")

    # Parse step config
    import json
    step_config = {}
    if card.step_config:
        try:
            step_config = json.loads(card.step_config)
        except:
            pass

    # Build agent config
    backend_url = "http://host.docker.internal:8000"
    agent_config = {
        "runner_type": card.runner_type or "claude-code",
        "title": card.title,
        "description": card.description,
        "model": step_config.get("model"),
        "agent_file_ids": agent_file_ids or [],
        "prompt_template": prompt_template,
        "previous_step_logs": None,  # No previous step for standalone cards
        "repo_url": repo.get_internal_git_url(backend_url),
        "branch_name": f"lazyaf/{job.id[:8]}",
        "base_branch": repo.default_branch,
        "is_continuation": False,
        "pipeline_run_id": None,  # Not a pipeline
    }

    # Merge step_config fields
    step_config.update({
        "runner_type": card.runner_type or "claude-code",
        "title": card.title,
        "description": card.description,
    })

    # Use a temporary workspace for standalone cards
    workspace_path = f"/tmp/lazyaf-card-{card.id}"

    try:
        # Build execution config
        exec_config = build_execution_config(
            step_type=card.step_type,
            step_config=step_config,
            workspace_path=workspace_path,
            timeout_seconds=3600,  # 1 hour default
            use_control_layer=True,
            backend_url=backend_url,
            agent_config=agent_config,
        )

        # Create execution key
        exec_key = ExecutionKey(
            pipeline_run_id=f"card-{card.id}",  # Use card ID as pseudo pipeline
            step_index=0,
            attempt=1,
        )

        # Update job status to running
        job.status = "running"
        job.started_at = datetime.utcnow()
        await db.commit()

        # Broadcast running status
        await manager.send_job_status({
            "id": job.id,
            "card_id": card.id,
            "status": "running",
            "error": None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": None,
        })

        # Execute via LocalExecutor
        executor = get_local_executor()
        logs_buffer = []

        async for item in executor.execute_step(exec_key, exec_config):
            if isinstance(item, str):
                logs_buffer.append(item)
                logger.debug(f"[card {card.id[:8]}] {item}")
            elif isinstance(item, ExecutionResult):
                # Execution complete
                success = item.success

                # Update job
                job.status = "completed" if success else "failed"
                job.completed_at = datetime.utcnow()
                job.logs = "\n".join(logs_buffer)
                if not success:
                    job.error = item.error or f"Exit code: {item.exit_code}"

                # Update card
                if success:
                    card.status = "in_review"
                    card.completed_runner_type = card.runner_type
                else:
                    card.status = "failed"

                await db.commit()
                await db.refresh(card)
                await db.refresh(job)

                # Broadcast completion
                await manager.send_job_status({
                    "id": job.id,
                    "card_id": card.id,
                    "status": job.status,
                    "error": job.error,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                })
                await manager.send_card_updated(card_to_ws_dict(card))

                logger.info(f"Card {card.id[:8]} completed: {'success' if success else 'failed'}")
                return

    except Exception as e:
        logger.error(f"Local execution failed for card {card.id}: {e}")

        # Update job as failed
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.error = str(e)

        # Update card as failed
        card.status = "failed"

        await db.commit()
        await db.refresh(card)
        await db.refresh(job)

        # Broadcast failure
        await manager.send_job_status({
            "id": job.id,
            "card_id": card.id,
            "status": "failed",
            "error": str(e),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })
        await manager.send_card_updated(card_to_ws_dict(card))


async def start_card_locally(
    card: Card,
    repo: Repo,
    job: Job,
    db: AsyncSession,
    agent_file_ids: Optional[list[str]] = None,
    prompt_template: Optional[str] = None,
) -> None:
    """
    Start card execution as a background task.

    This allows the HTTP request to return immediately while
    execution continues in the background.
    """
    # Create background task for execution
    asyncio.create_task(
        execute_card_locally(
            card=card,
            repo=repo,
            job=job,
            db=db,
            agent_file_ids=agent_file_ids,
            prompt_template=prompt_template,
        )
    )
