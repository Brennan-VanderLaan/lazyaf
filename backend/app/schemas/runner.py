from datetime import datetime
from pydantic import BaseModel

from app.models.runner import RunnerStatus


class RunnerRead(BaseModel):
    id: str
    container_id: str | None = None
    status: RunnerStatus
    current_job_id: str | None = None
    last_heartbeat: datetime

    class Config:
        from_attributes = True
