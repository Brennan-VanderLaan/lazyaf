"""
Specification layer models (Phase 12.2.5).

Feature -> UserStory -> AcceptanceCriterion, plus PromptTemplate.
Hierarchy is intentionally SHALLOW — tests and runs are orthogonal entities
that join back to criteria in Phase 12.2.6.

Statuses are plain string columns (matching the Card idiom); validation
happens in the pydantic schemas via the enums below.
"""
from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FeatureStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DONE = "done"


class StoryStatus(str, Enum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(50), default=FeatureStatus.DRAFT.value)
    # JSON-encoded list of repo IDs this feature touches (cross-repo by design)
    repo_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    stories: Mapped[list["UserStory"]] = relationship(
        "UserStory",
        back_populates="feature",
        cascade="all, delete-orphan",
    )


class UserStory(Base):
    __tablename__ = "user_stories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    feature_id: Mapped[str] = mapped_column(String(36), ForeignKey("features.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")  # free-form markdown, no gherkin enforcement
    status: Mapped[str] = mapped_column(String(50), default=StoryStatus.DRAFT.value)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)  # plain int, not story points
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    feature: Mapped["Feature"] = relationship("Feature", back_populates="stories")
    criteria: Mapped[list["AcceptanceCriterion"]] = relationship(
        "AcceptanceCriterion",
        back_populates="story",
        cascade="all, delete-orphan",
    )


class AcceptanceCriterion(Base):
    __tablename__ = "acceptance_criteria"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_story_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_stories.id"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    story: Mapped["UserStory"] = relationship("UserStory", back_populates="criteria")


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
