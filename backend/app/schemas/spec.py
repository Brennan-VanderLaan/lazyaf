"""
Pydantic schemas for the specification layer (Phase 12.2.5).

Status validation lives here (models store plain strings, matching the
Card idiom).
"""
from datetime import datetime

from pydantic import BaseModel

from app.models.spec import FeatureStatus, StoryStatus
from app.schemas._json_field import json_field_validator


# -----------------------------------------------------------------------------
# Feature
# -----------------------------------------------------------------------------

class FeatureCreate(BaseModel):
    title: str
    description: str = ""
    status: FeatureStatus = FeatureStatus.DRAFT
    repo_ids: list[str] = []


class FeatureUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: FeatureStatus | None = None
    repo_ids: list[str] | None = None


class FeatureRead(BaseModel):
    id: str
    title: str
    description: str
    status: FeatureStatus
    repo_ids: list[str]
    created_at: datetime
    updated_at: datetime

    # Parses repo_ids from the JSON string column; logs on malformed JSON.
    parse_repo_ids = json_field_validator("repo_ids", [])

    class Config:
        from_attributes = True


# -----------------------------------------------------------------------------
# UserStory
# -----------------------------------------------------------------------------

class UserStoryCreate(BaseModel):
    # feature_id is optional at the schema level: the nested route
    # POST /api/features/{id}/stories fills it from the path; the flat route
    # POST /api/user-stories rejects payloads without it (400).
    feature_id: str | None = None
    title: str
    narrative: str = ""
    status: StoryStatus = StoryStatus.DRAFT
    priority: int | None = None


class UserStoryUpdate(BaseModel):
    title: str | None = None
    narrative: str | None = None
    status: StoryStatus | None = None
    priority: int | None = None


class UserStoryRead(BaseModel):
    id: str
    feature_id: str
    title: str
    narrative: str
    status: StoryStatus
    priority: int | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# -----------------------------------------------------------------------------
# AcceptanceCriterion
# -----------------------------------------------------------------------------

class CriterionCreate(BaseModel):
    # Same pattern as UserStoryCreate.feature_id: nested route fills it.
    user_story_id: str | None = None
    text: str
    required: bool = True
    notes: str | None = None


class CriterionUpdate(BaseModel):
    text: str | None = None
    required: bool | None = None
    notes: str | None = None


class CriterionRead(BaseModel):
    id: str
    user_story_id: str
    text: str
    required: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# -----------------------------------------------------------------------------
# PromptTemplate
# -----------------------------------------------------------------------------

class PromptTemplateCreate(BaseModel):
    name: str
    description: str = ""
    content: str = ""


class PromptTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None


class PromptTemplateRead(BaseModel):
    id: str
    name: str
    description: str
    content: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
