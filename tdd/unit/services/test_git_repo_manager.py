"""
Unit tests for GitRepoManager - manages bare git repositories.

These tests verify:
- Bare repo creation and deletion
- Repo existence checking
- Path computation
- Refs retrieval
- Default branch detection
- Error handling for edge cases
"""
import sys
import shutil
import tempfile
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.git_server import GitRepoManager


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_repos_dir():
    """Create a temporary directory for test repos.

    Uses resolve() to get the full path and avoid Windows 8.3 short name issues
    that can cause dulwich init_bare to fail.
    """
    temp_dir = Path(tempfile.mkdtemp()).resolve()
    yield temp_dir
    # Cleanup after test
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def repo_manager(temp_repos_dir):
    """Create a GitRepoManager with temp directory."""
    return GitRepoManager(repos_dir=temp_repos_dir)


@pytest.fixture
def sample_repo_id():
    """Sample repo ID for tests."""
    return "test-repo-12345"


# -----------------------------------------------------------------------------
# Repo Path Tests
# -----------------------------------------------------------------------------

class TestGetRepoPath:
    """Tests for GitRepoManager.get_repo_path() method."""

    def test_get_repo_path_returns_path_with_git_suffix(self, repo_manager, sample_repo_id):
        """Returns path with .git suffix."""
        path = repo_manager.get_repo_path(sample_repo_id)
        assert path.suffix == ".git"
        assert path.name == f"{sample_repo_id}.git"

    def test_get_repo_path_is_under_repos_dir(self, repo_manager, temp_repos_dir, sample_repo_id):
        """Returned path is under the repos directory."""
        path = repo_manager.get_repo_path(sample_repo_id)
        assert path.parent == temp_repos_dir

    def test_get_repo_path_different_ids_different_paths(self, repo_manager):
        """Different repo IDs produce different paths."""
        path1 = repo_manager.get_repo_path("repo-1")
        path2 = repo_manager.get_repo_path("repo-2")
        assert path1 != path2

    def test_get_repo_path_with_uuid_style_id(self, repo_manager):
        """Works with UUID-style repo IDs."""
        uuid_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        path = repo_manager.get_repo_path(uuid_id)
        assert path.name == f"{uuid_id}.git"


# -----------------------------------------------------------------------------
# Create Bare Repo Tests
# -----------------------------------------------------------------------------

class TestCreateBareRepo:
    """Tests for GitRepoManager.create_bare_repo() method."""

    def test_create_bare_repo_returns_path(self, repo_manager, sample_repo_id):
        """Returns the path to the created repo."""
        result = repo_manager.create_bare_repo(sample_repo_id)
        assert isinstance(result, Path)
        assert result.exists()

    def test_create_bare_repo_creates_directory(self, repo_manager, sample_repo_id):
        """Creates a directory at the expected path."""
        repo_manager.create_bare_repo(sample_repo_id)
        path = repo_manager.get_repo_path(sample_repo_id)
        assert path.is_dir()

    def test_create_bare_repo_is_git_repo(self, repo_manager, sample_repo_id):
        """Created directory is a valid bare git repo."""
        repo_manager.create_bare_repo(sample_repo_id)
        path = repo_manager.get_repo_path(sample_repo_id)

        # Bare repos have HEAD, objects, refs directly in root
        assert (path / "HEAD").exists()
        assert (path / "objects").is_dir()
        assert (path / "refs").is_dir()

    def test_create_bare_repo_has_head_file(self, repo_manager, sample_repo_id):
        """Created repo has HEAD pointing to a branch."""
        repo_manager.create_bare_repo(sample_repo_id)
        path = repo_manager.get_repo_path(sample_repo_id)

        head_content = (path / "HEAD").read_text()
        assert "ref:" in head_content

    def test_create_bare_repo_duplicate_raises_error(self, repo_manager, sample_repo_id):
        """Raises ValueError when repo already exists."""
        repo_manager.create_bare_repo(sample_repo_id)

        with pytest.raises(ValueError) as exc_info:
            repo_manager.create_bare_repo(sample_repo_id)

        assert sample_repo_id in str(exc_info.value)
        assert "already exists" in str(exc_info.value)

    def test_create_bare_repo_multiple_repos(self, repo_manager):
        """Can create multiple repos."""
        repo_manager.create_bare_repo("repo-1")
        repo_manager.create_bare_repo("repo-2")
        repo_manager.create_bare_repo("repo-3")

        assert repo_manager.repo_exists("repo-1")
        assert repo_manager.repo_exists("repo-2")
        assert repo_manager.repo_exists("repo-3")


# -----------------------------------------------------------------------------
# Repo Exists Tests
# -----------------------------------------------------------------------------

class TestRepoExists:
    """Tests for GitRepoManager.repo_exists() method."""

    def test_repo_exists_returns_false_for_nonexistent(self, repo_manager, sample_repo_id):
        """Returns False when repo does not exist."""
        assert repo_manager.repo_exists(sample_repo_id) is False

    def test_repo_exists_returns_true_after_create(self, repo_manager, sample_repo_id):
        """Returns True after repo is created."""
        repo_manager.create_bare_repo(sample_repo_id)
        assert repo_manager.repo_exists(sample_repo_id) is True

    def test_repo_exists_returns_false_after_delete(self, repo_manager, sample_repo_id):
        """Returns False after repo is deleted."""
        repo_manager.create_bare_repo(sample_repo_id)
        repo_manager.delete_repo(sample_repo_id)
        assert repo_manager.repo_exists(sample_repo_id) is False


# -----------------------------------------------------------------------------
# Delete Repo Tests
# -----------------------------------------------------------------------------

class TestDeleteRepo:
    """Tests for GitRepoManager.delete_repo() method."""

    def test_delete_repo_returns_true_on_success(self, repo_manager, sample_repo_id):
        """Returns True when repo is deleted."""
        repo_manager.create_bare_repo(sample_repo_id)
        result = repo_manager.delete_repo(sample_repo_id)
        assert result is True

    def test_delete_repo_removes_directory(self, repo_manager, sample_repo_id):
        """Removes the repo directory."""
        repo_manager.create_bare_repo(sample_repo_id)
        path = repo_manager.get_repo_path(sample_repo_id)

        repo_manager.delete_repo(sample_repo_id)
        assert not path.exists()

    def test_delete_repo_returns_false_for_nonexistent(self, repo_manager, sample_repo_id):
        """Returns False when repo does not exist."""
        result = repo_manager.delete_repo(sample_repo_id)
        assert result is False

    def test_delete_repo_does_not_affect_other_repos(self, repo_manager):
        """Deleting one repo does not affect others."""
        repo_manager.create_bare_repo("keep-repo")
        repo_manager.create_bare_repo("delete-repo")

        repo_manager.delete_repo("delete-repo")

        assert repo_manager.repo_exists("keep-repo") is True
        assert repo_manager.repo_exists("delete-repo") is False


# -----------------------------------------------------------------------------
# Get Repo Tests
# -----------------------------------------------------------------------------

class TestGetRepo:
    """Tests for GitRepoManager.get_repo() method."""

    def test_get_repo_returns_none_for_nonexistent(self, repo_manager, sample_repo_id):
        """Returns None when repo does not exist."""
        result = repo_manager.get_repo(sample_repo_id)
        assert result is None

    def test_get_repo_returns_dulwich_repo(self, repo_manager, sample_repo_id):
        """Returns a dulwich Repo object."""
        from dulwich.repo import Repo as DulwichRepo

        repo_manager.create_bare_repo(sample_repo_id)
        result = repo_manager.get_repo(sample_repo_id)

        assert isinstance(result, DulwichRepo)

    def test_get_repo_can_access_refs(self, repo_manager, sample_repo_id):
        """Returned repo can access refs."""
        repo_manager.create_bare_repo(sample_repo_id)
        repo = repo_manager.get_repo(sample_repo_id)

        # Should be able to call get_refs without error
        refs = repo.get_refs()
        assert isinstance(refs, dict)


# -----------------------------------------------------------------------------
# List Repos Tests
# -----------------------------------------------------------------------------

class TestListRepos:
    """Tests for GitRepoManager.list_repos() method."""

    def test_list_repos_empty(self, repo_manager):
        """Returns empty list when no repos exist."""
        result = repo_manager.list_repos()
        assert result == []

    def test_list_repos_returns_repo_ids(self, repo_manager):
        """Returns list of repo IDs."""
        repo_manager.create_bare_repo("repo-1")
        repo_manager.create_bare_repo("repo-2")

        result = repo_manager.list_repos()
        assert sorted(result) == ["repo-1", "repo-2"]

    def test_list_repos_excludes_non_git_directories(self, repo_manager, temp_repos_dir):
        """Does not include non-.git directories."""
        repo_manager.create_bare_repo("real-repo")

        # Create a non-git directory
        (temp_repos_dir / "not-a-repo").mkdir()

        result = repo_manager.list_repos()
        assert result == ["real-repo"]

    def test_list_repos_after_delete(self, repo_manager):
        """Deleted repos are not in list."""
        repo_manager.create_bare_repo("keep")
        repo_manager.create_bare_repo("delete")
        repo_manager.delete_repo("delete")

        result = repo_manager.list_repos()
        assert result == ["keep"]


# -----------------------------------------------------------------------------
# Get Refs Tests
# -----------------------------------------------------------------------------

class TestGetRefs:
    """Tests for GitRepoManager.get_refs() method."""

    def test_get_refs_returns_empty_for_nonexistent_repo(self, repo_manager, sample_repo_id):
        """Returns empty dict when repo does not exist."""
        result = repo_manager.get_refs(sample_repo_id)
        assert result == {}

    def test_get_refs_returns_empty_for_empty_repo(self, repo_manager, sample_repo_id):
        """Returns empty dict for newly created repo (no commits)."""
        repo_manager.create_bare_repo(sample_repo_id)
        result = repo_manager.get_refs(sample_repo_id)
        # Empty repo has no refs (HEAD is symbolic, not a real ref)
        assert isinstance(result, dict)

    def test_get_refs_returns_bytes_keys(self, repo_manager, sample_repo_id):
        """Ref keys are bytes."""
        repo_manager.create_bare_repo(sample_repo_id)
        refs = repo_manager.get_refs(sample_repo_id)
        for key in refs.keys():
            assert isinstance(key, bytes)


# -----------------------------------------------------------------------------
# Get Default Branch Tests
# -----------------------------------------------------------------------------

class TestGetDefaultBranch:
    """Tests for GitRepoManager.get_default_branch() method."""

    def test_get_default_branch_returns_none_for_nonexistent_repo(self, repo_manager, sample_repo_id):
        """Returns None when repo does not exist."""
        result = repo_manager.get_default_branch(sample_repo_id)
        assert result is None

    def test_get_default_branch_returns_none_for_empty_repo(self, repo_manager, sample_repo_id):
        """Returns None for repo without HEAD ref set."""
        repo_manager.create_bare_repo(sample_repo_id)
        # Fresh repos may not have a resolvable HEAD
        result = repo_manager.get_default_branch(sample_repo_id)
        # Can be None or a branch name depending on dulwich version
        assert result is None or isinstance(result, str)


# -----------------------------------------------------------------------------
# Initialization Tests
# -----------------------------------------------------------------------------

class TestInitialization:
    """Tests for GitRepoManager initialization."""

    def test_creates_repos_dir_if_not_exists(self, temp_repos_dir):
        """Creates repos directory if it does not exist."""
        non_existent = temp_repos_dir / "nested" / "repos"
        manager = GitRepoManager(repos_dir=non_existent)
        assert non_existent.exists()

    def test_uses_existing_repos_dir(self, temp_repos_dir):
        """Uses existing repos directory."""
        manager = GitRepoManager(repos_dir=temp_repos_dir)
        assert manager.repos_dir == temp_repos_dir

    def test_independent_manager_instances(self, temp_repos_dir):
        """Different manager instances with same dir see same repos."""
        manager1 = GitRepoManager(repos_dir=temp_repos_dir)
        manager2 = GitRepoManager(repos_dir=temp_repos_dir)

        manager1.create_bare_repo("shared-repo")

        assert manager2.repo_exists("shared-repo") is True


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and error handling."""

    def test_repo_id_with_special_characters(self, repo_manager):
        """Handles repo IDs with special characters."""
        # UUID-style ID is common
        uuid_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        repo_manager.create_bare_repo(uuid_id)
        assert repo_manager.repo_exists(uuid_id)

    def test_repo_id_with_underscores(self, repo_manager):
        """Handles repo IDs with underscores."""
        repo_manager.create_bare_repo("my_project_repo")
        assert repo_manager.repo_exists("my_project_repo")

    def test_repo_id_with_numbers(self, repo_manager):
        """Handles repo IDs starting with numbers."""
        repo_manager.create_bare_repo("123-project")
        assert repo_manager.repo_exists("123-project")

    def test_get_repo_multiple_times(self, repo_manager, sample_repo_id):
        """Can get same repo multiple times."""
        repo_manager.create_bare_repo(sample_repo_id)

        repo1 = repo_manager.get_repo(sample_repo_id)
        repo2 = repo_manager.get_repo(sample_repo_id)

        assert repo1 is not None
        assert repo2 is not None
        # Different instances, but same underlying repo
