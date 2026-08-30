import json
from typing import Any
from pydantic import BaseModel, field_validator

from app.models.card import CardStatus, RunnerType, StepType
from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null
from app.schemas._strings import Body, Name


class CardBase(BaseModel):
    # Bare `str` on purpose: CardRead inherits this and must keep serializing
    # rows written before the bound existed. The bound goes on the INPUT
    # schemas below. See app/schemas/_strings.py.
    title: str
    description: str = ""


class CardCreate(CardBase):
    title: Name
    description: Body = ""
    runner_type: RunnerType = RunnerType.ANY
    step_type: StepType = StepType.AGENT
    step_config: dict[str, Any] | None = None  # {command: str} for script, {image: str, command: str} for docker
    prompt_template: str | None = None  # Custom prompt for AI agents (overrides global default)
    agent_file_ids: list[str] | None = None  # Agent files to make available


class CardUpdate(BaseModel):
    title: Name | None = None
    description: Body | None = None
    status: CardStatus | None = None
    runner_type: RunnerType | None = None
    step_type: StepType | None = None
    step_config: dict[str, Any] | None = None
    prompt_template: str | None = None
    agent_file_ids: list[str] | None = None
    # Spec layer links (Phase 12.2.5) — explicit None unlinks, absent leaves unchanged
    feature_id: str | None = None
    user_story_id: str | None = None

    # NOT NULL columns: null is a client error (422), not a 500. The rest
    # (step_config, prompt_template, agent_file_ids, feature_id,
    # user_story_id) are nullable and null is how a client clears them.
    _reject_nulls = not_null(
        "title", "description", "status", "runner_type", "step_type"
    )


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
    # Pipeline association
    pipeline_run_id: str | None = None
    pipeline_step_index: int | None = None
    # Spec layer links (Phase 12.2.5)
    feature_id: str | None = None
    user_story_id: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime

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
