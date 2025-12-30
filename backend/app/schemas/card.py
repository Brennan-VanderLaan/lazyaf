from datetime import datetime
from pydantic import BaseModel

from app.models.card import CardStatus, RunnerType


class CardBase(BaseModel):
    title: str
    description: str = ""


class CardCreate(CardBase):
    runner_type: RunnerType = RunnerType.ANY


class CardUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: CardStatus | None = None
    runner_type: RunnerType | None = None


class CardRead(CardBase):
    id: str
    repo_id: str
    status: CardStatus
    runner_type: RunnerType = RunnerType.ANY
    branch_name: str | None = None
    pr_url: str | None = None
    job_id: str | None = None
    completed_runner_type: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
