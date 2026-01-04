"""
LazyAF CLI - Ingest repos and land changes.

Usage:
    lazyaf ingest /path/to/repo --name my-project
    lazyaf land <repo_id> --branch feature/foo
    lazyaf debug <session_id> --token <token>
"""

import asyncio
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


@cli.command()
@click.argument("session_id")
@click.option("--token", "-t", required=True, help="Debug session auth token")
@click.option("--sidecar", is_flag=True, help="Use sidecar mode (filesystem inspection)")
@click.option("--shell", is_flag=True, help="Use shell mode (exec into running container)")
@click.option("--resume", "do_resume", is_flag=True, help="Resume pipeline execution")
@click.option("--abort", "do_abort", is_flag=True, help="Abort debug session")
@click.option("--status", "do_status", is_flag=True, help="Show session status")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug(
    session_id: str,
    token: str,
    sidecar: bool,
    shell: bool,
    do_resume: bool,
    do_abort: bool,
    do_status: bool,
    server: str | None,
):
    """
    Connect to a debug session for interactive debugging.

    Debug sessions are created when you click "Debug Re-run" on a failed pipeline.
    Use the join command shown in the UI to connect.

    Examples:
        lazyaf debug abc123 --token xyz --sidecar
        lazyaf debug abc123 --token xyz --shell
        lazyaf debug abc123 --token xyz --status
        lazyaf debug abc123 --token xyz --resume
        lazyaf debug abc123 --token xyz --abort
    """
    server_url = server or get_server_url()

    # Handle control commands (non-interactive)
    if do_resume or do_abort or do_status:
        asyncio.run(_handle_control_command(
            server_url, session_id, token, do_resume, do_abort, do_status
        ))
        return

    # Default to sidecar mode if neither specified
    mode = "shell" if shell else "sidecar"

    # Connect to terminal WebSocket
    asyncio.run(_connect_terminal(server_url, session_id, token, mode))


async def _handle_control_command(
    server_url: str,
    session_id: str,
    token: str,
    do_resume: bool,
    do_abort: bool,
    do_status: bool,
):
    """Handle control commands (resume/abort/status)."""
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            if do_status:
                response = client.get(f"{server_url}/api/debug/{session_id}")
                response.raise_for_status()
                info = response.json()

                console.print(Panel.fit(
                    f"Session ID: [cyan]{info['id']}[/cyan]\n"
                    f"Status: [yellow]{info['status']}[/yellow]\n"
                    f"Current Step: {info.get('current_step', {}).get('name', 'N/A')}\n"
                    f"Expires: {info.get('expires_at', 'N/A')}",
                    title="Debug Session Status",
                ))

            elif do_resume:
                response = client.post(f"{server_url}/api/debug/{session_id}/resume")
                response.raise_for_status()
                console.print("[green]Pipeline execution resumed.[/green]")

            elif do_abort:
                response = client.post(f"{server_url}/api/debug/{session_id}/abort")
                response.raise_for_status()
                console.print("[yellow]Debug session aborted.[/yellow]")

    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Session {session_id} not found")
        elif e.response.status_code == 410:
            console.print(f"[red]Error:[/red] Session has expired")
        else:
            console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
            console.print(e.response.text)
        sys.exit(1)


async def _connect_terminal(
    server_url: str,
    session_id: str,
    token: str,
    mode: str,
):
    """Connect to debug terminal via WebSocket."""
    try:
        import websockets
    except ImportError:
        console.print("[red]Error:[/red] websockets package not installed")
        console.print("Run: pip install websockets")
        sys.exit(1)

    # Convert HTTP URL to WebSocket URL
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/api/debug/{session_id}/terminal?mode={mode}&token={token}"

    console.print(f"Connecting to debug session [cyan]{session_id}[/cyan] ({mode} mode)...")

    try:
        async with websockets.connect(uri) as websocket:
            console.print("[green]Connected![/green] Type @help for commands, Ctrl+C to disconnect.")
            console.print()

            # Run interactive terminal
            await _run_terminal(websocket)

    except websockets.exceptions.InvalidStatusCode as e:
        if e.status_code == 4001:
            console.print("[red]Error:[/red] Invalid token")
        elif e.status_code == 4002:
            console.print("[red]Error:[/red] Cannot connect - session not at breakpoint")
        elif e.status_code == 4004:
            console.print("[red]Error:[/red] Session not found")
        else:
            console.print(f"[red]Error:[/red] Connection failed (code {e.status_code})")
        sys.exit(1)
    except websockets.exceptions.ConnectionClosed as e:
        console.print(f"[yellow]Disconnected:[/yellow] {e.reason or 'Connection closed'}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_terminal(websocket):
    """Run interactive terminal session."""
    import json

    async def receive_messages():
        """Receive and display messages from server."""
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type", "")

                    if msg_type == "response":
                        console.print(data.get("message", ""))
                    elif msg_type == "info":
                        console.print(f"[dim]{data.get('message', '')}[/dim]")
                    elif msg_type == "data":
                        # Terminal output
                        sys.stdout.write(data.get("content", ""))
                        sys.stdout.flush()
                    else:
                        console.print(f"[dim]< {message}[/dim]")
                except json.JSONDecodeError:
                    # Raw output
                    sys.stdout.write(message)
                    sys.stdout.flush()
        except Exception:
            pass

    async def send_input():
        """Read and send user input."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                # Read line from stdin (blocking)
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                await websocket.send(line.rstrip("\n"))

                # Check for exit commands
                if line.strip().lower() in {"@resume", "@abort"}:
                    await asyncio.sleep(0.5)  # Give server time to respond
                    break
        except Exception:
            pass

    # Run receive and send concurrently
    receive_task = asyncio.create_task(receive_messages())
    send_task = asyncio.create_task(send_input())

    try:
        # Wait for either task to complete
        done, pending = await asyncio.wait(
            [receive_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except KeyboardInterrupt:
        console.print("\n[yellow]Disconnecting...[/yellow]")


if __name__ == "__main__":
    cli()
