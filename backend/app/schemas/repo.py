from datetime import datetime
from pydantic import BaseModel


class RepoBase(BaseModel):
    name: str
    remote_url: str | None = None
    default_branch: str = "main"


class RepoCreate(RepoBase):
    """Create a new repo - will be ingested via CLI.

    If path is provided, files from that local path will be pushed
    to the internal git server automatically.
    """
    path: str | None = None  # Optional local path to push from


class RepoUpdate(BaseModel):
    name: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None


class RepoRead(RepoBase):
    id: str
    is_ingested: bool
    internal_git_url: str
    created_at: datetime

    class Config:
        from_attributes = True


class RepoIngest(BaseModel):
    """Response from ingest endpoint."""
    id: str
    name: str
    internal_git_url: str
    clone_url: str  # Full URL for git clone
