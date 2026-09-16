# LazyAF Quick Start

From `git clone` to watching an agent change your code. You need Docker and
about ten minutes of attention (plus build time, which is longer).

---

## Read this before you start

**LazyAF has no authentication, and its backend holds your Docker socket.**
Anyone who can reach its ports can define a pipeline step, a step is a
container command on your daemon, and that is root-equivalent on this
machine. The internal git server is open too — anyone who can reach it can
clone or push to every repo you ingest.

**Every compose file binds to localhost.** `LAZYAF_BACKEND_PORT` and
`LAZYAF_FRONTEND_PORT` default to `127.0.0.1:8000` and `127.0.0.1:5173`, so
out of the box only this machine can reach it. That is the single control
that keeps everything above from mattering — if you drop the `127.0.0.1:` to
reach it from elsewhere, you are publishing an unauthenticated API to that
network. See [Exposing it beyond
localhost](#exposing-it-beyond-localhost) below, [SECURITY.md](SECURITY.md),
and the fuller explanation in
[README.md](README.md#before-you-expose-it-what-this-actually-opens).

Run this on a machine and a network you trust. Do not put it on the open
internet. There is no configuration that makes that safe today.

---

## What you need

- **Docker Engine 24+ with Compose v2** (`docker compose version` must work).
  Docker Desktop on Windows/macOS includes both.
- **~15 GB free disk** for images and workspaces.
- **The `lazyaf` CLI** (step 2). One static binary, no Python: it writes
  your `.env`, checks the machine, ingests repos and lands branches.
- **Python 3.10+ only if you build the step images from source**
  (`scripts/build_images.py`, which also needs the Docker SDK:
  `pip install docker`) or run `lazyaf tests reconcile --from-collect`, which
  runs inside your own pytest. The release stack itself needs neither.
- **An API key** for [Anthropic](https://console.anthropic.com/) and/or
  [Google Gemini](https://aistudio.google.com/apikey) — *or* your own
  ollama/vLLM server (see [step 10](#10-optional-run-against-your-own-gpu)),
  *or* neither: repos, cards, pipelines, shell/container steps and the git
  server all work with no key at all.

---

## 1. Get the repo

```bash
git clone https://github.com/Brennan-VanderLaan/lazyaf.git
cd lazyaf
```

## 2. Install the CLI

The `lazyaf` binary is what creates your `.env` and checks the machine, so it
comes **before** the stack now — the onboarding order flipped with the Go CLI
(it used to be "compose first, `pip install ./cli` last").

**Install the CLI** (from v0.3.0)

```bash
curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash
```

Installs a single static binary into `~/.local/bin` after verifying its sha256
against the release's `checksums.txt`, and tells you the one line to add if
that directory is not on your PATH. Pin a version with
`LAZYAF_VERSION=v0.3.0`. Re-running upgrades (or repairs) in place.

**Windows:** run the same line in Git Bash (it installs `lazyaf.exe` into
`%USERPROFILE%\.local\bin`), or download `lazyaf_<ver>_windows_amd64.exe` and
`checksums.txt` from the release page, verify with
`Get-FileHash -Algorithm SHA256`, and put the folder on your user PATH.

The binary needs no Python. The one exception is
`lazyaf tests reconcile --from-collect`, which runs a collector *inside your
pytest* and therefore needs the interpreter your suite uses.

> **Until v0.3.0 is cut** that URL 404s. Build the same binary from this
> checkout with `bash scripts/build_cli.sh --host-only` (any Go >= 1.21; it
> downloads the pinned go1.26.8 once) and put `cli/bin/` on your PATH. The
> Python CLI (`pip install ./cli`) is still in the tree and still works, but
> it is being replaced and has neither `init` nor `doctor`. If you installed
> it that way, its `lazyaf` (a `Scripts/lazyaf` or `bin/lazyaf` under your
> Python) may sit earlier on PATH than `~/.local/bin`; `install.sh` warns
> about that by name, and `pip uninstall lazyaf-cli` removes it once the
> cutover has landed.

Check it works:

```bash
lazyaf --version
```

## 3. Make your `.env` and generate your secrets

**This step is mandatory.** The backend refuses to start without it, and so
does compose.

```bash
lazyaf init
```

That one command creates `.env` from `.env.example` if you do not have one, and
fills in the two shared secrets the backend refuses to start without. It prints
none of them, and it is safe to re-run — it never overwrites a value you set.
Run it in the checkout, or in the folder holding `docker-compose.release.yml`;
it refuses a directory that does not look like a LazyAF one rather than
guessing, and `--env-file PATH` points it anywhere else. `lazyaf init --check`
reports what is missing and changes nothing.

Then open `.env` and paste in whichever API keys you have. Every other variable
is optional; the defaults are correct for both compose files.

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
LazyAF backend would trust. There is no default now, in the source or in either
compose file. Start the stack without these and compose stops with a message
pointing at the command above; if a value is still one of the old constants,
`backend/app/config.py` treats it as unset.

Note what these two do **not** do: they authenticate LazyAF's internals to each
other. They do not put a password on the API, the UI or the git server. Nothing
does.

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
> `docker compose logs` is safe; `lazyaf doctor` and `lazyaf init` never
> print a value at all.

## 4. Doctor (optional, strongly recommended)

```bash
lazyaf doctor        # checking the pull path
lazyaf doctor --dev  # checking the build-from-source path
```

It checks Docker, free ports, disk space, whether your `.env` has usable keys
and the shared secrets set (it inspects shape only and never prints a value),
whether the images it needs exist — **including a registry lookup**
(`--offline` skips it), so it will tell you outright whether the published
images exist at the tag you chose — and whether a backend answers at the URL
the CLI would use (`--server` / `LAZYAF_SERVER`). Every failure it reports
comes with the command that fixes it. It changes nothing.

## 5. Start the stack

There are two ways. `LAZYAF_VERSION` in `.env` picks the image tag for the
pull path: `main` is what the publish workflow pushes on each commit to the
default branch and is what `.env.example` defaults to; `v0.2.0` is the first
release tag. `lazyaf doctor` (step 4) tells you whether the images are
actually there at the tag you chose.

### 5a. Build from source — always works from this checkout

```bash
docker compose up -d --build
```

This is the path that cannot be blocked by a missing published image, and it
is the path to use if you are working *on* LazyAF: it builds the backend and
frontend locally and bind-mounts the source. The first build is not quick —
three service images from scratch — so start it and come back.

It brings up four services: `backend` (8000), `frontend` (5173), a loopback
`runner-agent`, and `mock-endpoint` (8099, a stdlib OpenAI-compatible server
used by the test suite). All four publish on `127.0.0.1` or attach to the
internal network only.

### 5b. Pull prebuilt images — faster, if they exist for your tag

Set `LAZYAF_VERSION` in `.env` first, then:

```bash
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

> **If `pull` fails with "manifest unknown":** no image has been published at
> that tag. Try `main`, check the
> [packages page](https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend),
> or use 5a — it works today, unconditionally.

Either way, open **http://localhost:5173**. The API is on
http://localhost:8000, with interactive docs at http://localhost:8000/docs and
a health check at http://localhost:8000/health.

> **Do not run both stacks at once.** They both own the fixed-name
> `lazyaf-network` bridge, and only one compose project can. They also use
> different volumes, so they do not share a database. Bring one down before
> starting the other.

### Exposing it beyond localhost

Both compose files already bind to loopback, so this section is about
UNDOING that — deliberately. The port variables are interpolated straight
into the mapping, so the host IP lives in them:

```bash
# .env — reachable from anywhere on the network. Read SECURITY.md first.
LAZYAF_BACKEND_PORT=0.0.0.0:8000
LAZYAF_FRONTEND_PORT=0.0.0.0:5173
```

There is no authentication on the API or the git server, so the only safe
version of this is behind something that provides it: a reverse proxy that
authenticates, a VPN, or a tailnet. A bare LAN is not that.

Confirm whichever you chose actually took:

```bash
docker compose -f docker-compose.release.yml config | grep -A1 host_ip
```

`docker-compose.yml` (the source build) reads the same two variables, so the
same `.env` governs both stacks. Its mock-endpoint port (`8099`) is pinned to
`127.0.0.1` outright — a dev mock has no reason to be reachable off the host.
Whichever stack you run, check with `docker compose config`: no `host_ip` line
means the port is open to your whole network.

## 6. Get the step images

Pipeline steps and AI agent cards run inside dedicated step images. The
backend looks them up by their **local** tag — `lazyaf-claude:dev` and
friends — and it deliberately does **not** pull them for you: a missing image
fails the step with a clear message rather than downloading something behind
your back.

**Building from source (matches 5a):**

```bash
pip install docker                  # the build script drives the Docker SDK
python scripts/build_images.py      # builds all six, skips anything current
python scripts/build_images.py --check   # list missing/stale, build nothing
```

Six images in a three-level tree (`base` → `agent-base` → `claude`/`gemini`,
plus `test-runner` and `debug-sidecar`). The agent images install a Node
toolchain and the vendor CLIs, so this takes a while the first time. Later
runs skip anything whose content hash is unchanged.

**Pulling them instead (matches 5b):** they are published without the
`lazyaf-` prefix (the registry path already says `lazyaf`), so each one needs
a pull *and* a retag to the local name the backend looks for. Let doctor
write the commands — its image list is generated from
`scripts/build_images.py`'s table (and T1 fails if the two ever differ), so
it stays right when the set changes:

```bash
lazyaf doctor
```

It prints a `docker pull` + `docker tag` pair, filled in with your version,
for every step image you are missing. Copy and run them. By hand, if you
prefer:

```bash
PREFIX=ghcr.io/brennan-vanderlaan/lazyaf
VERSION=<the same tag you set as LAZYAF_VERSION>

for name in base debug-sidecar agent-base claude gemini test-runner; do
  docker pull $PREFIX/$name:$VERSION
  docker tag  $PREFIX/$name:$VERSION lazyaf-$name:dev
done
```

PowerShell:

```powershell
$PREFIX = "ghcr.io/brennan-vanderlaan/lazyaf"
$VERSION = "<the same tag you set as LAZYAF_VERSION>"

foreach ($name in "base","debug-sidecar","agent-base",
                  "claude","gemini","test-runner") {
  docker pull "$PREFIX/${name}:$VERSION"
  docker tag  "$PREFIX/${name}:$VERSION" "lazyaf-${name}:dev"
}
```

That list is a snapshot; the authoritative one is the `IMAGES` table in
`scripts/build_images.py`.

## 7. Ingest a repo

The CLI you installed in step 2 ingests your local repos into the platform and
lands finished branches back onto your real remote. With the stack up, check
they can see each other first:

```bash
lazyaf list          # talks to http://localhost:8000; override with LAZYAF_SERVER or --server
```

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

A card's work lands on a branch named `lazyaf/<first 8 characters of the
job id>`; `lazyaf branches <repo-id>` lists the real names. `ingest` left a
`lazyaf` remote in your checkout, so plain git works:

```bash
git fetch lazyaf
git merge lazyaf/ab12cd34
```

Or push a LazyAF branch straight to your real remote:

```bash
lazyaf branches <repo-id>                       # see what is there
lazyaf land <repo-id> --branch lazyaf/ab12cd34  # pushes to origin
lazyaf land <repo-id> --branch lazyaf/ab12cd34 --pr  # ...and opens a PR via gh
```

## 10. Optional: run against your own GPU

An agent step can drive any OpenAI-compatible server — ollama, vLLM,
llama.cpp, LM Studio — instead of a hosted API. Register it in the UI under
**Endpoints** (or `POST /api/model-endpoints`); registering probes it
immediately and tells you there and then whether the model can tool-call,
whether it streams, and how big its context window is. An endpoint that has
never been probed refuses to dispatch rather than quietly degrading.

Then name it from a pipeline step:

```yaml
  - id: fix
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"      # or: model: "endpoint:local-4090"
      task: "Fix the failing tests."
```

Two things to know today: **cards cannot select a self-hosted endpoint yet**
(the card `runner_type` enum has no `openai-harness` member — use a pipeline
step or the Playground), and `reach: runner-local` / `reach: proxy` are
implemented but have not been exercised against a second machine. Background
and the honest ledger: [README.md](README.md#agents-can-run-on-your-own-hardware).

---

## More pipeline examples

Worked `.lazyaf/pipelines/*.yaml` files — cheap-per-commit vs. expensive-nightly
review, hosted vs. self-hosted agents, fan-out, changelog and doc-drift jobs, a
secret-leak gate — live in
[docs/examples/pipelines/](docs/examples/pipelines/). LazyAF's own CI is
`.lazyaf/pipelines/test-suite.yaml` in this repo, and it is the most
load-bearing example there is.

## Choosing a version

`LAZYAF_VERSION` in `.env` sets the image tag for every service at once. It
only matters on the release compose file; a source build ignores it.

| Tag | What it is |
|-----|-----------|
| `main` | What the publish workflow pushes on each commit to the default branch. `.env.example`'s default. Moves under you; expect breakage. |
| `v0.2.0`, `v0.3.0`, … | A published release — reproducible, the tag never moves. `v0.2.0` is the first; `v0.3.0` is the one that ships the Go CLI. |
| `latest` | The newest stable release, once one has been published with that tag. |

Published tags are listed on the
[packages page](https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend).
`lazyaf doctor` checks your chosen tag against the registry for you.

Keep the step images (step 6) on the same tag as the services. Mixing
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

(For a source build, drop the `-f docker-compose.release.yml` and use
`up -d --build` in place of `pull`.)

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

(The source stack already runs one on loopback, unprofiled.)

On the same host this mostly demonstrates the mechanism. The real use is
running that image on another machine pointed at your backend's URL — in
which case use `wss://`, because the step dispatch frame carries the step's
JWT and its secret environment. The agent refuses plaintext `ws://` to a
non-loopback host unless you set `LAZYAF_RUNNER_ALLOW_INSECURE=1`, which the
bundled same-host runners do deliberately.

## Troubleshooting

**`docker compose` stops with "not set. Run: lazyaf init"** — you skipped
step 3, or you are running compose from a directory that does not contain
your `.env`. Run the command it names.

**`docker compose` says "port is already allocated"** — something else owns
8000, 5173 or 8099. Set `LAZYAF_BACKEND_PORT` / `LAZYAF_FRONTEND_PORT` in
`.env` (release stack), or stop the other process. `lazyaf doctor` names the
container when a container is the culprit.

**"network lazyaf-network was found but has incorrect label"** — the source
stack and the release stack are both trying to own that network. Bring the
other one down first.

**`pull` fails with "manifest unknown"** — nothing is published at that tag.
Run `lazyaf doctor` to see which tags exist, or build from source (step 5a).

**The UI loads but everything is empty** — nothing has been ingested yet.
Run `lazyaf ingest` (step 7). If `lazyaf list` cannot connect, the backend is
not up: check `docker compose logs backend`.

**A card or step fails with "Image not found: lazyaf-…:dev"** — you skipped
step 6, or the tags did not get applied. Re-run `lazyaf doctor` and follow
what it prints.

**An agent card fails immediately** — usually a missing or wrong API key.
Check `lazyaf doctor`, fix `.env`, then recreate the backend so it picks the
new value up: `docker compose up -d backend`.

**An `openai-harness` step is refused with "has never been probed"** — that
is deliberate. Probe the endpoint (the Endpoints UI, or
`POST /api/model-endpoints/{id}/probe`) before dispatching to it.

**Windows: `git` line endings churn in ingested repos** — set
`git config core.autocrlf input` in the repo you are ingesting.

---

More detail: [README.md](README.md) for what LazyAF is, what it exposes, and
how far along it is; `PLAN.md` for the project's own roadmap and engineering
decisions.
