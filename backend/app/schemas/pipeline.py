import json
from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator

from app.models.card import StepType
from app.models.pipeline import RunStatus


class PipelineStepConfig(BaseModel):
    """Configuration for a pipeline step (stored in Pipeline.steps JSON array)."""
    name: str
    type: StepType
    config: dict[str, Any] = {}  # Type-specific: {command}, {image, command}, {runner_type, title, description}
    on_success: str = "next"  # "next" | "stop" | "trigger:{card_id}" | "merge:{branch}"
    on_failure: str = "stop"  # "next" | "stop" | "trigger:{card_id}"
    timeout: int = 300  # Seconds
    continue_in_context: bool = False  # If true, next step runs in same container with preserved workspace


class PipelineBase(BaseModel):
    name: str
    description: str | None = None


class PipelineCreate(PipelineBase):
    steps: list[PipelineStepConfig] = []
    is_template: bool = False


class PipelineUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    steps: list[PipelineStepConfig] | None = None
    is_template: bool | None = None


class PipelineRead(PipelineBase):
    id: str
    repo_id: str
    steps: list[PipelineStepConfig] = []
    is_template: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("steps", mode="before")
    @classmethod
    def parse_steps(cls, v):
        """Parse steps from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return []
        return v

    class Config:
        from_attributes = True


class StepRunRead(BaseModel):
    id: str
    pipeline_run_id: str
    step_index: int
    step_name: str
    status: RunStatus
    job_id: str | None = None
    logs: str = ""
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    class Config:
        from_attributes = True


class PipelineRunRead(BaseModel):
    id: str
    pipeline_id: str
    status: RunStatus
    trigger_type: str
    trigger_ref: str | None = None
    current_step: int
    steps_completed: int
    steps_total: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    step_runs: list[StepRunRead] = []

    class Config:
        from_attributes = True


class PipelineRunCreate(BaseModel):
    """Parameters for starting a pipeline run."""
    trigger_type: str = "manual"
    trigger_ref: str | None = None
    params: dict[str, Any] | None = None  # Optional parameters passed to steps as env vars
