"""
Runner model for remote runner management.

Phase 12.6: Enhanced for WebSocket-based remote execution.
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RunnerStatus(str, Enum):
    """
    Runner status values.

    Phase 12.6: Extended for WebSocket-based lifecycle.
    """
    DISCONNECTED = "disconnected"  # No WebSocket connection
    CONNECTING = "connecting"      # WebSocket open, registration pending
    IDLE = "idle"                  # Ready to accept jobs
    ASSIGNED = "assigned"          # Job sent, awaiting ACK
    BUSY = "busy"                  # Executing step
    DEAD = "dead"                  # Heartbeat timeout, presumed crashed

    # Legacy values (for backward compat during migration)
    OFFLINE = "offline"            # Deprecated, use DISCONNECTED


class Runner(Base):
    """
    Remote runner that connects via WebSocket to execute steps.

    Runners can be:
    - Docker hosts that spawn containers
    - Embedded devices with native execution
    - GPU servers with CUDA support
    """
    __tablename__ = "runners"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )

    # Human-readable name
    name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True
    )

    # Runner type: claude-code, gemini, generic
    runner_type: Mapped[str] = mapped_column(
        String(50),
        default="claude-code"
    )

    # Current status
    status: Mapped[str] = mapped_column(
        String(50),
        default=RunnerStatus.DISCONNECTED.value
    )

    # Labels for requirement matching (JSON stored as text)
    # Example: {"arch": "arm64", "has": ["gpio", "camera"]}
    labels: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True
    )

    # Current step execution (FK to step_executions)
    current_step_execution_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("step_executions.id"),
        nullable=True
    )

    # WebSocket connection tracking
    websocket_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        unique=True
    )

    # Legacy field - kept for backward compat during migration
    container_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True
    )

    # Legacy field - kept for backward compat during migration
    current_job_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True
    )

    # Timestamps
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )

    connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True
    )

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )

    # Relationships
    # current_step_execution: Mapped[Optional["StepExecution"]] = relationship(
    #     "StepExecution",
    #     back_populates="runner",
    #     foreign_keys=[current_step_execution_id]
    # )

    def __repr__(self) -> str:
        return f"Runner(id={self.id!r}, name={self.name!r}, status={self.status!r})"

    @property
    def is_available(self) -> bool:
        """Check if runner is available to accept jobs."""
        return self.status == RunnerStatus.IDLE.value

    @property
    def is_connected(self) -> bool:
        """Check if runner has an active WebSocket connection."""
        return self.status in {
            RunnerStatus.CONNECTING.value,
            RunnerStatus.IDLE.value,
            RunnerStatus.ASSIGNED.value,
            RunnerStatus.BUSY.value,
        }

    def get_labels(self) -> dict:
        """Parse labels from JSON string."""
        if not self.labels:
            return {}
        import json
        try:
            return json.loads(self.labels)
        except json.JSONDecodeError:
            return {}

    def set_labels(self, labels: dict) -> None:
        """Set labels as JSON string."""
        import json
        self.labels = json.dumps(labels)

    def matches_requirements(self, requirements: dict) -> bool:
        """
        Check if runner matches the given requirements.

        Args:
            requirements: Dict with keys like 'arch', 'has'

        Returns:
            True if all requirements are satisfied
        """
        labels = self.get_labels()

        # Check architecture
        if "arch" in requirements:
            if labels.get("arch") != requirements["arch"]:
                return False

        # Check 'has' capabilities
        if "has" in requirements:
            runner_has = set(labels.get("has", []))
            required_has = set(requirements["has"])
            if not required_has.issubset(runner_has):
                return False

        return True
