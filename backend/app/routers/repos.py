from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Repo
from app.schemas import RepoCreate, RepoRead, RepoUpdate, RepoIngest
from app.services.git_server import git_repo_manager

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


@router.post("/ingest", response_model=RepoIngest, status_code=201)
async def ingest_repo(repo: RepoCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Create a repo and initialize its internal git storage.

    After calling this endpoint, push your local repo to the returned clone_url:
        git remote add lazyaf <clone_url>
        git push lazyaf --all
    """
    # Create the repo record
    db_repo = Repo(**repo.model_dump())
    db.add(db_repo)
    await db.commit()
    await db.refresh(db_repo)

    # Initialize bare repo for git storage
    try:
        git_repo_manager.create_bare_repo(db_repo.id)
        db_repo.is_ingested = True
        await db.commit()
        await db.refresh(db_repo)
    except Exception as e:
        # Rollback repo creation if git init fails
        await db.delete(db_repo)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to initialize git repo: {e}")

    # Build full clone URL
    base_url = str(request.base_url).rstrip("/")
    clone_url = f"{base_url}/git/{db_repo.id}.git"

    return RepoIngest(
        id=db_repo.id,
        name=db_repo.name,
        internal_git_url=db_repo.internal_git_url,
        clone_url=clone_url,
    )


@router.get("/{repo_id}", response_model=RepoRead)
async def get_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")
    return repo


@router.get("/{repo_id}/clone-url")
async def get_clone_url(repo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Get the internal git clone URL for a repo."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    base_url = str(request.base_url).rstrip("/")
    return {
        "clone_url": f"{base_url}/git/{repo.id}.git",
        "is_ingested": repo.is_ingested,
    }


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

    # Delete the git repo storage
    git_repo_manager.delete_repo(repo_id)

    await db.delete(repo)
    await db.commit()


@router.get("/{repo_id}/branches")
async def list_branches(repo_id: str, db: AsyncSession = Depends(get_db)):
    """List all branches in the internal git repo."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    branches = git_repo_manager.list_branches(repo_id)
    git_default_branch = git_repo_manager.get_default_branch(repo_id)

    # Sync default branch from git repo to database if it differs
    # This handles the case where user pushed with a different default branch
    if git_default_branch and git_default_branch != repo.default_branch:
        repo.default_branch = git_default_branch
        await db.commit()
        await db.refresh(repo)

    # Use git repo's default, or fall back to repo model's default, or first branch
    default_branch = git_default_branch or repo.default_branch
    if not default_branch and branches:
        # No HEAD set yet, use first non-lazyaf branch or just first branch
        non_lazyaf = [b for b in branches if not b.startswith("lazyaf/")]
        default_branch = non_lazyaf[0] if non_lazyaf else branches[0]

    # Get commit SHA for each branch
    branch_info = []
    for branch in branches:
        commit = git_repo_manager.get_branch_commit(repo_id, branch)
        branch_info.append({
            "name": branch,
            "commit": commit,
            "is_default": branch == default_branch,
            "is_lazyaf": branch.startswith("lazyaf/"),
        })

    return {
        "branches": branch_info,
        "default_branch": default_branch,
        "total": len(branches),
    }


@router.get("/{repo_id}/commits")
async def list_commits(
    repo_id: str,
    branch: str = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """Get commit history for a branch."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    commits = git_repo_manager.get_commit_log(repo_id, branch, max_count=min(limit, 100))

    return {
        "branch": branch or git_repo_manager.get_default_branch(repo_id),
        "commits": commits,
        "total": len(commits),
    }


@router.get("/{repo_id}/diff")
async def get_branch_diff(
    repo_id: str,
    base: str,
    head: str,
    db: AsyncSession = Depends(get_db)
):
    """Get diff between two branches."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    diff = git_repo_manager.get_diff(repo_id, base, head)

    if "error" in diff and diff["error"]:
        raise HTTPException(status_code=400, detail=diff["error"])

    return diff
