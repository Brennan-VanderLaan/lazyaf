import json
from typing import Any
from pydantic import BaseModel, field_validator

from app.models.job import JobStatus
from app.schemas._datetime import UTCDateTime


class JobRead(BaseModel):
    id: str
    card_id: str
    runner_id: str | None = None
    runner_type: str | None = None
    status: JobStatus
    logs: str = ""
    error: str | None = None
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    created_at: UTCDateTime
    # Step type and config (Phase 8.5)
    step_type: str = "agent"
    step_config: dict[str, Any] | None = None
    # Test result fields (Phase 8)
    tests_run: bool = False
    tests_passed: bool | None = None
    test_pass_count: int | None = None
    test_fail_count: int | None = None
    test_skip_count: int | None = None
    test_output: str | None = None

    @field_validator("step_config", mode="before")
    @classmethod
    def parse_step_config(cls, v):
        """Parse step_config from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    class Config:
        from_attributes = True
