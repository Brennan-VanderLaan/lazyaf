"""
Tests for git_helpers module - defines the contract for git operations.

These tests are written BEFORE the implementation to define expected behavior.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


class TestClone:
    """Tests for clone() function."""

    def test_clone_creates_repo_at_path(self, tmp_path, git_server_url):
        """clone(url, path) creates a git repo at the specified path."""
        from runner_common.git_helpers import clone

        target = tmp_path / "repo"
        clone(git_server_url, target)

        assert target.exists()
        assert (target / ".git").is_dir()

    def test_clone_raises_on_invalid_url(self, tmp_path):
        """clone() raises GitError for invalid URLs."""
        from runner_common.git_helpers import clone, GitError

        target = tmp_path / "repo"
        with pytest.raises(GitError) as exc_info:
            clone("https://invalid.example.com/nonexistent.git", target)

        assert "clone" in str(exc_info.value).lower()

    def test_clone_raises_on_auth_failure(self, tmp_path):
        """clone() raises GitAuthError for authentication failures."""
        from runner_common.git_helpers import clone, GitAuthError

        target = tmp_path / "repo"
        # URL that requires auth but none provided
        with pytest.raises(GitAuthError):
            clone("https://github.com/private/nonexistent-repo-12345.git", target)

    def test_clone_with_branch(self, tmp_path, git_server_url):
        """clone(url, path, branch) checks out the specified branch."""
        from runner_common.git_helpers import clone, get_current_branch

        target = tmp_path / "repo"
        clone(git_server_url, target, branch="main")

        assert get_current_branch(target) == "main"

    def test_clone_overwrites_existing_empty_dir(self, tmp_path, git_server_url):
        """clone() succeeds if target is an empty directory."""
        from runner_common.git_helpers import clone

        target = tmp_path / "repo"
        target.mkdir()

        clone(git_server_url, target)
        assert (target / ".git").is_dir()


class TestCheckout:
    """Tests for checkout() function."""

    def test_checkout_switches_branch(self, cloned_repo):
        """checkout(path, branch) switches to the specified branch."""
        from runner_common.git_helpers import checkout, get_current_branch

        # Create a new branch first
        subprocess.run(
            ["git", "checkout", "-b", "feature-branch"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        checkout(cloned_repo, "feature-branch")
        assert get_current_branch(cloned_repo) == "feature-branch"

    def test_checkout_creates_branch_from_remote(self, cloned_repo):
        """checkout() can create a local branch tracking a remote branch."""
        from runner_common.git_helpers import checkout, get_current_branch

        # This should work even if the branch only exists on remote
        checkout(cloned_repo, "main")
        assert get_current_branch(cloned_repo) == "main"

    def test_checkout_raises_on_nonexistent_branch(self, cloned_repo):
        """checkout() raises GitError for branches that don't exist."""
        from runner_common.git_helpers import checkout, GitError

        with pytest.raises(GitError) as exc_info:
            checkout(cloned_repo, "nonexistent-branch-12345")

        assert "branch" in str(exc_info.value).lower()


class TestGetCurrentBranch:
    """Tests for get_current_branch() function."""

    def test_get_current_branch_returns_string(self, cloned_repo):
        """get_current_branch(path) returns the current branch name as string."""
        from runner_common.git_helpers import get_current_branch

        branch = get_current_branch(cloned_repo)
        assert isinstance(branch, str)
        assert len(branch) > 0

    def test_get_current_branch_after_checkout(self, cloned_repo):
        """get_current_branch() reflects checkout changes."""
        from runner_common.git_helpers import get_current_branch

        subprocess.run(
            ["git", "checkout", "-b", "test-branch"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        assert get_current_branch(cloned_repo) == "test-branch"


class TestGetSha:
    """Tests for get_sha() function."""

    def test_get_sha_returns_40_char_hex(self, cloned_repo):
        """get_sha(path) returns a 40-character hex string."""
        from runner_common.git_helpers import get_sha

        sha = get_sha(cloned_repo)
        assert isinstance(sha, str)
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_get_sha_changes_after_commit(self, cloned_repo):
        """get_sha() returns different value after a new commit."""
        from runner_common.git_helpers import get_sha

        sha_before = get_sha(cloned_repo)

        # Make a commit
        test_file = cloned_repo / "test.txt"
        test_file.write_text("test content")
        subprocess.run(["git", "add", "."], cwd=cloned_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        sha_after = get_sha(cloned_repo)
        assert sha_before != sha_after


class TestPush:
    """Tests for push() function."""

    def test_push_sends_commits_to_remote(self, cloned_repo):
        """push(path, branch) pushes commits to the remote."""
        from runner_common.git_helpers import push

        # Make a commit
        test_file = cloned_repo / "push_test.txt"
        test_file.write_text("push test content")
        subprocess.run(["git", "add", "."], cwd=cloned_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "push test"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        # Push should not raise
        push(cloned_repo, "main")

    def test_push_creates_remote_branch(self, cloned_repo):
        """push(path, branch) can create a new branch on remote."""
        from runner_common.git_helpers import push

        # Create and checkout new branch
        subprocess.run(
            ["git", "checkout", "-b", "new-feature"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        # Make a commit
        test_file = cloned_repo / "feature.txt"
        test_file.write_text("feature content")
        subprocess.run(["git", "add", "."], cwd=cloned_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feature"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        push(cloned_repo, "new-feature", set_upstream=True)

    def test_push_raises_on_rejected(self, cloned_repo):
        """push() raises GitError when push is rejected."""
        from runner_common.git_helpers import push, GitError

        # Reset to before remote HEAD (simulating diverged history)
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=cloned_repo,
            check=True,
            capture_output=True,
        )

        # Push should fail (non-fast-forward)
        with pytest.raises(GitError):
            push(cloned_repo, "main", force=False)


class TestConfigureGit:
    """Tests for configure_git() function."""

    def test_configure_git_sets_user(self, tmp_path):
        """configure_git() sets user.name and user.email."""
        from runner_common.git_helpers import configure_git

        configure_git(
            email="test@example.com",
            name="Test User",
        )

        # Verify config was set
        result = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
        )
        assert "test@example.com" in result.stdout


class TestBranchExists:
    """Tests for branch_exists() function."""

    def test_branch_exists_returns_true_for_existing(self, cloned_repo):
        """branch_exists(path, branch) returns True for existing branches."""
        from runner_common.git_helpers import branch_exists

        assert branch_exists(cloned_repo, "main") is True

    def test_branch_exists_returns_false_for_nonexistent(self, cloned_repo):
        """branch_exists(path, branch) returns False for non-existent branches."""
        from runner_common.git_helpers import branch_exists

        assert branch_exists(cloned_repo, "nonexistent-12345") is False

    def test_branch_exists_checks_remote(self, cloned_repo):
        """branch_exists(path, branch, remote=True) checks remote branches."""
        from runner_common.git_helpers import branch_exists

        # main should exist on remote
        assert branch_exists(cloned_repo, "main", remote=True) is True


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def git_server_url(tmp_path):
    """Create a bare git repo to serve as a "remote"."""
    bare_repo = tmp_path / "remote.git"
    bare_repo.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare_repo, check=True, capture_output=True)

    # Create initial commit in a temp clone
    temp_clone = tmp_path / "temp_clone"
    subprocess.run(["git", "clone", str(bare_repo), str(temp_clone)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=temp_clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=temp_clone, check=True)

    readme = temp_clone / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=temp_clone, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_clone, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=temp_clone, check=True, capture_output=True)

    return str(bare_repo)


@pytest.fixture
def cloned_repo(tmp_path, git_server_url):
    """Create a cloned repo for testing."""
    repo_path = tmp_path / "cloned"
    subprocess.run(["git", "clone", git_server_url, str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True)
    return repo_path
