from datetime import datetime
from pydantic import BaseModel

from app.models.job import JobStatus


class JobRead(BaseModel):
    id: str
    card_id: str
    runner_id: str | None = None
    runner_type: str | None = None
    status: JobStatus
    logs: str = ""
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True
