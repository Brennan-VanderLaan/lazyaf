from pydantic import BaseModel

from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null
from app.schemas._strings import Name


class RepoBase(BaseModel):
    # Bare `str` on purpose: RepoRead inherits this and must keep serializing
    # rows written before the bound existed. The bound goes on the INPUT
    # schemas below. See app/schemas/_strings.py.
    name: str
    remote_url: str | None = None
    default_branch: str = "main"


class RepoCreate(RepoBase):
    """Create a new repo - will be ingested via CLI.

    If path is provided, files from that local path will be pushed
    to the internal git server automatically.
    """
    name: Name
    path: str | None = None  # Optional local path to push from


class RepoUpdate(BaseModel):
    name: Name | None = None
    remote_url: str | None = None
    default_branch: str | None = None

    # repos.remote_url is nullable (null clears it); the other two are not.
    _reject_nulls = not_null("name", "default_branch")


class RepoRead(RepoBase):
    id: str
    is_ingested: bool
    internal_git_url: str
    created_at: UTCDateTime

    class Config:
        from_attributes = True


class RepoIngest(BaseModel):
    """Response from ingest endpoint."""
    id: str
    name: str
    internal_git_url: str
    clone_url: str  # Full URL for git clone
