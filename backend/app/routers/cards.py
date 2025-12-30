from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Card, Repo, Job, AgentFile
from app.schemas import CardCreate, CardRead, CardUpdate
from app.services.job_queue import job_queue, QueuedJob
from app.services.websocket import manager
from app.services.git_server import git_repo_manager

router = APIRouter(tags=["cards"])


@router.get("/api/repos/{repo_id}/cards", response_model=list[CardRead])
async def list_cards(repo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    result = await db.execute(select(Card).where(Card.repo_id == repo_id))
    return result.scalars().all()


@router.post("/api/repos/{repo_id}/cards", response_model=CardRead, status_code=201)
async def create_card(repo_id: str, card: CardCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Repo not found")

    db_card = Card(repo_id=repo_id, **card.model_dump())
    db.add(db_card)
    await db.commit()
    await db.refresh(db_card)

    # Broadcast card creation via WebSocket
    await manager.send_card_updated({
        "id": db_card.id,
        "repo_id": db_card.repo_id,
        "title": db_card.title,
        "description": db_card.description,
        "status": db_card.status,
        "branch_name": db_card.branch_name,
        "pr_url": db_card.pr_url,
        "job_id": db_card.job_id,
    })

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
    for key, value in update_data.items():
        if key == "status" and value is not None:
            value = value.value
        setattr(card, key, value)

    await db.commit()
    await db.refresh(card)

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

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


class StartCardRequest(BaseModel):
    agent_file_ids: list[str] = []


@router.post("/api/cards/{card_id}/start", response_model=CardRead)
async def start_card(
    card_id: str,
    request: StartCardRequest = None,
    db: AsyncSession = Depends(get_db)
):
    """Trigger agent work on this card."""
    if request is None:
        request = StartCardRequest()
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if card.status != "todo":
        raise HTTPException(status_code=400, detail="Card must be in 'todo' status to start")

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

    # Validate agent file IDs
    if request.agent_file_ids:
        result = await db.execute(select(AgentFile).where(AgentFile.id.in_(request.agent_file_ids)))
        existing_agent_files = result.scalars().all()
        existing_ids = {af.id for af in existing_agent_files}
        missing_ids = set(request.agent_file_ids) - existing_ids
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Agent files not found: {', '.join(missing_ids)}"
            )

    # Create a job in the database
    job_id = str(uuid4())
    job = Job(id=job_id, card_id=card.id, status="queued")
    db.add(job)

    # Update card status and link to job
    card.status = "in_progress"
    card.job_id = job_id
    card.branch_name = f"lazyaf/{job_id[:8]}"

    await db.commit()
    await db.refresh(card)

    # Queue the job for a runner
    # Use internal git server for ingested repos (runner constructs URL from BACKEND_URL + repo_id)
    queued_job = QueuedJob(
        id=job_id,
        card_id=card.id,
        repo_id=repo.id,
        repo_url=repo.remote_url or "",  # Kept for reference, but runner uses internal git
        base_branch=repo.default_branch,
        card_title=card.title,
        card_description=card.description,
        use_internal_git=True,  # Always use internal git for ingested repos
        agent_file_ids=request.agent_file_ids,
    )
    await job_queue.enqueue(queued_job)

    # Broadcast job queued status via WebSocket
    await manager.send_job_status({
        "id": job_id,
        "card_id": card.id,
        "status": "queued",
        "error": None,
        "started_at": None,
        "completed_at": None,
    })

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

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

    # Get the repo
    result = await db.execute(select(Repo).where(Repo.id == card.repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    merge_result = None

    # Only merge if card has a branch
    if card.branch_name and repo.is_ingested:
        target_branch = request.target_branch or repo.default_branch

        # Perform the merge
        merge_result = git_repo_manager.merge_branch(
            repo_id=repo.id,
            source_branch=card.branch_name,
            target_branch=target_branch
        )

        if not merge_result["success"]:
            # If there are conflicts, return them without changing card status
            if "conflicts" in merge_result:
                return {
                    "card": CardRead.model_validate(card),
                    "merge_result": merge_result
                }
            # For other errors, raise exception
            raise HTTPException(
                status_code=400,
                detail=f"Merge failed: {merge_result['error']}"
            )

    # Update card status to done only if merge succeeded
    card.status = "done"
    await db.commit()
    await db.refresh(card)

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

    return {
        "card": CardRead.model_validate(card),
        "merge_result": merge_result
    }


@router.post("/api/cards/{card_id}/reject", response_model=CardRead)
async def reject_card(card_id: str, db: AsyncSession = Depends(get_db)):
    """Reject PR and move card back to todo."""
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # TODO: Close PR via GitHub API
    card.status = "todo"
    card.branch_name = None
    card.pr_url = None
    await db.commit()
    await db.refresh(card)

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

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

    # Apply conflict resolutions and merge
    merge_result = git_repo_manager.resolve_and_merge(
        repo_id=repo.id,
        source_branch=card.branch_name,
        target_branch=target_branch,
        resolutions=resolutions
    )

    if not merge_result["success"]:
        raise HTTPException(
            status_code=400,
            detail=f"Merge failed: {merge_result['error']}"
        )

    # Update card status to done
    card.status = "done"
    await db.commit()
    await db.refresh(card)

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

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

    if card.status not in ("failed", "in_review"):
        raise HTTPException(
            status_code=400,
            detail=f"Can only retry cards in 'failed' or 'in_review' status, current: {card.status}"
        )

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

    # Create a new job
    job_id = str(uuid4())
    job = Job(id=job_id, card_id=card.id, status="queued")
    db.add(job)

    # Update card status and link to new job
    card.status = "in_progress"
    card.job_id = job_id
    card.branch_name = f"lazyaf/{job_id[:8]}"
    card.pr_url = None  # Clear old PR URL

    await db.commit()
    await db.refresh(card)

    # Queue the job for a runner
    queued_job = QueuedJob(
        id=job_id,
        card_id=card.id,
        repo_id=repo.id,
        repo_url=repo.remote_url or "",
        base_branch=repo.default_branch,
        card_title=card.title,
        card_description=card.description,
        use_internal_git=True,
    )
    await job_queue.enqueue(queued_job)

    # Broadcast job queued status via WebSocket
    await manager.send_job_status({
        "id": job_id,
        "card_id": card.id,
        "status": "queued",
        "error": None,
        "started_at": None,
        "completed_at": None,
    })

    # Broadcast card update via WebSocket
    await manager.send_card_updated({
        "id": card.id,
        "repo_id": card.repo_id,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "branch_name": card.branch_name,
        "pr_url": card.pr_url,
        "job_id": card.job_id,
    })

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
