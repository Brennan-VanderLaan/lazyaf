"""
Git HTTP smart protocol endpoints.

Implements the server side of git clone/fetch/push over HTTP.
"""

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.services.git_server import git_backend, git_repo_manager

router = APIRouter(prefix="/git", tags=["git"])


@router.get("/{repo_id}.git/info/refs")
async def get_info_refs(
    repo_id: str,
    service: str = Query(..., description="git-upload-pack or git-receive-pack"),
):
    """
    Refs discovery endpoint for git clone/fetch/push.

    GET /git/{repo_id}.git/info/refs?service=git-upload-pack  (clone/fetch)
    GET /git/{repo_id}.git/info/refs?service=git-receive-pack (push)
    """
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    if service not in ("git-upload-pack", "git-receive-pack"):
        raise HTTPException(status_code=400, detail="Invalid service")

    try:
        content, content_type = git_backend.get_info_refs(repo_id, service)
        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")


@router.post("/{repo_id}.git/git-upload-pack")
async def git_upload_pack(repo_id: str, request: Request):
    """
    Handle git clone/fetch pack negotiation.

    POST /git/{repo_id}.git/git-upload-pack
    """
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    body = await request.body()

    try:
        result = git_backend.handle_upload_pack(repo_id, body)
        return Response(
            content=result,
            media_type="application/x-git-upload-pack-result",
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")


@router.post("/{repo_id}.git/git-receive-pack")
async def git_receive_pack(repo_id: str, request: Request):
    """
    Handle git push pack reception.

    POST /git/{repo_id}.git/git-receive-pack
    """
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    body = await request.body()

    try:
        result = git_backend.handle_receive_pack(repo_id, body)
        return Response(
            content=result,
            media_type="application/x-git-receive-pack-result",
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Git error: {e}")


@router.get("/{repo_id}.git/HEAD")
async def get_head(repo_id: str):
    """Get HEAD reference (required by some git clients)."""
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    default_branch = git_repo_manager.get_default_branch(repo_id)
    if default_branch:
        content = f"ref: refs/heads/{default_branch}\n"
    else:
        content = "ref: refs/heads/main\n"

    return Response(
        content=content,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
        },
    )
