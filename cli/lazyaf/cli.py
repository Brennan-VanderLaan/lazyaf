"""
LazyAF CLI - Ingest repos and land changes.

Usage:
    lazyaf ingest /path/to/repo --name my-project
    lazyaf land <repo_id> --branch feature/foo
    lazyaf tests reconcile <repo_id> --refs refs.json
    lazyaf tests reconcile <repo_id> --from-collect
"""

import json
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


@cli.group()
def tests():
    """Test tie-back commands (Phase 12.2.6)."""
    pass


RESULTS_MANIFEST_HINT = (
    "A results manifest lists only the tests that RAN in that invocation. "
    "Reconciling from one ORPHANS every declared test the run did not touch "
    "(a different tier, a -k filter, a failed collection)."
)


def _classify_manifest(data) -> str:
    """Classify a loaded manifest as 'results' | 'refs' | 'list' | 'unknown'.

    'results' is the pytest plugin's run output ({"version":1,"results":[...]},
    pinned contract #1). 'refs' is a declared set ({"refs": [...]}). 'list' is
    a bare JSON array of ref objects.
    """
    if isinstance(data, dict):
        if "results" in data:
            return "results"
        if "refs" in data:
            return "refs"
        return "unknown"
    if isinstance(data, list):
        return "list"
    return "unknown"


def _normalize_refs(entries) -> list[dict]:
    """Normalize manifest entries to [{lazyaf_test_id, file_path}], deduped."""
    refs = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        test_id = entry.get("lazyaf_test_id")
        if not test_id or test_id in seen:
            continue
        seen.add(test_id)
        refs.append({"lazyaf_test_id": test_id, "file_path": entry.get("file_path")})
    return refs


def _load_refs_manifest(manifest_path: Path, allow_results: bool = False) -> list[dict]:
    """Load a refs manifest and normalize to [{lazyaf_test_id, file_path}].

    Accepts three shapes:
    - a refs manifest: {"refs": [{"lazyaf_test_id": ..., "file_path": ...}]}
    - a bare JSON list of ref objects
    - the pytest plugin's RESULTS manifest {"version": 1, "results": [...]},
      but ONLY with allow_results=True (--allow-results-manifest). See
      RESULTS_MANIFEST_HINT: reconciling a partial run silently orphans every
      test it did not execute.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        console.print(f"[red]Error:[/red] {manifest_path} is not valid JSON: {e}")
        sys.exit(1)

    kind = _classify_manifest(data)
    if kind == "results":
        if not allow_results:
            console.print(
                f"[red]Refusing to reconcile:[/red] {manifest_path} is a test "
                "RESULTS manifest, not a refs manifest."
            )
            console.print(RESULTS_MANIFEST_HINT)
            console.print(
                "\nUse one of:\n"
                "  --from-collect            collect the full declared set with pytest\n"
                "  --refs <path>             an explicit refs manifest ({'refs': [...]})\n"
                "  --allow-results-manifest  only if it came from a FULL-suite run"
            )
            sys.exit(1)
        console.print(
            f"[yellow]Warning:[/yellow] reconciling from a results manifest "
            f"({manifest_path}). {RESULTS_MANIFEST_HINT}"
        )
        entries = data["results"]
    elif kind == "refs":
        entries = data["refs"]
    elif kind == "list":
        entries = data
    else:
        console.print(
            f"[red]Error:[/red] {manifest_path} has no 'refs' key and is not a "
            "list of ref objects"
        )
        sys.exit(1)

    return _normalize_refs(entries)


# Throwaway pytest plugin written to a temp dir for --from-collect.
#
# The shipped runner_common.pytest_lazyaf plugin only records OUTCOMES (it
# hooks pytest_runtest_makereport), so it produces nothing under
# --collect-only. Collecting the DECLARED set needs a collection-time hook,
# which is what this adds. Stdlib + pytest only, so it imports in any
# environment that can already run the target suite.
_COLLECT_PLUGIN_SOURCE = '''
# Throwaway collector: dump every lazyaf_test_id-marked test to JSON.
import json
import os

OUT = os.environ["LAZYAF_COLLECT_OUT"]
ROOT = os.environ.get("LAZYAF_COLLECT_ROOT") or ""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "lazyaf_test_id(id): LazyAF test tie-back identifier"
    )


def _relativize(path):
    # REPO-ROOT-relative with "/" separators (cross-agent contract #3).
    # A relpath that ESCAPES the root (8.3 short names, symlinks, a suite
    # outside the repo) is worse than no root at all - fall back to the
    # invocation dir, then to the raw path, rather than emitting a
    # "../../.." climb that matches nothing the server ever seeds.
    real = os.path.realpath(path)
    for base in (ROOT, os.getcwd()):
        if not base:
            continue
        try:
            rel = os.path.relpath(real, os.path.realpath(base))
        except ValueError:
            continue
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return path.replace(os.sep, "/")


def pytest_collection_finish(session):
    refs = {}
    for item in session.items:
        marker = item.get_closest_marker("lazyaf_test_id")
        if marker is None or not marker.args:
            continue
        test_id = marker.args[0]
        if not isinstance(test_id, str) or not test_id:
            continue
        path = str(getattr(item, "fspath", "") or "")
        path = _relativize(path) if path else ""
        refs.setdefault(
            test_id, {"lazyaf_test_id": test_id, "file_path": path or None}
        )
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"refs": list(refs.values())}, fh)
'''


def _find_repo_root(start: Path) -> Path:
    """Walk up for a .git marker; fall back to `start`.

    The file_path convention (cross-agent contract #3) is REPO-ROOT-relative,
    so collected paths must be made relative to the same root the pytest
    plugin resolves.
    """
    override = os.environ.get("LAZYAF_REPO_ROOT")
    if override:
        return Path(override)
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _collect_refs(collect_path: Path, pytest_args: tuple) -> list[dict]:
    """Run `pytest --collect-only` over the suite and return the declared set.

    This is the unambiguous input for reconcile: it sees every test the suite
    DECLARES, not just the ones one tier happened to execute.
    """
    import tempfile

    repo_root = _find_repo_root(collect_path)
    with tempfile.TemporaryDirectory(prefix="lazyaf-collect-") as tmp:
        tmp_path = Path(tmp)
        plugin_name = "lazyaf_collect_plugin"
        (tmp_path / f"{plugin_name}.py").write_text(
            _COLLECT_PLUGIN_SOURCE, encoding="utf-8"
        )
        out_file = tmp_path / "refs.json"

        env = dict(os.environ)
        env["LAZYAF_COLLECT_OUT"] = str(out_file)
        env["LAZYAF_COLLECT_ROOT"] = str(repo_root)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(tmp_path), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            plugin_name,
            *pytest_args,
        ]
        console.print(f"Collecting: [dim]{' '.join(cmd)}[/dim] (cwd={collect_path})")
        result = subprocess.run(
            cmd, cwd=str(collect_path), env=env, capture_output=True, text=True
        )

        if not out_file.exists():
            console.print(
                "[red]Refusing to reconcile:[/red] collection produced no ref "
                f"set - pytest exited {result.returncode} without running the "
                "collector."
            )
            _print_tail(result)
            sys.exit(1)

        if result.returncode != 0:
            # A partial collection is exactly the ambiguity this mode exists
            # to avoid: reconciling it would orphan every test in the modules
            # that failed to import.
            console.print(
                f"[red]Refusing to reconcile:[/red] pytest --collect-only "
                f"exited {result.returncode} (collection errors). The declared "
                "set is incomplete."
            )
            _print_tail(result)
            sys.exit(1)

        data = json.loads(out_file.read_text(encoding="utf-8"))

    return _normalize_refs(data.get("refs", []))


def _print_tail(result, lines: int = 20) -> None:
    """Echo the last lines of a subprocess' output (diagnostics on refusal)."""
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.strip().splitlines()[-lines:]:
        console.print(f"  [dim]{line}[/dim]")


@tests.command(
    context_settings={"ignore_unknown_options": True},
)
@click.argument("repo_id")
@click.option(
    "--refs",
    "--manifest",
    "-m",
    "refs_manifest",
    default=None,
    type=click.Path(),
    help=(
        "Path to a REFS manifest: {'refs': [{lazyaf_test_id, file_path}]} or a "
        "bare JSON list. Must describe the repo's FULL declared test set."
    ),
)
@click.option(
    "--from-collect",
    is_flag=True,
    help=(
        "Build the full declared set by running `pytest --collect-only` over "
        "the suite (see --collect-path; extra args are passed to pytest)."
    ),
)
@click.option(
    "--collect-path",
    "-C",
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Directory to run collection in with --from-collect (default: cwd)",
)
@click.option(
    "--allow-results-manifest",
    is_flag=True,
    help=(
        "Permit a pytest RESULTS manifest as --refs input. Only correct when "
        "it came from a FULL-suite run: a partial run orphans everything it "
        "did not execute."
    ),
)
@click.option("--server", "-s", default=None, help="LazyAF server URL")
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
def reconcile(
    repo_id: str,
    refs_manifest: str | None,
    from_collect: bool,
    collect_path: str,
    allow_results_manifest: bool,
    server: str | None,
    pytest_args: tuple,
):
    """
    Reconcile a repo's TestRefs against its FULL declared test set.

    Listed refs are upserted to active (with file_path); previously-active
    refs for the repo that are ABSENT from the input flip to ORPHAN. That
    orphaning is why the input must be the whole declared set - so there is
    no default source, and exactly one of --refs / --from-collect is
    required.

    Examples:
        lazyaf tests reconcile abc123 --from-collect
        lazyaf tests reconcile abc123 --from-collect -C backend ../tdd
        lazyaf tests reconcile abc123 --refs refs.json
    """
    server_url = server or get_server_url()

    if refs_manifest and from_collect:
        console.print(
            "[red]Refusing to reconcile:[/red] --refs and --from-collect are "
            "mutually exclusive - pick one source for the declared test set."
        )
        sys.exit(1)

    if not refs_manifest and not from_collect:
        console.print(
            "[red]Refusing to reconcile:[/red] no test-ref source given, and "
            "there is no safe default."
        )
        console.print(
            "Reconcile ORPHANS every active ref absent from its input, so "
            "defaulting to a results manifest (./test_results.json, or "
            "$LAZYAF_TEST_RESULTS_PATH from one tier's run) would silently "
            "orphan every test that tier did not run."
        )
        console.print(
            "\nPass exactly one of:\n"
            "  --from-collect   run `pytest --collect-only` for the full declared set\n"
            "  --refs <path>    an explicit refs manifest covering the whole suite"
        )
        sys.exit(1)

    if from_collect:
        refs = _collect_refs(Path(collect_path), pytest_args)
        source = f"pytest --collect-only in {collect_path}"
    else:
        manifest_path = Path(refs_manifest)
        if not manifest_path.exists():
            console.print(f"[red]Error:[/red] Manifest not found: {manifest_path}")
            sys.exit(1)
        refs = _load_refs_manifest(manifest_path, allow_results=allow_results_manifest)
        source = str(manifest_path)

    if not refs:
        console.print(
            "[red]Refusing to reconcile:[/red] the declared set came back EMPTY "
            f"({source}). Sending it would orphan every active ref for repo "
            f"{repo_id}. If that is genuinely intended, say so explicitly with "
            "a refs manifest containing an empty 'refs' list."
        )
        sys.exit(1)

    console.print(
        Panel(
            f"Reconciling [cyan]{len(refs)}[/cyan] test ref(s) for repo "
            f"[cyan]{repo_id}[/cyan] from {source}"
        )
    )

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{server_url}/api/test-refs/reconcile",
                json={"repo_id": repo_id, "refs": refs},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.ConnectError:
        console.print(f"[red]Error:[/red] Could not connect to {server_url}")
        console.print("Is the LazyAF server running?")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Repo {repo_id} not found")
        else:
            console.print(f"[red]Error:[/red] API returned {e.response.status_code}")
            console.print(e.response.text)
        sys.exit(1)

    console.print()
    console.print(
        Panel.fit(
            "[green]Reconciled![/green]\n\n"
            + "\n".join(f"{key}: [cyan]{value}[/cyan]" for key, value in data.items()),
            title="Test refs",
        )
    )


if __name__ == "__main__":
    cli()
