from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    card_id: Mapped[str] = mapped_column(String(36), ForeignKey("cards.id"), nullable=False)
    runner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runner_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Type of runner that executed the job
    status: Mapped[str] = mapped_column(String(50), default=JobStatus.QUEUED.value)
    logs: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Step type and config (Phase 8.5)
    step_type: Mapped[str] = mapped_column(String(50), default="agent")  # agent, script, docker
    step_config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON config for the step
    # Test result fields (Phase 8)
    tests_run: Mapped[bool] = mapped_column(default=False)  # Whether tests were detected and run
    tests_passed: Mapped[bool | None] = mapped_column(nullable=True)  # None = not run, True = all passed, False = some failed
    test_pass_count: Mapped[int | None] = mapped_column(nullable=True)
    test_fail_count: Mapped[int | None] = mapped_column(nullable=True)
    test_skip_count: Mapped[int | None] = mapped_column(nullable=True)
    test_output: Mapped[str | None] = mapped_column(Text, nullable=True)  # Raw test output
