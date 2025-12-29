from datetime import datetime
from pydantic import BaseModel

from app.models.card import CardStatus


class CardBase(BaseModel):
    title: str
    description: str = ""


class CardCreate(CardBase):
    pass


class CardUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: CardStatus | None = None


class CardRead(CardBase):
    id: str
    repo_id: str
    status: CardStatus
    branch_name: str | None = None
    pr_url: str | None = None
    job_id: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
