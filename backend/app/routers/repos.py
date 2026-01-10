from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Repo
from app.schemas import RepoCreate, RepoRead, RepoUpdate, RepoIngest
from app.services.git_server import git_repo_manager
from app.services.websocket import manager

router = APIRouter(prefix="/api/repos", tags=["repos"])


def repo_to_dict(repo: Repo) -> dict:
    """Convert Repo model to dictionary for WebSocket broadcast."""
    return {
        "id": repo.id,
        "name": repo.name,
        "remote_url": repo.remote_url,
        "default_branch": repo.default_branch,
        "is_ingested": repo.is_ingested,
        "internal_git_url": repo.internal_git_url,
        "created_at": repo.created_at.isoformat(),
    }


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

    # Broadcast repo creation
    await manager.send_repo_created(repo_to_dict(db_repo))

    return db_repo


@router.post("/ingest", response_model=RepoIngest, status_code=201)
async def ingest_repo(repo: RepoCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Create a repo and initialize its internal git storage.

    If `path` is provided, files from that local git repo will be pushed
    to the internal git server automatically.

    Otherwise, push your local repo to the returned clone_url:
        git remote add lazyaf <clone_url>
        git push lazyaf --all
    """
    # Create the repo record (exclude path from model_dump as it's not in the DB model)
    repo_data = repo.model_dump(exclude={"path"})
    db_repo = Repo(**repo_data)
    db.add(db_repo)
    await db.commit()
    await db.refresh(db_repo)

    # Initialize bare repo for git storage
    try:
        git_repo_manager.create_bare_repo(db_repo.id)

        # If path provided, push files from local repo
        if repo.path:
            push_result = git_repo_manager.push_from_local(db_repo.id, repo.path)
            if not push_result["success"]:
                # Clean up and fail
                git_repo_manager.delete_repo(db_repo.id)
                await db.delete(db_repo)
                await db.commit()
                raise HTTPException(status_code=400, detail=push_result["error"])

            # Use detected default branch from local repo
            if push_result.get("default_branch"):
                db_repo.default_branch = push_result["default_branch"]

        db_repo.is_ingested = True
        await db.commit()
        await db.refresh(db_repo)
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as e:
        # Rollback repo creation if git init fails
        git_repo_manager.delete_repo(db_repo.id)
        await db.delete(db_repo)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to initialize git repo: {e}")

    # Build full clone URL
    base_url = str(request.base_url).rstrip("/")
    clone_url = f"{base_url}/git/{db_repo.id}.git"

    # Broadcast repo creation
    await manager.send_repo_created(repo_to_dict(db_repo))

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

    # Broadcast repo update
    await manager.send_repo_updated(repo_to_dict(repo))

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

    # Broadcast repo deletion
    await manager.send_repo_deleted(repo_id)


@router.post("/{repo_id}/test-setup", response_model=RepoRead)
async def test_setup_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    """
    TEST ONLY: Initialize a repo with minimal git data for e2e testing.

    This creates a bare repo with an initial commit so that card workflows
    can be tested without requiring actual git data to be pushed.
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if repo.is_ingested:
        return repo  # Already set up

    try:
        # Create bare repo if it doesn't exist
        if not git_repo_manager.repo_exists(repo_id):
            git_repo_manager.create_bare_repo(repo_id)

        # Initialize with a minimal commit using dulwich
        from dulwich.repo import Repo as DulwichRepo
        from dulwich.objects import Blob, Tree, Commit

        repo_path = git_repo_manager.get_repo_path(repo_id)
        dulwich_repo = DulwichRepo(str(repo_path))

        # Create a README blob
        readme_content = b"# Test Repository\n\nCreated for e2e testing.\n"
        blob = Blob.from_string(readme_content)
        dulwich_repo.object_store.add_object(blob)

        # Create a tree with the README
        tree = Tree()
        tree.add(b"README.md", 0o100644, blob.id)
        dulwich_repo.object_store.add_object(tree)

        # Create initial commit
        commit = Commit()
        commit.tree = tree.id
        commit.author = commit.committer = b"LazyAF Test <test@lazyaf.local>"
        commit.author_time = commit.commit_time = 0
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = b"Initial commit for e2e testing"
        dulwich_repo.object_store.add_object(commit)

        # Set branch ref and HEAD
        branch_ref = f"refs/heads/{repo.default_branch}".encode()
        dulwich_repo.refs[branch_ref] = commit.id
        dulwich_repo.refs.set_symbolic_ref(b"HEAD", branch_ref)

        # Mark as ingested
        repo.is_ingested = True
        await db.commit()
        await db.refresh(repo)

        # Broadcast update
        await manager.send_repo_updated(repo_to_dict(repo))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to setup test repo: {e}")

    return repo


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


@router.get("/{repo_id}/branches/info")
async def get_branches_info(
    repo_id: str,
    verify: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Get detailed info about all branches, including orphan detection.
    Useful for branch management and cleanup.

    Args:
        verify: If True, verify object integrity for each branch (slower but detects damaged packs)
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    branches = git_repo_manager.get_branches_info(repo_id)
    orphaned_count = sum(1 for b in branches if b.get("is_orphaned"))
    damaged_count = 0

    # Optionally verify integrity
    if verify:
        integrity = git_repo_manager.verify_repo_integrity(repo_id)
        damaged_set = set(integrity.get("damaged_branches", []))

        # Get detailed missing objects info
        branch_integrity_map = {
            br.get("branch"): br for br in integrity.get("branches", [])
        }

        for branch in branches:
            if branch["name"] in damaged_set:
                branch["is_damaged"] = True
                branch_info = branch_integrity_map.get(branch["name"], {})
                branch["missing_objects"] = branch_info.get("missing_objects", [])
                branch["objects_checked"] = branch_info.get("objects_checked", 0)
                damaged_count += 1
            else:
                branch["is_damaged"] = False
                branch["missing_objects"] = []

    return {
        "branches": branches,
        "total": len(branches),
        "orphaned_count": orphaned_count,
        "damaged_count": damaged_count,
        "default_branch": repo.default_branch,
        "remote_url": repo.remote_url,
    }


@router.delete("/{repo_id}/branches/{branch_name:path}")
async def delete_branch(
    repo_id: str,
    branch_name: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a branch from the repository.
    The default branch cannot be deleted.
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    result = git_repo_manager.delete_branch(repo_id, branch_name)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/{repo_id}/cleanup-orphans")
async def cleanup_orphaned_branches(repo_id: str, db: AsyncSession = Depends(get_db)):
    """
    Remove all branches that point to non-existent commits.
    The default branch is protected and will not be deleted.
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    result = git_repo_manager.cleanup_orphaned_branches(repo_id)
    return result


@router.post("/{repo_id}/reinitialize")
async def reinitialize_repo(repo_id: str, db: AsyncSession = Depends(get_db)):
    """
    Completely reinitialize a repository, deleting all refs and objects.
    This is the nuclear option for fixing a corrupted repo.

    After calling this, the user must push their local repo again.
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    reinit_result = git_repo_manager.reinitialize_repo(repo_id)

    if not reinit_result["success"]:
        raise HTTPException(status_code=500, detail=reinit_result.get("error", "Reinitialize failed"))

    return reinit_result


@router.post("/{repo_id}/sync")
async def sync_repo_from_disk(repo_id: str, db: AsyncSession = Depends(get_db)):
    """
    Re-sync the repository state from disk.
    This is a "break-glass" operation to fix corrupted or inconsistent state.

    - Re-reads all refs from the git directory
    - Removes orphaned branches (refs pointing to non-existent commits)
    - Returns the cleaned-up list of valid branches
    """
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    if not repo.is_ingested:
        raise HTTPException(status_code=400, detail="Repo is not ingested")

    sync_result = git_repo_manager.sync_repo_from_disk(repo_id)

    if not sync_result["success"]:
        raise HTTPException(status_code=500, detail=sync_result.get("error", "Sync failed"))

    # Update default branch in DB if needed
    branches = sync_result.get("branches", [])
    if branches:
        default_branches = [b for b in branches if b.get("is_default")]
        if default_branches and default_branches[0]["name"] != repo.default_branch:
            repo.default_branch = default_branches[0]["name"]
            await db.commit()

    return sync_result
