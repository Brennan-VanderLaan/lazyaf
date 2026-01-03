"""
Tests for git_helpers module (Phase 12.0).

These tests DEFINE the contract for git operations. Write tests first,
then implement to make them pass.

Contract defined:
- clone(url, path) -> None, raises GitError on failure
- checkout(path, branch) -> None, raises GitError on failure
- get_sha(path) -> str, returns current commit SHA
- push(path, remote, branch) -> None, raises GitError on failure
- commit(path, message) -> str, returns commit SHA
- get_diff(path) -> str, returns diff of uncommitted changes
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from runner_common.git_helpers import (
        GitError,
        clone,
        checkout,
        get_sha,
        push,
        commit,
        get_diff,
        get_current_branch,
        has_uncommitted_changes,
    )
    RUNNER_COMMON_AVAILABLE = True
except ImportError:
    RUNNER_COMMON_AVAILABLE = False
    # Define placeholders so tests can be collected
    GitError = Exception
    clone = checkout = get_sha = push = commit = get_diff = None
    get_current_branch = has_uncommitted_changes = None


pytestmark = pytest.mark.skipif(
    not RUNNER_COMMON_AVAILABLE,
    reason="runner-common not yet implemented"
)


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return repo_path


@pytest.fixture
def bare_git_repo(tmp_path):
    """Create a bare git repository for testing clone/push."""
    bare_path = tmp_path / "bare-repo.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare_path)],
        check=True,
        capture_output=True,
    )
    return bare_path


class TestClone:
    """Tests for clone() function."""

    def test_clone_creates_repo_at_path(self, bare_git_repo, tmp_path):
        """clone(url, path) creates a git repository at the specified path."""
        target_path = tmp_path / "cloned-repo"

        clone(str(bare_git_repo), str(target_path))

        assert target_path.exists()
        assert (target_path / ".git").exists()

    def test_clone_raises_git_error_on_invalid_url(self, tmp_path):
        """clone() raises GitError when URL is invalid."""
        target_path = tmp_path / "cloned-repo"

        with pytest.raises(GitError) as exc_info:
            clone("invalid://not-a-real-url", str(target_path))

        assert "clone" in str(exc_info.value).lower() or "failed" in str(exc_info.value).lower()

    def test_clone_raises_git_error_on_nonexistent_repo(self, tmp_path):
        """clone() raises GitError when repository doesn't exist."""
        target_path = tmp_path / "cloned-repo"
        nonexistent = tmp_path / "does-not-exist.git"

        with pytest.raises(GitError):
            clone(str(nonexistent), str(target_path))

    def test_clone_with_branch(self, temp_git_repo, bare_git_repo, tmp_path):
        """clone() can clone a specific branch."""
        # First push temp_git_repo to bare repo
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare_git_repo)],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "master"],
            cwd=temp_git_repo,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=temp_git_repo,
            capture_output=True,
        )

        # Create a feature branch
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )
        (temp_git_repo / "feature.txt").write_text("feature content")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "feature"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )

        # Clone with specific branch
        target_path = tmp_path / "cloned-repo"
        clone(str(bare_git_repo), str(target_path), branch="feature")

        assert (target_path / "feature.txt").exists()


class TestCheckout:
    """Tests for checkout() function."""

    def test_checkout_branch_switches(self, temp_git_repo):
        """checkout(path, branch) switches to the specified branch."""
        # Create a new branch
        subprocess.run(
            ["git", "checkout", "-b", "develop"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "master"],
            cwd=temp_git_repo,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=temp_git_repo,
            capture_output=True,
        )

        checkout(str(temp_git_repo), "develop")

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "develop"

    def test_checkout_raises_on_nonexistent_branch(self, temp_git_repo):
        """checkout() raises GitError for non-existent branch."""
        with pytest.raises(GitError):
            checkout(str(temp_git_repo), "nonexistent-branch")

    def test_checkout_creates_branch_if_requested(self, temp_git_repo):
        """checkout() can create a new branch if create=True."""
        checkout(str(temp_git_repo), "new-branch", create=True)

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "new-branch"


class TestGetSha:
    """Tests for get_sha() function."""

    def test_get_current_sha_returns_string(self, temp_git_repo):
        """get_sha(path) returns the current commit SHA as a string."""
        sha = get_sha(str(temp_git_repo))

        assert isinstance(sha, str)
        assert len(sha) == 40  # Full SHA is 40 hex characters
        assert all(c in "0123456789abcdef" for c in sha)

    def test_get_sha_matches_git_command(self, temp_git_repo):
        """get_sha() returns the same value as git rev-parse HEAD."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        expected_sha = result.stdout.strip()

        sha = get_sha(str(temp_git_repo))

        assert sha == expected_sha

    def test_get_sha_raises_on_invalid_repo(self, tmp_path):
        """get_sha() raises GitError for non-git directory."""
        with pytest.raises(GitError):
            get_sha(str(tmp_path))


class TestGetCurrentBranch:
    """Tests for get_current_branch() function."""

    def test_get_current_branch_returns_string(self, temp_git_repo):
        """get_current_branch(path) returns the current branch name."""
        branch = get_current_branch(str(temp_git_repo))

        assert isinstance(branch, str)
        assert branch in ("master", "main")

    def test_get_current_branch_after_checkout(self, temp_git_repo):
        """get_current_branch() reflects branch changes."""
        subprocess.run(
            ["git", "checkout", "-b", "feature-branch"],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )

        branch = get_current_branch(str(temp_git_repo))

        assert branch == "feature-branch"


class TestCommit:
    """Tests for commit() function."""

    def test_commit_creates_new_commit(self, temp_git_repo):
        """commit(path, message) creates a new commit."""
        old_sha = get_sha(str(temp_git_repo))

        # Make a change
        (temp_git_repo / "new_file.txt").write_text("new content")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)

        new_sha = commit(str(temp_git_repo), "Test commit")

        assert new_sha != old_sha
        assert len(new_sha) == 40

    def test_commit_returns_sha(self, temp_git_repo):
        """commit() returns the SHA of the new commit."""
        (temp_git_repo / "another.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)

        sha = commit(str(temp_git_repo), "Another commit")

        assert sha == get_sha(str(temp_git_repo))

    def test_commit_with_no_changes_raises(self, temp_git_repo):
        """commit() raises GitError when there are no staged changes."""
        with pytest.raises(GitError):
            commit(str(temp_git_repo), "Empty commit")


class TestGetDiff:
    """Tests for get_diff() function."""

    def test_get_diff_returns_string(self, temp_git_repo):
        """get_diff(path) returns a string."""
        diff = get_diff(str(temp_git_repo))

        assert isinstance(diff, str)

    def test_get_diff_shows_uncommitted_changes(self, temp_git_repo):
        """get_diff() shows uncommitted changes."""
        (temp_git_repo / "README.md").write_text("# Modified content")

        diff = get_diff(str(temp_git_repo))

        assert "Modified content" in diff or "+# Modified content" in diff

    def test_get_diff_empty_when_no_changes(self, temp_git_repo):
        """get_diff() returns empty string when no changes."""
        diff = get_diff(str(temp_git_repo))

        assert diff == "" or diff.strip() == ""


class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes() function."""

    def test_has_uncommitted_changes_false_on_clean(self, temp_git_repo):
        """has_uncommitted_changes() returns False on clean repo."""
        assert has_uncommitted_changes(str(temp_git_repo)) is False

    def test_has_uncommitted_changes_true_on_modified(self, temp_git_repo):
        """has_uncommitted_changes() returns True when files are modified."""
        (temp_git_repo / "README.md").write_text("modified")

        assert has_uncommitted_changes(str(temp_git_repo)) is True

    def test_has_uncommitted_changes_true_on_staged(self, temp_git_repo):
        """has_uncommitted_changes() returns True when files are staged."""
        (temp_git_repo / "new.txt").write_text("new")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)

        assert has_uncommitted_changes(str(temp_git_repo)) is True


class TestPush:
    """Tests for push() function."""

    def test_push_to_remote(self, temp_git_repo, bare_git_repo):
        """push(path, remote, branch) pushes to remote."""
        # Add remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare_git_repo)],
            cwd=temp_git_repo,
            check=True,
            capture_output=True,
        )

        # Get current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip() or "master"

        push(str(temp_git_repo), "origin", branch)

        # Verify push succeeded by checking bare repo
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=bare_git_repo,
            capture_output=True,
        )
        assert result.returncode == 0

    def test_push_raises_on_invalid_remote(self, temp_git_repo):
        """push() raises GitError for non-existent remote."""
        with pytest.raises(GitError):
            push(str(temp_git_repo), "nonexistent", "main")
