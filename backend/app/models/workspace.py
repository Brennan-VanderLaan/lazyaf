"""
Workspace model for tracking pipeline execution workspaces.

A workspace is a Docker volume containing:
- Git checkout of the repository
- Home directory for persistent state
- Control directory for step configuration

Workspaces are tied to pipeline runs and can be shared across steps.
"""
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Integer, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkspaceStatus(str, Enum):
    """
    Workspace lifecycle states.

    Matches the state machine in workspace_state.py:
    - CREATING: Volume is being created/cloned
    - READY: Volume created, available for use
    - IN_USE: One or more steps actively using workspace
    - CLEANING: Cleanup in progress
    - CLEANED: Resources released, can be deleted
    - FAILED: Creation or cleanup failed
    """
    CREATING = "creating"
    READY = "ready"
    IN_USE = "in_use"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    FAILED = "failed"


class Workspace(Base):
    """
    Tracks a pipeline execution workspace (Docker volume).

    Each pipeline run gets one workspace that persists across all steps.
    The workspace contains the git checkout and home directory.
    """
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    # Links to the pipeline run
    pipeline_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id"),
        nullable=False,
        unique=True,  # One workspace per pipeline run
        index=True,
    )

    # Workspace status
    status: Mapped[str] = mapped_column(
        String(50),
        default=WorkspaceStatus.CREATING.value,
    )

    # Use count for concurrent step access
    use_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    # Docker volume name (same as id by convention)
    volume_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    # Repository info for workspace creation
    repo_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repos.id"),
        nullable=False,
    )

    # Branch/commit checked out in workspace
    branch: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    commit_sha: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    # Error message if failed
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # State machine history (JSON)
    state_history: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
    cleaned_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    # Relationships
    pipeline_run: Mapped["PipelineRun"] = relationship(
        "PipelineRun",
        back_populates="workspace",
    )
    repo: Mapped["Repo"] = relationship("Repo")

    def __init__(self, **kwargs):
        """Initialize Workspace with defaults."""
        if "status" not in kwargs:
            kwargs["status"] = WorkspaceStatus.CREATING.value
        if "id" not in kwargs:
            # Generate workspace ID from pipeline_run_id
            run_id = kwargs.get("pipeline_run_id", str(uuid4()))
            kwargs["id"] = f"lazyaf-ws-{run_id[:8]}"
        if "volume_name" not in kwargs:
            kwargs["volume_name"] = kwargs["id"]
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<Workspace {self.id} status={self.status} use_count={self.use_count}>"

    @classmethod
    def make_workspace_id(cls, pipeline_run_id: str) -> str:
        """
        Create a workspace ID from a pipeline run ID.

        Args:
            pipeline_run_id: Pipeline run ID

        Returns:
            Workspace ID string
        """
        return f"lazyaf-ws-{pipeline_run_id[:8]}"
