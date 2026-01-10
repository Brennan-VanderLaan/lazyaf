"""
Git helper functions for runner operations.

This module provides a clean interface for git operations used by runners.
All functions are designed to be testable and raise specific exceptions on failure.
"""

import subprocess
from pathlib import Path
from typing import Optional


class GitError(Exception):
    """Base exception for git operations."""
    pass


class GitAuthError(GitError):
    """Raised when git authentication fails."""
    pass


def _run_git(args: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
        )
        return result
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.lower() if e.stderr else ""
        # Check for authentication errors
        if any(keyword in stderr for keyword in ["authentication", "denied", "unauthorized", "401", "403", "invalid credentials"]):
            raise GitAuthError(f"Git authentication failed: {e.stderr}") from e
        raise GitError(f"Git command failed: {e.stderr}") from e


def clone(url: str, path: Path, branch: Optional[str] = None) -> None:
    """
    Clone a git repository to the specified path.

    Args:
        url: The git repository URL
        path: Target directory for the clone
        branch: Optional branch to checkout after cloning

    Raises:
        GitError: If the clone operation fails
        GitAuthError: If authentication is required but fails
    """
    args = ["clone", url, str(path)]
    if branch:
        args.extend(["--branch", branch])

    try:
        _run_git(args)
    except GitError as e:
        # Re-check stderr for more specific error types
        error_msg = str(e).lower()
        if "could not read from remote" in error_msg or "authentication" in error_msg:
            raise GitAuthError(f"Clone failed - authentication required: {e}") from e
        raise GitError(f"Clone failed: {e}") from e


def checkout(path: Path, branch: str) -> None:
    """
    Checkout a branch in the repository.

    Args:
        path: Path to the git repository
        branch: Branch name to checkout

    Raises:
        GitError: If the checkout fails (e.g., branch doesn't exist)
    """
    try:
        _run_git(["checkout", branch], cwd=path)
    except GitError as e:
        if "did not match" in str(e).lower() or "pathspec" in str(e).lower():
            raise GitError(f"Branch '{branch}' does not exist") from e
        raise


def get_current_branch(path: Path) -> str:
    """
    Get the current branch name.

    Args:
        path: Path to the git repository

    Returns:
        The current branch name as a string
    """
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return result.stdout.strip()


def get_sha(path: Path) -> str:
    """
    Get the current HEAD commit SHA.

    Args:
        path: Path to the git repository

    Returns:
        40-character hex SHA of HEAD commit
    """
    result = _run_git(["rev-parse", "HEAD"], cwd=path)
    return result.stdout.strip()


def push(path: Path, branch: str, set_upstream: bool = False, force: bool = False) -> None:
    """
    Push commits to the remote.

    Args:
        path: Path to the git repository
        branch: Branch name to push
        set_upstream: If True, set upstream tracking
        force: If True, force push

    Raises:
        GitError: If the push is rejected or fails
    """
    args = ["push"]
    if set_upstream:
        args.extend(["-u", "origin", branch])
    else:
        args.extend(["origin", branch])
    if force:
        args.append("--force")

    _run_git(args, cwd=path)


def configure_git(email: str, name: str) -> None:
    """
    Configure git user settings globally.

    Args:
        email: User email for commits
        name: User name for commits
    """
    _run_git(["config", "--global", "user.email", email])
    _run_git(["config", "--global", "user.name", name])


def branch_exists(path: Path, branch: str, remote: bool = False) -> bool:
    """
    Check if a branch exists.

    Args:
        path: Path to the git repository
        branch: Branch name to check
        remote: If True, check remote branches

    Returns:
        True if the branch exists, False otherwise
    """
    if remote:
        # Check remote branches
        result = _run_git(["ls-remote", "--heads", "origin", branch], cwd=path, check=False)
        return bool(result.stdout.strip())
    else:
        # Check local branches
        result = _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=path, check=False)
        return result.returncode == 0
