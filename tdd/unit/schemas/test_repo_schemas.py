"""
Unit tests for Repo Pydantic schemas.

These tests verify schema validation, serialization, and default values.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas import RepoCreate, RepoRead, RepoUpdate

from tdd.shared.assertions import assert_schema_valid, assert_schema_invalid


class TestRepoCreateSchema:
    """Tests for RepoCreate schema validation."""

    def test_valid_repo_create_minimal(self):
        """RepoCreate accepts minimal required fields."""
        data = {"name": "TestRepo"}
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.name == "TestRepo"

    def test_valid_repo_create_full(self):
        """RepoCreate accepts all fields."""
        data = {
            "name": "FullRepo",
            "remote_url": "https://github.com/org/repo.git",
            "default_branch": "dev",
        }
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.remote_url == "https://github.com/org/repo.git"
        assert schema.default_branch == "dev"

    def test_repo_create_default_branch(self):
        """RepoCreate defaults branch to 'main'."""
        data = {"name": "TestRepo"}
        schema = RepoCreate(**data)
        assert schema.default_branch == "main"

    def test_repo_create_remote_url_optional(self):
        """RepoCreate allows None for remote_url."""
        data = {"name": "LocalRepo", "remote_url": None}
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.remote_url is None

    def test_repo_create_missing_name_fails(self):
        """RepoCreate requires name field."""
        data = {}
        assert_schema_invalid(RepoCreate, data)


class TestRepoUpdateSchema:
    """Tests for RepoUpdate schema validation."""

    def test_repo_update_all_fields_optional(self):
        """RepoUpdate allows empty updates."""
        data = {}
        schema = assert_schema_valid(RepoUpdate, data)
        assert schema.name is None

    def test_repo_update_partial(self):
        """RepoUpdate accepts partial updates."""
        data = {"name": "NewName"}
        schema = assert_schema_valid(RepoUpdate, data)
        assert schema.name == "NewName"

    def test_repo_update_all_fields(self):
        """RepoUpdate accepts all fields."""
        data = {
            "name": "UpdatedRepo",
            "remote_url": "https://github.com/new/repo.git",
            "default_branch": "develop",
        }
        schema = assert_schema_valid(RepoUpdate, data)
        assert schema.name == "UpdatedRepo"
        assert schema.default_branch == "develop"


class TestRepoReadSchema:
    """Tests for RepoRead schema serialization."""

    def test_repo_read_from_dict(self):
        """RepoRead can be created from valid data."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "TestRepo",
            "remote_url": None,
            "default_branch": "main",
            "is_ingested": False,
            "internal_git_url": "/git/123e4567-e89b-12d3-a456-426614174000.git",
            "created_at": datetime.utcnow(),
        }
        schema = assert_schema_valid(RepoRead, data)
        assert schema.id == data["id"]
        assert schema.is_ingested == False
        assert schema.internal_git_url == data["internal_git_url"]

    def test_repo_read_requires_id(self):
        """RepoRead requires id field."""
        data = {
            "name": "TestRepo",
            "default_branch": "main",
            "is_ingested": False,
            "internal_git_url": "/git/test.git",
            "created_at": datetime.utcnow(),
        }
        assert_schema_invalid(RepoRead, data)

    def test_repo_read_requires_created_at(self):
        """RepoRead requires created_at field."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "TestRepo",
            "default_branch": "main",
            "is_ingested": False,
            "internal_git_url": "/git/test.git",
        }
        assert_schema_invalid(RepoRead, data)

    def test_repo_read_requires_is_ingested(self):
        """RepoRead requires is_ingested field."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "TestRepo",
            "default_branch": "main",
            "internal_git_url": "/git/test.git",
            "created_at": datetime.utcnow(),
        }
        assert_schema_invalid(RepoRead, data)

    def test_repo_read_requires_internal_git_url(self):
        """RepoRead requires internal_git_url field."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "TestRepo",
            "default_branch": "main",
            "is_ingested": False,
            "created_at": datetime.utcnow(),
        }
        assert_schema_invalid(RepoRead, data)
