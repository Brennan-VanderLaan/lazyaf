"""
DebugSession model for tracking debug re-run sessions.

Debug sessions allow users to re-run failed pipelines with breakpoints,
connect via CLI for interactive debugging, and resume/abort execution.
"""
from datetime import datetime
from enum import Enum
from uuid import uuid4
import secrets

from sqlalchemy import String, DateTime, Text, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DebugSessionStatus(str, Enum):
    """
    Status of a debug session.

    State flow:
        PENDING -> WAITING_AT_BP -> CONNECTED -> ENDED
                       |               |
                       v               v
                    TIMEOUT         TIMEOUT

    - PENDING: Debug run started, executing before first breakpoint
    - WAITING_AT_BP: At breakpoint, waiting for user to connect
    - CONNECTED: User connected via CLI
    - TIMEOUT: Session timed out
    - ENDED: User resumed/aborted, or pipeline completed
    """
    PENDING = "pending"
    WAITING_AT_BP = "waiting_at_bp"
    CONNECTED = "connected"
    TIMEOUT = "timeout"
    ENDED = "ended"


class DebugSession(Base):
    """
    Tracks a debug re-run session.

    Each debug session:
    - Links to a pipeline run (the debug re-run)
    - Stores breakpoints (step indices to pause before)
    - Tracks current state (pending, waiting, connected, etc.)
    - Has an auth token for CLI connections
    - Has timeout settings
    """
    __tablename__ = "debug_sessions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )

    # Links to the pipeline run being debugged
    pipeline_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id"),
        nullable=False,
        index=True,
    )

    # Original pipeline run that failed (for context)
    original_run_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )

    # Session status
    status: Mapped[str] = mapped_column(
        String(50),
        default=DebugSessionStatus.PENDING.value,
        insert_default=DebugSessionStatus.PENDING.value,
    )

    def __init__(self, **kwargs):
        """Initialize DebugSession with defaults."""
        # Apply default for status if not provided
        if "status" not in kwargs:
            kwargs["status"] = DebugSessionStatus.PENDING.value
        # Generate token if not provided
        if "token" not in kwargs:
            kwargs["token"] = secrets.token_urlsafe(32)
        super().__init__(**kwargs)

    # Breakpoints - JSON array of step indices
    # e.g., "[0, 2, 4]" means pause before steps 0, 2, and 4
    breakpoints: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    # Current breakpoint info (when waiting)
    current_step_index: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    current_step_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Connection mode (when connected)
    # "sidecar" - debug container for filesystem inspection
    # "shell" - exec into running step container
    connection_mode: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    # Container info for active connection
    sidecar_container_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # Authentication token for CLI connections
    token: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    # Timeout settings (in seconds)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=3600,  # 1 hour default
    )
    max_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=14400,  # 4 hours max
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
    breakpoint_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    # State history (JSON array of state transitions)
    state_history: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default="[]",
    )

    # Relationship to PipelineRun
    pipeline_run: Mapped["PipelineRun"] = relationship(
        "PipelineRun",
        foreign_keys=[pipeline_run_id],
    )

    def __repr__(self) -> str:
        return f"<DebugSession {self.id[:8]} status={self.status}>"

    def is_terminal(self) -> bool:
        """Check if session is in a terminal state."""
        return self.status in {
            DebugSessionStatus.TIMEOUT.value,
            DebugSessionStatus.ENDED.value,
        }

    def is_active(self) -> bool:
        """Check if session is still active (not terminal)."""
        return not self.is_terminal()

    def is_at_breakpoint(self) -> bool:
        """Check if session is waiting at a breakpoint."""
        return self.status == DebugSessionStatus.WAITING_AT_BP.value

    def is_connected(self) -> bool:
        """Check if user is connected."""
        return self.status == DebugSessionStatus.CONNECTED.value
