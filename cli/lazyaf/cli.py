"""
LazyAF CLI - Ingest repos and land changes.

Usage:
    lazyaf ingest /path/to/repo --name my-project
    lazyaf land <repo_id> --branch feature/foo
    lazyaf tests reconcile <repo_id> --refs refs.json
    lazyaf tests reconcile <repo_id> --from-collect
    lazyaf debug rerun <run_id> --break build
    lazyaf debug resume <session_id>

The backend is named by --server or $LAZYAF_SERVER, defaulting to
http://localhost:8000.

THE ERROR CONTRACT, which the rest of this file implements:

  * diagnostics on stderr, results on stdout, so `lazyaf list | ...` pipes
    clean data;
  * a failure never exits 0 (see `fail`);
  * text this CLI did not author - git's stderr, an API body - is quoted
    VERBATIM, never through rich's markup parser (see the note above `_echo`);
  * a refusal names the remedy, not `--help` (see `LazyafCommand`);
  * an unreachable or misconfigured backend says which URL it used and where
    that URL came from (see `describe_server`).

It stays non-interactive: nothing here prompts, so it behaves the same under
a harness as in a terminal.
"""

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import click
import httpx
from rich.console import Console
from rich.panel import Panel

console = Console()

#: Diagnostics go to STDERR, results go to STDOUT.
#:
#: click's own usage errors already go to stderr. Everything this file printed
#: went to stdout, so a caller that piped `lazyaf list` into another program
#: got error text in its data, and a caller that read stderr for diagnostics
#: saw a bare exit code. One stream per purpose, and `2>&1` still interleaves
#: them for a human.
err_console = Console(stderr=True)

EXIT_FAILURE = 1
#: click's own code for "you invoked this wrong". Reused so that a refusal
#: which is really a usage error is indistinguishable from click's.
EXIT_USAGE = 2

DEFAULT_SERVER = "http://localhost:8000"
SERVER_ENV_VAR = "LAZYAF_SERVER"


def get_server_url() -> str:
    """Get the LazyAF server URL from env or default."""
    return os.environ.get(SERVER_ENV_VAR, DEFAULT_SERVER)


# =============================================================================
# Saying what went wrong
# =============================================================================
#
# THE RULE IN THIS FILE: text the CLI did not author is printed with
# markup=False. Always.
#
# rich reads `[word]` as a style tag. It DELETES anything shaped like one and
# raises MarkupError on anything shaped like a closing tag. Both hit real
# output, and both hit the word that mattered:
#
#     git:  " ! [rejected]   main -> main (non-fast-forward)"
#     shown " !              main -> main (non-fast-forward)"
#
#     server: "no such file [/tmp/x]"  ->  MarkupError, mid-failure-report
#
# So the CLI could delete the one word that explained a failure, or crash
# while explaining it. Untrusted text (git stderr, an API body, anything
# interpolated from argv or a server response) never goes through the markup
# parser; colour comes from `style=`, which does not parse the string.


def _echo(message: str, *, style: str | None = None, stderr: bool = False) -> None:
    """Print one authored line, never parsing markup out of it."""
    target = err_console if stderr else console
    target.print(message, markup=False, highlight=False, style=style)


def _echo_verbatim(text: object, *, indent: str = "  ") -> None:
    """Echo somebody else's words - git's, the server's - EXACTLY, on stderr.

    Indented rather than merged into our own prose so that it reads as a
    quotation: the operator can tell what LazyAF said from what git said.
    """
    body = str(text).replace("\r\n", "\n").rstrip("\n")
    if not body.strip():
        return
    for line in body.split("\n"):
        err_console.print(f"{indent}{line}", markup=False, highlight=False, style="dim")


def _echo_remedy(remedy: str) -> None:
    """Print the fix. Blank line first: the remedy is the part to act on."""
    err_console.print("")
    for line in str(remedy).rstrip("\n").split("\n"):
        err_console.print(line, markup=False, highlight=False)


def fail(
    summary: str,
    *,
    detail: object = None,
    remedy: str | None = None,
    exit_code: int = EXIT_FAILURE,
) -> NoReturn:
    """Refuse: say what is wrong, quote whoever said so, name the remedy, exit.

    Modelled on the server's own refusals (see `_require_pushed_content` in
    backend/app/routers/cards.py): a message that does not name the next
    command is a message the reader has to go research. `exit_code` is never
    0 - a failure that exits 0 is the worst outcome this repo has, because
    every wrapper above it believes the success.
    """
    if exit_code == 0:  # pragma: no cover - guarded, not expected
        raise ValueError("fail() cannot exit 0")
    _echo(f"Error: {summary}", style="bold red", stderr=True)
    if detail is not None:
        _echo_verbatim(detail)
    if remedy:
        _echo_remedy(remedy)
    sys.exit(exit_code)


def warn(summary: str, *, detail: object = None) -> None:
    """A fact the operator needs that is not, on its own, a failure."""
    _echo(f"Warning: {summary}", style="yellow", stderr=True)
    if detail is not None:
        _echo_verbatim(detail)


# =============================================================================
# Which backend, and is it there
# =============================================================================


def _server_source(explicit: str | None) -> str:
    """Where the URL we are about to use came from.

    Half of "could not connect" reports are really "connected to the wrong
    thing": a stale $LAZYAF_SERVER in one shell, the default in another. The
    URL alone does not settle that; the URL plus its provenance does.
    """
    if explicit:
        return "--server"
    if os.environ.get(SERVER_ENV_VAR):
        return f"${SERVER_ENV_VAR}"
    return f"the built-in default ({DEFAULT_SERVER})"


def describe_server(server: str | None) -> str:
    """One line naming the backend in use and how to change it."""
    return (
        f"LazyAF backend: {server or get_server_url()} "
        f"(from {_server_source(server)})\n"
        f"Change it with --server <url> or {SERVER_ENV_VAR}=<url>."
    )


def resolve_server_url(server: str | None) -> str:
    """The backend base URL, validated and normalised, or a refusal.

    A missing scheme is REFUSED, not guessed - the same call
    `debug_cmd.terminal_url` makes, for the same reason (R3: one rule for
    one question). httpx's own message for a schemeless URL is a traceback
    ending in `UnsupportedProtocol`, which names neither the setting that
    produced it nor the fix.
    """
    raw = server if server is not None else get_server_url()
    url = (raw or "").strip().rstrip("/")
    source = _server_source(server)

    if not url:
        fail(
            f"no LazyAF backend URL: {source} is empty",
            remedy=(
                "Set one:\n"
                f"    {SERVER_ENV_VAR}={DEFAULT_SERVER}\n"
                "or pass it per command:\n"
                f"    lazyaf list --server {DEFAULT_SERVER}"
            ),
            exit_code=EXIT_FAILURE,
        )

    if not url.startswith(("http://", "https://")):
        guess = url.split("://", 1)[-1]
        fail(
            f"the LazyAF backend URL has no http:// or https:// scheme: {url}",
            remedy=(
                f"Read from {source}. The scheme is required rather than "
                "guessed - guessing http:// is how a request that should have "
                "been encrypted goes out in the clear.\n\n"
                f"    {SERVER_ENV_VAR}=http://{guess}\n"
                f"    lazyaf list --server http://{guess}"
            ),
            exit_code=EXIT_FAILURE,
        )

    return url


def _server_words(response) -> str:
    """The server's OWN account of a failure.

    A status code is not a reason. The API answers 400s and 422s with prose
    that names the remedy ("Repo 'x' has no commits yet ... git push lazyaf
    main"); printing only "API returned 400" throws that away and leaves the
    operator with a number.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is still the answer
        return (getattr(response, "text", "") or "").strip()

    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload

    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        # FastAPI/pydantic validation shape: [{"loc": [...], "msg": ...}]
        lines = []
        for item in detail:
            if isinstance(item, dict) and "msg" in item:
                where = ".".join(
                    str(part) for part in item.get("loc", []) if part != "body"
                )
                lines.append(f"{where}: {item['msg']}" if where else str(item["msg"]))
            else:
                lines.append(str(item))
        return "\n".join(lines)
    return json.dumps(detail, indent=2)


def api_request(
    method: str,
    path: str,
    server: str | None,
    *,
    not_found: str | None = None,
    **kwargs,
):
    """One HTTP call against the LazyAF API, with this file's error idiom.

    Every command routes through here so that "the backend said no" reads the
    same everywhere (R3). Before this, three commands printed the status code
    and dropped the body, two dumped the raw JSON envelope, and only the
    debug verbs quoted the server - the newest code being the only correct
    code is the usual sign that the idiom was never centralised.

    Every httpx failure is handled. The tree is wider than ConnectError:
    a timeout, a proxy refusal, a truncated response and a schemeless URL
    each used to end in a traceback, which tells the operator about httpx's
    internals instead of about their setup.
    """
    base = resolve_server_url(server)
    url = f"{base}{path}"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        words = _server_words(exc.response)
        if status == 404 and not_found:
            fail(not_found, detail=words or None, remedy=describe_server(server))
        fail(
            f"the LazyAF backend returned HTTP {status} for {method} {path}",
            detail=words or "(the server sent no explanation)",
            remedy=describe_server(server),
        )
    except httpx.TimeoutException as exc:
        fail(
            f"the LazyAF backend at {base} did not answer within 30s",
            detail=exc,
            remedy=(
                f"{describe_server(server)}\n\n"
                "A backend that accepts the connection but never answers is "
                "usually still starting, or blocked on its database."
            ),
        )
    except httpx.RequestError as exc:
        fail(
            f"could not reach the LazyAF backend at {base}",
            detail=exc,
            remedy=(
                f"{describe_server(server)}\n\n"
                "Check it is up and serving that port:\n"
                f"    curl {base}/health"
            ),
        )
    except httpx.InvalidURL as exc:
        fail(
            f"the LazyAF backend URL is not a usable URL: {base}",
            detail=exc,
            remedy=describe_server(server),
        )

    try:
        return response.json()
    except ValueError:
        # A 200 that is not JSON means we are talking to something that is
        # not the LazyAF API - a proxy error page, a dev server, a login
        # wall. Saying "invalid JSON" would blame the wrong component.
        fail(
            f"{base} answered {method} {path} with HTTP {response.status_code} "
            "but the body is not JSON, so this is not the LazyAF API",
            detail=(response.text or "")[:400],
            remedy=describe_server(server),
        )


def run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git_or_fail(
    args: list[str],
    cwd: Path | None,
    *,
    summary: str,
    remedy: str | None = None,
) -> subprocess.CompletedProcess:
    """Run git; on failure quote git verbatim and stop.

    git's stderr is the diagnosis - "src refspec X does not match any",
    "! [rejected] ... (non-fast-forward)" - so it is reproduced exactly,
    including the bracketed words rich used to eat.
    """
    result = run_git(args, cwd=cwd)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        fail(
            summary,
            detail=f"$ git {' '.join(args)}\n{detail}",
            remedy=remedy,
        )
    return result


# =============================================================================
# Usage errors that carry a working example
# =============================================================================


def command_examples(cmd: click.Command) -> list[str]:
    """The `Example(s):` lines out of a command's own help text.

    The docstring is the single source (R3): the example a reader gets after
    a mistake is the same one `--help` shows, so it cannot drift into being
    wrong only on the path nobody reads.
    """
    examples: list[str] = []
    collecting = False
    for line in (cmd.help or "").splitlines():
        stripped = line.strip()
        if not collecting:
            if stripped.lower().startswith("example") and stripped.endswith(":"):
                collecting = True
            continue
        if not stripped:
            if examples:
                break
            continue
        if not line[:1].isspace():
            break
        examples.append(stripped)
    return examples


class LazyafCommand(click.Command):
    """A command whose usage errors show an invocation that works.

    click names the missing parameter and then points at `--help`. Naming the
    flag is not the same as showing the command: the reader still has to run
    a second thing and pick the right line out of it. Every command here
    already documents a working example, so a usage error reprints it.
    """

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            examples = command_examples(self)
            if examples:
                path = ctx.command_path if ctx else self.name
                exc.message = "{}\n\nA working {} looks like:\n{}".format(
                    exc.message,
                    path,
                    "\n".join(f"    {line}" for line in examples),
                )
            raise


class LazyafGroup(click.Group):
    """A group whose subcommands inherit the above, and that spells."""

    command_class = LazyafCommand
    #: click's sentinel for "subgroups are this same class".
    group_class = type

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            typo = args[0] if args else ""
            close = difflib.get_close_matches(typo, self.list_commands(ctx), n=1)
            if close:
                exc.message = f"{exc.message} Did you mean '{close[0]}'?"
            raise


@click.group(cls=LazyafGroup)
@click.version_option()
def cli():
    """LazyAF - Visual orchestrator for AI agents.

    Every command talks to a LazyAF backend, named by --server or
    $LAZYAF_SERVER and defaulting to http://localhost:8000.

    Example:
        lazyaf list --server http://localhost:8000
    """
    pass


def local_branches(path: Path) -> list[str]:
    """Every local branch in `path`. Empty means the repo has no commits."""
    result = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=path
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def require_pushable_content(path: Path, branch: str | None, all_branches: bool) -> None:
    """Refuse to ingest a repo that has nothing to push. R1, earliest point.

    `git push --all` on a repo with no commits succeeds and transfers
    NOTHING, so ingest used to print a green "Success!" over an empty repo.
    The failure then surfaced minutes later, in the UI, as the server's
    `_require_pushed_content` refusal when a card was started - the right
    failure told at the wrong time, about a command the reader had already
    been told worked.

    A named branch that does not exist is refused here too, before the API
    call: pushing it fails anyway, but only AFTER a repo record has been
    created, leaving an empty repo in LazyAF for every typo.
    """
    branches = local_branches(path)

    if not branches:
        fail(
            f"{path} is a git repository with no commits, so there is nothing "
            "to ingest",
            remedy=(
                "LazyAF stores your code on its own git server and agents "
                "branch from what you push. An empty repo gives them nothing "
                "to check out.\n\n"
                "Commit something first:\n"
                "    git add -A\n"
                '    git commit -m "initial commit"\n\n'
                "Then run this command again."
            ),
        )

    if branch and branch not in branches:
        fail(
            f"branch '{branch}' does not exist in {path}",
            remedy=(
                "Local branches:\n"
                + "\n".join(f"    {name}" for name in sorted(branches))
                + "\n\nPick one of those, or drop --branch to push the "
                "current branch."
            ),
            exit_code=EXIT_FAILURE,
        )


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

    # --name is required, so click catches its absence. An all-whitespace
    # name gets past click and is rejected by the API's min_length, which
    # answered with a raw pydantic envelope; refusing here names the flag.
    name = (name or "").strip()
    if not name:
        fail(
            "--name is empty",
            remedy=(
                "The name is how the repo is listed in LazyAF and in "
                "`lazyaf list`, so it cannot be blank:\n\n"
                f"    lazyaf ingest {repo_path} --name {path.name or 'my-project'}"
            ),
            exit_code=EXIT_FAILURE,
        )

    # Validate it's a git repo
    git_dir = path / ".git"
    if not git_dir.exists():
        fail(
            f"{path} is not a git repository",
            remedy=(
                "ingest takes the path to a git working tree, and pushes it "
                "to LazyAF's internal git server.\n\n"
                f"    cd {path} && git init\n"
                f"    lazyaf ingest {repo_path} --name {name}"
            ),
            exit_code=EXIT_FAILURE,
        )

    require_pushable_content(path, branch, all_branches)

    console.print(Panel(f"Ingesting [cyan]{name}[/cyan] from {path}"))

    # Detect default branch if not specified
    # --all-branches used to skip detection entirely and send
    # `default_branch: "main"` below, so a repo whose trunk is `master` was
    # ingested with every one of its branches present and a default naming
    # none of them. The card then failed at workspace clone time. Detect in
    # BOTH modes; --all-branches changes what gets PUSHED, not what the
    # repo's default is.
    if not branch:
        result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        if result.returncode != 0 or not result.stdout.strip():
            fail(
                f"could not detect the current branch of {path}",
                detail=(result.stderr or "").strip(),
                remedy=(
                    "A detached HEAD has no branch name to push. Name one "
                    "explicitly:\n\n"
                    f"    lazyaf ingest {repo_path} --name {name} --branch "
                    f"{(local_branches(path) or ['main'])[0]}"
                ),
            )
        branch = result.stdout.strip()
        console.print(f"Using current branch as the default: [cyan]{branch}[/cyan]")

    # Get remote URL if exists (for future landing)
    result = run_git(["remote", "get-url", "origin"], cwd=path)
    remote_url = result.stdout.strip() if result.returncode == 0 else None

    # Call ingest API
    server_url = resolve_server_url(server)
    console.print(f"Creating repo on [blue]{server_url}[/blue]...")
    data = api_request(
        "POST",
        "/api/repos/ingest",
        server,
        json={
            "name": name,
            "remote_url": remote_url,
            # Always the detected branch. Never a hardcoded "main": it is
            # what made a repo whose trunk is "master" land pointing at a
            # branch that does not exist (R1 - no silent fallbacks).
            "default_branch": branch,
        },
    )

    repo_id = data["id"]
    clone_url = data["clone_url"]
    console.print(f"Created repo [green]{repo_id}[/green]")

    # Add lazyaf remote
    console.print("Adding lazyaf remote...")
    run_git(["remote", "remove", "lazyaf"], cwd=path)  # Remove if exists
    git_or_fail(
        ["remote", "add", "lazyaf", clone_url],
        path,
        summary=f"could not add the 'lazyaf' remote to {path}",
        remedy=(
            "The repo record exists on the server; only the local remote "
            "failed. Add it by hand and push:\n\n"
            f"    git -C {path} remote add lazyaf {clone_url}\n"
            f"    git -C {path} push lazyaf {branch or '--all'}"
        ),
    )

    # Push to internal server
    if all_branches:
        console.print("Pushing all branches...")
        push_args = ["push", "lazyaf", "--all"]
    else:
        console.print(f"Pushing branch [cyan]{branch}[/cyan]...")
        push_args = ["push", "lazyaf", branch]

    git_or_fail(
        push_args,
        path,
        summary=f"could not push {path} to LazyAF (repo {repo_id} was created)",
        remedy=(
            "git's reason is quoted above. The repo record exists but has no "
            "content, so agents cannot branch from it. Fix the cause and "
            "push again:\n\n"
            f"    git -C {path} push lazyaf {' '.join(push_args[2:])}"
        ),
    )

    # A push can succeed and transfer nothing. Ask the server what it
    # actually has rather than reporting success on our own say-so.
    landed = api_request(
        "GET",
        f"/api/repos/{repo_id}/branches",
        server,
        not_found=f"repo {repo_id} vanished between creating it and pushing to it",
    )
    if not landed.get("branches"):
        fail(
            f"the push reported success but repo {repo_id} still has no "
            "branches on the server",
            remedy=(
                "Nothing was transferred, so an agent would have nothing to "
                "check out. Push again and read git's output:\n\n"
                f"    git -C {path} push lazyaf {' '.join(push_args[2:])}"
            ),
        )

    console.print()
    console.print(Panel.fit(
        f"[green]Success![/green]\n\n"
        f"Repo ID: [cyan]{repo_id}[/cyan]\n"
        f"Server:  {server_url}\n"
        f"Clone URL: {clone_url}\n"
        f"Branches on the server: "
        f"{', '.join(b['name'] for b in landed['branches'])}\n\n"
        f"Create cards in the UI to start working with AI agents.\n"
        f"Check it from here with:  lazyaf branches {repo_id}",
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
    server_url = resolve_server_url(server)

    console.print(Panel(f"Landing branch [cyan]{branch}[/cyan] from repo [cyan]{repo_id}[/cyan]"))

    # Get repo info from API
    console.print(f"Fetching repo info from [blue]{server_url}[/blue]...")
    not_found = (
        f"repo {repo_id} does not exist on the LazyAF backend. "
        "`lazyaf list` shows the ids that do."
    )
    repo_data = api_request("GET", f"/api/repos/{repo_id}", server, not_found=not_found)
    url_data = api_request(
        "GET", f"/api/repos/{repo_id}/clone-url", server, not_found=not_found
    )

    clone_url = url_data["clone_url"]
    remote_url = repo_data.get("remote_url")
    default_branch = repo_data.get("default_branch", "main")
    base_branch = base or default_branch

    if not remote_url:
        warn(
            f"repo {repo_id} has no remote_url recorded, so LazyAF cannot "
            f"confirm that '{remote}' is the right destination"
        )

    # We need to be in a git repo to fetch/push
    # Create a temp directory or use current if it's the right repo
    cwd = Path.cwd()
    git_dir = cwd / ".git"

    if not git_dir.exists():
        fail(
            f"the current directory is not a git repository: {cwd}",
            remedy=(
                "land pushes from YOUR clone to YOUR remote, so it has to run "
                "inside that clone:\n\n"
                f"    cd /path/to/your/clone\n"
                f"    lazyaf land {repo_id} --branch {branch}"
            ),
            exit_code=EXIT_FAILURE,
        )

    # Add/update lazyaf remote
    console.print("Configuring lazyaf remote...")
    run_git(["remote", "remove", "lazyaf"], cwd=cwd)
    git_or_fail(
        ["remote", "add", "lazyaf", clone_url],
        cwd,
        summary=f"could not add the 'lazyaf' remote to {cwd}",
        remedy=f"    git remote add lazyaf {clone_url}",
    )

    # Fetch from lazyaf
    console.print(f"Fetching [cyan]{branch}[/cyan] from LazyAF...")
    git_or_fail(
        ["fetch", "lazyaf", branch],
        cwd,
        summary=f"could not fetch branch '{branch}' from LazyAF",
        remedy=(
            "git's reason is quoted above. If the branch simply is not "
            "there, list what the server has:\n\n"
            f"    lazyaf branches {repo_id}"
        ),
    )

    # Push to origin.
    #
    # BOTH SIDES FULLY QUALIFIED, and not cosmetically. The old refspec
    # `lazyaf/<branch>:<branch>` fails whenever the destination branch does
    # not exist yet - which is the normal case for landing an agent branch:
    #
    #     error: The destination you provided is not a full refname [...]
    #     Neither worked, so we gave up. You must fully qualify the ref.
    #
    # git cannot tell whether an unqualified <dst> means a branch or a tag
    # when nothing of that name is there to match, and the <src> being a
    # remote-tracking ref gives it no hint. Naming refs/heads/ says it.
    console.print(f"Pushing to [cyan]{remote}/{branch}[/cyan]...")
    git_or_fail(
        ["push", remote, f"refs/remotes/lazyaf/{branch}:refs/heads/{branch}"],
        cwd,
        summary=f"could not push '{branch}' to remote '{remote}'",
        remedy=(
            "git's reason is quoted above. Nothing was landed. Check the "
            "remote exists and you can write to it:\n\n"
            f"    git remote -v\n"
            f"    git push {remote} refs/remotes/lazyaf/{branch}:refs/heads/{branch}"
        ),
    )

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
            # The branch landed; the PR did not. Reporting that as success
            # (which is what a 0 exit meant here) tells a script the whole
            # request was honoured when half of it was not - so the branch
            # result is stated plainly and the exit code says "failed".
            fail(
                f"branch '{branch}' was pushed to {remote}, but --pr could "
                "not create the pull request",
                detail=(result.stderr or result.stdout or "").strip(),
                remedy=(
                    "The push is done and does not need repeating. Create "
                    "the PR when the cause above is fixed:\n\n"
                    f"    gh pr create --base {base_branch} --head {branch} --fill"
                ),
            )
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
    """List all repos in LazyAF.

    Also the quickest way to see WHICH backend this shell is pointed at -
    the URL and where it came from are printed above the results.

    Example:
        lazyaf list
        lazyaf list --server http://localhost:8000
    """
    server_url = resolve_server_url(server)
    repos = api_request("GET", "/api/repos", server)

    console.print(f"[dim]{server_url} (from {_server_source(server)})[/dim]")

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
    """List branches in a LazyAF repo.

    Example:
        lazyaf branches abc123
    """
    data = api_request(
        "GET",
        f"/api/repos/{repo_id}/branches",
        server,
        not_found=(
            f"repo {repo_id} does not exist on the LazyAF backend. "
            "`lazyaf list` shows the ids that do."
        ),
    )

    branches = data["branches"]
    if not branches:
        # Not an error - a registered repo legitimately has no refs until
        # something is pushed - but it IS the state that makes agents fail
        # later, so it names the fix rather than just the fact.
        console.print(
            f"Repo {repo_id} has no branches yet: nothing has been pushed to "
            "it, so an agent would have nothing to check out."
        )
        console.print("Push your code with:  lazyaf ingest <path> --name <name>")
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
    """Test tie-back commands (Phase 12.2.6).

    Example:
        lazyaf tests reconcile <repo_id> --from-collect
    """
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
    # Validate the backend URL before collecting: `--from-collect` runs the
    # whole suite's collection, and finding out afterwards that $LAZYAF_SERVER
    # was mistyped wastes all of it.
    resolve_server_url(server)

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
            fail(
                f"--refs manifest not found: {manifest_path}",
                remedy=(
                    "Point --refs at a refs manifest, or build the declared "
                    "set here and now:\n\n"
                    f"    lazyaf tests reconcile {repo_id} --from-collect"
                ),
                exit_code=EXIT_FAILURE,
            )
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

    data = api_request(
        "POST",
        "/api/test-refs/reconcile",
        server,
        json={"repo_id": repo_id, "refs": refs},
        not_found=(
            f"repo {repo_id} does not exist on the LazyAF backend, so there "
            "are no test refs to reconcile. `lazyaf list` shows the ids that do."
        ),
    )

    console.print()
    console.print(
        Panel.fit(
            "[green]Reconciled![/green]\n\n"
            + "\n".join(f"{key}: [cyan]{value}[/cyan]" for key, value in data.items()),
            title="Test refs",
        )
    )



# =============================================================================
# Debug re-run (Phase 12.7)
# =============================================================================
#
# `lazyaf debug` is a click GROUP, not `lazyaf debug <id> --resume`. Stated as
# a deliberate deviation from PLAN's flag forms: a flag that changes the verb
# is not a flag, a group gives real per-verb `--help`, and `lazyaf tests
# reconcile` is the precedent already in this file.
#
# Every verb here is plain HTTP. Controlling a debug session - resuming it,
# aborting it, extending its deadline - never depends on having a TTY, which
# is also why the terminal's `@`-commands are a convenience rather than the
# only way in.


DEBUG_ATTACH_NOT_INTERACTIVE = (
    "This build's `attach` mints and prints the join credential; it does not "
    "open an interactive shell. The raw-TTY terminal client ships as "
    "cli/lazyaf/debug_cmd.py with its own `websockets` dependency and "
    "replaces this subcommand when registered."
)


def _debug_request(method: str, path: str, server: str | None, **kwargs) -> dict:
    """One HTTP call against the debug API, with this file's error idiom.

    Failures print the SERVER's reason rather than a generic code: every 4xx
    the debug API emits is a fact the operator needs ("session already ended
    (aborted by user)", "unknown breakpoint step key(s): build").

    This verb-specific wrapper was where that idiom started; `api_request` is
    now the one implementation for the whole CLI (R3). The name stays because
    `debug_cmd` imports it.
    """
    return api_request(method, path, server, **kwargs)


@cli.group()
def debug():
    """Debug re-run commands (Phase 12.7).

    Example:
        lazyaf debug rerun <run_id> --break build
    """
    pass


@debug.command("rerun")
@click.argument("run_id")
@click.option(
    "--break",
    "breakpoints",
    multiple=True,
    metavar="STEP_KEY",
    help=(
        "Pause BEFORE this step, named by its step id in the pipeline graph "
        "(NOT its position - index keys were the v1 array's address and are "
        "retired). Repeatable. An unknown key is refused, so a breakpoint "
        "either fires or says why it cannot."
    ),
)
@click.option("--commit", default=None, help="Re-run at this commit instead of the original")
@click.option("--branch", default=None, help="Re-run on this branch instead of the original")
@click.option(
    "--timeout",
    "timeout_seconds",
    default=None,
    type=int,
    help="How long a breakpoint pause may wait (seconds, clamped to 4h)",
)
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_rerun(run_id, breakpoints, commit, branch, timeout_seconds, server):
    """Re-run a pipeline run with breakpoints.

    The re-run carries ONLY the original run's branch and commit. on_pass /
    on_fail actions and card routing are deliberately dropped, so a debug
    re-run can never merge a branch and never moves a card.

    Example:
        lazyaf debug rerun <run_id> --break build
        lazyaf debug rerun <run_id> --break build --break test --timeout 900
    """
    payload = {
        "breakpoints": list(breakpoints),
        "use_original_commit": commit is None and branch is None,
    }
    if commit:
        payload["commit_sha"] = commit
    if branch:
        payload["branch"] = branch
    if timeout_seconds:
        payload["timeout_seconds"] = timeout_seconds

    data = _debug_request(
        "POST", f"/api/pipeline-runs/{run_id}/debug-rerun", server, json=payload
    )
    console.print()
    console.print(
        Panel.fit(
            "[green]Debug re-run started[/green]\n\n"
            f"run:     [cyan]{data['run_id']}[/cyan]\n"
            f"session: [cyan]{data['debug_session_id']}[/cyan]\n\n"
            f"{data['join_command']}",
            title="Debug",
        )
    )


@debug.command("list")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_list(server):
    """List debug sessions that have not ended.

    Example:
        lazyaf debug list
    """
    sessions = _debug_request("GET", "/api/debug", server)
    if not sessions:
        console.print("No active debug sessions.")
        return
    console.print(f"{len(sessions)} active debug session(s):\n")
    for session in sessions:
        step = session.get("current_step") or {}
        where = f" at [yellow]{step.get('name') or step.get('key')}[/yellow]" if step else ""
        console.print(f"  [cyan]{session['id']}[/cyan]  {session['status']}{where}")


def _print_session(session: dict) -> None:
    step = session.get("current_step") or {}
    lines = [
        f"status:      [cyan]{session['status']}[/cyan]",
        f"run:         {session['pipeline_run_id']}",
        f"breakpoints: {', '.join(session['breakpoints']) or '(none)'}",
        f"  hit:       {', '.join(session['breakpoints_hit']) or '(none)'}",
        f"  pending:   {', '.join(session['breakpoints_pending']) or '(none)'}",
    ]
    if step:
        lines.append(f"paused at:   {step.get('name')} (key {step.get('key')})")
    if session.get("expires_at"):
        lines.append(f"expires:     {session['expires_at']}")
    if session.get("attach_available"):
        lines.append("attach:      [green]available[/green]")
    else:
        # R1: never a silent "no" - the API always states the reason, and the
        # CLI is the surface where a remote-step pause has to say so out loud.
        lines.append(
            "attach:      [yellow]unavailable[/yellow] - "
            f"{session.get('attach_unavailable_reason') or 'no reason given'}"
        )
    if session.get("end_reason"):
        lines.append(f"ended:       {session['end_reason']}")
    console.print(Panel.fit("\n".join(lines), title="Debug session"))


@debug.command("status")
@click.argument("session_id")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_status(session_id, server):
    """Show one debug session.

    Example:
        lazyaf debug status <session_id>
    """
    _print_session(_debug_request("GET", f"/api/debug/{session_id}", server))


@debug.command("attach")
@click.argument("session_id")
@click.option(
    "--sidecar/--shell",
    "sidecar",
    default=True,
    help="Sidecar is the only mode at a breakpoint (see below).",
)
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_attach(session_id, sidecar, server):
    """Mint a terminal credential for a paused session.

    `--shell` is REFUSED, not downgraded: a breakpoint is a pre-step gate, so
    the step container does not exist yet. Use the sidecar to inspect the
    workspace the step is about to run against.

    Example:
        lazyaf debug attach <session_id>
    """
    if not sidecar:
        fail(
            "no step container exists at a pre-step breakpoint - the step "
            "has not started",
            remedy=(
                "Use the sidecar to inspect the workspace the step is about "
                "to run against:\n\n"
                f"    lazyaf debug attach {session_id} --sidecar"
            ),
            exit_code=EXIT_USAGE,
        )

    session = _debug_request("GET", f"/api/debug/{session_id}", server)
    if not session.get("attach_available"):
        fail(
            "cannot attach to this session: "
            f"{session.get('attach_unavailable_reason') or 'the server gave no reason'}",
            remedy=(
                "A session is attachable only while it is paused at a "
                "breakpoint on a local step.\n\n"
                f"    lazyaf debug status {session_id}"
            ),
            exit_code=EXIT_USAGE,
        )

    data = _debug_request("POST", f"/api/debug/{session_id}/join-token", server)
    server_url = (server or get_server_url()).rstrip("/")
    ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
    console.print()
    console.print(
        Panel.fit(
            f"token:   [cyan]{data['token']}[/cyan]\n"
            f"expires: {data['expires_at']}\n"
            f"socket:  {ws_url}/api/debug/{session_id}/terminal?mode=sidecar\n\n"
            "[yellow]/workspace is mounted READ-WRITE:[/yellow] edits there are "
            "seen by the resumed step.\n\n"
            f"{DEBUG_ATTACH_NOT_INTERACTIVE}",
            title="Debug terminal",
        )
    )


@debug.command("resume")
@click.argument("session_id")
@click.option(
    "--all",
    "clear_remaining",
    is_flag=True,
    help="Drop the remaining breakpoints and run to completion",
)
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_resume(session_id, clear_remaining, server):
    """Release a paused step and continue to the next breakpoint.

    Example:
        lazyaf debug resume <session_id>
        lazyaf debug resume <session_id> --all
    """
    data = _debug_request(
        "POST",
        f"/api/debug/{session_id}/resume",
        server,
        json={"clear_remaining": clear_remaining},
    )
    nxt = data.get("next_breakpoint")
    console.print(
        f"[green]Resumed.[/green] Session is [cyan]{data['status']}[/cyan]; "
        + (f"next breakpoint: [yellow]{nxt}[/yellow]" if nxt else "no breakpoints left.")
    )


@debug.command("abort")
@click.argument("session_id")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_abort(session_id, server):
    """End the session AND cancel its pipeline run.

    Example:
        lazyaf debug abort <session_id>
    """
    data = _debug_request("POST", f"/api/debug/{session_id}/abort", server)
    console.print(
        f"[green]Aborted.[/green] Session is [cyan]{data['status']}[/cyan] "
        f"({data['end_reason']}); the run was cancelled."
    )


@debug.command("extend")
@click.argument("session_id")
@click.option("--minutes", default=30, type=int, help="Minutes to add (1-180)")
@click.option("--server", "-s", default=None, help="LazyAF server URL")
def debug_extend(session_id, minutes, server):
    """Push out a paused session's deadline.

    Example:
        lazyaf debug extend <session_id> --minutes 60
    """
    data = _debug_request(
        "POST",
        f"/api/debug/{session_id}/extend",
        server,
        json={"additional_minutes": minutes},
    )
    console.print(f"[green]Extended.[/green] Expires at [cyan]{data['expires_at']}[/cyan]")
    if data.get("clamped"):
        console.print(
            "[yellow]Clamped[/yellow] to the session's maximum lifetime "
            "(max_timeout_seconds)."
        )


if __name__ == "__main__":
    cli()
