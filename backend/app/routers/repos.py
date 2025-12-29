from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Repo
from app.schemas import RepoCreate, RepoRead, RepoUpdate

router = APIRouter(prefix="/api/repos", tags=["repos"])


@router.get("", response_model=list[RepoRead])
async def list_repos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo))
    return result.scalars().all()


@router.post("", response_model=RepoRead, status_code=201)
async def create_repo(repo: RepoCreate, db: AsyncSession = Depends(get_db)):
    db_repo = Repo(**repo.model_dump())
    db.add(db_repo)
    await db.commit()
    await db.refresh(db_repo)
    return db_repo


@router.get("/{repo_id}", response_model=RepoRead)
async def get_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    return repo


@router.patch("/{repo_id}", response_model=RepoRead)
async def update_repo(repo_id: str, update: RepoUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(repo, key, value)

    await db.commit()
    await db.refresh(repo)
    return repo


@router.delete("/{repo_id}", status_code=204)
async def delete_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    await db.delete(repo)
    await db.commit()
