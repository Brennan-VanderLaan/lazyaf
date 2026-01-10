"""
Git helper functions for runner operations.

This module provides a clean interface for git operations used by runners.
All functions are designed to be testable and raise specific exceptions on failure.
"""

from pathlib import Path
from typing import Optional


class GitError(Exception):
    """Base exception for git operations."""
    pass


class GitAuthError(GitError):
    """Raised when git authentication fails."""
    pass


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
    raise NotImplementedError("TODO: Implement clone()")


def checkout(path: Path, branch: str) -> None:
    """
    Checkout a branch in the repository.

    Args:
        path: Path to the git repository
        branch: Branch name to checkout

    Raises:
        GitError: If the checkout fails (e.g., branch doesn't exist)
    """
    raise NotImplementedError("TODO: Implement checkout()")


def get_current_branch(path: Path) -> str:
    """
    Get the current branch name.

    Args:
        path: Path to the git repository

    Returns:
        The current branch name as a string
    """
    raise NotImplementedError("TODO: Implement get_current_branch()")


def get_sha(path: Path) -> str:
    """
    Get the current HEAD commit SHA.

    Args:
        path: Path to the git repository

    Returns:
        40-character hex SHA of HEAD commit
    """
    raise NotImplementedError("TODO: Implement get_sha()")


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
    raise NotImplementedError("TODO: Implement push()")


def configure_git(email: str, name: str) -> None:
    """
    Configure git user settings globally.

    Args:
        email: User email for commits
        name: User name for commits
    """
    raise NotImplementedError("TODO: Implement configure_git()")


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
    raise NotImplementedError("TODO: Implement branch_exists()")
