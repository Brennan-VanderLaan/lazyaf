"""
Workspace model for pipeline execution workspaces (Phase 12.2-INT).

A workspace is a named Docker volume containing the repo checkout
(/workspace/repo) plus per-run persistent state. Exactly one workspace
exists per pipeline run (unique pipeline_run_id).

Design notes (per the failure_01 salvage audit):
- Status vocabulary comes from main's tested WorkspaceStatus enum
  (app.services.workspace.state_machine) — the model does NOT define its
  own copy.
- Volume names carry the FULL pipeline_run_id via generate_volume_name
  (failure_01 truncated to 8 chars, which collides and orphans volumes).
- NO SQLAlchemy relationship into the pipeline models: consumers query by
  pipeline_run_id. This keeps the model free of lazy-load traps
  (MissingGreenlet) in async services and avoids coupling migrations.
- failure_01's never-written state_history column is dropped (audit:
  "drop or wire" — dropped; history lives in logs).

Migration note: this model is defined WITHOUT an alembic revision; the
serialized migration stage authors it (0002/0003). Tests rely on
conftest's Base.metadata.create_all.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.workspace.state_machine import WorkspaceStatus


class Workspace(Base):
    """Tracks a pipeline run's workspace (a named Docker volume).

    Lifecycle (WorkspaceStatus): creating -> ready <-> in_use -> cleaning
    -> cleaned, with failed reachable from creating/cleaning and
    retryable via cleaning. use_count mirrors WorkspaceStateMachine's
    concurrent-step accounting and is persisted here.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # One workspace per pipeline run. Plain column, no FK/relationship —
    # query by id (see module docstring).
    pipeline_run_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True
    )

    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # Full docker volume name: lazyaf-ws-{pipeline_run_id} (generate_volume_name).
    volume_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkspaceStatus.CREATING.value, index=True
    )

    # Number of steps currently using the workspace (WorkspaceStateMachine).
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What was (or will be) checked out into /workspace/repo.
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Populated when creation/cleanup fails (R1: failures stay observable).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Doubles as the last-activity timestamp for orphan detection.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Workspace {self.id} run={self.pipeline_run_id} "
            f"status={self.status} use_count={self.use_count}>"
        )
