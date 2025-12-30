from datetime import datetime
from pydantic import BaseModel


class AgentFileBase(BaseModel):
    name: str
    content: str
    description: str | None = None


class AgentFileCreate(AgentFileBase):
    """Create a new agent file."""
    pass


class AgentFileUpdate(BaseModel):
    name: str | None = None
    content: str | None = None
    description: str | None = None


class AgentFileRead(AgentFileBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
