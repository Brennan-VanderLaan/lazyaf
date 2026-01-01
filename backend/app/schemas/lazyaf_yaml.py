"""
YAML schemas for repo-defined pipelines and agents.

These schemas define the structure for .lazyaf/ directory content:
- .lazyaf/pipelines/*.yaml - Pipeline definitions
- .lazyaf/agents/*.yaml - Agent definitions
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


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
    name: str = Field(..., description="Display name of the agent")
    description: Optional[str] = Field(None, description="Brief description of what this agent does")
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
    name: str = Field(..., description="Display name of the step")
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
    name: str = Field(..., description="Display name of the pipeline")
    description: Optional[str] = Field(None, description="Brief description of the pipeline")
    steps: list[PipelineStepYaml] = Field(default_factory=list, description="Ordered list of pipeline steps")


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
    source: str = Field(..., description="'repo' or 'platform'")
    branch: Optional[str] = Field(None, description="Branch the pipeline was read from (if repo)")
    filename: Optional[str] = Field(None, description="Filename in .lazyaf/pipelines/ (if repo)")
