"""
Trigger service - matches events to pipeline triggers and starts runs.

Handles automatic pipeline triggering based on:
- Card completion (status changes to done/in_review)
- Git push events
"""

import json
import logging
from fnmatch import fnmatch
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, Repo, Card
from app.models.pipeline import PipelineRun

logger = logging.getLogger(__name__)


def parse_triggers(triggers_str: str | None) -> list[dict]:
    """Parse triggers from JSON string to list."""
    if not triggers_str:
        return []
    try:
        return json.loads(triggers_str)
    except (json.JSONDecodeError, TypeError):
        return []


class TriggerService:
    """Handles event-based pipeline triggering."""

    async def on_card_status_change(
        self,
        db: AsyncSession,
        card: Card,
        old_status: str,
        new_status: str,
    ) -> list[PipelineRun]:
        """
        Called when a card's status changes.

        Finds matching pipelines with card_complete triggers and starts runs.
        Passes the card's branch and commit info as trigger context.
        """
        logger.info(
            f"Card {card.id[:8]} status changed: {old_status} -> {new_status}, "
            f"checking for triggers"
        )

        # Find pipelines for this repo with card_complete triggers
        triggered_runs = []
        pipelines = await self._find_pipelines_for_repo(db, card.repo_id)

        for pipeline in pipelines:
            triggers = parse_triggers(pipeline.triggers)

            for trigger in triggers:
                if not trigger.get("enabled", True):
                    continue

                if trigger.get("type") != "card_complete":
                    continue

                config = trigger.get("config", {})
                target_status = config.get("status")

                # Check if this trigger matches the new status
                if target_status and target_status != new_status:
                    continue

                # Match! Start the pipeline with card context
                logger.info(
                    f"Trigger matched: pipeline '{pipeline.name}' triggered by "
                    f"card {card.id[:8]} reaching status '{new_status}'"
                )

                context = {
                    "branch": card.branch_name,
                    "card_id": card.id,
                    "card_title": card.title,
                    "repo_id": card.repo_id,
                    # Trigger actions to execute on pipeline completion
                    "on_pass": trigger.get("on_pass", "nothing"),
                    "on_fail": trigger.get("on_fail", "nothing"),
                }

                # Get commit SHA if we have a branch
                if card.branch_name:
                    from app.services.git_server import git_repo_manager
                    try:
                        commit_sha = git_repo_manager.get_branch_commit(
                            card.repo_id, card.branch_name
                        )
                        context["commit_sha"] = commit_sha
                    except Exception as e:
                        logger.warning(f"Could not get commit SHA: {e}")

                run = await self._start_pipeline(
                    db=db,
                    pipeline=pipeline,
                    trigger_type="card",
                    trigger_ref=card.id,
                    trigger_context=context,
                )
                if run:
                    triggered_runs.append(run)

        return triggered_runs

    async def on_push(
        self,
        db: AsyncSession,
        repo_id: str,
        branch: str,
        commit_sha: str,
        old_sha: str | None = None,
    ) -> list[PipelineRun]:
        """
        Called when a push is received to the internal git server.

        Finds matching pipelines with push triggers and starts runs.
        """
        logger.info(
            f"Push received: repo {repo_id[:8]}, branch {branch}, "
            f"commit {commit_sha[:8] if commit_sha else 'unknown'}"
        )

        triggered_runs = []
        pipelines = await self._find_pipelines_for_repo(db, repo_id)

        for pipeline in pipelines:
            triggers = parse_triggers(pipeline.triggers)

            for trigger in triggers:
                if not trigger.get("enabled", True):
                    continue

                if trigger.get("type") != "push":
                    continue

                config = trigger.get("config", {})
                branch_patterns = config.get("branches", [])

                # If no branches specified, match all
                if branch_patterns:
                    matched = False
                    for pattern in branch_patterns:
                        if fnmatch(branch, pattern):
                            matched = True
                            break
                    if not matched:
                        continue

                # Match! Start the pipeline with push context
                logger.info(
                    f"Trigger matched: pipeline '{pipeline.name}' triggered by "
                    f"push to branch '{branch}'"
                )

                context = {
                    "branch": branch,
                    "commit_sha": commit_sha,
                    "old_sha": old_sha,
                    "push_ref": f"refs/heads/{branch}",
                }

                run = await self._start_pipeline(
                    db=db,
                    pipeline=pipeline,
                    trigger_type="push",
                    trigger_ref=f"{branch}:{commit_sha[:8] if commit_sha else 'unknown'}",
                    trigger_context=context,
                )
                if run:
                    triggered_runs.append(run)

        return triggered_runs

    async def _find_pipelines_for_repo(
        self,
        db: AsyncSession,
        repo_id: str,
    ) -> list[Pipeline]:
        """Find all pipelines for a repo."""
        result = await db.execute(
            select(Pipeline).where(Pipeline.repo_id == repo_id)
        )
        return list(result.scalars().all())

    async def _start_pipeline(
        self,
        db: AsyncSession,
        pipeline: Pipeline,
        trigger_type: str,
        trigger_ref: str,
        trigger_context: dict[str, Any],
    ) -> PipelineRun | None:
        """Start a pipeline run with the given trigger context."""
        # Get repo
        result = await db.execute(
            select(Repo).where(Repo.id == pipeline.repo_id)
        )
        repo = result.scalar_one_or_none()
        if not repo:
            logger.error(f"Repo not found for pipeline {pipeline.id}")
            return None

        if not repo.is_ingested:
            logger.warning(
                f"Repo {repo.id[:8]} not ingested, skipping pipeline trigger"
            )
            return None

        # Import here to avoid circular imports
        from app.services.pipeline_executor import pipeline_executor

        try:
            run = await pipeline_executor.start_pipeline(
                db=db,
                pipeline=pipeline,
                repo=repo,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
                trigger_context=trigger_context,
            )
            logger.info(
                f"Started pipeline run {run.id[:8]} for '{pipeline.name}' "
                f"(trigger: {trigger_type})"
            )
            return run
        except Exception as e:
            logger.error(f"Failed to start pipeline {pipeline.name}: {e}")
            return None


# Global instance
trigger_service = TriggerService()
