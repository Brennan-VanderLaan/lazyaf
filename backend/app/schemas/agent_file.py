from pydantic import BaseModel

from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null
from app.schemas._strings import Body, Name


class AgentFileBase(BaseModel):
    # Bare `str` on purpose: AgentFileRead inherits this and must keep
    # serializing rows written before the bound existed. The bound goes on
    # the INPUT schemas below. `content` stays unbounded everywhere - it is a
    # whole agent definition file. See app/schemas/_strings.py.
    name: str
    content: str
    description: str | None = None


class AgentFileCreate(AgentFileBase):
    """Create a new agent file."""
    name: Name
    description: Body | None = None


class AgentFileUpdate(BaseModel):
    name: Name | None = None
    content: str | None = None
    description: Body | None = None

    # agent_files.name/.content are NOT NULL; .description is nullable, so
    # an explicit null there legitimately clears it.
    _reject_nulls = not_null("name", "content")


class AgentFileRead(AgentFileBase):
    id: str
    created_at: UTCDateTime
    updated_at: UTCDateTime

    class Config:
        from_attributes = True
