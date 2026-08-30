"""Runner registry row - Phase 12.6.

The `runners` table was dead: five polling-era columns, imported by
`routers/runners.py` and never queried, while `RunnerPool` held 100% of the
truth in memory. 12.6 makes this table the DURABLE PROJECTION of a live
WebSocket connection - the thing a backend restart, a second worker, or a
dispatcher scanning for a match can actually read.

Status vocabulary (cross-agent contract #4): `RunnerState` from
`app.services.execution.runner_state` is the SINGLE vocabulary for the state
machine, this column, the API and the UI. The old three-value `RunnerStatus`
(idle/busy/offline) is DELETED - two enums for one concept is exactly how
failure_01 ended up with a DB status that never left "idle" while the state
machine walked a different path.

Import note: `RunnerState` is imported lazily inside the methods that need
it. A model module must not pull in `app.services.execution.__init__`, which
imports `local_executor` (and therefore `docker`) - a models import is not
the place to require a container runtime.
"""
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: Duplicated from RunnerState.DISCONNECTED for the column default only (see
#: the import note above). Pinned equal by tdd/unit/models/test_runner_model.py.
_DEFAULT_STATUS = "disconnected"

#: Default when an agent registers without one. Matches the migration's
#: server_default so a row created by either path reads the same.
DEFAULT_RUNNER_TYPE = "claude-code"


class Runner(Base):
    """A runner known to this backend, live or not.

    A row exists from first enrollment onward; `status` says whether it is
    reachable right now. `websocket_id` is the split-brain fence: it changes
    on every connection, so a message from a superseded socket is detectably
    stale even when it carries the right runner_id.
    """

    __tablename__ = "runners"

    #: Client-asserted runner id (stable across agent restarts), or uuid4.
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runner_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=DEFAULT_RUNNER_TYPE,
        server_default=DEFAULT_RUNNER_TYPE,
    )
    #: A RunnerState value. Indexed: the dispatcher scans for idle runners.
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=_DEFAULT_STATUS, index=True
    )
    #: JSON dict of advertised labels: {"arch": "arm64", "has": ["gpio"]}.
    labels: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: WRITTEN ON EVERY ASSIGNMENT. failure_01 declared this column and never
    #: wrote it, which silently neutered the whole of job recovery: a dead
    #: runner's in-flight step was unfindable, so nothing was ever requeued.
    current_step_execution_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("step_executions.id"), nullable=True
    )
    #: uuid4 per CONNECTION (not per runner). Unique index: two live sockets
    #: cannot both claim to be this runner's current connection.
    websocket_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    #: Forensics: what the agent said it speaks / what build it is.
    protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Stamped BACKEND-SIDE at receipt, never from the wire. A runner with a
    #: clock hours off must not be able to make itself immortal (or instantly
    #: dead) by supplying its own timestamp.
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=datetime.utcnow
    )

    # -- labels ---------------------------------------------------------------

    def get_labels(self) -> dict:
        """Decode the labels JSON. Malformed content reads as {}: a corrupt
        label blob must make a runner match NOTHING, never crash the
        dispatcher scanning every row."""
        if not self.labels:
            return {}
        try:
            decoded = json.loads(self.labels)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def set_labels(self, labels: dict | None) -> None:
        self.labels = json.dumps(labels or {}, sort_keys=True)

    # -- matching (section 2.4 grammar) ---------------------------------------

    def matches_requirements(self, requirements: dict | None) -> bool:
        """Does this runner satisfy a step's `requires:` block?

        Grammar - empty requirements match everything:

            runner_id   -> exact match against self.id
            runner_type -> exact match against self.runner_type;
                           "any" matches everything
            arch        -> normalize_arch equality against labels["arch"]
            has         -> set(required) <= set(labels.get("has", []))
            any other k -> equality against labels.get(k)

        The last rule is the point. failure_01 IGNORED unknown keys, so
        `requires: {gpu: a100}` matched every runner in the fleet. Generic
        label equality makes free-form labels useful AND makes an
        unsatisfiable pin visibly unsatisfiable instead of silently
        universal.
        """
        from app.services.execution.runner_protocol import normalize_arch

        if not requirements:
            return True

        labels = self.get_labels()

        for key, wanted in requirements.items():
            if key == "runner_id":
                if self.id != wanted:
                    return False
            elif key == "runner_type":
                if wanted not in ("any", None, "") and self.runner_type != wanted:
                    return False
            elif key == "arch":
                if normalize_arch(labels.get("arch")) != normalize_arch(wanted):
                    return False
            elif key == "has":
                wanted_set = set(_as_list(wanted))
                if not wanted_set.issubset(set(_as_list(labels.get("has")))):
                    return False
            else:
                if labels.get(key) != wanted:
                    return False

        return True

    # -- availability ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Ready to accept an assignment. Delegates to RunnerState so the
        property and the state machine cannot drift."""
        from app.services.execution.runner_state import RunnerState

        return self.status == RunnerState.IDLE.value

    @property
    def is_connected(self) -> bool:
        """Has a live socket. Delegates to RunnerState.is_connected's set."""
        from app.services.execution.runner_state import RunnerState

        return self.status in {
            RunnerState.CONNECTING.value,
            RunnerState.IDLE.value,
            RunnerState.ASSIGNED.value,
            RunnerState.BUSY.value,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Runner {self.id} status={self.status} "
            f"type={self.runner_type} step={self.current_step_execution_id}>"
        )


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]
