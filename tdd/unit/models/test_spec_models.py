"""
Unit tests for the specification layer models (Phase 12.2.5).

Feature / UserStory / AcceptanceCriterion / PromptTemplate structure,
enums, defaults, and the Card spec-link columns — no I/O, matching the
unit-tier convention (table metadata + direct construction only).
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (
    AcceptanceCriterion,
    Card,
    Feature,
    FeatureStatus,
    PromptTemplate,
    StoryStatus,
    UserStory,
)


class TestSpecStatusEnums:
    """Tests for the spec-layer status enums."""

    def test_feature_status_values(self):
        """FeatureStatus matches the interface contract: draft|active|done."""
        assert {s.value for s in FeatureStatus} == {"draft", "active", "done"}

    def test_story_status_values(self):
        """StoryStatus follows the PLAN model section."""
        expected = {"draft", "accepted", "in_progress", "done", "blocked"}
        assert {s.value for s in StoryStatus} == expected

    def test_status_enums_are_string_enums(self):
        """Both enums are string enums for JSON serialization (Card idiom)."""
        assert issubclass(FeatureStatus, str)
        assert issubclass(StoryStatus, str)
        assert FeatureStatus.DRAFT == "draft"
        assert StoryStatus.IN_PROGRESS == "in_progress"


class TestFeatureModel:
    """Tests for the Feature SQLAlchemy model."""

    def test_feature_table_name(self):
        assert Feature.__tablename__ == "features"

    def test_feature_status_is_plain_string_column(self):
        """Status is stored as a plain string, validated in schemas."""
        col = Feature.__table__.c.status
        assert col.type.python_type is str
        assert col.default.arg == "draft"

    def test_feature_repo_ids_defaults_to_empty_json_list(self):
        col = Feature.__table__.c.repo_ids
        assert col.default.arg == "[]"
        assert col.nullable is False

    def test_feature_has_timestamps(self):
        assert "created_at" in Feature.__table__.c
        assert "updated_at" in Feature.__table__.c

    def test_feature_construction(self):
        feature = Feature(
            title="Self-hosted CI",
            description="LazyAF gates LazyAF",
            status=FeatureStatus.ACTIVE.value,
            repo_ids='["abc"]',
        )
        assert feature.title == "Self-hosted CI"
        assert feature.status == "active"
        assert feature.repo_ids == '["abc"]'

    def test_feature_has_stories_relationship_with_cascade(self):
        assert hasattr(Feature, "stories")
        cascade = Feature.stories.property.cascade
        assert "delete-orphan" in cascade


class TestUserStoryModel:
    """Tests for the UserStory SQLAlchemy model."""

    def test_story_table_name(self):
        assert UserStory.__tablename__ == "user_stories"

    def test_story_requires_feature_fk(self):
        col = UserStory.__table__.c.feature_id
        assert col.nullable is False
        fks = {fk.target_fullname for fk in col.foreign_keys}
        assert fks == {"features.id"}

    def test_story_priority_is_nullable_int(self):
        col = UserStory.__table__.c.priority
        assert col.nullable is True
        assert col.type.python_type is int

    def test_story_status_default_is_draft(self):
        assert UserStory.__table__.c.status.default.arg == "draft"

    def test_story_narrative_is_freeform_text(self):
        """No gherkin enforcement — narrative is a plain Text column."""
        col = UserStory.__table__.c.narrative
        assert col.type.python_type is str

    def test_story_has_criteria_relationship_with_cascade(self):
        assert hasattr(UserStory, "criteria")
        cascade = UserStory.criteria.property.cascade
        assert "delete-orphan" in cascade

    def test_story_has_feature_relationship(self):
        assert hasattr(UserStory, "feature")


class TestAcceptanceCriterionModel:
    """Tests for the AcceptanceCriterion SQLAlchemy model."""

    def test_criterion_table_name(self):
        assert AcceptanceCriterion.__tablename__ == "acceptance_criteria"

    def test_criterion_requires_story_fk(self):
        col = AcceptanceCriterion.__table__.c.user_story_id
        assert col.nullable is False
        fks = {fk.target_fullname for fk in col.foreign_keys}
        assert fks == {"user_stories.id"}

    def test_criterion_required_defaults_to_true(self):
        col = AcceptanceCriterion.__table__.c.required
        assert col.default.arg is True
        assert col.nullable is False

    def test_criterion_notes_is_nullable(self):
        assert AcceptanceCriterion.__table__.c.notes.nullable is True

    def test_criterion_has_story_relationship(self):
        assert hasattr(AcceptanceCriterion, "story")


class TestPromptTemplateModel:
    """Tests for the PromptTemplate SQLAlchemy model."""

    def test_prompt_template_table_name(self):
        assert PromptTemplate.__tablename__ == "prompt_templates"

    def test_prompt_template_name_is_unique(self):
        col = PromptTemplate.__table__.c.name
        assert col.unique is True
        assert col.nullable is False

    def test_prompt_template_defaults(self):
        assert PromptTemplate.__table__.c.description.default.arg == ""
        assert PromptTemplate.__table__.c.content.default.arg == ""


class TestCardSpecLinkColumns:
    """Tests for the Card spec-link columns added in Phase 12.2.5."""

    def test_card_has_feature_id_column(self):
        col = Card.__table__.c.feature_id
        assert col.nullable is True
        fks = {fk.target_fullname for fk in col.foreign_keys}
        assert fks == {"features.id"}

    def test_card_has_user_story_id_column(self):
        col = Card.__table__.c.user_story_id
        assert col.nullable is True
        fks = {fk.target_fullname for fk in col.foreign_keys}
        assert fks == {"user_stories.id"}

    def test_card_link_columns_default_to_none(self):
        card = Card(repo_id="r" * 36, title="t")
        assert card.feature_id is None
        assert card.user_story_id is None
