# lazyaf-cli

Command-line client for [LazyAF](https://github.com/Brennan-VanderLaan/lazyaf) — a
self-hosted platform for running AI coding agents as CI/CD.

The CLI is the thin half of the system. It talks to a LazyAF server over HTTP:
it pushes a local repo into the server's internal git server, pulls agent-made
branches back down onto your real remote, and reconciles your test suite with
the platform's test registry.

```
your machine                        your LazyAF server
------------                        ------------------
$ lazyaf ingest ./my-app  ───────▶  internal git repo + agents
$ lazyaf branches <id>    ◀───────  branches the agents pushed
$ lazyaf land <id> -b fix ───────▶  your real origin (optionally a PR)
```

## Install

```bash
pip install lazyaf-cli
```

Or, without polluting an environment:

```bash
uv tool install lazyaf-cli
# or: pipx install lazyaf-cli
```

Then:

```bash
lazyaf --help
```

Requires Python 3.10+. `git` must be on `PATH`; `lazyaf land --pr` additionally
shells out to GitHub's [`gh`](https://cli.github.com/) CLI.

You also need a running LazyAF server — see
[the quickstart](https://github.com/Brennan-VanderLaan/lazyaf/blob/main/QUICKSTART.md)
to bring the stack up with Docker Compose in a couple of minutes.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LAZYAF_SERVER` | `http://localhost:8000` | Server base URL. Every command also takes `--server/-s`. |
| `LAZYAF_REPO_ROOT` | auto-detected | Overrides repo-root detection when reconciling tests. |

**The CLI never asks for, stores, or transmits AI provider API keys.** Anthropic
or Google credentials belong to the *server* — they are read from its
environment and injected into runner containers there. Nothing in this package
reads `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or any sibling. If you find
yourself pasting a provider key into a `lazyaf` command, stop: that is not a
supported flow.

## Commands

### `lazyaf ingest <path> --name <name>`

Create a repo record on the server and push the local repository into LazyAF's
internal git server. Agents work against that internal copy, so your real
remote is never touched by an agent.

```bash
lazyaf ingest ./my-app --name my-app              # current branch
lazyaf ingest ./my-app --name my-app -b main      # a specific branch
lazyaf ingest ./my-app --name my-app --all-branches
```

### `lazyaf list`

List the repos the server knows about, with their ids.

### `lazyaf branches <repo_id>`

Show the branches on the internal repo — including the ones agents created.

### `lazyaf land <repo_id> --branch <branch>`

Pull an agent branch out of LazyAF and push it to your real remote. Add `--pr`
to open a pull request via `gh`.

```bash
lazyaf land abc123 --branch agent/fix-login
lazyaf land abc123 --branch agent/fix-login --pr --base main
```

### `lazyaf tests reconcile <repo_id>`

Reconcile the repo's test registry against the **full declared test set**, so
the platform can tie individual test outcomes back to work items.

```bash
lazyaf tests reconcile abc123 --from-collect            # run pytest --collect-only
lazyaf tests reconcile abc123 --from-collect -C backend ../tdd
lazyaf tests reconcile abc123 --refs refs.json          # explicit manifest
```

Tests present in the input are upserted as active; previously-active tests that
are **absent** flip to orphaned. That is why there is no default input source —
feeding it one tier's partial results would orphan everything that tier did not
run. The command refuses ambiguous input rather than guessing.

## Development

This package lives in the `cli/` directory of the LazyAF monorepo. To build a
wheel from a checkout:

```bash
uv build --wheel --out-dir dist cli
```

The wheel contains the `lazyaf` package and nothing else — no tests, no backend
source, no environment files. `tdd/unit/packaging/` asserts that: the metadata
guards run in the normal suite, and `-m slow` builds a real wheel, inspects the
archive, and installs it into a throwaway virtualenv.

## License

MIT — see [LICENSE](https://github.com/Brennan-VanderLaan/lazyaf/blob/main/LICENSE).
