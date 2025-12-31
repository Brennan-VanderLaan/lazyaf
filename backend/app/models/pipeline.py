from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    repo_id: Mapped[str] = mapped_column(String(36), ForeignKey("repos.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array of PipelineStep
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    repo: Mapped["Repo"] = relationship("Repo", back_populates="pipelines")
    runs: Mapped[list["PipelineRun"]] = relationship("PipelineRun", back_populates="pipeline", cascade="all, delete-orphan")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.PENDING.value)
    trigger_type: Mapped[str] = mapped_column(String(50), default="manual")  # manual, webhook, card, push, schedule
    trigger_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    steps_completed: Mapped[int] = mapped_column(Integer, default=0)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pipeline: Mapped["Pipeline"] = relationship("Pipeline", back_populates="runs")
    step_runs: Mapped[list["StepRun"]] = relationship("StepRun", back_populates="pipeline_run", cascade="all, delete-orphan")


class StepRun(Base):
    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    pipeline_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.PENDING.value)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # Links to Job table when step creates a job
    logs: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    pipeline_run: Mapped["PipelineRun"] = relationship("PipelineRun", back_populates="step_runs")
