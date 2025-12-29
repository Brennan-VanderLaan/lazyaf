from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Card, Repo, Job
from app.schemas import CardCreate, CardRead, CardUpdate
from app.services.job_queue import job_queue, QueuedJob

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
    return card


@router.delete("/api/cards/{card_id}", status_code=204)
async def delete_card(card_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    await db.delete(card)
    await db.commit()


@router.post("/api/cards/{card_id}/start", response_model=CardRead)
async def start_card(card_id: str, db: AsyncSession = Depends(get_db)):
    """Trigger agent work on this card."""
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
    )
    await job_queue.enqueue(queued_job)

    return card


@router.post("/api/cards/{card_id}/approve", response_model=CardRead)
async def approve_card(card_id: str, db: AsyncSession = Depends(get_db)):
    """Approve PR and move card to done."""
    result = await db.execute(select(Card).where(Card.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # TODO: Merge PR via GitHub API
    card.status = "done"
    await db.commit()
    await db.refresh(card)
    return card


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
    return card


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

    return card
