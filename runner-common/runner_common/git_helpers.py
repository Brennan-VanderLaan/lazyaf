"""
Git helper functions for runner operations.

Provides a clean interface for git operations used by runners.
"""

import subprocess
from pathlib import Path


class GitError(Exception):
    """Raised when a git operation fails."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _run_git(args: list[str], cwd: str | Path | None = None) -> tuple[int, str, str]:
    """
    Run a git command and return (returncode, stdout, stderr).

    Args:
        args: Git command arguments (without 'git' prefix)
        cwd: Working directory

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    cmd = ["git"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def clone(url: str, path: str, branch: str | None = None) -> None:
    """
    Clone a git repository.

    Args:
        url: Repository URL
        path: Target path for the clone
        branch: Optional branch to clone

    Raises:
        GitError: If clone fails
    """
    args = ["clone"]
    if branch:
        args.extend(["-b", branch])
    args.extend([url, path])

    returncode, stdout, stderr = _run_git(args)

    if returncode != 0:
        raise GitError(
            f"Failed to clone {url}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def checkout(path: str, branch: str, create: bool = False) -> None:
    """
    Checkout a branch.

    Args:
        path: Repository path
        branch: Branch name
        create: If True, create the branch if it doesn't exist

    Raises:
        GitError: If checkout fails
    """
    if create:
        args = ["checkout", "-b", branch]
    else:
        args = ["checkout", branch]

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to checkout {branch}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def get_sha(path: str) -> str:
    """
    Get the current commit SHA.

    Args:
        path: Repository path

    Returns:
        Full 40-character commit SHA

    Raises:
        GitError: If not a git repository or other error
    """
    returncode, stdout, stderr = _run_git(["rev-parse", "HEAD"], cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to get SHA: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )

    return stdout.strip()


def get_current_branch(path: str) -> str:
    """
    Get the current branch name.

    Args:
        path: Repository path

    Returns:
        Current branch name

    Raises:
        GitError: If not a git repository or detached HEAD
    """
    returncode, stdout, stderr = _run_git(["branch", "--show-current"], cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to get current branch: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )

    branch = stdout.strip()
    if not branch:
        # Detached HEAD
        raise GitError("Detached HEAD - no current branch")

    return branch


def push(path: str, remote: str, branch: str, force: bool = False) -> None:
    """
    Push to a remote repository.

    Args:
        path: Repository path
        remote: Remote name (e.g., "origin")
        branch: Branch to push
        force: If True, force push

    Raises:
        GitError: If push fails
    """
    args = ["push"]
    if force:
        args.append("--force")
    args.extend([remote, branch])

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to push to {remote}/{branch}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def commit(path: str, message: str, allow_empty: bool = False) -> str:
    """
    Create a commit.

    Args:
        path: Repository path
        message: Commit message
        allow_empty: If True, allow empty commits

    Returns:
        The new commit SHA

    Raises:
        GitError: If commit fails (including no staged changes)
    """
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        if "nothing to commit" in stderr or "nothing to commit" in stdout:
            raise GitError(
                "Nothing to commit - no staged changes",
                returncode=returncode,
                stderr=stderr,
            )
        raise GitError(
            f"Failed to commit: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )

    return get_sha(path)


def get_diff(path: str, staged: bool = False) -> str:
    """
    Get the diff of uncommitted changes.

    Args:
        path: Repository path
        staged: If True, show only staged changes

    Returns:
        Diff output as string (empty if no changes)
    """
    args = ["diff"]
    if staged:
        args.append("--cached")

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to get diff: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )

    return stdout


def has_uncommitted_changes(path: str) -> bool:
    """
    Check if there are uncommitted changes.

    Args:
        path: Repository path

    Returns:
        True if there are uncommitted changes (staged or unstaged)
    """
    # Check for staged changes
    returncode, stdout, stderr = _run_git(["diff", "--cached", "--quiet"], cwd=path)
    if returncode != 0:
        return True

    # Check for unstaged changes
    returncode, stdout, stderr = _run_git(["diff", "--quiet"], cwd=path)
    if returncode != 0:
        return True

    # Check for untracked files
    returncode, stdout, stderr = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=path,
    )
    if stdout.strip():
        return True

    return False


def add(path: str, files: list[str] | str = ".") -> None:
    """
    Stage files for commit.

    Args:
        path: Repository path
        files: Files to add (default: all)

    Raises:
        GitError: If add fails
    """
    if isinstance(files, str):
        files = [files]

    args = ["add"] + files

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to add files: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def fetch(path: str, remote: str = "origin", branch: str | None = None) -> None:
    """
    Fetch from remote.

    Args:
        path: Repository path
        remote: Remote name
        branch: Optional specific branch to fetch

    Raises:
        GitError: If fetch fails
    """
    args = ["fetch", remote]
    if branch:
        args.append(branch)

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to fetch from {remote}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def merge(path: str, branch: str, no_ff: bool = False) -> None:
    """
    Merge a branch into the current branch.

    Args:
        path: Repository path
        branch: Branch to merge
        no_ff: If True, always create a merge commit

    Raises:
        GitError: If merge fails (including conflicts)
    """
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    args.append(branch)

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to merge {branch}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )


def config_set(path: str, key: str, value: str, local: bool = True) -> None:
    """
    Set a git config value.

    Args:
        path: Repository path
        key: Config key (e.g., "user.email")
        value: Config value
        local: If True, set locally; otherwise globally

    Raises:
        GitError: If config fails
    """
    args = ["config"]
    if local:
        args.append("--local")
    else:
        args.append("--global")
    args.extend([key, value])

    returncode, stdout, stderr = _run_git(args, cwd=path)

    if returncode != 0:
        raise GitError(
            f"Failed to set config {key}: {stderr}",
            returncode=returncode,
            stderr=stderr,
        )
