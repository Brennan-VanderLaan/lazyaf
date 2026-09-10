from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CardStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"


class RunnerType(str, Enum):
    """Which AGENT executes a card.

    ONE list, and it is the card-shaped half of
    `agent_run.AGENT_BY_RUNNER_TYPE` (cross-agent contract #5) - a value
    missing here cannot be saved even though dispatch understands it, which
    is exactly the gap `openai-harness` sat in: the card modal offered
    "Self-hosted endpoint", every layer below accepted it, and CardCreate
    422'd on the way past. A test pins the two together
    (test_cards_api.TestCardModelSelection.test_runner_type_enum_covers_the_agent_vocabulary).

    NOT a DB migration: `Card.runner_type` is String(50) and stores the wire
    value, so adding a member widens what validation accepts and touches no
    column.
    """

    ANY = "any"  # Any available runner
    CLAUDE_CODE = "claude-code"
    GEMINI = "gemini"
    MOCK = "mock"  # Mock executor for E2E testing
    # M14. LazyAF supplies the agent loop and drives a model the operator
    # hosts; the endpoint is named in `step_config.model` as `endpoint:<name>`.
    OPENAI_HARNESS = "openai-harness"


class StepType(str, Enum):
    AGENT = "agent"      # AI agent (Claude/Gemini) implements feature
    SCRIPT = "script"    # Run shell command directly
    DOCKER = "docker"    # Run command in specified container image


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("repos.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(50), default=CardStatus.TODO.value)
    runner_type: Mapped[str] = mapped_column(String(50), default=RunnerType.ANY.value)
    # Step type and config (Phase 8.5)
    step_type: Mapped[str] = mapped_column(String(50), default=StepType.AGENT.value)
    step_config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON config for script/docker steps
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)  # Custom prompt for AI agents
    agent_file_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of agent file IDs
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_runner_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Type of runner that completed the job
    # Pipeline association (Phase 9.1)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=True)
    pipeline_step_index: Mapped[int | None] = mapped_column(nullable=True)  # Step index in the pipeline
    # Spec layer links (Phase 12.2.5)
    feature_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("features.id"), nullable=True)
    user_story_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("user_stories.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    repo: Mapped["Repo"] = relationship("Repo", back_populates="cards")
