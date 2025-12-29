from datetime import datetime
from pydantic import BaseModel


class RepoBase(BaseModel):
    name: str
    path: str
    remote_url: str | None = None
    default_branch: str = "main"


class RepoCreate(RepoBase):
    pass


class RepoUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None


class RepoRead(RepoBase):
    id: str
    created_at: datetime

    class Config:
        from_attributes = True
