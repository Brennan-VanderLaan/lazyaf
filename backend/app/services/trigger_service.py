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
from uuid import uuid4

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, Repo, Card
from app.models.pipeline import PipelineRun
from app.schemas.lazyaf_yaml import PipelineYaml, pipeline_yaml_to_graph
from app.schemas.pipeline import ArrayConversionError
from app.services.workspace.trigger_dedup import (
    TriggerDeduplicator,
    generate_trigger_key,
)

logger = logging.getLogger(__name__)

# Name pattern for platform Pipeline rows materialized from .lazyaf/pipelines/
# yaml files (shared with the manual run-by-name endpoint in lazyaf_files).
MATERIALIZED_PIPELINE_PREFIX = "[repo] "

# Repo directory that holds pipeline definition yaml files.
PIPELINES_DIR = ".lazyaf/pipelines"

# Duplicate push events for the same (pipeline, branch, sha) inside this
# window produce exactly one run (two rapid pushes = one run).
PUSH_DEDUP_WINDOW_SECONDS = 10.0

# Dedup records older than this are evicted opportunistically on each push
# so the process-local dict cannot grow without bound.
DEDUP_RECORD_MAX_AGE_SECONDS = 600.0

# Process-local by design (trigger_dedup keeps no DB state). Module-level so
# the test-mode API and tests can import and reset it.
trigger_deduplicator = TriggerDeduplicator()


def reset_trigger_dedup() -> None:
    """Reset hook: clear process-local trigger dedup state (test-mode API)."""
    trigger_deduplicator.clear()


def parse_triggers(triggers_str: str | None) -> list[dict]:
    """Parse triggers from JSON string to list."""
    if not triggers_str:
        return []
    try:
        return json.loads(triggers_str)
    except (json.JSONDecodeError, TypeError):
        return []


def materialized_pipeline_name(yaml_name: str) -> str:
    """Platform pipeline name for a repo-defined pipeline yaml."""
    return f"{MATERIALIZED_PIPELINE_PREFIX}{yaml_name}"


def describe_conversion_refusal(exc: Exception) -> str:
    """The `definition_error` text for a refused conversion.

    `ArrayConversionError` already carries a list of reasons, each naming the
    step it blames. A bare pydantic `ValidationError` can still reach here if
    the graph models and `describe_terminal_action` ever drift (12.8 §4.2
    constructs `StepActions` rather than appending into it precisely so that
    drift is loud), so it is rendered rather than str()'d - a pydantic repr
    pasted into a UI badge is unreadable.
    """
    if isinstance(exc, ArrayConversionError):
        return "; ".join(exc.reasons)
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<pipeline>'}: "
            f"{error['msg']}"
            for error in exc.errors()
        )
    return str(exc)


async def upsert_materialized_pipeline(
    db: AsyncSession,
    repo_id: str,
    pipeline_yaml: PipelineYaml,
) -> Pipeline:
    """
    Create or refresh the materialized platform Pipeline row for a
    repo-defined pipeline yaml. The yaml is the source of truth for these
    rows: description, the execution GRAPH and triggers are overwritten.
    Does not commit.

    The array in the yaml is converted to a graph HERE, at the boundary
    (12.8 §4.4): the executor runs graphs and only graphs, and the file stays
    an array because that is the authoring format. `Pipeline.steps` is no
    longer written - it is dead weight from here until the column is dropped.

    A conversion that REFUSES is recorded on `Pipeline.definition_error` and
    the previous graph is LEFT IN PLACE, unruntime-able: both run guards
    (`POST /api/pipelines/{id}/run` and `run_repo_pipeline`) read the field
    and refuse, so a broken CI file cannot silently re-run yesterday's
    definition under today's name. It also does not raise: `sync_repo_pipelines`
    calls this OUTSIDE its per-file `except Exception: continue`, under an
    `except Exception: rollback; raise`, so one unconvertible file raising
    here would discard every other pipeline the same push synced.
    """
    result = await db.execute(
        select(Pipeline)
        .where(Pipeline.repo_id == repo_id)
        .where(Pipeline.name == materialized_pipeline_name(pipeline_yaml.name))
    )
    pipeline = result.scalar_one_or_none()

    triggers_json = json.dumps([t.model_dump() for t in pipeline_yaml.triggers])

    steps_graph_json: str | None = None
    definition_error: str | None = None
    try:
        steps_graph_json = pipeline_yaml_to_graph(pipeline_yaml).model_dump_json()
    except (ArrayConversionError, ValidationError) as exc:
        definition_error = describe_conversion_refusal(exc)
        logger.warning(
            "Pipeline yaml %r in repo %s cannot be converted to a graph: %s",
            pipeline_yaml.name,
            repo_id[:8],
            definition_error,
        )

    if pipeline:
        pipeline.description = pipeline_yaml.description
        pipeline.triggers = triggers_json
        pipeline.definition_error = definition_error
        if definition_error is None:
            # The yaml is the source of truth for a materialized row, so a
            # successful sync always replaces the graph - including one a UI
            # edit put there, which would otherwise shadow the file forever.
            pipeline.steps_graph = steps_graph_json
    else:
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo_id,
            name=materialized_pipeline_name(pipeline_yaml.name),
            description=pipeline_yaml.description,
            steps_graph=steps_graph_json,
            definition_error=definition_error,
            triggers=triggers_json,
            is_template=False,
        )
        db.add(pipeline)

    return pipeline


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

    async def sync_repo_pipelines(
        self,
        repo: Repo,
        branch: str,
        commit_sha: str,
        old_sha: str | None = None,
    ) -> list[Pipeline]:
        """
        Re-read .lazyaf/pipelines/ at the pushed commit and create/refresh the
        materialized platform Pipeline rows (description, steps, triggers).

        Runs BEFORE trigger matching so CI changes take effect on the push
        that introduces them.

        Only syncs on pushes to the repo's default branch: the CI definition
        follows the trunk. Feature-branch pushes still match triggers, but
        against the trunk's materialized definition.

        A yaml file truly ABSENT from the pushed tree clears the triggers on
        its materialized row; the row itself is kept because run history
        hangs off it. A yaml that exists but is empty or unparseable keeps
        the row's previous definition AND triggers.

        Runs in its OWN database session (from app.database's sessionmaker),
        fully atomically: all upserts plus the trigger-clear sweep commit
        once at the end, and any error rolls the whole sync back and
        re-raises - the caller's session is never touched, so a failed sync
        cannot poison it.

        When old_sha is a real commit and the .lazyaf/pipelines subtree is
        identical at old_sha and commit_sha, the sync short-circuits (the
        push cannot have changed any pipeline definition).

        Returns the pipelines synced from yaml files.
        """
        if not repo.default_branch or branch != repo.default_branch:
            return []

        # Import here to keep the singleton swappable in tests (matches the
        # existing lazy-import style in this module)
        from app.services.git_server import git_repo_manager
        from starlette.concurrency import run_in_threadpool

        repo_id = repo.id

        def _read_pipeline_files() -> tuple[str, dict[str, bytes | None] | None]:
            """Synchronous dulwich reads, run in a threadpool so the event
            loop never blocks inside the push handler.

            Returns ("unchanged"|"unknown", None) or ("ok", {filename: bytes|None}).
            """
            # Short-circuit: identical .lazyaf/pipelines subtree at old and
            # new sha means the push cannot have changed any definition. An
            # empty or all-zeros old_sha (new branch / synthetic event) gives
            # no "before" tree, so no short-circuit.
            if old_sha and set(old_sha) != {"0"}:
                old_tree = git_repo_manager.get_tree_sha_at_commit(
                    repo_id, old_sha, PIPELINES_DIR
                )
                new_tree = git_repo_manager.get_tree_sha_at_commit(
                    repo_id, commit_sha, PIPELINES_DIR
                )
                if (
                    old_tree is not None
                    and new_tree is not None
                    and old_tree == new_tree
                ):
                    return ("unchanged", None)

            filenames = git_repo_manager.list_directory_at_commit(
                repo_id, commit_sha, PIPELINES_DIR
            )
            if filenames is None:
                return ("unknown", None)

            files: dict[str, bytes | None] = {}
            for filename in sorted(filenames):
                if not (filename.endswith('.yaml') or filename.endswith('.yml')):
                    continue
                files[filename] = git_repo_manager.get_file_content_at_commit(
                    repo_id, commit_sha, f"{PIPELINES_DIR}/{filename}"
                )
            return ("ok", files)

        status, files = await run_in_threadpool(_read_pipeline_files)

        if status == "unchanged":
            logger.debug(
                f"{PIPELINES_DIR} unchanged by push to repo {repo_id[:8]}; "
                f"skipping pipeline definition sync"
            )
            return []
        if status == "unknown":
            # Repo/commit unreadable (e.g. a synthetic push event): the tree
            # is unknown, so leave the materialized rows untouched.
            logger.warning(
                f"Cannot read {PIPELINES_DIR} at commit "
                f"{commit_sha[:8] if commit_sha else 'unknown'} for repo "
                f"{repo_id[:8]}; skipping pipeline definition sync"
            )
            return []

        # Own session, read at call time so tests can swap the sessionmaker
        from app.database import async_session

        synced: list[Pipeline] = []
        seen_names: set[str] = set()

        async with async_session() as session:
            try:
                for filename, content in files.items():
                    # Every file here EXISTS in the pushed tree. A file that
                    # is empty or unparseable must keep its materialized
                    # row's previous definition AND triggers - only files
                    # truly absent from the tree may lead to trigger
                    # clearing in the sweep below.
                    stem = filename.rsplit('.', 1)[0]

                    if not content:
                        logger.warning(
                            f"Pipeline yaml '{filename}' in repo {repo_id[:8]} "
                            f"is empty; keeping its previous materialized "
                            f"definition"
                        )
                        seen_names.add(materialized_pipeline_name(stem))
                        continue

                    data = None
                    try:
                        data = yaml.safe_load(content.decode('utf-8'))
                        pipeline_yaml = PipelineYaml(**data)
                    except Exception as e:
                        # A broken CI file must not break the push; skip it
                        # (an existing materialized row keeps its previous
                        # definition and triggers).
                        logger.warning(
                            f"Skipping invalid pipeline yaml '{filename}' in "
                            f"repo {repo_id[:8]}: {e}"
                        )
                        # Best-effort names for the broken file: its stem
                        # (files are conventionally named after the pipeline)
                        # and, when readable, its declared name.
                        seen_names.add(materialized_pipeline_name(stem))
                        if isinstance(data, dict) and isinstance(data.get("name"), str):
                            seen_names.add(materialized_pipeline_name(data["name"]))
                        continue

                    pipeline = await upsert_materialized_pipeline(
                        session, repo_id, pipeline_yaml
                    )
                    seen_names.add(pipeline.name)
                    synced.append(pipeline)

                # Clear triggers on materialized rows whose yaml no longer
                # exists in the pushed tree
                result = await session.execute(
                    select(Pipeline)
                    .where(Pipeline.repo_id == repo_id)
                    .where(Pipeline.name.startswith(MATERIALIZED_PIPELINE_PREFIX))
                )
                for pipeline in result.scalars().all():
                    if pipeline.name not in seen_names and parse_triggers(pipeline.triggers):
                        logger.info(
                            f"Repo pipeline yaml for '{pipeline.name}' removed; "
                            f"clearing its triggers"
                        )
                        pipeline.triggers = "[]"

                await session.commit()
            except Exception:
                # Atomic: nothing from this sync survives a failure. The
                # caller (on_push) logs and continues with trigger matching.
                await session.rollback()
                raise

        return synced

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

        First syncs repo-defined pipeline definitions from the pushed commit,
        then finds matching pipelines with push triggers and starts runs.
        """
        logger.info(
            f"Push received: repo {repo_id[:8]}, branch {branch}, "
            f"commit {commit_sha[:8] if commit_sha else 'unknown'}"
        )

        # Cheap periodic eviction: keep the process-local dedup dict from
        # growing without bound
        await trigger_deduplicator.cleanup(
            max_age_seconds=DEDUP_RECORD_MAX_AGE_SECONDS
        )

        # Sync repo-defined pipelines BEFORE trigger matching so a push that
        # adds/changes .lazyaf/pipelines/*.yaml is gated by its own definition
        result = await db.execute(select(Repo).where(Repo.id == repo_id))
        repo = result.scalar_one_or_none()
        if repo and commit_sha:
            try:
                # Runs in its own session: a sync failure cannot poison `db`,
                # and trigger matching below always proceeds
                await self.sync_repo_pipelines(
                    repo, branch, commit_sha, old_sha=old_sha
                )
            except Exception as e:
                # Trigger matching must still run even if sync fails
                logger.error(f"Pipeline definition sync failed: {e}")

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

                # Deduplicate: the same (pipeline, branch, sha) within the
                # window starts exactly one run, no matter how many push
                # events arrive for it
                dedup_key = generate_trigger_key(
                    "push", repo_id, f"{pipeline.id}:{branch}:{commit_sha}"
                )
                if not await trigger_deduplicator.should_trigger(
                    dedup_key, PUSH_DEDUP_WINDOW_SECONDS
                ):
                    logger.info(
                        f"Deduplicated push trigger for pipeline "
                        f"'{pipeline.name}' ({branch}@"
                        f"{commit_sha[:8] if commit_sha else 'unknown'})"
                    )
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

                try:
                    run = await self._start_pipeline(
                        db=db,
                        pipeline=pipeline,
                        trigger_type="push",
                        trigger_ref=f"{branch}:{commit_sha[:8] if commit_sha else 'unknown'}",
                        trigger_context=context,
                    )
                except Exception:
                    # A failed start must not burn the dedup key: release it
                    # so a retry push inside the window can still fire
                    trigger_deduplicator.release(dedup_key)
                    raise

                if run:
                    await trigger_deduplicator.record_trigger(dedup_key, run.id)
                    triggered_runs.append(run)
                else:
                    # No run was started (repo missing/not ingested, or the
                    # executor failed): free the key for a retry push
                    trigger_deduplicator.release(dedup_key)

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
