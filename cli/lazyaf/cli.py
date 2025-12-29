"""
LazyAF CLI - Ingest repos and land changes.

Usage:
    lazyaf ingest /path/to/repo --name my-project
    lazyaf land <repo_id> --branch feature/foo
"""

import os
import subprocess
import sys
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.panel import Panel

console = Console()

DEFAULT_SERVER = "http://localhost:8000"


def get_server_url() -> str:
    """Get the LazyAF server URL from env or default."""
    return os.environ.get("LAZYAF_SERVER", DEFAULT_SERVER)


def run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@click.group()
@click.version_option()
def cli():
    """LazyAF - Visual orchestrator for AI agents."""
    pass


@cli.command()
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--name", "-n", required=True, help="Name for the repo in LazyAF")
@click.option("--branch", "-b", default=None, help="Branch to push (default: current branch)")
@click.option("--all-branches", "-a", is_flag=True, help="Push all branches")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def ingest(repo_path: str, name: str, branch: str | None, all_branches: bool, server: str | None):
    """
    Ingest a local git repository into LazyAF.

    This creates a repo record and pushes the content to LazyAF's internal git server.
    Agents will work against this internal copy, keeping your real remote clean.

    Example:
        lazyaf ingest ./my-project --name my-project
        lazyaf ingest ./my-project --name my-project --branch main
        lazyaf ingest ./my-project --name my-project --all-branches
    """
    path = Path(repo_path)
    server_url = server or get_server_url()

    # Validate it's a git repo
    git_dir = path / ".git"
    if not git_dir.exists():
        console.print(f"[red]Error:[/red] {path} is not a git repository")
        sys.exit(1)

    console.print(Panel(f"Ingesting [cyan]{name}[/cyan] from {path}"))

    # Detect default branch if not specified
    if not branch and not all_branches:
        result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        if result.returncode != 0:
            console.print(f"[red]Error:[/red] Could not detect current branch")
            sys.exit(1)
        branch = result.stdout.strip()
        console.print(f"Using current branch: [cyan]{branch}[/cyan]")

    # Get remote URL if exists (for future landing)
    result = run_git(["remote", "get-url", "origin"], cwd=path)
    remote_url = result.stdout.strip() if result.returncode == 0 else None

    # Call ingest API
    console.print(f"Creating repo on [blue]{server_url}[/blue]...")
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{server_url}/api/repos/ingest",
                json={
                    "name": name,
                    "remote_url": remote_url,
                    "default_branch": branch or "main",
                },
            )
            response.raise_for_status()
            data = response.json()
    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        console.print("Is the LazyAF server running?")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
        console.print(e.response.text)
        sys.exit(1)

    repo_id = data["id"]
    clone_url = data["clone_url"]
    console.print(f"Created repo [green]{repo_id}[/green]")

    # Add lazyaf remote
    console.print("Adding lazyaf remote...")
    run_git(["remote", "remove", "lazyaf"], cwd=path)  # Remove if exists
    result = run_git(["remote", "add", "lazyaf", clone_url], cwd=path)
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Failed to add remote: {result.stderr}")
        sys.exit(1)

    # Push to internal server
    if all_branches:
        console.print("Pushing all branches...")
        push_args = ["push", "lazyaf", "--all"]
    else:
        console.print(f"Pushing branch [cyan]{branch}[/cyan]...")
        push_args = ["push", "lazyaf", branch]

    result = run_git(push_args, cwd=path)
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Push failed")
        console.print(result.stderr)
        sys.exit(1)

    console.print()
    console.print(Panel.fit(
        f"[green]Success![/green]\n\n"
        f"Repo ID: [cyan]{repo_id}[/cyan]\n"
        f"Clone URL: {clone_url}\n\n"
        f"Your repo is now available in LazyAF.\n"
        f"Create cards in the UI to start working with AI agents.",
        title="Ingested",
    ))


@cli.command()
@click.argument("repo_id")
@click.option("--branch", "-b", required=True, help="Branch to land")
@click.option("--remote", "-r", default="origin", help="Remote to push to (default: origin)")
@click.option("--pr", is_flag=True, help="Create a pull request using gh CLI")
@click.option("--base", default=None, help="Base branch for PR (default: repo's default branch)")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def land(repo_id: str, branch: str, remote: str, pr: bool, base: str | None, server: str | None):
    """
    Land a branch from LazyAF's internal git server to a real remote.

    This fetches the branch from LazyAF and pushes it to your configured remote
    (usually origin/GitHub/GitLab).

    Example:
        lazyaf land abc123 --branch feature/new-api
        lazyaf land abc123 --branch feature/new-api --pr
        lazyaf land abc123 --branch feature/new-api --pr --base develop
    """
    server_url = server or get_server_url()

    console.print(Panel(f"Landing branch [cyan]{branch}[/cyan] from repo [cyan]{repo_id}[/cyan]"))

    # Get repo info from API
    console.print(f"Fetching repo info from [blue]{server_url}[/blue]...")
    try:
        with httpx.Client(timeout=30.0) as client:
            # Get repo details
            response = client.get(f"{server_url}/api/repos/{repo_id}")
            response.raise_for_status()
            repo_data = response.json()

            # Get clone URL
            response = client.get(f"{server_url}/api/repos/{repo_id}/clone-url")
            response.raise_for_status()
            url_data = response.json()
    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Repo {repo_id} not found")
        else:
            console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
        sys.exit(1)

    clone_url = url_data["clone_url"]
    remote_url = repo_data.get("remote_url")
    default_branch = repo_data.get("default_branch", "main")
    base_branch = base or default_branch

    if not remote_url:
        console.print(f"[yellow]Warning:[/yellow] No remote URL configured for this repo")
        console.print("You'll need to push manually or configure the remote URL")

    # We need to be in a git repo to fetch/push
    # Create a temp directory or use current if it's the right repo
    cwd = Path.cwd()
    git_dir = cwd / ".git"

    if not git_dir.exists():
        console.print(f"[red]Error:[/red] Current directory is not a git repository")
        console.print("Run this command from your local clone of the repo")
        sys.exit(1)

    # Add/update lazyaf remote
    console.print("Configuring lazyaf remote...")
    run_git(["remote", "remove", "lazyaf"], cwd=cwd)
    result = run_git(["remote", "add", "lazyaf", clone_url], cwd=cwd)
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Failed to add remote: {result.stderr}")
        sys.exit(1)

    # Fetch from lazyaf
    console.print(f"Fetching [cyan]{branch}[/cyan] from LazyAF...")
    result = run_git(["fetch", "lazyaf", branch], cwd=cwd)
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Fetch failed")
        console.print(result.stderr)
        sys.exit(1)

    # Push to origin
    console.print(f"Pushing to [cyan]{remote}/{branch}[/cyan]...")
    result = run_git(["push", remote, f"lazyaf/{branch}:{branch}"], cwd=cwd)
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Push failed")
        console.print(result.stderr)
        sys.exit(1)

    console.print(f"[green]Pushed branch {branch} to {remote}[/green]")

    # Create PR if requested
    if pr:
        console.print(f"\nCreating PR against [cyan]{base_branch}[/cyan]...")
        result = subprocess.run(
            ["gh", "pr", "create", "--base", base_branch, "--head", branch, "--fill"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Warning:[/yellow] PR creation failed")
            console.print(result.stderr)
            console.print("You can create the PR manually on GitHub")
        else:
            pr_url = result.stdout.strip()
            console.print(f"[green]Created PR:[/green] {pr_url}")

    console.print()
    console.print(Panel.fit(
        f"[green]Landed![/green]\n\n"
        f"Branch [cyan]{branch}[/cyan] is now on [cyan]{remote}[/cyan]",
        title="Success",
    ))


@cli.command("list")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def list_repos(server: str | None):
    """List all repos in LazyAF."""
    server_url = server or get_server_url()

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{server_url}/api/repos")
            response.raise_for_status()
            repos = response.json()
    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
        sys.exit(1)

    if not repos:
        console.print("No repos found. Use [cyan]lazyaf ingest[/cyan] to add one.")
        return

    console.print(f"Found {len(repos)} repo(s):\n")
    for repo in repos:
        status = "[green]ingested[/green]" if repo["is_ingested"] else "[yellow]not ingested[/yellow]"
        console.print(f"  [cyan]{repo['id']}[/cyan]  {repo['name']}  {status}")
        if repo.get("remote_url"):
            console.print(f"    Remote: {repo['remote_url']}")


@cli.command()
@click.argument("repo_id")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def branches(repo_id: str, server: str | None):
    """List branches in a LazyAF repo."""
    server_url = server or get_server_url()

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{server_url}/api/repos/{repo_id}/branches")
            response.raise_for_status()
            data = response.json()
    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Repo {repo_id} not found")
        else:
            console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
        sys.exit(1)

    branches = data["branches"]
    if not branches:
        console.print("No branches found. Push some content first.")
        return

    console.print(f"Branches in repo ({data['total']}):\n")
    for branch in branches:
        markers = []
        if branch["is_default"]:
            markers.append("[green]default[/green]")
        if branch["is_lazyaf"]:
            markers.append("[blue]lazyaf[/blue]")
        marker_str = " ".join(markers)
        console.print(f"  [cyan]{branch['name']}[/cyan]  {branch['commit'][:8]}  {marker_str}")


if __name__ == "__main__":
    cli()
