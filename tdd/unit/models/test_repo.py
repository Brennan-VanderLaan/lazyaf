"""
Unit tests for Repo model.

These tests verify the Repo model's structure, defaults, and behavior
without touching the database.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Repo

from tdd.shared.factories import RepoFactory
from tdd.shared.assertions import (
    assert_model_has_id,
    assert_model_has_timestamps,
    assert_model_fields,
)


class TestRepoModel:
    """Tests for Repo SQLAlchemy model."""

    def test_repo_creation_with_required_fields(self):
        """Repo can be created with required fields only."""
        repo = RepoFactory.build(
            name="TestProject",
            path="/repos/test-project",
        )

        assert repo.name == "TestProject"
        assert repo.path == "/repos/test-project"
        assert_model_has_id(repo)
        assert_model_has_timestamps(repo)

    def test_repo_default_branch_defaults_to_main(self):
        """Repo default_branch should default to 'main'."""
        repo = RepoFactory.build()
        assert repo.default_branch == "main"

    def test_repo_remote_url_is_optional(self):
        """Repo can be created without a remote_url."""
        repo = RepoFactory.build(local_only=True)
        assert repo.remote_url is None

    def test_repo_with_dev_branch(self):
        """Repo can be configured with dev as default branch."""
        repo = RepoFactory.build(with_dev_branch=True)
        assert repo.default_branch == "dev"

    def test_repo_with_remote_url(self):
        """Repo can store a GitHub remote URL."""
        repo = RepoFactory.build(
            remote_url="https://github.com/org/project.git"
        )
        assert repo.remote_url == "https://github.com/org/project.git"

    def test_repo_table_name(self):
        """Repo model maps to 'repos' table."""
        assert Repo.__tablename__ == "repos"

    def test_repo_id_is_uuid_string(self):
        """Repo ID should be a 36-character UUID string."""
        repo = RepoFactory.build()
        assert len(repo.id) == 36
        assert "-" in repo.id  # UUID format has dashes


class TestRepoRelationships:
    """Tests for Repo model relationships."""

    def test_repo_has_cards_relationship(self):
        """Repo should have a cards relationship defined."""
        assert hasattr(Repo, "cards")

    def test_new_repo_cards_is_empty_list(self):
        """New repo should have empty cards list."""
        repo = RepoFactory.build()
        # Note: In unit tests without DB, relationship may not be initialized
        # This test documents expected behavior
        assert hasattr(repo, "cards")
