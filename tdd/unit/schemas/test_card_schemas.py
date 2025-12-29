"""
Unit tests for Card Pydantic schemas.

These tests verify schema validation, status handling, and serialization.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.card import CardStatus
from app.schemas import CardCreate, CardRead, CardUpdate

from tdd.shared.assertions import assert_schema_valid, assert_schema_invalid


class TestCardCreateSchema:
    """Tests for CardCreate schema validation."""

    def test_valid_card_create_minimal(self):
        """CardCreate accepts title only (description has default)."""
        data = {"title": "Add user authentication"}
        schema = assert_schema_valid(CardCreate, data)
        assert schema.title == "Add user authentication"
        assert schema.description == ""

    def test_valid_card_create_full(self):
        """CardCreate accepts title and description."""
        data = {
            "title": "Implement OAuth2",
            "description": "Add Google and GitHub OAuth2 login support",
        }
        schema = assert_schema_valid(CardCreate, data)
        assert schema.description == "Add Google and GitHub OAuth2 login support"

    def test_card_create_missing_title_fails(self):
        """CardCreate requires title field."""
        data = {"description": "Some description"}
        assert_schema_invalid(CardCreate, data)

    def test_card_create_empty_title_allowed(self):
        """CardCreate allows empty string title (UI validation should prevent)."""
        data = {"title": ""}
        # This may or may not be valid depending on schema constraints
        # Document current behavior
        schema = CardCreate(**data)
        assert schema.title == ""


class TestCardUpdateSchema:
    """Tests for CardUpdate schema validation."""

    def test_card_update_all_fields_optional(self):
        """CardUpdate allows empty updates."""
        data = {}
        schema = assert_schema_valid(CardUpdate, data)
        assert schema.title is None
        assert schema.description is None
        assert schema.status is None

    def test_card_update_title_only(self):
        """CardUpdate accepts title only."""
        data = {"title": "Updated Title"}
        schema = assert_schema_valid(CardUpdate, data)
        assert schema.title == "Updated Title"

    def test_card_update_status_with_enum(self):
        """CardUpdate accepts CardStatus enum."""
        data = {"status": CardStatus.IN_PROGRESS}
        schema = assert_schema_valid(CardUpdate, data)
        assert schema.status == CardStatus.IN_PROGRESS

    def test_card_update_status_with_string(self):
        """CardUpdate accepts status as string value."""
        data = {"status": "in_progress"}
        schema = assert_schema_valid(CardUpdate, data)
        assert schema.status == CardStatus.IN_PROGRESS

    def test_card_update_invalid_status_fails(self):
        """CardUpdate rejects invalid status values."""
        data = {"status": "invalid_status"}
        assert_schema_invalid(CardUpdate, data)

    def test_card_update_all_statuses_valid(self):
        """CardUpdate accepts all valid status values."""
        for status in CardStatus:
            data = {"status": status.value}
            schema = CardUpdate(**data)
            assert schema.status == status


class TestCardReadSchema:
    """Tests for CardRead schema serialization."""

    def test_card_read_from_complete_data(self):
        """CardRead can be created from complete card data."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "repo_id": "987e6543-e21b-12d3-a456-426614174000",
            "title": "Test Card",
            "description": "Test description",
            "status": CardStatus.TODO,
            "branch_name": None,
            "pr_url": None,
            "job_id": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        schema = assert_schema_valid(CardRead, data)
        assert schema.id == data["id"]
        assert schema.status == CardStatus.TODO

    def test_card_read_in_progress_state(self):
        """CardRead correctly represents in-progress card."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "repo_id": "987e6543-e21b-12d3-a456-426614174000",
            "title": "Feature Card",
            "description": "Implementing feature",
            "status": CardStatus.IN_PROGRESS,
            "branch_name": "feature/add-login",
            "pr_url": None,
            "job_id": "456e7890-e12b-34d5-a678-901234567890",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        schema = assert_schema_valid(CardRead, data)
        assert schema.status == CardStatus.IN_PROGRESS
        assert schema.branch_name == "feature/add-login"
        assert schema.job_id is not None

    def test_card_read_requires_timestamps(self):
        """CardRead requires both created_at and updated_at."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "repo_id": "987e6543-e21b-12d3-a456-426614174000",
            "title": "Test Card",
            "description": "",
            "status": CardStatus.TODO,
        }
        assert_schema_invalid(CardRead, data)

    def test_card_read_status_serialization(self):
        """CardRead status serializes correctly."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "repo_id": "987e6543-e21b-12d3-a456-426614174000",
            "title": "Test",
            "description": "",
            "status": "in_review",
            "branch_name": "feature/test",
            "pr_url": "https://github.com/org/repo/pull/123",
            "job_id": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        schema = CardRead(**data)
        assert schema.status == CardStatus.IN_REVIEW
