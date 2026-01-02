"""
Git HTTP smart protocol endpoints.

Implements the server side of git clone/fetch/push over HTTP.
"""

import gzip

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.git_server import git_backend, git_repo_manager

router = APIRouter(prefix="/git", tags=["git"])


class PushEventRequest(BaseModel):
    """Request body for internal push event notification."""
    branch: str
    new_sha: str
    old_sha: str = ""


async def get_request_body(request: Request) -> bytes:
    """Get request body, decompressing gzip if needed."""
    body = await request.body()
    content_encoding = request.headers.get("content-encoding", "").lower()

    if content_encoding == "gzip" or (len(body) > 2 and body[:2] == b'\x1f\x8b'):
        # Decompress gzip data
        try:
            body = gzip.decompress(body)
            print(f"[git] Decompressed gzip: {len(body)} bytes")
        except Exception as e:
            print(f"[git] Failed to decompress gzip: {e}")
            # Continue with original body if decompression fails

    return body


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

    body = await get_request_body(request)
    print(f"[git] upload-pack request: {len(body)} bytes")

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
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Git error: {e}")


@router.post("/{repo_id}.git/git-receive-pack")
async def git_receive_pack(repo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle git push pack reception.

    POST /git/{repo_id}.git/git-receive-pack

    After successfully processing the push, this triggers any matching
    push-based pipeline triggers for the pushed branches.
    """
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    body = await get_request_body(request)

    try:
        result = git_backend.handle_receive_pack(repo_id, body)
        output = result["output"]
        pushed_refs = result.get("pushed_refs", [])

        # Trigger pipelines for each pushed branch
        if pushed_refs:
            from app.services.trigger_service import trigger_service
            for ref in pushed_refs:
                try:
                    await trigger_service.on_push(
                        db=db,
                        repo_id=repo_id,
                        branch=ref["branch"],
                        commit_sha=ref["new_sha"],
                        old_sha=ref["old_sha"],
                    )
                except Exception as e:
                    # Don't fail the push if trigger fails
                    print(f"[git] Warning: Push trigger failed: {e}")

        return Response(
            content=output,
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


@router.post("/{repo_id}.git/_internal/push-event")
async def handle_push_event(
    repo_id: str,
    event: PushEventRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Internal endpoint called after git push to trigger pipelines.

    This endpoint is called by the git server after successfully processing
    a git push. It checks for matching push triggers and starts pipelines.
    """
    if not git_repo_manager.repo_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    from app.services.trigger_service import trigger_service

    runs = await trigger_service.on_push(
        db=db,
        repo_id=repo_id,
        branch=event.branch,
        commit_sha=event.new_sha,
        old_sha=event.old_sha,
    )

    return {
        "triggered_runs": len(runs),
        "run_ids": [r.id for r in runs],
    }
