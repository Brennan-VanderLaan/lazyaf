# LazyAF Quick Start

Run the whole stack from prebuilt images. No compiler, no Node, no Python
environment for the services themselves — just Docker.

**Before you start, know this:** LazyAF gives a container access to your
Docker socket so it can run pipeline steps. That is root-equivalent on your
machine. Run it on a machine you trust, bound to localhost. Do not put this
on the open internet.

---

## What you need

- **Docker Engine 24+ with Compose v2** (`docker compose version` must work).
  Docker Desktop on Windows/macOS includes both.
- **~15 GB free disk** for images and workspaces.
- **An API key** for [Anthropic](https://console.anthropic.com/) and/or
  [Google Gemini](https://aistudio.google.com/apikey) — only if you want AI
  agents to do work. Everything else runs without one.
- **Python 3.10+** — only for the optional `lazyaf` CLI and the preflight
  check. The stack itself does not need it.

---

## 1. Get the repo

```bash
git clone https://github.com/Brennan-VanderLaan/lazyaf.git
cd lazyaf
```

You only strictly need `docker-compose.release.yml` and `.env.example`, but
the clone also gets you the CLI source and the preflight script.

## 2. Make your `.env` and generate your secrets

```bash
python scripts/bootstrap_secrets.py
```

That one command creates `.env` from `.env.example` if you do not have one, and
fills in the two shared secrets the backend refuses to start without. It prints
none of them, and it is safe to re-run — it never overwrites a value you set.

Then open `.env` and paste in whichever API keys you have. Every other variable
is optional; the defaults are correct for this compose file.

`.env` is gitignored. Never commit it, and never put a real key in
`.env.example`.

### Why there are secrets to generate

Two variables authenticate LazyAF's own internals:

| Variable | What it does |
|---|---|
| `LAZYAF_STEP_AUTH_SECRET` | Signs the short-lived JWT a step container uses to call `/api/steps/*` — how a running step reports its logs, status and results. |
| `LAZYAF_RUNNER_AUTH_SECRET` | The shared enrollment secret a runner agent presents at `/ws/runner`. |

Earlier versions fell back to constants written into the source. A constant in a
public repository is not a secret: anyone could read it and mint credentials any
LazyAF backend would trust. There is no default now. Start the stack without
these and compose stops with a message pointing at the command above; if a value
is still one of the old constants, it is treated as unset.

You do not need to think about them again unless you deploy somewhere real.

### Deploying somewhere real

Each secret also has a `_FILE` form holding a **path** whose contents are the
value — which is how docker secrets and Kubernetes mounted Secrets deliver one:

```bash
LAZYAF_STEP_AUTH_SECRET_FILE=/run/secrets/lazyaf_step_auth_secret
LAZYAF_RUNNER_AUTH_SECRET_FILE=/run/secrets/lazyaf_runner_auth_secret
```

The `_FILE` form **takes precedence** over the inline variable. A `_FILE` that is
set but unreadable or empty is a hard startup error, never a silent fallback — a
broken mount should stop the backend, not sign tokens with a stale key.

A runner agent takes its half from `LAZYAF_RUNNER_TOKEN_FILE`,
`LAZYAF_RUNNER_TOKEN`, `LAZYAF_RUNNER_AUTH_SECRET_FILE` or
`LAZYAF_RUNNER_AUTH_SECRET`, in that order, and it must **equal** the backend's
value. Example manifests: [`deploy/k8s/`](deploy/k8s/README.md).

> There is one escape hatch, `LAZYAF_DEV_EPHEMERAL_SECRETS=1`, which generates a
> value per process and warns loudly. It is for a throwaway single-process run
> only. Tokens minted under it stop verifying the moment the backend restarts,
> and a runner agent in another container can never authenticate against it.

> **Careful what you paste in public.** `docker compose config` and
> `docker inspect` print the *interpolated* environment — your API keys **and
> these secrets** in plain text. Redact before sharing either one in an issue.
> `docker compose logs` is safe; `scripts/preflight.py` and
> `scripts/bootstrap_secrets.py` never print a value at all.

## 3. Preflight (optional, recommended)

```bash
python scripts/preflight.py
```

It checks Docker, free ports, disk space, whether your `.env` has usable keys
and the shared secrets set (it inspects shape only and never prints a value),
and whether the images it needs are available. Every failure it reports comes
with the command that fixes it. It changes nothing.

## 4. Pull and start

Pick a version first — put it in `.env` as `LAZYAF_VERSION` (see
[Choosing a version](#choosing-a-version)). Then:

```bash
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

Open **http://localhost:5173**.

The API is on http://localhost:8000, with interactive docs at
http://localhost:8000/docs and a health check at http://localhost:8000/health.

> **If `pull` fails with "manifest unknown":** no release has been published
> at that tag yet. Either pick a tag that exists on the
> [packages page](https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend),
> or [build from source](#building-from-source-instead) — it is one command
> and works today.

> **Do not run this alongside the development stack** (`docker-compose.yml`).
> Both own the fixed-name `lazyaf-network` bridge, and only one compose
> project can. Bring one down before starting the other.

## 5. Get the step images

Pipeline steps and AI agent cards run inside dedicated step images. The
backend looks them up by their **local** tag — `lazyaf-claude:dev` and
friends — and it deliberately does **not** pull them for you: a missing image
fails the step with a clear message rather than downloading something behind
your back.

The easiest way is to let preflight write the commands for you — it reads the
image list from `scripts/build_images.py`, so it is right even when the set
changes between releases:

```bash
python scripts/preflight.py
```

It prints a `docker pull` + `docker tag` pair, filled in with your version,
for every step image you are missing. Copy and run them.

By hand, if you prefer:

```bash
PREFIX=ghcr.io/brennan-vanderlaan/lazyaf
VERSION=<the same tag you set as LAZYAF_VERSION>

# Published as $PREFIX/<name> - the registry path already says "lazyaf", so
# the image name does not repeat it. Locally they are lazyaf-<name>:dev,
# which is the name the backend looks for, hence the retag.
for name in base debug-sidecar agent-base claude gemini test-runner; do
  docker pull $PREFIX/$name:$VERSION
  docker tag  $PREFIX/$name:$VERSION lazyaf-$name:dev
done
```

PowerShell:

```powershell
$PREFIX = "ghcr.io/brennan-vanderlaan/lazyaf"
$VERSION = "<the same tag you set as LAZYAF_VERSION>"

# Remote name drops the "lazyaf-" prefix; the local tag keeps it.
foreach ($name in "base","debug-sidecar","agent-base",
                  "claude","gemini","test-runner") {
  docker pull "$PREFIX/${name}:$VERSION"
  docker tag  "$PREFIX/${name}:$VERSION" "lazyaf-${name}:dev"
}
```

That list is a snapshot; the authoritative one is the `IMAGES` table in
`scripts/build_images.py`.

Building from source instead? `python scripts/build_images.py` builds them
all and skips any that are already current.

## 6. Install the CLI

The `lazyaf` CLI ingests your local repos into the platform and lands
finished branches back onto your real remote.

```bash
pip install ./cli
```

Or, if a release has published a wheel, download
`lazyaf_cli-<version>-py3-none-any.whl` from the
[releases page](https://github.com/Brennan-VanderLaan/lazyaf/releases) and:

```bash
pip install lazyaf_cli-<version>-py3-none-any.whl
```

It is not on PyPI. Check it works:

```bash
lazyaf --version
lazyaf list          # talks to http://localhost:8000; override with LAZYAF_SERVER
```

## 7. Ingest a repo

```bash
lazyaf ingest /path/to/your/repo --name my-project
```

This creates the repo inside LazyAF's internal git server, adds a git remote
called `lazyaf` to your local checkout, and pushes your current branch to it.
Your real `origin` is never touched — agents only ever work on branches
inside LazyAF.

Add `--all-branches` to push everything, or `--branch <name>` to pick one.

## 8. Run your first card

1. In the UI (**Board**), select your repo.
2. Add a card. Give it a title and a description — the description is the
   agent's task.
   > Title: `Document the project`
   > Description: `Create a file called rocks-that-think.txt explaining what this project does`
3. Create it, then **Start**.
4. Watch the log stream while the agent works on its own branch.
5. When it finishes, review the diff and **Approve** (merges to the target
   branch) or **Reject**. **Retry** re-runs it.

No API key set, or the step images missing? The card will fail with a message
saying which. That is the expected behaviour, not a crash.

## 9. Get the work back

`ingest` left a `lazyaf` remote in your checkout, so plain git works:

```bash
git fetch lazyaf
git merge lazyaf/card-123-feature-name
```

Or push a LazyAF branch straight to your real remote:

```bash
lazyaf branches <repo-id>                       # see what is there
lazyaf land <repo-id> --branch card-123-name    # pushes to origin
lazyaf land <repo-id> --branch card-123-name --pr   # ...and opens a PR via gh
```

---

## Choosing a version

`LAZYAF_VERSION` in `.env` sets the image tag for every service at once.

| Tag | What it is |
|-----|-----------|
| `v0.1.0`, `v0.2.0`, … | A published release. **Use this.** Reproducible — the tag never moves. |
| `main` | The tip of the default branch. Moves under you; expect breakage. |
| `latest` | Whatever the most recent release build pushed. Convenient, not reproducible. |

Published tags are listed on the
[packages page](https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend)
and the [releases page](https://github.com/Brennan-VanderLaan/lazyaf/releases).

Keep the step images (step 5) on the same tag as the services. Mixing
versions is not tested.

## Everyday commands

```bash
# logs
docker compose -f docker-compose.release.yml logs -f backend

# stop, keeping all your data
docker compose -f docker-compose.release.yml down

# upgrade: change LAZYAF_VERSION in .env, then
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d

# DELETE EVERYTHING: repos, cards, pipeline history, workspaces
docker compose -f docker-compose.release.yml down -v
```

Your state lives in two named volumes, `lazyaf-release_lazyaf-data` (SQLite
database + the internal bare git repos) and
`lazyaf-release_lazyaf-workspaces` (step working directories). `down -v`
destroys both.

## Optional: a runner agent

By default every pipeline step runs on the backend's own executor. A runner
agent lets steps run on a machine the backend does not own — it dials the
backend over `/ws/runner`, advertises capability labels, and picks up steps
pinned to those labels with a `requires:` block in the pipeline definition.

```bash
docker compose -f docker-compose.release.yml --profile runner up -d
```

On the same host this mostly demonstrates the mechanism. The real use is
running that image on another machine pointed at your backend's URL — in
which case use `wss://`, because the step dispatch frame carries the step's
credentials.

## Building from source instead

Everything above assumes published images. If none exist yet, or you are
working on LazyAF itself, build locally — the dev stack is the same topology
with live source mounts:

```bash
python scripts/bootstrap_secrets.py   # creates .env + the shared secrets
docker compose up -d --build
python scripts/build_images.py     # the lazyaf-*:dev step images
python scripts/preflight.py --dev  # checks the source-build path
```

## Troubleshooting

**`docker compose` says "port is already allocated"** — something else owns
8000 or 5173. Set `LAZYAF_BACKEND_PORT` / `LAZYAF_FRONTEND_PORT` in `.env`,
or stop the other process. `preflight.py` names the container when a
container is the culprit.

**"network lazyaf-network was found but has incorrect label"** — the dev
stack and the release stack are both trying to own that network. Bring the
other one down first.

**The UI loads but everything is empty** — nothing has been ingested yet.
Run `lazyaf ingest` (step 7). If `lazyaf list` cannot connect, the backend is
not up: `docker compose -f docker-compose.release.yml logs backend`.

**A card or step fails with "Image not found: lazyaf-…:dev"** — you skipped
step 5, or the tags did not get applied. Re-run `python scripts/preflight.py`
and follow what it prints.

**An agent card fails immediately** — usually a missing or wrong API key.
Check `preflight.py`, fix `.env`, then recreate the backend so it picks the
new value up: `docker compose -f docker-compose.release.yml up -d backend`.

**Windows: `git` line endings churn in ingested repos** — set
`git config core.autocrlf input` in the repo you are ingesting.

---

More detail: [README.md](README.md) for what LazyAF is and how the pieces fit
together, `PLAN.md` for the project's own roadmap and engineering decisions.
