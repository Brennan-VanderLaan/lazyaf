import json
from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator

from app.models.card import CardStatus, RunnerType, StepType


class CardBase(BaseModel):
    title: str
    description: str = ""


class CardCreate(CardBase):
    runner_type: RunnerType = RunnerType.ANY
    step_type: StepType = StepType.AGENT
    step_config: dict[str, Any] | None = None  # {command: str} for script, {image: str, command: str} for docker
    prompt_template: str | None = None  # Custom prompt for AI agents (overrides global default)
    agent_file_ids: list[str] | None = None  # Agent files to make available


class CardUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: CardStatus | None = None
    runner_type: RunnerType | None = None
    step_type: StepType | None = None
    step_config: dict[str, Any] | None = None
    prompt_template: str | None = None
    agent_file_ids: list[str] | None = None


class CardRead(CardBase):
    id: str
    repo_id: str
    status: CardStatus
    runner_type: RunnerType = RunnerType.ANY
    step_type: StepType = StepType.AGENT
    step_config: dict[str, Any] | None = None
    prompt_template: str | None = None
    agent_file_ids: list[str] | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    job_id: str | None = None
    completed_runner_type: str | None = None
    created_at: datetime
    updated_at: datetime

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

    @field_validator("agent_file_ids", mode="before")
    @classmethod
    def parse_agent_file_ids(cls, v):
        """Parse agent_file_ids from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    class Config:
        from_attributes = True
