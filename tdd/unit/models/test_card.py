"""
Unit tests for Card model.

These tests verify the Card model's structure, status transitions,
and behavior without touching the database.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Card, CardStatus

from tdd.shared.factories import CardFactory
from tdd.shared.assertions import (
    assert_model_has_id,
    assert_model_has_timestamps,
    assert_model_fields,
    assert_enum_value,
)


class TestCardStatus:
    """Tests for CardStatus enum."""

    def test_card_status_values(self):
        """CardStatus enum should have all expected values."""
        expected_statuses = {"todo", "in_progress", "in_review", "done", "failed"}
        actual_statuses = {status.value for status in CardStatus}
        assert actual_statuses == expected_statuses

    def test_card_status_is_string_enum(self):
        """CardStatus should be a string enum for JSON serialization."""
        assert issubclass(CardStatus, str)
        assert CardStatus.TODO == "todo"
        assert CardStatus.IN_PROGRESS == "in_progress"


class TestCardModel:
    """Tests for Card SQLAlchemy model."""

    def test_card_creation_with_required_fields(self):
        """Card can be created with required fields only."""
        card = CardFactory.build(
            title="Add login feature",
            description="Implement OAuth2 login",
        )

        assert card.title == "Add login feature"
        assert card.description == "Implement OAuth2 login"
        assert_model_has_id(card)

    def test_card_default_status_is_todo(self):
        """Card status should default to 'todo'."""
        card = CardFactory.build()
        assert card.status == CardStatus.TODO.value

    def test_card_optional_fields_default_to_none(self):
        """Card optional fields should default to None."""
        card = CardFactory.build()
        assert card.branch_name is None
        assert card.pr_url is None
        assert card.job_id is None

    def test_card_has_timestamps(self):
        """Card should have created_at and updated_at timestamps."""
        card = CardFactory.build()
        assert_model_has_timestamps(card)
        assert card.updated_at is not None

    def test_card_table_name(self):
        """Card model maps to 'cards' table."""
        assert Card.__tablename__ == "cards"


class TestCardStatusTransitions:
    """Tests for Card in different states (using factory traits)."""

    def test_card_todo_state(self):
        """Card in TODO state has no branch or PR."""
        card = CardFactory.build()
        assert card.status == CardStatus.TODO.value
        assert card.branch_name is None
        assert card.pr_url is None

    def test_card_in_progress_state(self):
        """Card in IN_PROGRESS state has a branch name."""
        card = CardFactory.build(in_progress=True)
        assert card.status == CardStatus.IN_PROGRESS.value
        assert card.branch_name is not None
        assert card.branch_name.startswith("feature/")

    def test_card_in_review_state(self):
        """Card in IN_REVIEW state has branch and PR URL."""
        card = CardFactory.build(in_review=True)
        assert card.status == CardStatus.IN_REVIEW.value
        assert card.branch_name is not None
        assert card.pr_url is not None
        assert "github.com" in card.pr_url

    def test_card_done_state(self):
        """Card in DONE state preserves branch and PR URL."""
        card = CardFactory.build(done=True)
        assert card.status == CardStatus.DONE.value
        assert card.branch_name is not None
        assert card.pr_url is not None

    def test_card_failed_state(self):
        """Card in FAILED state."""
        card = CardFactory.build(failed=True)
        assert card.status == CardStatus.FAILED.value


class TestCardRelationships:
    """Tests for Card model relationships."""

    def test_card_has_repo_relationship(self):
        """Card should have a repo relationship defined."""
        assert hasattr(Card, "repo")

    def test_card_requires_repo_id(self):
        """Card must have a repo_id."""
        card = CardFactory.build()
        assert card.repo_id is not None
        assert len(card.repo_id) == 36  # UUID format
