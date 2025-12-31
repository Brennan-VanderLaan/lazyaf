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
    # Test result fields (Phase 8)
    tests_run: bool = False
    tests_passed: bool | None = None
    test_pass_count: int | None = None
    test_fail_count: int | None = None
    test_skip_count: int | None = None
    test_output: str | None = None

    class Config:
        from_attributes = True
