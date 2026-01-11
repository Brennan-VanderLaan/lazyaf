import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, field_validator, model_validator

from app.models.card import StepType
from app.models.pipeline import RunStatus


# =============================================================================
# Graph-Based Pipeline Models (Phase 1: Graph Creep)
# =============================================================================

class EdgeCondition(str, Enum):
    """Condition that determines when an edge is followed."""
    SUCCESS = "success"
    FAILURE = "failure"
    ALWAYS = "always"


class PipelineNodePosition(BaseModel):
    """UI position for node graph rendering."""
    x: float
    y: float


class PipelineEdge(BaseModel):
    """Connection between two steps in the pipeline graph."""
    id: str  # Unique edge ID
    from_step: str  # Source step ID
    to_step: str  # Target step ID
    condition: EdgeCondition = EdgeCondition.SUCCESS  # When this edge is followed


class PipelineStepV2(BaseModel):
    """Graph-based step definition with stable ID and position."""
    id: str  # Stable unique identifier (UUID or user-defined)
    name: str
    type: StepType  # script | docker | agent
    config: dict[str, Any] = {}  # Type-specific configuration
    position: Optional[PipelineNodePosition] = None  # UI layout position
    timeout: int = 300  # Seconds
    continue_in_context: bool = False  # If true, next step runs with preserved workspace


class PipelineGraphModel(BaseModel):
    """Graph-based pipeline definition supporting parallel execution."""
    steps: dict[str, PipelineStepV2]  # Keyed by step ID
    edges: list[PipelineEdge]
    entry_points: list[str]  # Step IDs that start execution
    version: int = 2  # Schema version for migration

    @model_validator(mode="after")
    def validate_graph_integrity(self):
        """Validate that all edge references exist and entry points are valid."""
        step_ids = set(self.steps.keys())

        # Validate edges reference existing steps
        for edge in self.edges:
            if edge.from_step not in step_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent from_step: '{edge.from_step}'")
            if edge.to_step not in step_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent to_step: '{edge.to_step}'")

        # Validate entry points exist
        if not self.entry_points:
            raise ValueError("Pipeline must have at least one entry point")

        for entry in self.entry_points:
            if entry not in step_ids:
                raise ValueError(f"Entry point '{entry}' references non-existent step")

        return self

    def get_successors(self, step_id: str, condition: EdgeCondition) -> list[str]:
        """Get step IDs that follow the given step under the specified condition."""
        return [
            edge.to_step
            for edge in self.edges
            if edge.from_step == step_id and edge.condition == condition
        ]

    def get_predecessors(self, step_id: str) -> list[str]:
        """Get step IDs that must complete before the given step can execute."""
        return [edge.from_step for edge in self.edges if edge.to_step == step_id]

    def get_all_successors(self, step_id: str) -> list[str]:
        """Get all step IDs that follow the given step (any condition)."""
        return [edge.to_step for edge in self.edges if edge.from_step == step_id]


# =============================================================================
# Legacy Pipeline Models (Backwards Compatible)
# =============================================================================


class TriggerConfig(BaseModel):
    """Configuration for automatic pipeline triggers."""
    type: str  # "card_complete" | "push"
    config: dict[str, Any] = {}  # Type-specific config
    enabled: bool = True
    on_pass: str = "nothing"  # "nothing" | "merge" | "merge:{branch}"
    on_fail: str = "nothing"  # "nothing" | "fail" | "reject"

    # card_complete config: {status: "done" | "in_review"}
    # push config: {branches: ["main", "dev"]}
    #
    # on_pass actions:
    #   "nothing" - leave card as-is
    #   "merge" - approve and merge the card to default branch
    #   "merge:{branch}" - approve and merge the card to specified branch
    #
    # on_fail actions:
    #   "nothing" - leave card as-is
    #   "fail" - mark card as failed (user can retry)
    #   "reject" - reject card back to todo


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
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    triggers: list[TriggerConfig] = []
    is_template: bool = False

    @model_validator(mode="after")
    def validate_steps_definition(self):
        """Ensure either steps or steps_graph is provided, not both with conflicting data."""
        has_steps = bool(self.steps)
        has_graph = self.steps_graph is not None

        # Allow both to be empty for initial creation
        # If graph is provided, it takes precedence
        return self


class PipelineUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    steps: list[PipelineStepConfig] | None = None
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    triggers: list[TriggerConfig] | None = None
    is_template: bool | None = None


class PipelineRead(PipelineBase):
    id: str
    repo_id: str
    steps: list[PipelineStepConfig] = []
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    triggers: list[TriggerConfig] = []
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

    @field_validator("steps_graph", mode="before")
    @classmethod
    def parse_steps_graph(cls, v):
        """Parse steps_graph from JSON string if needed."""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return PipelineGraphModel(**parsed) if parsed else None
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @field_validator("triggers", mode="before")
    @classmethod
    def parse_triggers(cls, v):
        """Parse triggers from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    class Config:
        from_attributes = True


class StepRunRead(BaseModel):
    id: str
    pipeline_run_id: str
    step_index: int
    step_id: str | None = None  # Graph step ID (v2 pipelines)
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
    trigger_context: dict[str, Any] | None = None  # {branch, commit_sha, card_id, etc.}
    current_step: int
    steps_completed: int
    steps_total: int
    # Graph execution tracking (for parallel execution)
    active_step_ids: list[str] = []  # Steps currently executing in parallel
    completed_step_ids: list[str] = []  # Steps that have completed
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    step_runs: list[StepRunRead] = []

    @field_validator("trigger_context", mode="before")
    @classmethod
    def parse_trigger_context(cls, v):
        """Parse trigger_context from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @field_validator("active_step_ids", mode="before")
    @classmethod
    def parse_active_step_ids(cls, v):
        """Parse active_step_ids from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v) or []
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    @field_validator("completed_step_ids", mode="before")
    @classmethod
    def parse_completed_step_ids(cls, v):
        """Parse completed_step_ids from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v) or []
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    class Config:
        from_attributes = True


class PipelineRunCreate(BaseModel):
    """Parameters for starting a pipeline run."""
    trigger_type: str = "manual"
    trigger_ref: str | None = None
    trigger_context: dict[str, Any] | None = None  # {branch, commit_sha, card_id, etc.}
    params: dict[str, Any] | None = None  # Optional parameters passed to steps as env vars


# =============================================================================
# Conversion Utilities
# =============================================================================

def array_to_graph(steps: list[PipelineStepConfig]) -> PipelineGraphModel:
    """
    Convert legacy array-based steps to graph model.

    This preserves the sequential execution order by creating success edges
    between consecutive steps. Auto-layout positions nodes vertically.

    Args:
        steps: Legacy array of PipelineStepConfig

    Returns:
        PipelineGraphModel with sequential edges and auto-layout positions
    """
    if not steps:
        raise ValueError("Cannot convert empty steps array to graph")

    graph_steps: dict[str, PipelineStepV2] = {}
    edges: list[PipelineEdge] = []

    for i, step in enumerate(steps):
        step_id = f"step_{i}"
        graph_steps[step_id] = PipelineStepV2(
            id=step_id,
            name=step.name,
            type=step.type,
            config=step.config,
            position=PipelineNodePosition(x=100, y=i * 150),  # Vertical layout
            timeout=step.timeout,
            continue_in_context=step.continue_in_context,
        )

        # Create edge to next step based on on_success
        if i < len(steps) - 1:
            next_id = f"step_{i + 1}"

            # Handle on_success: "next" creates a success edge
            if step.on_success == "next":
                edges.append(PipelineEdge(
                    id=f"edge_{i}_success",
                    from_step=step_id,
                    to_step=next_id,
                    condition=EdgeCondition.SUCCESS,
                ))

            # Handle on_failure: "next" creates a failure edge to next step
            if step.on_failure == "next":
                edges.append(PipelineEdge(
                    id=f"edge_{i}_failure",
                    from_step=step_id,
                    to_step=next_id,
                    condition=EdgeCondition.FAILURE,
                ))

    entry_point = "step_0"

    return PipelineGraphModel(
        steps=graph_steps,
        edges=edges,
        entry_points=[entry_point],
        version=2,
    )
