"""Runner read models - Phase 12.6.

The runners API becomes READ-ONLY over the registry: there is no register /
heartbeat / claim-a-job surface any more, so there is no request schema here
either. What remains is the projection the UI renders and
`scripts/verify_executor.py` asserts against.

`status` is a plain string carrying a `RunnerState` value (cross-agent
contract #4). Deliberately not typed as the enum: an unknown status written
by a future backend must render in the panel as an unknown status, not 500
the list endpoint for every runner.
"""

from pydantic import BaseModel, Field

from app.schemas._datetime import UTCDateTime


class RunnerRead(BaseModel):
    """One row of GET /api/runners."""

    id: str
    name: str | None = None
    runner_type: str = "generic"
    #: A RunnerState value: disconnected|connecting|idle|assigned|busy|dead.
    status: str
    labels: dict = Field(default_factory=dict)
    current_step_execution_id: str | None = None
    protocol_version: int | None = None
    agent_version: str | None = None
    connected_at: UTCDateTime | None = None
    last_heartbeat: UTCDateTime | None = None
    created_at: UTCDateTime | None = None
    #: "websocket" when this backend process holds a live socket for the row,
    #: "none" otherwise. The DB row alone cannot answer this - a status of
    #: "idle" left behind by a crashed process looks identical - so the
    #: registry stamps it from `_connections` at snapshot time. Gate
    #: assertion 9 reads exactly this field.
    connection: str = "none"

    class Config:
        from_attributes = True


__all__ = ["RunnerRead"]
