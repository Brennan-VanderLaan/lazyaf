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
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array of PipelineStep (legacy v1)
    steps_graph: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON PipelineGraphModel (v2 graph-based)
    triggers: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON array of TriggerConfig
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    repo: Mapped["Repo"] = relationship("Repo", back_populates="pipelines")
    runs: Mapped[list["PipelineRun"]] = relationship("PipelineRun", back_populates="pipeline", cascade="all, delete-orphan")

    def has_graph_definition(self) -> bool:
        """Check if pipeline uses graph-based (v2) definition."""
        return self.steps_graph is not None and self.steps_graph != ""


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.PENDING.value)
    trigger_type: Mapped[str] = mapped_column(String(50), default="manual")  # manual, webhook, card, push, schedule
    trigger_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trigger_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON with branch, sha, card_id
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    steps_completed: Mapped[int] = mapped_column(Integer, default=0)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    # Graph execution tracking (for parallel execution)
    active_step_ids: Mapped[str | None] = mapped_column(Text, nullable=True, default="[]")  # JSON: ["step_a", "step_b"]
    completed_step_ids: Mapped[str | None] = mapped_column(Text, nullable=True, default="[]")  # JSON: ["step_1", "step_2"]
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pipeline: Mapped["Pipeline"] = relationship("Pipeline", back_populates="runs")
    step_runs: Mapped[list["StepRun"]] = relationship("StepRun", back_populates="pipeline_run", cascade="all, delete-orphan")


class StepRun(Base):
    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    pipeline_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)  # Position for legacy pipelines
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Graph step ID (v2 pipelines)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.PENDING.value)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # Links to Job table when step creates a job
    logs: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    pipeline_run: Mapped["PipelineRun"] = relationship("PipelineRun", back_populates="step_runs")
    executions: Mapped[list["StepExecution"]] = relationship("StepExecution", back_populates="step_run", cascade="all, delete-orphan")


class StepExecutionStatus(str, Enum):
    """Status values for step executions."""
    PENDING = "pending"
    ASSIGNED = "assigned"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class StepExecution(Base):
    """
    Tracks individual execution attempts for a step.

    Each StepRun can have multiple StepExecutions (for retries).
    The execution_key ensures idempotent execution:
        execution_key = "{pipeline_run_id}:{step_index}:{attempt}"
    """
    __tablename__ = "step_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    execution_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    step_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("step_runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=StepExecutionStatus.PENDING.value)
    runner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # Remote executor only
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Local executor only
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: {"percent": 50, "message": "..."}
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    step_run: Mapped["StepRun"] = relationship("StepRun", back_populates="executions")
