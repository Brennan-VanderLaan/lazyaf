"""
Workspace model for pipeline execution workspaces (Phase 12.2-INT).

A workspace is a named Docker volume containing the repo checkout
(/workspace/repo) plus per-run persistent state. A pipeline run owns one
workspace PER LANE (``(pipeline_run_id, worker_key)`` is unique) — normally
exactly one, the ``default`` lane, but K parallel workers of a fan-out each
get their own independent checkout (M13-1). That is the schema fact the
"planner + parallel cheap workers integrating through git" hypothesis needs:
without it, K parallel steps all write into ONE working tree and any
conflict rate measured is measuring the schema, not the strategy.

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

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.workspace.state_machine import WorkspaceStatus
from app.services.workspace.worker_key import DEFAULT_WORKER_KEY


class Workspace(Base):
    """Tracks a pipeline run's workspace (a named Docker volume).

    Lifecycle (WorkspaceStatus): creating -> ready <-> in_use -> cleaning
    -> cleaned, with failed reachable from creating/cleaning and
    retryable via cleaning. use_count mirrors WorkspaceStateMachine's
    concurrent-step accounting and is persisted here — it counts the
    concurrent HOLDERS OF ONE LANE (parallel entry points in the same lane,
    plus the debug gate pinning the volume while a paused step still holds
    it), which is why it stays an integer rather than becoming a boolean.
    """

    __tablename__ = "workspaces"

    # ONE row per (run, lane). Declared as a real Index rather than a
    # UniqueConstraint so create_all and migration 0012 produce the same
    # named object (the migration-parity test compares index names).
    #
    # worker_key is NOT NULL and has a sentinel on purpose: both SQLite and
    # Postgres treat NULLs as DISTINCT inside a unique index, so a nullable
    # lane column would constrain NOTHING for exactly the rows that are the
    # common case — a run could then accumulate unlimited duplicate default
    # workspaces, each with its own volume, only one of which anything ever
    # finds again.
    __table_args__ = (
        Index(
            "uq_workspaces_run_worker", "pipeline_run_id", "worker_key", unique=True
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # Plain column, no FK/relationship — query by id (see module docstring).
    # NOT unique on its own any more: a run owns one workspace per lane.
    # No separate index either — it is the LEADING column of
    # uq_workspaces_run_worker, which serves every WHERE pipeline_run_id = ?
    # lookup on its own.
    pipeline_run_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Which checkout of the run this is. "default" is the trunk lane every
    # pre-M13 pipeline uses; a fan-out names one lane per worker ("w1"...).
    worker_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_WORKER_KEY,
        server_default=DEFAULT_WORKER_KEY,
    )

    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # Full docker volume name (generate_volume_name):
    #   lazyaf-ws-{pipeline_run_id}          — the default lane
    #   lazyaf-ws-{pipeline_run_id}-{slug}   — any other lane
    # Still UNIQUE, as a second and independent guard: it turns a bug in the
    # naming function (two lanes colliding onto one name) into a loud
    # IntegrityError instead of two workspaces silently sharing one volume.
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
            f"lane={self.worker_key} status={self.status} "
            f"use_count={self.use_count}>"
        )
