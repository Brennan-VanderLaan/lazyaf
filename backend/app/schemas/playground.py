"""
Playground schemas for ephemeral agent testing.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas._datetime import UTCDateTime

# An agent prompt is a prompt, not a payload. 64 KiB is roughly ten thousand
# words - far past any real task description and far short of the 280 KB blob
# the QA probe posted whole with no complaint. Refusing at the edge (R1) beats
# discovering it inside a container that already cost money to start.
MAX_TASK_LENGTH = 64 * 1024


class PlaygroundTestRequest(BaseModel):
    """Request to start a playground test."""

    agent_id: str | None = None  # Platform agent file ID
    repo_agent_name: str | None = None  # OR repo-defined agent name
    # The vocabulary is NOT re-spelled here. `agent_run.AGENT_BY_RUNNER_TYPE`
    # is the one source of truth for which runner types exist (R3); this
    # field validates against it so the frontend and the backend cannot drift
    # into two different vocabularies again, and an unknown value is a loud
    # 422 rather than a silent fallback to claude-code.
    runner_type: str = "claude-code"
    model: str | None = None  # Specific model (e.g., claude-sonnet-4-20250514, gemini-2.5-pro)
    branch: str  # Branch to test against
    task_override: str | None = Field(default=None, max_length=MAX_TASK_LENGTH)
    save_to_branch: str | None = None  # If set, save changes to this branch

    @field_validator("runner_type")
    @classmethod
    def _known_runner_type(cls, value: str) -> str:
        # Imported lazily: the schema layer must not carry a service import at
        # module scope just to know a vocabulary.
        from app.services.agent_run import AGENT_BY_RUNNER_TYPE

        if value not in AGENT_BY_RUNNER_TYPE:
            known = ", ".join(sorted(AGENT_BY_RUNNER_TYPE))
            raise ValueError(
                f"unknown runner_type {value!r}; known runner types are {known}"
            )
        return value

    @field_validator("task_override")
    @classmethod
    def _task_not_blank(cls, value: str | None) -> str | None:
        """A whitespace-only prompt must not start an agent container.

        The button is guarded client-side too, but the button is not the
        contract - this is.
        """
        if value is None:
            return None
        if value.strip() == "":
            raise ValueError("task_override cannot be blank")
        return value


class PlaygroundTestResponse(BaseModel):
    """Response from starting a playground test."""

    session_id: str
    status: str  # "queued" | "running"
    message: str


class PlaygroundStatus(BaseModel):
    """Current status of a playground session."""

    session_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    # Where these facts came from. "session" is the live in-memory session;
    # "run" is the durable PipelineRun the session left behind, read after the
    # 30-minute in-memory TTL swept it (or after a backend restart).
    source: Literal["session", "run"] = "session"


class PlaygroundResult(BaseModel):
    """Result of a completed playground test."""

    session_id: str
    status: str
    diff: str | None = None  # Git diff output
    files_changed: list[str] = []
    branch_saved: str | None = None  # Branch name if saved
    error: str | None = None
    logs: str = ""
    duration_seconds: float | None = None
    # See PlaygroundStatus.source. This is load-bearing, not decoration: a
    # playground work branch is DELETED once its diff has been computed
    # (`agent_run._dispose_playground_branch`), so a result read from the
    # durable run record has the full transcript but CANNOT have the diff.
    # Reporting `diff: null` without saying why would be indistinguishable
    # from "the agent changed nothing", which is the silent-loss shape R1
    # forbids - the client renders a different sentence for source="run".
    source: Literal["session", "run"] = "session"


class PlaygroundSessionSummary(BaseModel):
    """One past playground run, read from its durable PipelineRun.

    12.5 already leaves a complete record of every playground run: a
    PipelineRun with ``trigger_type='playground'`` and
    ``trigger_ref=<session_id>``, whose single StepRun carries the transcript,
    hanging off a hidden ``__lazyaf_adhoc__:playground:<id>`` Pipeline that
    carries the prompt. History is a READ of that, not a new table.
    """

    session_id: str
    run_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    prompt: str
    agent: str | None = None
    model: str | None = None
    base_branch: str | None = None
    work_branch: str | None = None
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    duration_seconds: float | None = None
    # True while the in-memory session still exists, which is the only window
    # in which this run's DIFF can still be shown (see PlaygroundResult.source).
    live: bool = False


class PlaygroundLogEvent(BaseModel):
    """SSE event for log streaming."""

    type: str  # "log" | "tool" | "status" | "complete" | "error" | "ping"
    data: str
    timestamp: UTCDateTime
