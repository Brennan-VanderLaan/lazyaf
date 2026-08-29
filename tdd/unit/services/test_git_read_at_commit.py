"""
Unit tests for GitRepoManager read-at-commit helpers.

These helpers back pipeline-definition sync-on-push: the trigger service must
read .lazyaf/pipelines/ from the EXACT pushed commit, not whatever a branch
tip happens to point at by the time the event is handled.

Contract:
- get_file_content_at_commit: bytes, or None when repo/commit/file unreadable
- list_directory_at_commit: None when the repo/commit cannot be read (tree
  unknown), [] when the commit is readable but the directory is missing
  (directory removed), else the filename list
"""
import subprocess
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

def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


# Module-scoped: git subprocess setup is expensive on Windows, and every test
# reads at a PINNED sha, so later commits added by one test cannot change what
# another test observes.

@pytest.fixture(scope="module")
def temp_repos_dir():
    """Temp dir for bare repos (resolve() avoids Windows 8.3 short names)."""
    temp_dir = Path(tempfile.mkdtemp()).resolve()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def repo_manager(temp_repos_dir):
    return GitRepoManager(repos_dir=temp_repos_dir)


@pytest.fixture(scope="module")
def local_repo():
    """Local working repo with a .lazyaf/pipelines/ci.yaml at commit 1."""
    temp_dir = Path(tempfile.mkdtemp()).resolve()
    repo_path = temp_dir / "work"
    repo_path.mkdir()

    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "test@test.com")
    _git(repo_path, "config", "user.name", "Test")

    (repo_path / "README.md").write_text("# Test")
    pipelines_dir = repo_path / ".lazyaf" / "pipelines"
    pipelines_dir.mkdir(parents=True)
    (pipelines_dir / "ci.yaml").write_text("name: ci\nsteps: []\n")

    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "initial")

    yield repo_path
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def pushed_repo(repo_manager, local_repo):
    """Bare repo populated from local_repo; returns (repo_id, local_repo, sha1)."""
    repo_id = "at-commit-repo"
    repo_manager.create_bare_repo(repo_id)
    result = repo_manager.push_from_local(repo_id, str(local_repo))
    assert result["success"], result
    sha1 = _git(local_repo, "rev-parse", "HEAD")
    return repo_id, local_repo, sha1


# -----------------------------------------------------------------------------
# get_file_content_at_commit
# -----------------------------------------------------------------------------

class TestGetFileContentAtCommit:
    def test_reads_file_at_commit(self, repo_manager, pushed_repo):
        repo_id, _, sha1 = pushed_repo
        content = repo_manager.get_file_content_at_commit(
            repo_id, sha1, ".lazyaf/pipelines/ci.yaml"
        )
        assert content == b"name: ci\nsteps: []\n"

    def test_missing_file_returns_none(self, repo_manager, pushed_repo):
        repo_id, _, sha1 = pushed_repo
        assert repo_manager.get_file_content_at_commit(
            repo_id, sha1, ".lazyaf/pipelines/nope.yaml"
        ) is None

    def test_unknown_commit_returns_none(self, repo_manager, pushed_repo):
        repo_id, _, _ = pushed_repo
        assert repo_manager.get_file_content_at_commit(
            repo_id, "0" * 40, ".lazyaf/pipelines/ci.yaml"
        ) is None

    def test_unknown_repo_returns_none(self, repo_manager, pushed_repo):
        _, _, sha1 = pushed_repo
        assert repo_manager.get_file_content_at_commit(
            "no-such-repo", sha1, ".lazyaf/pipelines/ci.yaml"
        ) is None

    def test_old_commit_still_readable_after_new_push(self, repo_manager, pushed_repo):
        """Reading at a sha is pinned to that commit, not the branch tip."""
        repo_id, local_repo, sha1 = pushed_repo

        yaml_path = local_repo / ".lazyaf" / "pipelines" / "ci.yaml"
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("name: ci\ndescription: v2\nsteps: []\n")
        _git(local_repo, "add", ".")
        _git(local_repo, "commit", "-m", "update ci")
        result = repo_manager.push_from_local(repo_id, str(local_repo))
        assert result["success"], result
        sha2 = _git(local_repo, "rev-parse", "HEAD")

        assert repo_manager.get_file_content_at_commit(
            repo_id, sha1, ".lazyaf/pipelines/ci.yaml"
        ) == b"name: ci\nsteps: []\n"
        assert repo_manager.get_file_content_at_commit(
            repo_id, sha2, ".lazyaf/pipelines/ci.yaml"
        ) == b"name: ci\ndescription: v2\nsteps: []\n"


# -----------------------------------------------------------------------------
# list_directory_at_commit
# -----------------------------------------------------------------------------

class TestListDirectoryAtCommit:
    def test_lists_directory_at_commit(self, repo_manager, pushed_repo):
        repo_id, _, sha1 = pushed_repo
        files = repo_manager.list_directory_at_commit(
            repo_id, sha1, ".lazyaf/pipelines"
        )
        assert files == ["ci.yaml"]

    def test_missing_directory_returns_empty_list(self, repo_manager, pushed_repo):
        """Commit readable + directory absent => [] (removed), not None."""
        repo_id, _, sha1 = pushed_repo
        assert repo_manager.list_directory_at_commit(
            repo_id, sha1, ".lazyaf/agents"
        ) == []

    def test_unknown_commit_returns_none(self, repo_manager, pushed_repo):
        """Unreadable commit => None (tree unknown)."""
        repo_id, _, _ = pushed_repo
        assert repo_manager.list_directory_at_commit(
            repo_id, "f" * 40, ".lazyaf/pipelines"
        ) is None

    def test_unknown_repo_returns_none(self, repo_manager, pushed_repo):
        _, _, sha1 = pushed_repo
        assert repo_manager.list_directory_at_commit(
            "no-such-repo", sha1, ".lazyaf/pipelines"
        ) is None

    def test_file_path_returns_empty_list(self, repo_manager, pushed_repo):
        """A blob path is not a directory."""
        repo_id, _, sha1 = pushed_repo
        assert repo_manager.list_directory_at_commit(
            repo_id, sha1, ".lazyaf/pipelines/ci.yaml"
        ) == []

    def test_directory_removed_in_later_commit(self, repo_manager, pushed_repo):
        """Old commit keeps the dir; new commit without it reads as []."""
        repo_id, local_repo, sha1 = pushed_repo

        _git(local_repo, "rm", "-r", ".lazyaf")
        _git(local_repo, "commit", "-m", "drop lazyaf dir")
        result = repo_manager.push_from_local(repo_id, str(local_repo))
        assert result["success"], result
        sha2 = _git(local_repo, "rev-parse", "HEAD")

        assert repo_manager.list_directory_at_commit(
            repo_id, sha1, ".lazyaf/pipelines"
        ) == ["ci.yaml"]
        assert repo_manager.list_directory_at_commit(
            repo_id, sha2, ".lazyaf/pipelines"
        ) == []


# -----------------------------------------------------------------------------
# get_tree_sha_at_commit
# -----------------------------------------------------------------------------

class TestGetTreeShaAtCommit:
    """Contract: hex sha when the path names a tree, "" when the commit is
    readable but the path is absent/not a tree, None when repo/commit is
    unreadable. Backs the sync-on-push short-circuit."""

    def test_returns_subtree_sha(self, repo_manager, pushed_repo):
        repo_id, _, sha1 = pushed_repo
        tree_sha = repo_manager.get_tree_sha_at_commit(
            repo_id, sha1, ".lazyaf/pipelines"
        )
        assert isinstance(tree_sha, str)
        assert len(tree_sha) == 40
        assert set(tree_sha) <= set("0123456789abcdef")

    def test_missing_path_returns_empty_string(self, repo_manager, pushed_repo):
        repo_id, _, sha1 = pushed_repo
        assert repo_manager.get_tree_sha_at_commit(
            repo_id, sha1, ".lazyaf/agents"
        ) == ""

    def test_blob_path_returns_empty_string(self, repo_manager, pushed_repo):
        """A file path does not name a tree."""
        repo_id, _, sha1 = pushed_repo
        assert repo_manager.get_tree_sha_at_commit(
            repo_id, sha1, ".lazyaf/pipelines/ci.yaml"
        ) == ""

    def test_unknown_commit_returns_none(self, repo_manager, pushed_repo):
        repo_id, _, _ = pushed_repo
        assert repo_manager.get_tree_sha_at_commit(
            repo_id, "0" * 40, ".lazyaf/pipelines"
        ) is None

    def test_unknown_repo_returns_none(self, repo_manager, pushed_repo):
        _, _, sha1 = pushed_repo
        assert repo_manager.get_tree_sha_at_commit(
            "no-such-repo", sha1, ".lazyaf/pipelines"
        ) is None

    def test_sha_stable_across_unrelated_commits_and_changes_on_edit(
        self, repo_manager, pushed_repo
    ):
        """Equal subtree shas iff the directory content is identical."""
        repo_id, local_repo, _ = pushed_repo

        # Re-create a known pipelines dir state (earlier tests may have
        # removed it from the working tree)
        pipelines_dir = local_repo / ".lazyaf" / "pipelines"
        pipelines_dir.mkdir(parents=True, exist_ok=True)
        (pipelines_dir / "tree-sha.yaml").write_text("name: tree-sha\nsteps: []\n")
        _git(local_repo, "add", ".")
        _git(local_repo, "commit", "-m", "seed pipelines dir")
        assert repo_manager.push_from_local(repo_id, str(local_repo))["success"]
        base_sha = _git(local_repo, "rev-parse", "HEAD")

        # Unrelated change: subtree sha must not move
        (local_repo / "unrelated.txt").write_text("x")
        _git(local_repo, "add", ".")
        _git(local_repo, "commit", "-m", "unrelated change")
        assert repo_manager.push_from_local(repo_id, str(local_repo))["success"]
        unrelated_sha = _git(local_repo, "rev-parse", "HEAD")

        base_tree = repo_manager.get_tree_sha_at_commit(
            repo_id, base_sha, ".lazyaf/pipelines"
        )
        assert base_tree
        assert repo_manager.get_tree_sha_at_commit(
            repo_id, unrelated_sha, ".lazyaf/pipelines"
        ) == base_tree

        # Pipeline edit: subtree sha must move
        (pipelines_dir / "tree-sha.yaml").write_text(
            "name: tree-sha\ndescription: v2\nsteps: []\n"
        )
        _git(local_repo, "add", ".")
        _git(local_repo, "commit", "-m", "edit pipeline yaml")
        assert repo_manager.push_from_local(repo_id, str(local_repo))["success"]
        edited_sha = _git(local_repo, "rev-parse", "HEAD")

        edited_tree = repo_manager.get_tree_sha_at_commit(
            repo_id, edited_sha, ".lazyaf/pipelines"
        )
        assert edited_tree
        assert edited_tree != base_tree
