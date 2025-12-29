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
        data = {"name": "TestRepo", "path": "/repos/test"}
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.name == "TestRepo"
        assert schema.path == "/repos/test"

    def test_valid_repo_create_full(self):
        """RepoCreate accepts all fields."""
        data = {
            "name": "FullRepo",
            "path": "/repos/full",
            "remote_url": "https://github.com/org/repo.git",
            "default_branch": "dev",
        }
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.remote_url == "https://github.com/org/repo.git"
        assert schema.default_branch == "dev"

    def test_repo_create_default_branch(self):
        """RepoCreate defaults branch to 'main'."""
        data = {"name": "TestRepo", "path": "/repos/test"}
        schema = RepoCreate(**data)
        assert schema.default_branch == "main"

    def test_repo_create_remote_url_optional(self):
        """RepoCreate allows None for remote_url."""
        data = {"name": "LocalRepo", "path": "/repos/local", "remote_url": None}
        schema = assert_schema_valid(RepoCreate, data)
        assert schema.remote_url is None

    def test_repo_create_missing_name_fails(self):
        """RepoCreate requires name field."""
        data = {"path": "/repos/test"}
        assert_schema_invalid(RepoCreate, data)

    def test_repo_create_missing_path_fails(self):
        """RepoCreate requires path field."""
        data = {"name": "TestRepo"}
        assert_schema_invalid(RepoCreate, data)


class TestRepoUpdateSchema:
    """Tests for RepoUpdate schema validation."""

    def test_repo_update_all_fields_optional(self):
        """RepoUpdate allows empty updates."""
        data = {}
        schema = assert_schema_valid(RepoUpdate, data)
        assert schema.name is None
        assert schema.path is None

    def test_repo_update_partial(self):
        """RepoUpdate accepts partial updates."""
        data = {"name": "NewName"}
        schema = assert_schema_valid(RepoUpdate, data)
        assert schema.name == "NewName"
        assert schema.path is None

    def test_repo_update_all_fields(self):
        """RepoUpdate accepts all fields."""
        data = {
            "name": "UpdatedRepo",
            "path": "/new/path",
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
            "path": "/repos/test",
            "remote_url": None,
            "default_branch": "main",
            "created_at": datetime.utcnow(),
        }
        schema = assert_schema_valid(RepoRead, data)
        assert schema.id == data["id"]

    def test_repo_read_requires_id(self):
        """RepoRead requires id field."""
        data = {
            "name": "TestRepo",
            "path": "/repos/test",
            "default_branch": "main",
            "created_at": datetime.utcnow(),
        }
        assert_schema_invalid(RepoRead, data)

    def test_repo_read_requires_created_at(self):
        """RepoRead requires created_at field."""
        data = {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "name": "TestRepo",
            "path": "/repos/test",
            "default_branch": "main",
        }
        assert_schema_invalid(RepoRead, data)
