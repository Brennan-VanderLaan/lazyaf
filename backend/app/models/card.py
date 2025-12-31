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
    ANY = "any"  # Any available runner
    CLAUDE_CODE = "claude-code"
    GEMINI = "gemini"


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
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_runner_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Type of runner that completed the job
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    repo: Mapped["Repo"] = relationship("Repo", back_populates="cards")
