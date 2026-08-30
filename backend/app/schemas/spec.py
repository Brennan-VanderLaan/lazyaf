"""
Pydantic schemas for the specification layer (Phase 12.2.5).

Status validation lives here (models store plain strings, matching the
Card idiom).
"""

from pydantic import BaseModel

from app.models.spec import FeatureStatus, StoryStatus
from app.schemas._datetime import UTCDateTime
from app.schemas._json_field import json_field_validator
from app.schemas._patch import not_null
from app.schemas._strings import Body, Name, Sentence


# -----------------------------------------------------------------------------
# Feature
# -----------------------------------------------------------------------------

class FeatureCreate(BaseModel):
    title: Name
    description: Body = ""
    status: FeatureStatus = FeatureStatus.DRAFT
    repo_ids: list[str] = []


class FeatureUpdate(BaseModel):
    title: Name | None = None
    description: Body | None = None
    status: FeatureStatus | None = None
    repo_ids: list[str] | None = None

    # Every features column here is NOT NULL: null is a 422, not a 500.
    _reject_nulls = not_null("title", "description", "status", "repo_ids")


class FeatureRead(BaseModel):
    id: str
    title: str
    description: str
    status: FeatureStatus
    repo_ids: list[str]
    created_at: UTCDateTime
    updated_at: UTCDateTime

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
    title: Name
    narrative: Body = ""
    status: StoryStatus = StoryStatus.DRAFT
    priority: int | None = None


class UserStoryUpdate(BaseModel):
    title: Name | None = None
    narrative: Body | None = None
    status: StoryStatus | None = None
    priority: int | None = None

    # user_stories.priority is nullable (null clears it); the rest are not.
    _reject_nulls = not_null("title", "narrative", "status")


class UserStoryRead(BaseModel):
    id: str
    feature_id: str
    title: str
    narrative: str
    status: StoryStatus
    priority: int | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime

    class Config:
        from_attributes = True


# -----------------------------------------------------------------------------
# AcceptanceCriterion
# -----------------------------------------------------------------------------

class CriterionCreate(BaseModel):
    # Same pattern as UserStoryCreate.feature_id: nested route fills it.
    user_story_id: str | None = None
    text: Sentence
    required: bool = True
    notes: Body | None = None


class CriterionUpdate(BaseModel):
    text: Sentence | None = None
    required: bool | None = None
    notes: Body | None = None

    # acceptance_criteria.notes is nullable (null clears it); the rest are not.
    _reject_nulls = not_null("text", "required")


class CriterionRead(BaseModel):
    id: str
    user_story_id: str
    text: str
    required: bool
    notes: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime

    class Config:
        from_attributes = True


# -----------------------------------------------------------------------------
# PromptTemplate
# -----------------------------------------------------------------------------

class PromptTemplateCreate(BaseModel):
    name: Name
    description: Body = ""
    # `content` stays unbounded: it is a whole prompt template body.
    content: str = ""


class PromptTemplateUpdate(BaseModel):
    name: Name | None = None
    description: Body | None = None
    content: str | None = None

    # Every prompt_templates column here is NOT NULL.
    _reject_nulls = not_null("name", "description", "content")


class PromptTemplateRead(BaseModel):
    id: str
    name: str
    description: str
    content: str
    created_at: UTCDateTime
    updated_at: UTCDateTime

    class Config:
        from_attributes = True
