"""
YAML schemas for repo-defined pipelines and agents.

These schemas define the structure for .lazyaf/ directory content:
- .lazyaf/pipelines/*.yaml - Pipeline definitions
- .lazyaf/agents/*.yaml - Agent definitions
"""

from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError

from app.schemas._strings import Body, Name
from app.schemas.pipeline import (
    ArrayConversionError,
    PipelineGraphModel,
    PipelineStepConfig,
    TriggerConfig,
    array_to_graph,
)


class AgentYaml(BaseModel):
    """
    Schema for .lazyaf/agents/*.yaml files.

    Example:
    ```yaml
    name: "Test Fixer"
    description: "Specialized agent for fixing test failures"
    prompt_template: |
      You are a test specialist...
      ## Task
      {{description}}
    ```
    """
    name: Name = Field(..., description="Display name of the agent")
    description: Optional[Body] = Field(None, description="Brief description of what this agent does")
    prompt_template: str = Field(..., description="Prompt template with {{variable}} placeholders")


class PipelineStepYaml(BaseModel):
    """
    Schema for a step within a pipeline YAML.

    Example:
    ```yaml
    - id: "tests"
      name: "Run Tests"
      type: script
      config:
        command: pytest -v
      on_success: next
      on_failure: stop
      timeout: 300
      continue_in_context: true
    ```
    """
    id: Optional[str] = Field(None, description="Optional stable ID for context directory references")
    name: Name = Field(..., description="Display name of the step")
    # Deliberately a bare `str` and NOT StepType (12.8). This model is the
    # AUTHORING file as the user wrote it, and the read endpoints
    # (GET /api/repos/{id}/lazyaf/pipelines[/{name}]) exist to show that file
    # back verbatim. An unknown type is refused where it matters - at
    # materialization, where `pipeline_yaml_to_graph` builds the execution
    # definition and `PipelineStepConfig.type` IS the enum - and the refusal
    # lands on `Pipeline.definition_error` instead of making the file
    # unreadable.
    type: str = Field("script", description="Step type: agent, script, or docker")
    config: dict[str, Any] = Field(default_factory=dict, description="Type-specific configuration")
    on_success: str = Field("next", description="Action on success: next, stop, trigger:{id}, merge:{branch}")
    on_failure: str = Field("stop", description="Action on failure: next, stop, trigger:{id}")
    timeout: int = Field(300, description="Step timeout in seconds")
    continue_in_context: bool = Field(False, description="Preserve workspace for next step")


class PipelineYaml(BaseModel):
    """
    Schema for .lazyaf/pipelines/*.yaml files.

    Example:
    ```yaml
    name: "Test Suite"
    description: "Run tests on feature branches"
    triggers:
      - type: push
        config:
          branches: ["main"]
      - type: card_complete
        config:
          status: in_review
        on_pass: merge
        on_fail: reject
    steps:
      - name: "Install & Test"
        type: script
        config:
          command: |
            pip install -e ".[test]"
            pytest -v
        continue_in_context: true
      - name: "Fix Failures"
        type: agent
        config:
          title: "Fix Test Failures"
          description: "Review test output and fix failing tests"
          agent: "test-fixer"
        on_failure: stop
    ```
    """
    name: Name = Field(..., description="Display name of the pipeline")
    description: Optional[Body] = Field(None, description="Brief description of the pipeline")
    # Same shape as the platform Pipeline.triggers JSON (TriggerConfig) so
    # materialized rows can store it verbatim and trigger matching just works.
    triggers: list[TriggerConfig] = Field(default_factory=list, description="Trigger bindings synced onto the materialized platform pipeline")
    steps: list[PipelineStepYaml] = Field(default_factory=list, description="Ordered list of pipeline steps")


def pipeline_yaml_to_graph(pipeline_yaml: PipelineYaml) -> PipelineGraphModel:
    """The v2 execution graph for a repo-authored pipeline file (12.8 §4.4).

    This is the YAML door. `.lazyaf/pipelines/*.yaml` stays an ARRAY - that is
    the authoring format and it is not going anywhere - but the executor runs
    graphs and only graphs, so the array is converted HERE, once, at the
    boundary, instead of being persisted as a second definition format that
    the executor has to fork on.

    Two different refusals reach this function and both must come out as one:

      * `PipelineStepYaml.type` is a bare `str` while `PipelineStepConfig.type`
        is the `StepType` enum, so `type: banana` raises a pydantic
        ValidationError rather than an ArrayConversionError. Left as-is that
        is a 500 on the push path and a rolled-back sync (the whole push loses
        every OTHER pipeline it touched), so it is caught per step, attributed
        to the step that caused it, and re-raised as the same
        ArrayConversionError everything else raises.
      * `array_to_graph` refuses anything it cannot hold faithfully.

    Raises:
        ArrayConversionError: naming every step that made the file
            unconvertible. Callers turn this into `Pipeline.definition_error`
            (a visible refusal on the row) - never into a silent skip.
    """
    configs: list[PipelineStepConfig] = []
    reasons: list[str] = []

    for i, step in enumerate(pipeline_yaml.steps):
        try:
            configs.append(PipelineStepConfig.model_validate(step.model_dump()))
        except ValidationError as exc:
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "<step>"
                # The OFFENDING VALUE is the whole point of the message: "type
                # must be one of ..." without saying what was written leaves
                # the author hunting. Truncated because a step `config` is
                # `dict[str, Any]` and a yaml alias bomb expands inside it -
                # this string ends up in a database column and a UI badge.
                offender = repr(error.get("input"))
                if len(offender) > 120:
                    offender = offender[:117] + "..."
                reasons.append(
                    f"step #{i} ({step.name!r}) has an invalid {field}: "
                    f"{error['msg']} (got {offender})"
                )

    if reasons:
        raise ArrayConversionError(reasons)

    return array_to_graph(configs)


class RepoAgentResponse(BaseModel):
    """Response schema for repo-defined agents with source info."""
    name: str
    description: Optional[str] = None
    prompt_template: str
    source: str = Field(..., description="'repo' or 'platform'")
    branch: Optional[str] = Field(None, description="Branch the agent was read from (if repo)")
    filename: Optional[str] = Field(None, description="Filename in .lazyaf/agents/ (if repo)")


class RepoPipelineResponse(BaseModel):
    """Response schema for repo-defined pipelines with source info."""
    name: str
    description: Optional[str] = None
    steps: list[dict[str, Any]]
    triggers: list[dict[str, Any]] = Field(default_factory=list)
    source: str = Field(..., description="'repo' or 'platform'")
    branch: Optional[str] = Field(None, description="Branch the pipeline was read from (if repo)")
    filename: Optional[str] = Field(None, description="Filename in .lazyaf/pipelines/ (if repo)")
