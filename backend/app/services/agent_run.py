"""
Ad-hoc agent runs - Phase 12.5.

Card work and the playground are agent execution, and since 12.5 agent
execution happens in an ephemeral control-mode container driven by the
LocalExecutor. But the control endpoints are structurally anchored to a
PipelineRun: ``StepExecution.step_run_id`` is a hard FK to ``StepRun``,
which is a hard FK to ``PipelineRun``, and the workspace service is keyed by
``pipeline_run_id``. There is therefore no "just call the executor" seam - a
caller that wants a workspace, a StepRun, a StepExecution, control mode,
streamed logs, test-result tie-back and a StepUsage row has to have a run.

So ad-hoc agent work GETS a run: one ephemeral (hidden) ``Pipeline`` row with
a single agent step, plus a real, visible ``PipelineRun``. That is not a
workaround - Milestone 13's ``TrialIteration`` already specifies
``pipeline_run_id  # each iteration IS a visible pipeline run``. Ad-hoc agent
work becoming a first-class run is the shape M13 needs.

What this buys, all for free and with no second implementation of any of it:

  workspace volume, branch/commit checkout, StepRun row, StepExecution row +
  step JWT, control mode, POST /api/steps/{id}/logs -> StepRun.logs + WS
  frames, POST /api/steps/{id}/test-results, POST /api/steps/{id}/usage, the
  hard-deadline watchdog, and cancellation.

Completion routes off PERSISTED columns (``PipelineRun.trigger_type`` /
``trigger_ref``), never an in-memory registry: a backend restart mid-run must
not orphan a card in ``in_progress`` forever. ``on_run_complete`` is the one
hook, called from ``pipeline_executor._complete_pipeline`` (cross-agent
contract #7), and it no-ops on every other trigger type.

Ownership notes (R3: one writer per datum)
------------------------------------------
- ``Job`` rows for card work are written HERE, from the run's state. The
  cards router still CREATES the Job row (``card.job_id`` and the
  ``lazyaf/{job_id[:8]}`` branch name are load-bearing for the existing UI
  and the jobs API); this module owns every subsequent status transition.
- There is no runner queue left to fall back to (12.6 deleted it). The
  ad-hoc agent path runs on the control layer, and
  ``tdd/unit/services/test_no_legacy_code.py`` asserts the removed
  modules stay removed - unconditionally, with no importorskip that a
  later deletion could disarm.
- The card's TEST GATE lives here too. A card reaches ``in_review`` only
  when the agent step succeeded AND every test tied back to the run is
  green; a red suite lands the card ``failed`` and fires no ``card_complete``
  trigger. Evidence is the persisted ``TestRun`` rows of the run (12.2.6
  tie-back), never an in-memory handoff - see ``run_test_summary``.
- ``Job.logs``, ``Job.tests_*`` and ``Job.test_output`` are mirrored off the
  run at completion, because the card modal reads the JOB row, not the
  StepRun. ``StepRun.logs`` stays the single writer; this is a read-only
  copy taken once, at the end.

The card gate and the executor's step gate are two scopes of one rule
--------------------------------------------------------------------
``pipeline_executor._finish_local_step_locked`` demotes a STEP that reported
failing tests, keyed on ``TestRun.step_run_id``. That covers the agent step's
own suite run. The gate here is keyed on ``TestRun.pipeline_run_id``, so it
also covers a POST-AGENT VERIFICATION STEP of the same ad-hoc run, whose
results land under its own step run and are therefore invisible to the agent
step's gate - that run passes, and only the card-scoped read holds the card.
Both read the same rows written by the same endpoint; neither is a fallback
for the other.

The agent step-config vocabulary written here is the SAME vocabulary a user
writes in ``.lazyaf/pipelines/*.yaml`` (see the ``mock-agent`` step in
``.lazyaf/pipelines/test-suite.yaml``) and is consumed by
``pipeline_executor._build_local_execution_config`` -> the agent payload in
``/workspace/.control/agent.<step_execution_id>.json``. Keys:

    agent           "claude-code" | "gemini" | "mock"   (required)
    model           model id passed to the CLI            (optional)
    task            the work, as prose                    (optional)
    title           card/session title                    (optional)
    description     long-form task description            (optional)
    prompt_template custom prompt template                (optional)
    agent_file_ids  resolved agent files -> --agents      (optional)
    base_branch     branch the workspace is cloned at     (optional)
    branch          branch the agent commits/pushes to    (optional)
    commit          bool, or {enabled, message, push,     (default true)
                    allow_empty}: whether/how to land it
    card_id         the card this work belongs to         (optional)
    mock_config     deterministic MockExecutor script     (optional)
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.schemas._datetime import utc_isoformat
from app.schemas.pipeline import (
    EdgeCondition,
    PipelineEdge,
    PipelineGraphModel,
    PipelineNodePosition,
    PipelineStepV2,
)
from app.models import (
    Card,
    Job,
    Pipeline,
    PipelineRun,
    Repo,
    RunStatus,
    StepRun,
    TestRef,
    TestRun,
    TestRunStatus,
)

logger = logging.getLogger(__name__)


# Ephemeral pipelines created for ad-hoc agent work are named with this
# prefix and hidden from GET /api/pipelines (the RUNS stay visible - that is
# the point). They cascade-delete with their runs.
ADHOC_PREFIX = "__lazyaf_adhoc__"

# PipelineRun.trigger_type values this module owns (cross-agent contract #7).
TRIGGER_CARD_WORK = "card_work"
TRIGGER_PLAYGROUND = "playground"
# 12.6.5: one experiment cell is an ad-hoc agent run like any other; the
# literal is defined in app.models.experiment and mirrored here so this
# module keeps no import-time dependency on the experiment package.
TRIGGER_EXPERIMENT = "experiment"
ADHOC_TRIGGER_TYPES = (TRIGGER_CARD_WORK, TRIGGER_PLAYGROUND, TRIGGER_EXPERIMENT)

# An agent step is not a script step: 300s is a rounding error for an agent.
DEFAULT_AGENT_TIMEOUT = 1800

# Card.runner_type / playground runner_type -> the 12.5 agent vocabulary
# (cross-agent contract #5). "any" resolves to claude-code: the legacy queue
# let any registered runner take the job, but a local agent step must name
# one CLI at dispatch, and claude-code is the platform default.
# M14 adds `openai-harness`. `schemas/experiment.AGENT_VOCABULARY` derives
# from this dict, so cards, the playground AND the 12.6.5 experiment matrix
# all inherit the new agent from this one line - which is why a self-hosted
# matrix cell (`{"agent": "openai-harness", "model": "endpoint:local-4090"}`)
# needs no schema change anywhere.
AGENT_BY_RUNNER_TYPE = {
    "any": "claude-code",
    "claude-code": "claude-code",
    "gemini": "gemini",
    "mock": "mock",
    "openai-harness": "openai-harness",
}
DEFAULT_AGENT = "claude-code"

# PipelineRun.trigger_type for the one-step run that probes a `runner-local`
# endpoint from the network position the real step will occupy (M14 s2.3).
TRIGGER_ENDPOINT_PROBE = "endpoint_probe"

# What that run executes. A module of the tested runner-common package, exactly
# like the agent wrapper - the operator never writes it.
ENDPOINT_PROBE_COMMAND = "python3 -m runner_common.endpoint_probe"

# Its budget. Four HTTP requests with a 60s total cap, plus container start on
# someone else's hardware.
ENDPOINT_PROBE_TIMEOUT = 120

# Step-config keys this module writes itself; anything else on a card's
# step_config is passed through untouched (forward compatibility with keys
# C's dispatch grows later).
_RESERVED_STEP_CONFIG_KEYS = frozenset(
    {
        "agent",
        "model",
        "task",
        "title",
        "description",
        "prompt_template",
        "agent_file_ids",
        "base_branch",
        "branch",
        "commit",
        "card_id",
        "mock_config",
    }
)


# A run is still LIVE while it holds one of these statuses. Everything else
# is terminal.
#
# This matters because ``pipeline_executor.start_pipeline`` can complete a
# run SYNCHRONOUSLY, before it ever returns: an image-preflight failure, an
# empty step list and a graph with no entry points all call
# ``_complete_pipeline`` inline - which calls ``on_run_complete`` inline,
# which already landed the card/session in its terminal state. A caller that
# unconditionally writes "running" afterwards resurrects a run that already
# failed: the card twin reads in_progress forever, the playground SSE stream
# (which only terminates on a terminal session status) pings until the
# 30-minute TTL, and the observer it registered leaks for that long too.
LIVE_RUN_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


async def run_status(db: AsyncSession, run_id: str) -> str | None:
    """Re-read a run's status from the DATABASE, not the identity map.

    A column select cannot be served from the session's identity map, so
    this sees a status another session committed - which is exactly the
    case that matters (the step task owns its own session).
    """
    result = await db.execute(
        select(PipelineRun.status).where(PipelineRun.id == run_id)
    )
    return result.scalar_one_or_none()


async def run_is_live(db: AsyncSession, run_id: str) -> bool:
    """True while a run can still be transitioned to "running".

    ``None`` (the row is gone) counts as NOT live: there is nothing left to
    report progress about.
    """
    return await run_status(db, run_id) in LIVE_RUN_STATUSES


def resolve_agent(runner_type: str | None) -> str:
    """Map a card/session runner_type onto the agent vocabulary.

    An unknown value resolves to the default agent rather than raising: the
    value comes from a user-editable card field, and refusing to start work
    over a typo in a field that used to mean "any runner will do" is worse
    than running the default and saying so.
    """
    if not runner_type:
        return DEFAULT_AGENT
    agent = AGENT_BY_RUNNER_TYPE.get(runner_type)
    if agent is None:
        logger.warning(
            "Unknown runner_type %r on an agent run - falling back to %r. "
            "Known values: %s",
            runner_type,
            DEFAULT_AGENT,
            ", ".join(sorted(AGENT_BY_RUNNER_TYPE)),
        )
        return DEFAULT_AGENT
    return agent


def adhoc_pipeline_name(trigger_type: str, trigger_ref: str) -> str:
    """Name for the hidden Pipeline row backing one ad-hoc run."""
    return f"{ADHOC_PREFIX}:{trigger_type}:{(trigger_ref or 'anon')[:8]}"


def is_adhoc_pipeline_name(name: str | None) -> bool:
    """True for the hidden ephemeral pipelines ad-hoc runs create.

    Used by the pipelines list endpoints so ad-hoc rows never clutter the
    pipeline list. Matching is on the prefix, not on an exact name, so the
    filter cannot drift from the writer above.
    """
    return bool(name) and name.startswith(ADHOC_PREFIX)


def adhoc_steps_graph(steps: list[dict[str, Any]]) -> str:
    """The v2 graph JSON for an ad-hoc pipeline's LINEAR chain of steps.

    The single writer of the ad-hoc pipeline DEFINITION (R3). Every ad-hoc
    pipeline the product creates - card work, a playground session, an
    endpoint probe, an experiment cell - is a chain of one or two steps that
    runs in order and stops on the first failure, so this takes the step
    dicts those callers already build and returns the graph the executor
    runs.

    A graph and not the v1 array, because 12.8 retires the array and these
    callers are AUTHORS, not an authoring EDGE: nobody hand-writes an
    ephemeral pipeline, so there is no v1 text left to keep accepting here.
    Nothing is lost in the move. The two v1 flow keys these sites carried
    said exactly what the edges below say and nothing more:

      * ``on_success: "next"`` IS the SUCCESS edge to the following step -
        and on the LAST step it was already a no-op, because
        ``_execute_step`` guarded its continuation with
        ``current_step + 1 < len(steps)``.
      * ``on_failure: "stop"`` IS the absence of a failure edge.

    And no ad-hoc writer has ever emitted an EFFECT (``merge:`` /
    ``trigger:``), so no node built here carries an ``actions`` entry. Worth
    saying explicitly because it is the thing a reader will fear was
    dropped: a card's auto-merge does NOT ride on a step action. It is the
    human ``POST /api/cards/{id}/approve`` calling
    ``git_repo_manager.merge_branch`` directly, plus the run-level
    ``TriggerConfig.on_pass`` action that ``_complete_pipeline`` reads off
    ``PipelineRun.trigger_context`` - and an ad-hoc run writes no ``on_pass``
    into that context and ``triggers="[]"`` onto the row. Neither path has
    ever looked at a step's ``on_success``.

    Built THROUGH ``PipelineGraphModel`` rather than as a hand-rolled dict so
    a definition this module writes clears the same validator a hand-authored
    graph clears at the API boundary (R6): a bad step type or a dangling edge
    raises here, at the writer, instead of becoming a run that cannot
    dispatch.

    Args:
        steps: step dicts carrying ``id``, ``name``, ``type``, ``config`` and
            ``timeout``, in execution order. The first is the entry point.

    Returns:
        JSON for ``Pipeline.steps_graph``.

    Raises:
        ValueError: on an empty list or duplicate step ids. Duplicates
            matter because the graph is keyed BY id - two steps sharing one
            would silently collapse into a pipeline with a step missing.
    """
    if not steps:
        raise ValueError(
            "an ad-hoc pipeline needs at least one step: a graph must have an "
            "entry point, so there is no such thing as an empty definition"
        )

    ids = [step["id"] for step in steps]
    if len(set(ids)) != len(ids):
        raise ValueError(
            f"ad-hoc pipeline step ids must be unique (the graph is keyed by "
            f"id, so a duplicate silently drops a step), got {ids}"
        )

    graph = PipelineGraphModel(
        steps={
            step["id"]: PipelineStepV2(
                id=step["id"],
                name=step["name"],
                type=step["type"],
                config=step.get("config") or {},
                # Same vertical layout `array_to_graph` gives a converted
                # array, so an ad-hoc run opened in the graph view reads the
                # way every other converted pipeline does.
                position=PipelineNodePosition(x=100, y=i * 150),
                timeout=step.get("timeout", 300),
            )
            for i, step in enumerate(steps)
        },
        edges=[
            PipelineEdge(
                id=f"edge_{i}_success",
                from_step=ids[i],
                to_step=ids[i + 1],
                condition=EdgeCondition.SUCCESS,
            )
            for i in range(len(steps) - 1)
        ],
        entry_points=[ids[0]],
        version=2,
    )
    return graph.model_dump_json()


def build_agent_step_config(
    *,
    agent: str,
    model: str | None = None,
    task: str | None = None,
    title: str | None = None,
    description: str | None = None,
    prompt_template: str | None = None,
    agent_file_ids: list[str] | None = None,
    base_branch: str | None = None,
    branch: str | None = None,
    commit_enabled: bool = True,
    push_branch: bool = True,
    card_id: str | None = None,
    mock_config: dict | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Build the `config:` block of a single agent step.

    Empty values are dropped so the persisted step config stays readable and
    so a `None` never reads as an intentional override downstream.
    """
    config: dict[str, Any] = {"agent": agent}
    optional = {
        "model": model,
        "task": task,
        "title": title,
        "description": description,
        "prompt_template": prompt_template,
        "agent_file_ids": list(agent_file_ids) if agent_file_ids else None,
        "base_branch": base_branch,
        "branch": branch,
        "card_id": card_id,
        "mock_config": mock_config,
    }
    for key, value in optional.items():
        if value not in (None, "", [], {}):
            config[key] = value
    # `commit` is always explicit: `commit: false` is the difference between
    # the dogfood ratchet and a run that pushes to its own repo.
    #
    # The bool spelling means "commit and push" / "do neither" - the two
    # cases everything in 12.5 actually wants. Only the odd combination
    # (commit locally, do not push) needs the dict spelling, so the common
    # config stays one word instead of carrying a `push` key that duplicates
    # what `commit` already said.
    commit_enabled = bool(commit_enabled)
    push = bool(push_branch and commit_enabled)
    if commit_enabled and not push:
        config["commit"] = {"enabled": True, "push": False}
    else:
        config["commit"] = commit_enabled
    for key, value in (extra or {}).items():
        if key not in _RESERVED_STEP_CONFIG_KEYS:
            config[key] = value
    return config


async def start_adhoc_agent_run(
    db: AsyncSession,
    repo: Repo,
    *,
    trigger_type: str,
    trigger_ref: str,
    base_branch: str | None = None,
    work_branch: str | None = None,
    agent: str = DEFAULT_AGENT,
    model: str | None = None,
    prompt_template: str | None = None,
    task: dict[str, Any] | None = None,
    agent_file_ids: list[str] | None = None,
    mock_config: dict | None = None,
    commit_enabled: bool = True,
    push_branch: bool = True,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
    step_name: str | None = None,
    extra_config: dict | None = None,
) -> PipelineRun:
    """Create an ephemeral single-agent-step pipeline and start it.

    Reuses ``pipeline_executor.start_pipeline`` verbatim: workspace volume,
    StepRun, StepExecution, control mode, logs, test-results, usage and the
    existing WS frames all come for free.

    Args:
        trigger_type: ``card_work`` or ``playground`` - the durable routing
            key ``on_run_complete`` dispatches on.
        trigger_ref: the card id / playground session id.
        base_branch: branch the workspace is cloned at (defaults to the
            repo's default branch).
        work_branch: branch the agent commits and pushes to.

    Returns:
        The started PipelineRun. Dispatch is async (R5): this returns as soon
        as the run row exists and the step is dispatched.
    """
    if trigger_type not in ADHOC_TRIGGER_TYPES:
        raise ValueError(
            f"start_adhoc_agent_run: trigger_type must be one of "
            f"{ADHOC_TRIGGER_TYPES}, got {trigger_type!r}"
        )
    if not trigger_ref:
        raise ValueError("start_adhoc_agent_run: trigger_ref is required")

    task = task or {}
    base_branch = base_branch or repo.default_branch
    step_config = build_agent_step_config(
        agent=agent,
        model=model,
        task=task.get("description") or task.get("title"),
        title=task.get("title"),
        description=task.get("description"),
        prompt_template=prompt_template,
        agent_file_ids=agent_file_ids,
        base_branch=base_branch,
        branch=work_branch,
        commit_enabled=commit_enabled,
        push_branch=push_branch,
        card_id=task.get("card_id"),
        mock_config=mock_config,
        extra=extra_config,
    )

    step = {
        "id": "agent",
        "name": step_name or task.get("title") or "Agent work",
        "type": "agent",
        "config": step_config,
        "timeout": timeout,
    }

    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name=adhoc_pipeline_name(trigger_type, trigger_ref),
        description=(
            "Ephemeral single-agent-step pipeline created for ad-hoc agent "
            f"work ({trigger_type}). Hidden from GET /api/pipelines; its RUN "
            "is visible. Cascade-deletes with its runs."
        ),
        steps_graph=adhoc_steps_graph([step]),
        triggers="[]",
        is_template=False,
    )
    db.add(pipeline)
    await db.commit()
    await db.refresh(pipeline)

    trigger_context: dict[str, Any] = {
        "branch": base_branch,
        "base_branch": base_branch,
        "repo_id": repo.id,
        "adhoc": True,
    }
    if work_branch:
        trigger_context["work_branch"] = work_branch
    if task.get("card_id"):
        trigger_context["card_id"] = task["card_id"]

    from app.services.pipeline_executor import pipeline_executor

    pipeline_run = await pipeline_executor.start_pipeline(
        db=db,
        pipeline=pipeline,
        repo=repo,
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        trigger_context=trigger_context,
    )
    logger.info(
        "Started ad-hoc agent run %s (%s ref=%s, agent=%s, branch=%s)",
        pipeline_run.id[:8],
        trigger_type,
        trigger_ref[:8],
        agent,
        work_branch,
    )
    return pipeline_run


async def start_endpoint_probe_run(db: AsyncSession, endpoint) -> PipelineRun:
    """Probe a `runner-local` endpoint FROM THE RUNNER (M14 s2.3).

    A `runner-local` endpoint is unreachable from the backend BY DEFINITION -
    that is the whole point of the mode - so probing it uses the machinery that
    already reaches that host: a one-step ad-hoc SCRIPT run pinned by
    `requires: {has: [<runner_label>]}`, which 12.6's router sends remote and
    12.6's dispatcher matches against the runner registry.

    Worth the extra plumbing for one reason: it probes from **the exact network
    position the real step will occupy**, which the backend cannot do for this
    mode. And the failure to schedule it is itself information - if no runner
    carries the label, the run fails at `NO_RUNNER_TIMEOUT` with "no runner
    carries label endpoint:local-4090", which is the true reason the endpoint
    is unusable.

    The container reports back to `/api/model-endpoints/{id}/probe-result` with
    the step JWT it holds; `probed_from` is stamped SERVER-SIDE from
    `step_execution.runner_id` and never read from the payload.

    THE REPO IS INCIDENTAL. A probe reads no code, but 12.6's remote path
    provisions its workspace from a clone URL, so this borrows the oldest
    registered repo and SAYS SO in the log rather than inventing a repo-less
    execution path for one step type.
    """
    from app.models.model_endpoint import default_runner_label

    if endpoint.reach != "runner-local":
        raise ValueError(
            f"endpoint '{endpoint.name}' has reach={endpoint.reach!r}; only a "
            f"runner-local endpoint is probed by a run (every other reach is "
            f"probed in-process by the backend, which is the network position "
            f"that matters for it)"
        )

    repo = (
        await db.execute(select(Repo).order_by(Repo.created_at).limit(1))
    ).scalars().first()
    if repo is None:
        raise ValueError(
            "a runner-local endpoint is probed by a real pipeline run, and a "
            "pipeline run needs a repo to provision its workspace from. "
            "Register a repo first."
        )

    label = endpoint.runner_label or default_runner_label(endpoint.name)
    logger.info(
        "Probing runner-local endpoint %s from a run pinned to label %r "
        "(workspace borrowed from repo %s - a probe reads no code)",
        endpoint.name,
        label,
        repo.id[:8],
    )

    # The runner pin is written by the ONE writer of that injection
    # (`pipeline_executor.inject_endpoint_requirements`), not spelled again
    # here: the probe run and a real harness step must land on the SAME runner
    # for the probe to mean anything, and two spellings of one pin is how they
    # drift apart.
    from app.services.pipeline_executor import inject_endpoint_requirements

    step_config = inject_endpoint_requirements(
        {
            "image": "lazyaf-agent-base:dev",
            "command": ENDPOINT_PROBE_COMMAND,
            "environment": {"LAZYAF_PROBE_ENDPOINT_ID": endpoint.id},
            # Read by `pipeline_executor._prepare_control_mode`: this step, and
            # only this step, gets its own JWT placed in the SECRET channel so
            # the probe can authenticate its report. Nothing else in the
            # platform puts a step token in a step's environment.
            "endpoint_probe": endpoint.id,
        },
        endpoint,
    )
    step = {
        "id": "probe",
        "name": f"Probe endpoint {endpoint.name}",
        "type": "script",
        "config": step_config,
        "timeout": ENDPOINT_PROBE_TIMEOUT,
    }

    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo.id,
        name=adhoc_pipeline_name(TRIGGER_ENDPOINT_PROBE, endpoint.id),
        description=(
            "Ephemeral one-step pipeline that probes a runner-local model "
            "endpoint from the box that hosts it. Hidden from "
            "GET /api/pipelines; its RUN is visible."
        ),
        steps_graph=adhoc_steps_graph([step]),
        triggers="[]",
        is_template=False,
    )
    db.add(pipeline)
    await db.commit()
    await db.refresh(pipeline)

    from app.services.pipeline_executor import pipeline_executor

    return await pipeline_executor.start_pipeline(
        db=db,
        pipeline=pipeline,
        repo=repo,
        trigger_type=TRIGGER_ENDPOINT_PROBE,
        trigger_ref=endpoint.id,
        trigger_context={
            "branch": repo.default_branch,
            "base_branch": repo.default_branch,
            "repo_id": repo.id,
            "adhoc": True,
            "model_endpoint_id": endpoint.id,
        },
    )


async def start_card_work(
    db: AsyncSession,
    card: Card,
    repo: Repo,
    *,
    job_id: str,
    prompt_template: str | None = None,
    agent_file_ids: list[str] | None = None,
    step_config: dict | None = None,
) -> PipelineRun:
    """Start agent work for a card on the control layer.

    The caller (``routers/cards.py``) has already created the Job row and set
    ``card.branch_name``; this starts the run and flips the Job to running.
    """
    step_config = dict(step_config or {})
    mock_config = step_config.pop("mock_config", None)
    agent = resolve_agent(card.runner_type)

    pipeline_run = await start_adhoc_agent_run(
        db,
        repo,
        trigger_type=TRIGGER_CARD_WORK,
        trigger_ref=card.id,
        base_branch=repo.default_branch,
        work_branch=card.branch_name,
        agent=agent,
        model=step_config.pop("model", None),
        prompt_template=prompt_template,
        task={
            "card_id": card.id,
            "title": card.title,
            "description": card.description,
        },
        agent_file_ids=agent_file_ids,
        mock_config=mock_config,
        commit_enabled=True,
        push_branch=True,
        step_name=card.title,
        extra_config=step_config,
    )

    await _mark_job_running(db, job_id, agent, pipeline_run)
    return pipeline_run


async def _mark_job_running(
    db: AsyncSession, job_id: str, agent: str, pipeline_run: PipelineRun
) -> None:
    """Flip the card's Job row to running and link it to the agent StepRun.

    The Job table is the legacy UI's window onto card work; until 12.6
    rebuilds that panel, these rows have to keep telling the truth - now
    from the RUN's state instead of from a runner's poll.

    NEVER unconditionally: ``start_pipeline`` can complete the run before it
    returns (see ``LIVE_RUN_STATUSES``), in which case ``on_run_complete``
    has ALREADY written this Job's terminal status and the card's. Writing
    "running" over that would strand the card in_progress with a job that
    claims to be running a container that never started.
    """
    job = await db.get(Job, job_id)
    if job is None:
        logger.warning("Job %s vanished before the ad-hoc run started", job_id[:8])
        return
    if job.status not in ("queued", "running"):
        logger.info(
            "Job %s is already %s - not marking it running (the ad-hoc run "
            "completed synchronously)",
            job_id[:8],
            job.status,
        )
        return
    if not await run_is_live(db, pipeline_run.id):
        logger.warning(
            "Ad-hoc run %s was already terminal when it returned from "
            "start_pipeline - leaving job %s as %s",
            pipeline_run.id[:8],
            job_id[:8],
            job.status,
        )
        return
    step_run = await _agent_step_run(db, pipeline_run.id)
    job.status = "running"
    job.runner_type = agent
    job.started_at = datetime.utcnow()
    if step_run is not None:
        job.step_run_id = step_run.id
    await db.commit()

    from app.services.websocket import manager

    await manager.send_job_status(job_ws_dict(job))


async def _agent_step_run(db: AsyncSession, run_id: str) -> StepRun | None:
    """The single agent StepRun of an ad-hoc run (index 0)."""
    result = await db.execute(
        select(StepRun)
        .where(StepRun.pipeline_run_id == run_id)
        .order_by(StepRun.step_index)
    )
    return result.scalars().first()


def job_ws_dict(job: Job) -> dict:
    """The job frame the card panel renders.

    Carries the test columns because ``JobStatus.svelte`` renders them off
    the pushed job object (``job.tests_run`` gates the whole test block); a
    frame without them makes a red suite invisible until a manual refetch.
    """
    return {
        "id": job.id,
        "card_id": job.card_id,
        "status": job.status,
        "error": job.error,
        "started_at": utc_isoformat(job.started_at),
        "completed_at": utc_isoformat(job.completed_at),
        "tests_run": job.tests_run,
        "tests_passed": job.tests_passed,
        "test_pass_count": job.test_pass_count,
        "test_fail_count": job.test_fail_count,
        "test_skip_count": job.test_skip_count,
    }


# -----------------------------------------------------------------------------
# Evidence read back off a finished run
# -----------------------------------------------------------------------------


# Cap on how many failing test ids are named in Job.test_output. A red suite
# with 4000 failures must not put 4000 lines in a column the card modal
# renders inline.
_MAX_NAMED_FAILURES = 50


@dataclass(frozen=True)
class RunTestSummary:
    """What the repo's test suite said about one ad-hoc run.

    Assembled ONLY from PERSISTED evidence - the ``TestRun`` rows the 12.2.6
    tie-back writes when a control-mode step POSTs its manifest to
    ``/api/steps/{id}/test-results``. Keyed on the RUN, not on one step, so
    it covers the agent step's own suite run AND any post-agent verification
    step added to the same ad-hoc run, with no second protocol and no
    in-memory handoff a backend restart could lose.

    ``tests_run=False`` means "no evidence either way", which is NOT the same
    as "green": a run with no manifest is not gated (there was no suite to be
    red), but it also never claims a passing suite.
    """

    tests_run: bool = False
    tests_passed: bool | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    skip_count: int | None = None
    output: str | None = None


async def run_test_summary(db: AsyncSession, run_id: str) -> RunTestSummary:
    """Summarise every TestRun row tied back to one ad-hoc run."""
    result = await db.execute(
        select(TestRun.status, TestRef.lazyaf_test_id)
        .join(TestRef, TestRef.id == TestRun.test_ref_id)
        .where(TestRun.pipeline_run_id == run_id)
        .order_by(TestRef.lazyaf_test_id)
    )
    rows = result.all()
    if not rows:
        return RunTestSummary()

    passed = sum(1 for status, _ in rows if status == TestRunStatus.PASSED.value)
    skipped = sum(1 for status, _ in rows if status == TestRunStatus.SKIPPED.value)
    failures = [
        test_id for status, test_id in rows if status == TestRunStatus.FAILED.value
    ]

    lines = [f"{passed} passed, {len(failures)} failed, {skipped} skipped"]
    lines.extend(f"FAILED: {test_id}" for test_id in failures[:_MAX_NAMED_FAILURES])
    if len(failures) > _MAX_NAMED_FAILURES:
        lines.append(f"... and {len(failures) - _MAX_NAMED_FAILURES} more")

    return RunTestSummary(
        tests_run=True,
        tests_passed=not failures,
        pass_count=passed,
        fail_count=len(failures),
        skip_count=skipped,
        output="\n".join(lines),
    )


async def run_logs(db: AsyncSession, run_id: str) -> str:
    """Every step's logs for one ad-hoc run, in step order.

    StepRun.logs is the ONE writer for control-mode log lines (R3); this is
    a read-only mirror for the Job row the card modal reads. Single-step
    runs (the common case) come back verbatim; a run that grew a
    verification step gets one header line per step so the two are
    distinguishable in the card's log pane.
    """
    result = await db.execute(
        select(StepRun.step_name, StepRun.logs)
        .where(StepRun.pipeline_run_id == run_id)
        .order_by(StepRun.step_index)
    )
    rows = [(name, logs or "") for name, logs in result.all()]
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0][1]
    return "".join(
        f"[lazyaf] --- {name} ---\n{logs}" for name, logs in rows
    )


async def _run_error(db: AsyncSession, run_id: str) -> str | None:
    """First non-empty StepRun.error of a run, for the card/session error."""
    result = await db.execute(
        select(StepRun.error)
        .where(StepRun.pipeline_run_id == run_id)
        .order_by(StepRun.step_index)
    )
    for (error,) in result.all():
        if error:
            return error
    return None


def _run_agent_name(
    pipeline_run: PipelineRun, steps_graph_json: str | None
) -> str | None:
    """The agent that ran, read back off the ephemeral pipeline definition.

    Reads the v2 GRAPH (12.8): the definition is a `steps` MAPPING keyed by
    step id, not an array. Failing over to None here is not cosmetic - the
    caller writes it to `card.completed_runner_type` and `job.runner_type`,
    so a None from a shape this could not read is dark data loss on every
    card with no error anywhere. Hence the WARNING below: the two ways this
    returns None are "the run had no agent step" (a script-only ad-hoc run -
    normal, silent) and "the definition would not read", which is a defect
    and says so.
    """
    graph = None
    if steps_graph_json:
        try:
            graph = json.loads(steps_graph_json)
        except (json.JSONDecodeError, TypeError):
            graph = None
    steps = (graph or {}).get("steps")
    if not isinstance(steps, dict):
        logger.warning(
            "Run %s: could not read a v2 graph off its pipeline definition, so "
            "the agent that ran is unknown - the card and job will record no "
            "runner type",
            pipeline_run.id[:8],
        )
        return None
    for step in steps.values():
        if isinstance(step, dict) and step.get("type") == "agent":
            return (step.get("config") or {}).get("agent")
    return None


# -----------------------------------------------------------------------------
# Cancellation - the other end of a run's life (QA finding T2)
# -----------------------------------------------------------------------------
#
# Ending card work early is NOT "write failed on the Job row". Since 12.5 the
# container doing the work belongs to a PipelineRun and the Job is its twin;
# flipping the twin without touching the run is a cancel that cancels nothing
# - the agent keeps running (and burning provider budget) while the UI
# reports it stopped. That is precisely how `reject` used to strand a run:
# the card went back to `todo` with its branch nulled, the agent kept
# committing to that branch, and a second agent could be started alongside it.
#
# Every caller that ends card work early comes through here:
# POST /api/jobs/{id}/cancel and POST /api/cards/{id}/reject. Neither of them
# reimplements any of it (R3: one writer for one decision).


class CancelRunFailed(Exception):
    """A live run behind a card could not be cancelled.

    Carries the run id so the HTTP layer can name it. NEVER swallow this:
    the container may still be running, and the only thing worse than a
    failed cancel is one that LOOKS like it worked (R1).
    """

    def __init__(self, run_id: str, cause: Exception):
        super().__init__(f"could not cancel run {run_id[:8]}: {cause}")
        self.run_id = run_id
        self.cause = cause


@dataclass
class CardWorkCancellation:
    """What a cancel actually stopped.

    ``job`` is the Job row this call LANDED (already mutated, not yet
    committed - the caller owns the transaction and the WS frame).
    """

    run_ids: list[str]
    job: Job | None = None

    @property
    def stopped_anything(self) -> bool:
        return bool(self.run_ids) or self.job is not None


def _live_run_query(*conditions):
    """A PipelineRun select whose ``step_runs`` are genuinely loaded.

    ``populate_existing`` because the run row may already be in this
    session's identity map (the request that started it used the same
    session in tests, and a read-then-cancel request pair can do it in
    production too), and an eager loader is SKIPPED for an instance that is
    already there. ``cancel_run`` walks ``step_runs`` to find the containers
    to kill and re-reads the run through ``Session.refresh``; a lazy load
    there raises under asyncio and kills nothing.
    """
    return (
        select(PipelineRun)
        .where(*conditions)
        .options(selectinload(PipelineRun.step_runs))
        .execution_options(populate_existing=True)
    )


async def live_card_work_runs(db: AsyncSession, card_id: str) -> list[PipelineRun]:
    """Every still-live ad-hoc card-work run for this card.

    Keyed on the PERSISTED trigger columns, so it finds a run whose Job has
    not been linked back yet (``Job.step_run_id`` is written at dispatch) -
    the window an instant Reject or a double-click lands in. Normally 0 or 1
    rows; more than one means an older duplicate-start got through, and
    cancelling a card must stop all of them.
    """
    result = await db.execute(
        _live_run_query(
            PipelineRun.trigger_type == TRIGGER_CARD_WORK,
            PipelineRun.trigger_ref == card_id,
            PipelineRun.status.in_(LIVE_RUN_STATUSES),
        )
    )
    return list(result.scalars().all())


async def _run_behind_job(
    db: AsyncSession, job: Job
) -> tuple[PipelineRun | None, bool]:
    """``(run, cancelling_the_job_owns_it)`` for the run this job's step is in.

    The link is ``Job.step_run_id`` (written by ``_mark_job_running`` at
    dispatch) -> StepRun -> PipelineRun.
    """
    if not job.step_run_id:
        return None, False

    run_id = (
        await db.execute(
            select(StepRun.pipeline_run_id).where(StepRun.id == job.step_run_id)
        )
    ).scalar_one_or_none()
    if run_id is None:
        logger.info(
            "Job %s has no step run %s any more - nothing to cancel",
            job.id[:8],
            job.step_run_id[:8],
        )
        return None, False

    run = (await db.execute(_live_run_query(PipelineRun.id == run_id))).scalar_one_or_none()
    if run is None:
        logger.info(
            "Job %s has no pipeline run behind step run %s - nothing to cancel",
            job.id[:8],
            job.step_run_id[:8],
        )
        return None, False

    # Ad-hoc runs only. A card's ad-hoc run exists solely to do this job, so
    # cancelling the job means cancelling the run. A job that belongs to a
    # step of a REAL pipeline does not own that pipeline, and cancelling one
    # card's job must not take a multi-step run down with it.
    if run.trigger_type not in ADHOC_TRIGGER_TYPES:
        logger.info(
            "Job %s belongs to pipeline run %s (trigger_type=%r), not to an "
            "ad-hoc card-work run - not cancelling the run",
            job.id[:8],
            run.id[:8],
            run.trigger_type,
        )
        return run, False

    return run, True


async def runs_to_cancel_for_job(db: AsyncSession, job: Job) -> list[PipelineRun]:
    """The runs that cancelling this JOB must stop.

    Two lookups, because the exact link is not always there yet:

    1. ``Job.step_run_id`` -> StepRun -> PipelineRun (ad-hoc runs only).
    2. Failing that, the card's own live card-work runs. A job cancelled
       between ``start`` and dispatch has no ``step_run_id`` yet, so lookup 1
       finds nothing while a real run is already pending - and returning
       "nothing to cancel" there is how a rejected card kept an agent.

    Lookup 2 is skipped when lookup 1 found a NON-ad-hoc run: that job is a
    pipeline step, and the card it names is not what it is running.
    """
    run, owns_it = await _run_behind_job(db, job)
    if run is not None and not owns_it:
        return []
    if run is not None:
        return [run] if run.status in LIVE_RUN_STATUSES else []
    if job.card_id:
        return await live_card_work_runs(db, job.card_id)
    return []


async def cancel_runs(db: AsyncSession, runs: list[PipelineRun]) -> list[str]:
    """Cancel each live run. Raises CancelRunFailed on the first failure."""
    from app.services.pipeline_executor import pipeline_executor

    cancelled: list[str] = []
    for run in runs:
        if run.status not in LIVE_RUN_STATUSES:
            logger.info(
                "Run %s is already %s - nothing to cancel", run.id[:8], run.status
            )
            continue
        try:
            await pipeline_executor.cancel_run(db, run)
        except Exception as e:
            logger.exception(
                "Could not cancel run %s - the agent container may STILL BE "
                "RUNNING",
                run.id[:8],
            )
            raise CancelRunFailed(run.id, e) from e
        cancelled.append(run.id)
        logger.info("Cancelled ad-hoc run %s", run.id[:8])
    return cancelled


async def cancel_card_work(
    db: AsyncSession,
    *,
    card: Card | None = None,
    job: Job | None = None,
    error: str,
) -> CardWorkCancellation:
    """Stop the work behind a card and/or a job, and land the Job row.

    Does NOT commit and does NOT broadcast: the caller owns the transaction
    (``reject`` lands the card in the same one) and the WS frames.

    Scope follows what it was given. A JOB alone is job-scoped, so a
    pipeline-step job cannot take its pipeline down. A CARD also sweeps
    every live card-work run for that card, because a card whose status is
    being unwound must not leave one running behind a stale ``job_id``.
    """
    if job is None and card is not None and card.job_id:
        job = await db.get(Job, card.job_id)

    runs: dict[str, PipelineRun] = {}
    if job is not None:
        for run in await runs_to_cancel_for_job(db, job):
            runs[run.id] = run
    if card is not None:
        for run in await live_card_work_runs(db, card.id):
            runs.setdefault(run.id, run)

    run_ids = await cancel_runs(db, list(runs.values()))

    landed = None
    if job is not None and job.status in ("queued", "running"):
        job.status = "failed"
        job.error = error
        job.completed_at = datetime.utcnow()
        landed = job

    return CardWorkCancellation(run_ids=run_ids, job=landed)


# -----------------------------------------------------------------------------
# Completion (cross-agent contract #7)
# -----------------------------------------------------------------------------


async def on_run_complete(
    db: AsyncSession, pipeline_run: PipelineRun, success: bool
) -> None:
    """Route an ad-hoc run's completion to its originator.

    Called once from ``pipeline_executor._complete_pipeline``; a no-op on
    every trigger type this module does not own, so ordinary pipeline runs
    pay one string comparison.

    NEVER raises. It runs at the tail of run completion, after the run row is
    already terminal and the workspace is already cleaned - an exception here
    would turn a finished run into a logged crash and change nothing about
    the run's own outcome. Each branch is also IDEMPOTENT (it returns early
    when the card/session is already terminal), so a duplicated call site
    cannot double-fire a status change or a trigger.
    """
    trigger_type = getattr(pipeline_run, "trigger_type", None)
    if trigger_type not in ADHOC_TRIGGER_TYPES:
        return
    # A CANCELLED run never succeeded, whatever the caller was told. Nothing
    # calls this from cancel_run today, but a straggler step task that
    # finishes after a cancel and squeaks past the executor's status guard
    # must not be able to walk a cancelled card into in_review and fire its
    # card_complete triggers (that is the self-triggering loop, one step
    # removed).
    if getattr(pipeline_run, "status", None) == RunStatus.CANCELLED.value:
        if success:
            logger.warning(
                "Ad-hoc run %s reported success but the run is CANCELLED - "
                "completing it as a failure",
                pipeline_run.id[:8],
            )
        success = False
    try:
        if trigger_type == TRIGGER_CARD_WORK:
            await _complete_card_work(db, pipeline_run, success)
        elif trigger_type == TRIGGER_EXPERIMENT:
            # Local import for the same reason pipeline_executor's is:
            # no import-time dependency between the two modules.
            from app.services.experiment_service import on_cell_complete

            await on_cell_complete(db, pipeline_run, success)
        else:
            await _complete_playground(db, pipeline_run, success)
    except Exception:
        logger.exception(
            "Ad-hoc run completion handling failed for run %s (%s ref=%s); "
            "the run itself is already terminal",
            pipeline_run.id[:8],
            trigger_type,
            (pipeline_run.trigger_ref or "")[:8],
        )


async def _complete_card_work(
    db: AsyncSession, pipeline_run: PipelineRun, success: bool
) -> None:
    """Land a card-work run: card status, Job row, WS frames, gate trigger.

    THE TEST GATE. A card only reaches ``in_review`` when the agent step
    succeeded AND no test tied back to this run came back red. The legacy
    runner path had this gate (``routers/jobs.py`` job_callback: "if tests
    were run and failed, mark card as failed instead of in_review"); losing
    it on the 12.5 path would offer red work for merge, and - because
    reaching in_review is what fires the ``card_complete`` triggers - would
    hand a red branch to the verification pipeline as if it were done.

    Evidence is the persisted ``TestRun`` rows of the whole run (see
    ``run_test_summary``), so the gate reads the agent step's own suite run
    and any post-agent verification step of the same ad-hoc run without
    caring which produced the manifest.
    """
    card_id = pipeline_run.trigger_ref
    card = await db.get(Card, card_id) if card_id else None
    if card is None:
        logger.warning(
            "Card %s for ad-hoc run %s no longer exists - nothing to complete",
            (card_id or "?")[:8],
            pipeline_run.id[:8],
        )
        return

    # Re-read both rows from the database before deciding anything. This
    # runs on the STEP TASK's session, which is expire_on_commit=False and
    # may have loaded the card earlier in the run - the cached copy would
    # still say in_progress after a cancel wrote failed from another session,
    # and the guard below would sail straight past it.
    await db.refresh(card)
    job = await db.get(Job, card.job_id) if card.job_id else None
    if job is not None:
        await db.refresh(job)

    # STALE-COMPLETION GUARD. A card-work run may only land a card that is
    # still being worked on: `in_progress` with a live Job. Anything else
    # means something already ended this work - cancellation is the case
    # that matters, and the executor's own "is the run still running?" guard
    # is not enough, because a straggler step task whose session predates the
    # cancel commit can read a stale RUNNING and complete the run anyway,
    # overwriting CANCELLED. Without this, a cancelled card walks into
    # in_review, its Job is rewritten to completed, and the card_complete
    # triggers fire on work the user stopped.
    job_is_live = job is None or job.status in ("queued", "running")
    if card.status != "in_progress" or not job_is_live:
        logger.info(
            "Ad-hoc run %s completed for card %s, but the card is %s with a "
            "%s job - something already ended this work; leaving it alone",
            pipeline_run.id[:8],
            card.id[:8],
            card.status,
            job.status if job is not None else "missing",
        )
        return

    tests = await run_test_summary(db, pipeline_run.id)
    tests_are_red = tests.tests_run and tests.tests_passed is False
    verified = success and not tests_are_red

    target = "in_review" if verified else "failed"
    old_status = card.status

    if not success:
        error = await _run_error(db, pipeline_run.id)
    elif tests_are_red:
        error = (
            f"{tests.fail_count} test(s) failed on this run - the card stays "
            f"out of review ({tests.pass_count} passed, {tests.skip_count} "
            f"skipped)"
        )
        logger.warning(
            "Card %s: agent step passed but the suite is RED (%s failed) - "
            "holding the card out of in_review",
            card.id[:8],
            tests.fail_count,
        )
    else:
        error = None

    pipeline = await db.get(Pipeline, pipeline_run.pipeline_id)
    agent = _run_agent_name(pipeline_run, pipeline.steps_graph if pipeline else None)

    card.status = target
    if verified:
        card.completed_runner_type = agent

    if job is not None:
        job.status = "completed" if verified else "failed"
        job.completed_at = datetime.utcnow()
        if not verified:
            job.error = error or "agent step failed"
        if agent:
            job.runner_type = agent
        # The card modal reads JOB logs, not StepRun logs, and until 12.6
        # rebuilds that panel a blank log pane is the only thing a user sees
        # of a whole agent run. StepRun.logs stays the single writer; this is
        # a durable mirror taken once, at the end.
        mirrored = await run_logs(db, pipeline_run.id)
        if mirrored:
            job.logs = mirrored
        # Test counts JobStatus.svelte renders (tests_run gates the block).
        job.tests_run = tests.tests_run
        job.tests_passed = tests.tests_passed
        job.test_pass_count = tests.pass_count
        job.test_fail_count = tests.fail_count
        job.test_skip_count = tests.skip_count
        job.test_output = tests.output

    # card.pr_url is deliberately NOT written here. The legacy runner set it
    # from `gh pr create`, which it only ran against an EXTERNAL remote; the
    # 12.5 path clones from and pushes to the internal git server, where
    # there is no PR to create and no URL to report. The card's work is its
    # branch (card.branch_name), which /api/cards/{id}/diff reads. Card start
    # and retry both clear any stale pr_url so a re-run cannot leave the
    # previous run's PR link pointing at the wrong branch.

    await db.commit()
    await db.refresh(card)

    from app.routers.cards import card_to_ws_dict
    from app.services.websocket import manager

    await manager.send_card_updated(card_to_ws_dict(card))
    if job is not None:
        await manager.send_job_status(job_ws_dict(job))

    logger.info(
        "Card %s -> %s from ad-hoc run %s",
        card.id[:8],
        target,
        pipeline_run.id[:8],
    )

    if not verified:
        return

    # THE GATE (US-2): a card reaching in_review is what fires the
    # card_complete triggers that run the verification pipeline. `verified`,
    # not `success`: a red suite must not fire them.
    from app.services.trigger_service import trigger_service

    await trigger_service.on_card_status_change(db, card, old_status, target)


async def _complete_playground(
    db: AsyncSession, pipeline_run: PipelineRun, success: bool
) -> None:
    """Land a playground run: status, server-side diff, branch disposal."""
    from app.services.playground_service import playground_service

    session_id = pipeline_run.trigger_ref
    session = playground_service.get_session(session_id) if session_id else None
    if session is None:
        logger.info(
            "Playground session %s is gone (expired/reset) - run %s completed "
            "with nothing to report to",
            (session_id or "?")[:8],
            pipeline_run.id[:8],
        )
        return
    if session.status in ("completed", "failed", "cancelled"):
        return  # idempotent

    try:
        if success:
            await _finish_playground_success(playground_service, session)
        else:
            error = await _run_error(db, pipeline_run.id) or "agent step failed"
            await playground_service.update_status(session.id, "failed", error)
    finally:
        playground_service.detach_run(session.id)


async def _finish_playground_success(playground_service, session) -> None:
    """Compute the diff SERVER-SIDE and dispose of the throwaway branch.

    The diff comes from the internal git server, not from the workspace
    volume: the volume is cleaned the moment the run completes, so reading it
    is a race the platform loses at random. The agent pushed its work to
    ``playground/<session_id[:8]>``, which is durable, readable and diffable
    exactly like a card branch (`GET /api/cards/{id}/diff` uses this call).
    """
    from app.services.git_server import git_repo_manager

    diff = git_repo_manager.get_diff(
        session.repo_id, session.branch, session.work_branch
    )
    if diff.get("error"):
        # No branch pushed = the agent changed nothing. That is a legitimate
        # (and common) playground outcome, not a failure - report an empty
        # diff rather than inventing one.
        logger.info(
            "Playground session %s: no diff (%s)",
            session.id[:8],
            diff["error"],
        )
        diff = {"diff": "", "files": []}

    files_changed = [f["path"] for f in diff.get("files") or []]
    branch_saved = _dispose_playground_branch(session)

    await playground_service.update_status(session.id, "completed")
    await playground_service.set_result(
        session.id,
        diff=diff.get("diff") or "",
        files_changed=files_changed,
        branch_saved=branch_saved,
    )


def _dispose_playground_branch(session) -> str | None:
    """Keep or delete the throwaway branch; return the kept branch name.

    ``save_to_branch`` asked for a NAMED branch: point that name at the work
    branch's head and drop the ``playground/`` ref, so the user gets the
    branch they asked for and no litter. Without it, the branch is deleted -
    a playground run must not accumulate refs in the user's repo.
    """
    from app.services.git_server import git_repo_manager

    work_branch = session.work_branch
    save_branch = session.save_branch
    head = git_repo_manager.get_branch_commit(session.repo_id, work_branch)
    if head is None:
        return None  # nothing was pushed; nothing to keep or delete

    if not save_branch:
        git_repo_manager.delete_branch(session.repo_id, work_branch, force=True)
        return None

    if save_branch == work_branch:
        return work_branch

    repo = git_repo_manager.get_repo(session.repo_id)
    if repo is None:
        return work_branch
    try:
        repo.refs[f"refs/heads/{save_branch}".encode()] = head.encode("ascii")
    except Exception:
        logger.exception(
            "Playground session %s: could not create branch %r - keeping the "
            "work branch %r instead",
            session.id[:8],
            save_branch,
            work_branch,
        )
        return work_branch
    git_repo_manager.delete_branch(session.repo_id, work_branch, force=True)
    return save_branch
