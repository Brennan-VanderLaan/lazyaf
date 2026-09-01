import json
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Card, Repo, Job, AgentFile
from app.routers.spec import (
    _create_feature,
    _get_feature_or_404,
    _get_story_or_404,
)
from app.schemas import CardCreate, CardRead, CardUpdate
from app.schemas._datetime import utc_isoformat
from app.schemas.spec import FeatureCreate, FeatureRead
from app.services import agent_run
from app.services.websocket import manager
from app.services.git_server import git_repo_manager

router = APIRouter(tags=["cards"])


def parse_step_config(config_str: str | None) -> dict | None:
    """Parse step_config from JSON string to dict."""
    if not config_str:
        return None
    try:
        return json.loads(config_str)
    except (json.JSONDecodeError, TypeError):
        return None


def serialize_step_config(config: dict | None) -> str | None:
    """Serialize step_config from dict to JSON string."""
    if not config:
        return None
    return json.dumps(config)


def parse_agent_file_ids(ids_str: str | None) -> list[str] | None:
    """Parse agent_file_ids from JSON string to list."""
    if not ids_str:
        return None
    try:
        return json.loads(ids_str)
    except (json.JSONDecodeError, TypeError):
        return None


def serialize_agent_file_ids(ids: list[str] | None) -> str | None:
    """Serialize agent_file_ids from list to JSON string."""
    if not ids:
        return None
    return json.dumps(ids)


# ---------------------------------------------------------------------------
# Step-type support (Phase 12.4 fallout, revisited at 12.5)
#
# Card.step_type still carries the "script" and "docker" values, but Phase
# 12.4 deleted script/docker execution from the runners: every runner
# entrypoint's `execute_job` now REJECTS those step types.
#
# Phase 12.5 gave AGENT cards a local execution path: starting a card creates
# an ad-hoc single-agent-step PipelineRun (app/services/agent_run.py), so a
# card now has exactly the PipelineRun/StepRun the LocalExecutor is driven
# by. That path is agent-only ON PURPOSE. Script/docker work belongs in a
# real pipeline step: a card carries no command, no image and no step graph,
# and inventing an ad-hoc script step here would fork the pipeline step
# vocabulary into a second, card-shaped dialect (R3).
#
# So starting a script/docker CARD is still rejected at the API - with a
# message that names the reason and the supported alternative - instead of
# leaving the card stuck in_progress or silently running something the user
# did not describe.
#
# DEPRECATED: CardCreate/CardUpdate/CardRead.step_type values "script" and
# "docker" (app/schemas/card.py -> app.models.card.StepType).
#
# 12.7: the refusal moved to CREATE. Accepting a step type that can never be
# started meant the product INVITED the mistake - the card was created, sat on
# the board looking normal, and only said no three clicks later when Start was
# pressed. Refuse at the edge instead (R1), naming what to use instead.
#
# PATCH still accepts them, deliberately: a card created before this guard must
# stay editable (its title, its description, its link to a story) instead of
# 422-ing on every save. Starting one is still refused.
# ---------------------------------------------------------------------------
DEPRECATED_CARD_STEP_TYPES = ("script", "docker")


def _reject_deprecated_step_type_on_create(step_type: str | None) -> None:
    """422 on CREATING a card whose step_type has no execution path.

    Same set as `_reject_unrunnable_step_type` (ONE list - R3), refused one
    step earlier: nothing should be able to put a card on the board that
    Start is guaranteed to reject.
    """
    # CardCreate hands this over as a StepType enum; the message must name the
    # WIRE value ("docker"), not the enum repr Python 3.11+ formats it as.
    step_type = getattr(step_type, "value", step_type)
    if step_type in DEPRECATED_CARD_STEP_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"step_type='{step_type}' is no longer supported for cards: "
                "Phase 12.4 removed script/docker execution from the runners, "
                "so a card created this way could never be started. Use "
                "step_type='agent' for a card, or put script/docker work in a "
                "pipeline step, where it still runs on the local executor."
            ),
        )


def _reject_unrunnable_step_type(card: Card) -> None:
    """400 on a card whose step_type has no execution path.

    Raises HTTPException(400) for script/docker cards (see the module note
    above). Called by every card-start entry point (start, retry) so a user
    can never get a silent in_progress -> failed loop out of one.
    """
    step_type = card.step_type
    if step_type in DEPRECATED_CARD_STEP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cards with step_type='{step_type}' can no longer be started: "
                "Phase 12.4 removed script/docker execution from the runners, "
                "and the ad-hoc card run introduced in Phase 12.5 is an AGENT "
                "step only (a card carries no command or image to run). "
                f"step_type='{step_type}' is deprecated for cards - move this "
                "work into a pipeline step, where it runs on the local "
                "executor, or change the card to step_type='agent'."
            ),
        )


# ---------------------------------------------------------------------------
# The card lifecycle (QA findings T2 + T6)
# ---------------------------------------------------------------------------
# ONE definition of which transition is legal and who owns it.
#
# Before this, `start` and `retry` each hand-rolled a status check and
# `approve`, `reject`, `resolve-conflicts` and `PATCH status` had none at all:
#
#   * a card that never ran - no branch, no job, no diff - could be approved
#     to `done` with 200 OK: nothing merged, no error shown, and the
#     `card_complete` triggers fired on work that never happened;
#   * dragging a running card into Done did the same AND stranded its Job at
#     `running` forever, because the run's completion handler refuses to land
#     a card that has already left `in_progress`;
#   * rejecting an in_progress card sent it back to `todo` and nulled its
#     branch without cancelling anything: the agent kept committing to a
#     branch no card pointed at, and `start` accepted the card again, so two
#     agents worked one repo at once.
#
# Two rules, applied at every entry point:
#
#   1. Every operation NAMES the statuses it accepts and refuses anything else
#      with a 400 that names the card's ACTUAL status (`_require_status`).
#   2. The check and the write are ONE statement (`_claim_card`): a
#      conditional UPDATE whose rowcount is the decision. The read-check-write
#      it replaces was a TOCTOU window six awaits wide - five barrier-released
#      starts produced five jobs, five runs and five branches, and a
#      simultaneous approve + reject were both accepted.
#
# PATCH /api/cards/{id} may not be used to walk around any of it. The board's
# drag handler is a raw PATCH, so `status` there is restricted to the moves
# that carry NO side effect (`MANUAL_STATUSES`): `in_progress` stands for a
# live agent run and `done` for a merged branch, and a field update neither
# starts, stops nor merges anything.

# Statuses each guarded operation accepts.
START_FROM = ("todo",)
RETRY_FROM = ("failed", "in_review")
APPROVE_FROM = ("in_review",)
REJECT_FROM = ("in_progress", "in_review", "failed")
RESOLVE_CONFLICTS_FROM = ("in_review",)

# Statuses PATCH may move a card between - excluding `in_progress` and `done`
# in BOTH directions (see above).
MANUAL_STATUSES = ("todo", "in_review", "failed")


def _statuses(statuses: tuple[str, ...]) -> str:
    quoted = [f"'{s}'" for s in statuses]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _refuse(
    operation: str, current: str, allowed: tuple[str, ...], hint: str = ""
) -> HTTPException:
    """The one refusal shape: what was asked for, and what the card ACTUALLY is."""
    detail = (
        f"Cannot {operation} a card in '{current}' status: it requires "
        f"{_statuses(allowed)}."
    )
    return HTTPException(status_code=400, detail=f"{detail} {hint}".strip())


def _require_status(
    card: Card, *, operation: str, allowed: tuple[str, ...], hint: str = ""
) -> None:
    """Refuse an operation the card's CURRENT status does not allow."""
    if card.status not in allowed:
        raise _refuse(operation, card.status, allowed, hint)


def _require_manual_transition(card: Card, target: str) -> None:
    """Guard a status written by PATCH (the kanban board's drag handler).

    Every refused move has an endpoint that performs it properly, and the
    message names that endpoint: moving a card into Done must MERGE it,
    moving one into In Progress must START it, and a card being worked on
    belongs to its run until that run ends.
    """
    if target == card.status:
        return
    if card.status in MANUAL_STATUSES and target in MANUAL_STATUSES:
        return

    if target == "done":
        hint = (
            "POST /api/cards/{id}/approve is what moves a card to 'done' - it "
            "merges the card's branch first. Setting the field alone would "
            "show work as landed that was never merged."
        )
    elif target == "in_progress":
        hint = (
            "POST /api/cards/{id}/start (or /retry) is what moves a card to "
            "'in_progress' - it creates the job, the branch and the agent run."
        )
    elif card.status == "in_progress":
        hint = (
            "This card has a live run: cancel its job (POST "
            "/api/jobs/{job_id}/cancel) or reject the card (POST "
            "/api/cards/{id}/reject). Moving it by hand strands the run."
        )
    else:  # leaving 'done'
        hint = "'done' is terminal: the card's branch is already merged."

    raise HTTPException(
        status_code=400,
        detail=(
            f"Cannot move a card from '{card.status}' to '{target}' with "
            f"PATCH: only {_statuses(MANUAL_STATUSES)} can be set directly. "
            f"{hint}"
        ),
    )


async def _claim_card(
    db: AsyncSession, card_id: str, *, expected: tuple[str, ...], values: dict
) -> bool:
    """Make the status check and the status write ONE statement.

    Returns True when this caller won the transition. SQLite has no
    ``SELECT ... FOR UPDATE``, so the portable form of "check and write
    atomically" is a conditional UPDATE whose rowcount is the decision:
    concurrent callers serialize on the write lock and every loser matches
    zero rows.

    Deliberately does NOT commit. `start` commits with the new Job row in the
    same transaction; `approve` holds the transaction open across the merge,
    so a failed merge rolls the claim back with it and there is no restore
    step to get wrong.
    """
    result = await db.execute(
        sql_update(Card)
        .where(Card.id == card_id, Card.status.in_(expected))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def _reload_card(db: AsyncSession, card_id: str) -> Card | None:
    """Re-read a card from the DATABASE after a conditional UPDATE.

    ``populate_existing`` refreshes the session's copy, which the Core UPDATE
    deliberately did not touch. A plain ``Session.refresh`` would raise
    ObjectDeletedError if the row went away mid-request (start racing
    delete); this answers None and lets the caller return a clean 404.
    """
    result = await db.execute(
        select(Card)
        .where(Card.id == card_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lost_claim(
    db: AsyncSession,
    card_id: str,
    *,
    operation: str,
    allowed: tuple[str, ...],
) -> HTTPException:
    """The exception for a caller that LOST the race for a transition.

    Rolls that caller's transaction back first: nothing it staged - a Job
    row, a cancelled run, a merge - may survive a transition it did not win.
    """
    await db.rollback()
    current = (
        await db.execute(select(Card.status).where(Card.id == card_id))
    ).scalar_one_or_none()
    if current is None:
        return HTTPException(status_code=404, detail="Card not found")
    return _refuse(
        operation, current, allowed, "Another request changed this card first."
    )


def card_to_ws_dict(card: Card) -> dict:
    """Convert a Card model to a dict for websocket broadcast."""
    return {
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "runner_type": card.runner_type,
        "step_type": card.step_type,
        "step_config": parse_step_config(card.step_config),
        "prompt_template": card.prompt_template,
        "agent_file_ids": parse_agent_file_ids(card.agent_file_ids),
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
        "completed_runner_type": card.completed_runner_type,
        "feature_id": card.feature_id,
        "user_story_id": card.user_story_id,
        # utc_isoformat, not .isoformat(): a hand-built frame carries the
        # SAME wire format as the REST response (app/schemas/_datetime.py).
        "created_at": utc_isoformat(card.created_at),
        "updated_at": utc_isoformat(card.updated_at),
    }


@router.get("/api/repos/{repo_id}/cards", response_model=list[CardRead])
async def list_cards(
    repo_id: str,
    include_pipeline_cards: bool = Query(False, description="Include cards created by pipelines"),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    query = select(Card).where(Card.repo_id == repo_id)
    if not include_pipeline_cards:
        query = query.where(Card.pipeline_run_id == None)  # noqa: E711

    result = await db.execute(query)
    return result.scalars().all()


@router.post("/api/repos/{repo_id}/cards", response_model=CardRead, status_code=201)
async def create_card(repo_id: str, card: CardCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    # A card that cannot be STARTED is not a card worth creating (see
    # DEPRECATED_CARD_STEP_TYPES above).
    _reject_deprecated_step_type_on_create(card.step_type)

    # Handle JSON field serialization (dict/list -> JSON string)
    card_data = card.model_dump()
    card_data["step_config"] = serialize_step_config(card_data.get("step_config"))
    card_data["agent_file_ids"] = serialize_agent_file_ids(card_data.get("agent_file_ids"))

    db_card = Card(repo_id=repo_id, **card_data)
    db.add(db_card)
    await db.commit()
    await db.refresh(db_card)

    # Broadcast card creation via WebSocket
    await manager.send_card_updated(card_to_ws_dict(db_card))

    return db_card


@router.get("/api/cards/{card_id}", response_model=CardRead)
async def get_card(card_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


@router.patch("/api/cards/{card_id}", response_model=CardRead)
async def update_card(card_id: str, update: CardUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    update_data = update.model_dump(exclude_unset=True)

    # (An explicit JSON null on a NOT NULL column is refused before this,
    # by CardUpdate's not_null validator in app/schemas/card.py.)

    # Validate spec-layer links before applying (SQLite does not enforce FKs)
    if update_data.get("feature_id") is not None:
        await _get_feature_or_404(db, update_data["feature_id"])
    if update_data.get("user_story_id") is not None:
        await _get_story_or_404(db, update_data["user_story_id"])

    # `status` is a state TRANSITION, not a field write. It goes through the
    # lifecycle guard (so the board's drag handler cannot walk around
    # start/approve/reject) and lands as a conditional UPDATE (so it cannot
    # overwrite a status the run changed underneath it).
    target_status = update_data.pop("status", None)
    if target_status is not None:
        target_status = target_status.value
        _require_manual_transition(card, target_status)

    for key, value in update_data.items():
        if key == "runner_type" and value is not None:
            value = value.value
        elif key == "step_type" and value is not None:
            value = value.value
        elif key == "step_config":
            value = serialize_step_config(value)
        elif key == "agent_file_ids":
            value = serialize_agent_file_ids(value)
        setattr(card, key, value)

    if target_status is not None and target_status != card.status:
        claimed = await _claim_card(
            db,
            card_id,
            expected=(card.status,),
            values={"status": target_status},
        )
        if not claimed:
            await db.rollback()
            current = (
                await db.execute(select(Card.status).where(Card.id == card_id))
            ).scalar_one_or_none()
            if current is None:
                raise HTTPException(status_code=404, detail="Card not found")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot set status to '{target_status}': the card is now "
                    f"'{current}' (another request changed it first)."
                ),
            )

    await db.commit()
    card = await _reload_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # Broadcast card update via WebSocket
    await manager.send_card_updated(card_to_ws_dict(card))

    return card


@router.delete("/api/cards/{card_id}", status_code=204)
async def delete_card(card_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    await db.delete(card)
    await db.commit()

    # Broadcast card deletion via WebSocket
    await manager.send_card_deleted(card_id)


@router.post(
    "/api/cards/{card_id}/promote-to-feature",
    response_model=FeatureRead,
    status_code=201,
)
async def promote_card_to_feature(card_id: str, db: AsyncSession = Depends(get_db)):
    """Promote a card to a spec-layer Feature (Phase 12.2.5).

    Creates a Feature from the card's title/description with the card's repo
    in repo_ids, links the card to it, and returns the feature.
    """
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if card.feature_id:
        raise HTTPException(
            status_code=400,
            detail="Card is already linked to a feature",
        )

    # Reuse spec.py's create-feature logic (runs _validate_repo_ids);
    # commit=False keeps feature creation + card link in one transaction.
    feature = await _create_feature(
        db,
        FeatureCreate(
            title=card.title,
            description=card.description or "",
            repo_ids=[card.repo_id],
        ),
        commit=False,
    )

    card.feature_id = feature.id
    await db.commit()
    await db.refresh(feature)
    await db.refresh(card)

    # Broadcast card update via WebSocket (link changed)
    await manager.send_card_updated(card_to_ws_dict(card))

    return feature


#: Shown in the refusal below. A separate constant so the JSON quoting
#: does not have to survive nesting inside an f-string.
_DEFAULT_BRANCH_PATCH_EXAMPLE = '{"default_branch": "<one of the above>"}'


def _require_startable_default_branch(repo) -> None:
    """Refuse to start work unless the repo's default branch actually exists.

    Agent work branches FROM `repo.default_branch`, and the workspace clones
    it with `git clone --branch <default>`. Two states get here and neither
    used to be caught, so both failed identically: several seconds later,
    deep in workspace population, as

        local execution error: Failed to create workspace for run <id> lane
        'default': Workspace population for volume 'lazyaf-ws-<id>' ...

    by which point the card is already dirty, the user is reading executor
    internals, and nothing names the remedy.

    NO BRANCHES AT ALL - a registered repo has a bare repo and an UNBORN
    HEAD, so it has no refs until someone pushes.

    BRANCHES, BUT NOT THAT ONE - the more surprising case, and the one a
    real repo hits. `lazyaf ingest --all-branches` pushes every branch but
    sends `default_branch: "main"` regardless of what the repo's trunk is
    called, so a repo with thousands of commits on `master` lands with a
    default naming none of them. Checking merely "are there any branches"
    passes that straight through.

    A ref listing is cheap, and it is worth doing before a card is moved.
    """
    try:
        branches = git_repo_manager.list_branches(repo.id)
    except Exception:  # noqa: BLE001 - a listing failure is not a refusal
        # Never block work because the listing itself broke; failing later
        # and loudly is strictly better than refusing a repo that is fine.
        return

    if repo.default_branch in branches:
        return

    if not branches:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Repo '{repo.name}' has no commits yet, so there is no "
                f"'{repo.default_branch}' to branch from and an agent would "
                "have nothing to check out. Push your code first:\n\n"
                f"    git remote add lazyaf {repo.internal_git_url or '<clone-url>'}\n"
                f"    git push lazyaf {repo.default_branch}\n\n"
                "Then start the card again."
            ),
        )

    real = sorted(b for b in branches if not b.startswith("lazyaf/"))
    shown = ", ".join(real[:8]) or "(only agent branches)"
    more = f" (+{len(real) - 8} more)" if len(real) > 8 else ""
    raise HTTPException(
        status_code=400,
        detail=(
            f"Repo '{repo.name}' says its default branch is "
            f"'{repo.default_branch}', but no such branch exists here, so an "
            f"agent has nothing to branch from. Branches present: "
            f"{shown}{more}.\n\n"
            "This is what `lazyaf ingest --all-branches` produces when the "
            "repo's trunk is not called 'main'. Point the repo at the right "
            "branch:\n\n"
            f"    PATCH /api/repos/{repo.id}   body: "
            + _DEFAULT_BRANCH_PATCH_EXAMPLE
            + "\n\n"
            "Then start the card again."
        ),
    )


@router.post("/api/cards/{card_id}/start", response_model=CardRead)
async def start_card(
    card_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Trigger agent work on this card.

    Agent cards only: step_type 'script'/'docker' is rejected with 400
    (deprecated for cards since Phase 12.4 - see _reject_unrunnable_step_type).
    """
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    _require_status(
        card,
        operation="start",
        allowed=START_FROM,
        hint=(
            "Use POST /api/cards/{id}/retry to run a failed or in-review card "
            "again."
        ),
    )

    # 12.4: script/docker cards have no execution path - reject before any
    # state is mutated (never leave the card stuck in_progress).
    _reject_unrunnable_step_type(card)

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    # Check if repo is ready for work
    if not repo.is_ingested:
        raise HTTPException(
            status_code=400,
            detail="Repo must be ingested before starting work. Use the CLI to ingest the repo first."
        )

    _require_startable_default_branch(repo)

    # Get agent file IDs from the card
    agent_file_ids = parse_agent_file_ids(card.agent_file_ids) or []

    # Validate agent file IDs exist
    if agent_file_ids:
        result = await db.execute(select(AgentFile).where(AgentFile.id.in_(agent_file_ids)))
        existing_agent_files = result.scalars().all()
        existing_ids = {af.id for af in existing_agent_files}
        missing_ids = set(agent_file_ids) - existing_ids
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Agent files not found: {', '.join(missing_ids)}"
            )

    # Parse step_config from card
    step_config = parse_step_config(card.step_config)

    job_id = str(uuid4())
    job = Job(
        id=job_id,
        card_id=card.id,
        status="queued",
        step_type=card.step_type,
        step_config=card.step_config,  # Already JSON string
    )

    # CLAIM the card: the status check above is the friendly error, this is
    # the one that decides. N simultaneous starts hit one conditional UPDATE
    # and exactly one matches a row, so one double-click can no longer create
    # two jobs, two runs, two branches and two agent containers.
    #
    # A new run also means a new branch: any PR link left over from an earlier
    # attempt points at work this run is not doing. Retry clears it too, so
    # the two entry points cannot disagree.
    claimed = await _claim_card(
        db,
        card_id,
        expected=START_FROM,
        values={
            "status": "in_progress",
            "job_id": job_id,
            "branch_name": f"lazyaf/{job_id[:8]}",
            "pr_url": None,
            "completed_runner_type": None,
        },
    )
    if not claimed:
        raise await _lost_claim(
            db, card_id, operation="start", allowed=START_FROM
        )

    # Only now does the Job row exist: a start that lost the race must not
    # leave one behind.
    db.add(job)
    await db.commit()

    card = await _reload_card(db, card_id)
    if card is None:
        # Deleted between the claim and the read (start racing delete). The
        # run has not been dispatched yet, so there is nothing to unwind.
        raise HTTPException(status_code=404, detail="Card not found")

    # Get prompt template: card-specific > global default > None (agent uses built-in)
    settings = get_settings()
    prompt_template = card.prompt_template or settings.default_prompt_template

    # Broadcast job queued status via WebSocket BEFORE dispatch: the run
    # start flips the same Job row to running and broadcasts again, so the UI
    # sees queued -> running in order rather than a job that appears already
    # running.
    await manager.send_job_status({
        "id": job_id,
        "card_id": card.id,
        "status": "queued",
        "error": None,
        "started_at": None,
        "completed_at": None,
    })
    await manager.send_card_updated(card_to_ws_dict(card))

    # 12.5: card work runs on the control layer as an ad-hoc agent run - an
    # ephemeral hidden Pipeline + a real PipelineRun with one agent step.
    # Nothing is enqueued for a polling runner (asserted by
    # tdd/unit/services/test_no_legacy_enqueue.py).
    await agent_run.start_card_work(
        db,
        card,
        repo,
        job_id=job_id,
        prompt_template=prompt_template,
        agent_file_ids=agent_file_ids,
        step_config=step_config,
    )

    # Return the card as of the START, deliberately un-refreshed. Agent work
    # is asynchronous now: on_run_complete writes the terminal status from
    # the step task's OWN session, so refreshing here would make this
    # response race the run and sometimes answer "failed" to "please start".
    return card


class ApproveRequest(BaseModel):
    target_branch: Optional[str] = None  # If None, uses repo's default branch


class ApproveResponse(BaseModel):
    card: CardRead
    merge_result: Optional[dict] = None


@router.post("/api/cards/{card_id}/approve")
async def approve_card(
    card_id: str,
    request: ApproveRequest = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Approve and merge the card's branch into target branch.

    If target_branch is not specified, uses the repo's default branch.
    Returns the card and merge result details.
    """
    if request is None:
        request = ApproveRequest()

    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    _require_status(
        card,
        operation="approve",
        allowed=APPROVE_FROM,
        hint=(
            "A card reaches 'in_review' by RUNNING: start it and let the agent "
            "push a branch there is something to merge."
        ),
    )

    # Approve MERGES. A card with no branch has nothing to merge, and marking
    # it done anyway was this platform's worst lie: work that never happened,
    # shown on the board as landed, with the card_complete triggers fired.
    if not card.branch_name:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot approve this card: it has no branch, so there is "
                "nothing to merge. Start the card and let the agent push its "
                "work first."
            ),
        )

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot approve this card: repo '{repo.name}' is not ingested, "
                "so its branch cannot be merged. Ingest the repo first."
            ),
        )

    target_branch = request.target_branch or repo.default_branch

    # CLAIM the transition before merging, and hold the transaction open
    # across the merge. Winning the claim is what makes this request the one
    # allowed to finish the card: a concurrent approve or reject matches zero
    # rows and is refused, instead of both being accepted and the card
    # settling on whichever committed last. If the merge then fails, the
    # rollback takes the claim with it - the card is still in_review and
    # there is no "put the status back" step to get wrong.
    claimed = await _claim_card(
        db, card_id, expected=APPROVE_FROM, values={"status": "done"}
    )
    if not claimed:
        raise await _lost_claim(
            db, card_id, operation="approve", allowed=APPROVE_FROM
        )

    try:
        merge_result = git_repo_manager.merge_branch(
            repo_id=repo.id,
            source_branch=card.branch_name,
            target_branch=target_branch
        )
    except Exception:
        await db.rollback()
        raise

    if not merge_result["success"]:
        await db.rollback()
        # Conflicts are not an error: the UI asks the user to resolve them.
        # Either way the card is still in_review - the claim rolled back.
        if "conflicts" in merge_result:
            card = await _reload_card(db, card_id)
            if card is None:
                raise HTTPException(status_code=404, detail="Card not found")
            return {
                "card": CardRead.model_validate(card),
                "merge_result": merge_result
            }
        raise HTTPException(
            status_code=400,
            detail=f"Merge failed: {merge_result['error']}"
        )

    await db.commit()
    card = await _reload_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # Broadcast card update via WebSocket
    await manager.send_card_updated(card_to_ws_dict(card))

    # Check for pipeline triggers on card completion. Reached ONCE per card
    # now: a second approve loses the claim, so clicking Approve twice can no
    # longer start two verification runs off one piece of work.
    from app.services.trigger_service import trigger_service
    await trigger_service.on_card_status_change(db, card, "in_review", "done")

    return {
        "card": CardRead.model_validate(card),
        "merge_result": merge_result
    }


@router.post("/api/cards/{card_id}/reject", response_model=CardRead)
async def reject_card(card_id: str, db: AsyncSession = Depends(get_db)):
    """Reject the card's work: stop the run, drop the branch, back to todo."""
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    _require_status(
        card,
        operation="reject",
        allowed=REJECT_FROM,
        hint=(
            "There is nothing to reject: a 'todo' card has no work yet, and a "
            "'done' card is already merged."
        ),
    )

    # CLAIM first: winning the transition is what makes this request the one
    # that gets to stop the work, so a concurrent approve is refused rather
    # than both being accepted on one card.
    claimed = await _claim_card(
        db,
        card_id,
        expected=REJECT_FROM,
        # TODO: Close PR via GitHub API
        values={"status": "todo", "branch_name": None, "pr_url": None},
    )
    if not claimed:
        raise await _lost_claim(
            db, card_id, operation="reject", allowed=REJECT_FROM
        )

    # THEN stop the work. Rejecting an in_progress card used to cancel
    # nothing: the agent kept committing to the branch this endpoint had just
    # nulled, the Job sat at 'running' forever (the run's completion handler
    # refuses to land a card that has left in_progress), and /start accepted
    # the card again - so a second agent ran beside the first.
    #
    # A failed cancel is a 503 with the whole request rolled back: the card
    # stays where it was rather than being reported rejected while its agent
    # keeps running (R1 - nothing dark).
    try:
        cancellation = await agent_run.cancel_card_work(
            db, card=card, error="Cancelled: the card was rejected"
        )
    except agent_run.CancelRunFailed as e:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail=(
                f"could not cancel the agent run {e.run_id[:8]} behind this "
                f"card ({e.cause}); the container may still be running, so the "
                "card was left as it was"
            ),
        ) from e

    await db.commit()
    card = await _reload_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # Broadcast card update via WebSocket
    await manager.send_card_updated(card_to_ws_dict(card))
    if cancellation.job is not None:
        # The card modal polls the Job every 3s; without this frame it spins
        # on a job that is never coming back.
        await manager.send_job_status(agent_run.job_ws_dict(cancellation.job))

    return card


@router.post("/api/cards/{card_id}/resolve-conflicts")
async def resolve_conflicts(
    card_id: str,
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Resolve merge conflicts by accepting resolved file contents.

    Request body should contain:
    - target_branch: str (optional, defaults to repo default branch)
    - resolutions: list of {"path": str, "content": str}
    """
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Same gate as approve, for the same reason: this endpoint MERGES, and it
    # used to do so from any status - including a card that was already
    # 'done', or one that never ran.
    _require_status(
        card,
        operation="resolve conflicts on",
        allowed=RESOLVE_CONFLICTS_FROM,
        hint="Conflicts only exist for a card whose branch is waiting to merge.",
    )
    if not card.branch_name:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot resolve conflicts on this card: it has no branch, so "
                "there is nothing to merge."
            ),
        )

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    target_branch = request.get("target_branch") or repo.default_branch
    resolutions = request.get("resolutions", [])

    if not resolutions:
        raise HTTPException(status_code=400, detail="No conflict resolutions provided")

    # Claim then merge, exactly as approve does (see _claim_card).
    claimed = await _claim_card(
        db, card_id, expected=RESOLVE_CONFLICTS_FROM, values={"status": "done"}
    )
    if not claimed:
        raise await _lost_claim(
            db,
            card_id,
            operation="resolve conflicts on",
            allowed=RESOLVE_CONFLICTS_FROM,
        )

    # Apply conflict resolutions and merge
    try:
        merge_result = git_repo_manager.resolve_and_merge(
            repo_id=repo.id,
            source_branch=card.branch_name,
            target_branch=target_branch,
            resolutions=resolutions
        )
    except Exception:
        await db.rollback()
        raise

    if not merge_result["success"]:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Merge failed: {merge_result['error']}"
        )

    await db.commit()
    card = await _reload_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # Broadcast card update via WebSocket
    await manager.send_card_updated(card_to_ws_dict(card))

    return {
        "card": CardRead.model_validate(card),
        "merge_result": merge_result
    }


@router.post("/api/cards/{card_id}/retry", response_model=CardRead)
async def retry_card(card_id: str, db: AsyncSession = Depends(get_db)):
    """Retry a failed card by creating a new job."""
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    _require_status(
        card,
        operation="retry",
        allowed=RETRY_FROM,
        hint="Use POST /api/cards/{id}/start for a card that has not run yet.",
    )

    # 12.4: script/docker cards have no execution path (see
    # _reject_unrunnable_step_type). Retrying one would re-enter the
    # in_progress -> failed loop, so refuse the same way start does.
    _reject_unrunnable_step_type(card)

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(
            status_code=400,
            detail="Repo must be ingested before starting work"
        )

    # Parse step_config from card
    step_config = parse_step_config(card.step_config)

    job_id = str(uuid4())
    job = Job(
        id=job_id,
        card_id=card.id,
        status="queued",
        step_type=card.step_type,
        step_config=card.step_config,  # Already JSON string
    )

    # Same atomic claim as start (see _claim_card): double-clicking Retry on
    # an in_review card used to launch two agent runs.
    claimed = await _claim_card(
        db,
        card_id,
        expected=RETRY_FROM,
        values={
            "status": "in_progress",
            "job_id": job_id,
            "branch_name": f"lazyaf/{job_id[:8]}",
            "pr_url": None,  # Clear old PR URL
            "completed_runner_type": None,  # New runner's type will show
        },
    )
    if not claimed:
        raise await _lost_claim(
            db, card_id, operation="retry", allowed=RETRY_FROM
        )

    db.add(job)
    await db.commit()

    card = await _reload_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # Get agent file IDs from the card
    agent_file_ids = parse_agent_file_ids(card.agent_file_ids) or []

    # Get prompt template: card-specific > global default > None (agent uses built-in)
    settings = get_settings()
    prompt_template = card.prompt_template or settings.default_prompt_template

    await manager.send_job_status({
        "id": job_id,
        "card_id": card.id,
        "status": "queued",
        "error": None,
        "started_at": None,
        "completed_at": None,
    })
    await manager.send_card_updated(card_to_ws_dict(card))

    # 12.5: same control-layer path as start (see start_card above).
    await agent_run.start_card_work(
        db,
        card,
        repo,
        job_id=job_id,
        prompt_template=prompt_template,
        agent_file_ids=agent_file_ids,
        step_config=step_config,
    )

    # Return the card as of the START, deliberately un-refreshed. Agent work
    # is asynchronous now: on_run_complete writes the terminal status from
    # the step task's OWN session, so refreshing here would make this
    # response race the run and sometimes answer "failed" to "please start".
    return card


class RebaseRequest(BaseModel):
    onto_branch: Optional[str] = None  # If None, uses repo's default branch


class RebaseResponse(BaseModel):
    card: CardRead
    rebase_result: Optional[dict] = None


@router.post("/api/cards/{card_id}/rebase")
async def rebase_card_branch(
    card_id: str,
    request: RebaseRequest = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Rebase the card's branch onto the target branch (pull in latest changes).

    This updates the card's branch to include the latest commits from the target branch,
    helping to avoid merge conflicts when the card is eventually approved.

    If onto_branch is not specified, uses the repo's default branch.
    Returns the card and rebase result details.
    """
    if request is None:
        request = RebaseRequest()

    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if not card.branch_name:
        raise HTTPException(status_code=400, detail="Card has no branch to rebase")

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    onto_branch = request.onto_branch or repo.default_branch

    # Perform the rebase
    rebase_result = git_repo_manager.rebase_branch(
        repo_id=repo.id,
        branch_name=card.branch_name,
        onto_branch=onto_branch
    )

    if not rebase_result["success"]:
        # If there are conflicts, return them without changing anything
        if "conflicts" in rebase_result:
            return {
                "card": CardRead.model_validate(card),
                "rebase_result": rebase_result
            }
        # For other errors, raise exception
        raise HTTPException(
            status_code=400,
            detail=f"Rebase failed: {rebase_result['error']}"
        )

    # Refresh card to reflect any changes
    await db.refresh(card)

    return {
        "card": CardRead.model_validate(card),
        "rebase_result": rebase_result
    }


@router.get("/api/cards/{card_id}/diff")
async def get_card_diff(
    card_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get diff for a card's branch against the repo's default branch."""
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if not card.branch_name:
        raise HTTPException(status_code=400, detail="Card has no branch")

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    # Get diff between default branch and card's branch
    diff = git_repo_manager.get_diff(repo.id, repo.default_branch, card.branch_name)

    if "error" in diff and diff["error"]:
        raise HTTPException(status_code=400, detail=diff["error"])

    return diff


@router.post("/api/cards/{card_id}/resolve-rebase-conflicts")
async def resolve_rebase_conflicts(
    card_id: str,
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Resolve rebase conflicts by accepting resolved file contents.

    Request body should contain:
    - onto_branch: str (optional, defaults to repo default branch)
    - resolutions: list of {"path": str, "content": str}

    Unlike resolve-conflicts (for merges), this updates the feature branch
    rather than the target branch.
    """
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if not card.branch_name:
        raise HTTPException(status_code=400, detail="Card has no branch to rebase")

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    onto_branch = request.get("onto_branch") or repo.default_branch
    resolutions = request.get("resolutions", [])

    if not resolutions:
        raise HTTPException(status_code=400, detail="No conflict resolutions provided")

    # Apply conflict resolutions and complete rebase
    rebase_result = git_repo_manager.resolve_rebase_conflicts(
        repo_id=repo.id,
        branch_name=card.branch_name,
        onto_branch=onto_branch,
        resolutions=resolutions
    )

    if not rebase_result["success"]:
        raise HTTPException(
            status_code=400,
            detail=f"Rebase failed: {rebase_result['error']}"
        )

    # Card status stays the same (in_review) - rebase just updates the branch
    await db.refresh(card)

    # Broadcast card update via WebSocket (branch sha changed)
    await manager.send_card_updated(card_to_ws_dict(card))

    return {
        "card": CardRead.model_validate(card),
        "rebase_result": rebase_result
    }
