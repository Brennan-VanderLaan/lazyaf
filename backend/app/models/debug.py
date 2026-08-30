"""DebugSession model - Phase 12.7 (table `debug_sessions`).

One row per debug re-run. It is the SINGLE source of truth for debug state
(R3): no `RunStatus` member was added for it, and no in-memory registry
mirrors it. The paused executor gate re-reads THIS ROW on every wake; the
`asyncio.Event` the service keeps is a wakeup, never a fact.

Adapted from failure_01's `models/debug_session.py` with four changes, each
fixing something the salvage audit named:

- **No second status enum.** failure_01 declared `DebugSessionStatus` here
  AND `DebugState` in `services/execution/debug_state.py`, with identical
  members and no test pinning them together. The vocabulary is imported from
  the state machine that owns it.
- **No `token` column.** The join credential is a short-lived, re-mintable
  JWT (`POST /api/debug/{id}/join-token`), so there is no stored secret to
  leak through a polled `GET`. Revocation is free: the terminal upgrade
  re-reads this row and refuses a terminal session whatever the JWT says.
- **`hit_breakpoints` is durable.** A key is appended when its gate fires, so
  a re-dispatch of the same step cannot re-pause on a breakpoint already
  serviced, and "which breakpoints never fired" is answerable at session end.
- **`end_reason` is NOT NULLable in spirit (R1).** Nothing sets a terminal
  status without also saying why; the column is nullable only because a
  non-terminal row has no reason yet.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.services.execution.debug_state import DebugState, TERMINAL_STATES


class DebugSession(Base):
    """A debug re-run session: breakpoints, pause state, and its deadline."""

    __tablename__ = "debug_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    #: The debug RE-RUN this session drives. UNIQUE: the executor gate looks a
    #: session up by run id on every step, and two sessions for one run would
    #: make "which one pauses this step?" unanswerable.
    pipeline_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id"),
        nullable=False,
        unique=True,
        index=True,
    )

    #: The failed run this re-runs (context only; may be None).
    original_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )

    #: DebugState vocabulary. String column, not a DB enum: the same choice
    #: every other status column in this schema makes.
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DebugState.PENDING.value,
        insert_default=DebugState.PENDING.value,
        index=True,
    )

    #: JSON list of STEP KEYS (see debug_state.debug_step_key), not indices.
    breakpoints: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: JSON list of step keys whose gate has already fired.
    hit_breakpoints: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- Where the run is paused (stamped at the gate, cleared on resume) ---
    current_step_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_step_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 'local' | 'remote'. The ONLY input to `attach_available`: a remote
    #: step's workspace lives on the runner host and the backend's docker
    #: client cannot see it (12.7 ships local attach only).
    current_step_executor: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: Set when a terminal first attaches; the container outlives a dropped
    #: CLI so a reconnect lands in the same shell host.
    sidecar_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 'sidecar' is the only value in 12.7 (a breakpoint is a PRE-step gate,
    #: so there is no step container to exec into).
    connection_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600, insert_default=3600
    )
    max_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14400, insert_default=14400
    )
    #: Authoritative pause deadline. `/extend` moves it; the paused gate is
    #: the sole owner of enforcing it (there is no monitor task).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    breakpoint_hit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: R1: a session never reaches a terminal state without saying why.
    end_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: DebugStateMachine.to_dict(). failure_01 declared this column and never
    #: wrote it; here every transition goes through the machine and lands.
    state_history: Mapped[str | None] = mapped_column(
        Text, nullable=True, default="[]"
    )

    pipeline_run: Mapped["PipelineRun"] = relationship(  # noqa: F821
        "PipelineRun",
        foreign_keys=[pipeline_run_id],
    )

    def __repr__(self) -> str:
        return f"<DebugSession {self.id[:8]} status={self.status}>"

    def is_terminal(self) -> bool:
        """True when the session can never transition again."""
        return self.status in {state.value for state in TERMINAL_STATES}

    def is_active(self) -> bool:
        """True while the session can still pause, connect or resume."""
        return not self.is_terminal()

    def is_at_breakpoint(self) -> bool:
        """True while a step is held at the gate."""
        return self.status in (
            DebugState.WAITING_AT_BP.value,
            DebugState.CONNECTED.value,
        )

    def is_connected(self) -> bool:
        """True while a terminal is attached."""
        return self.status == DebugState.CONNECTED.value


__all__ = ["DebugSession"]
