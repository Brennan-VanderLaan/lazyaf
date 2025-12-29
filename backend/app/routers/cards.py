from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Card, Repo
from app.schemas import CardCreate, CardRead, CardUpdate

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

    # TODO: Create job and assign to runner
    card.status = "in_progress"
    await db.commit()
    await db.refresh(card)
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
