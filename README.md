# LazyAF

LazyAF is a self-hosted CI/CD platform in which AI coding agents are a step type.
You write a pipeline out of shell steps, container steps, and **agent** steps, and LazyAF
runs it in isolated Docker containers against your repo — which it keeps a copy of on its
own internal git server, so agents work on branches and never touch your origin.

It is not a chat window bolted onto a repo. It is a build system that can run Claude Code,
the Gemini CLI, or a model on your own GPU as a step, next to `pytest`, with the same logs,
the same pass/fail semantics, and the same isolation.

> **Status:** pre-1.0, single-node, **unauthenticated**, and under active development — but
> real enough to run its own CI. Every push to LazyAF's internal remote runs LazyAF's test
> suite through LazyAF's own pipeline engine, and that run gates the branch.
>
> Two things to read before you run it: [what running this
> exposes](#before-you-expose-it-what-this-actually-opens) and [how far along it
> is](#how-far-along-this-actually-is).

---

## Show me

A pipeline is a YAML file in your repo at `.lazyaf/pipelines/*.yaml`. Push it, and LazyAF
picks it up on the same push it arrived in.

```yaml
# .lazyaf/pipelines/ci.yaml
name: "CI"
description: "Run the suite; if it breaks, let an agent look at it"

triggers:
  - type: push
    config:
      branches: ["main"]

steps:
  - id: test
    name: "Run tests"
    type: script
    config:
      image: "python:3.12"
      command: |
        pip install -e ".[test]"
        pytest -q
    continue_in_context: true   # keep the workspace for the next step
    on_failure: next            # don't stop — carry on to the agent

  - id: fix
    name: "Fix the failures"
    type: agent
    config:
      agent: claude-code        # claude-code | gemini | openai-harness | mock
      task: "Check the test output above. If anything failed, fix it and re-run pytest."
    on_failure: stop
```

(In this flat list form the agent step runs either way. To run it *only* when the tests
fail, draw the pipeline in the UI's graph editor and give the edge a `failure` condition —
repo YAML accepts the ordered-list form only.)

More worked examples: [docs/examples/pipelines/](docs/examples/pipelines/).

Three things happen that a normal CI system does not do:

1. The agent step runs in a container on the **same workspace volume** as the test step, so
   it sees the same checkout, the same installed dependencies, and the previous step's log
   output in its prompt.
2. Its commits land on a **branch on LazyAF's internal git server**. Your `origin` is
   untouched until you decide to land them.
3. The step reports its **token count and dollar cost** back to the platform alongside its
   logs.

The other way in is a **card**: a unit of work with a title and a description, on a kanban
board. Create it, hit Start, and an agent picks it up on a fresh branch — `todo →
in_progress → in_review → done` (or `failed`). At `in_review` you get a diff to approve or
reject; approving merges it to a target branch. A pipeline with a `card_complete` trigger
can gate that merge on the tests passing first.

---

## Agents can run on your own hardware

Milestone 14 landed on 2026-08-30: an agent step can drive **any OpenAI-compatible
inference server** — ollama, vLLM, llama.cpp, LM Studio — instead of a hosted API.

Register the endpoint once (UI: **Endpoints**, or `POST /api/model-endpoints`), then name
it from a step:

```yaml
  - id: fix
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"      # or: model: "endpoint:local-4090"
      task: "Fix the failing tests."
```

What makes that more than a base-URL swap:

- **An inference server is not an agent.** It has no loop, no tools, and no way to stop
  itself. LazyAF supplies all three: `runner-common/runner_common/harness/` is a
  tool-calling loop with six sandboxed tools (`list_files`, `read_file`, `write_file`,
  `apply_patch`, `run_shell`, `finish`), a budgeted transcript, and explicit stop
  conditions — an iteration ceiling, a total-token ceiling, and a consecutive-tool-error
  limit, each a named constant in `harness/constants.py`.
- **A capability probe runs before any GPU time is spent.** Four requests — `GET
  {base}/models`, two `POST {base}/chat/completions` (tools, streaming), and ollama's
  `POST /api/show` for the context window. `supports_tools` is judged on the *response
  shape*, not on the server accepting the `tools` parameter, because several servers accept
  it and emit prose anyway. An endpoint that has never been probed **refuses** to dispatch
  rather than silently taking a degraded path.
- **A text fallback for models that cannot tool-call**, which is most of the interesting
  small ones: the harness asks for one fenced `lazyaf` block per reply and corrects the
  model at most three consecutive times. On the fourth unparseable reply the step **fails
  loudly** (exit 5) with the raw response in the log. It never silently passes.
- **NAT'd hardware works.** An endpoint with `reach: runner-local` is probed and driven
  from the machine that hosts it, over the outbound WebSocket that machine's runner agent
  already opened. The model's URL never has to be reachable from the backend.
- **Cost is still recorded.** A self-hosted step writes the same `StepUsage` row with real
  token counts, attributed to a GPU node rather than a vendor.

**Verified, and not.** Registering, probing, and driving a real `llama3.1:8b` through
ollama — both the tools loop and the text fallback — was done by hand on one host on
2026-08-30, and the dogfood pipeline runs two `openai-harness` steps against a stdlib mock
OpenAI server on every push (so CI needs no GPU). Not yet verified: `reach: runner-local`
and `reach: proxy`, which need a second machine. And **cards cannot use a self-hosted
endpoint yet** — `RunnerType` in `backend/app/models/card.py` has no `openai-harness`
member, so `POST /api/repos/{id}/cards` rejects it. Pipeline steps and the Playground work
today.

---

## Why it exists

Most "AI coding" tools are a chat window, a diff, and a hope. Most CI systems treat an LLM
as a curiosity you shell out to. LazyAF is the position that agents belong in the same
place as your tests: in a pipeline, sandboxed, logged, and gated.

Concretely, what is different here:

| | |
|---|---|
| **Agents are steps, not a chatbot** | An agent step has a timeout, an exit code, `on_success`/`on_failure` edges, and logs — the same contract as a shell step. It can be one node in a graph, downstream of a build and upstream of a gate. |
| **Every run is isolated** | Each step runs in a fresh, ephemeral container. Containers are throwaway; the **workspace volume** underneath them persists for the run, so `HOME` (`/workspace/home`), installed tools, and the checkout (`/workspace/repo`) survive between steps. |
| **An internal git server** | `lazyaf ingest` pushes your repo to a bare repo LazyAF owns. Agents branch and push there. Nothing reaches your real remote until you run `lazyaf land`. |
| **Your model or theirs** | The same agent step runs Claude Code, the Gemini CLI, or a model on your own GPU behind an OpenAI-compatible server. |
| **A spec layer that ties tests to intent** | Features → user stories → acceptance criteria live in the database. Tests annotate themselves with `@pytest.mark.lazyaf_test_id(...)`, runs ship a manifest back, and `GET /api/criteria/{id}/history` gives you the pass/fail series for a criterion across commits and branches. |
| **Effort telemetry per step** | Every step writes a `StepUsage` row: tokens, dollars, wall-clock, container-seconds, and where the cost number came from. A step whose CLI reports nothing still records `cost_source: unknown` rather than quietly reporting $0. Rolled up at `GET /api/pipeline-runs/{id}/usage`. |
| **It runs its own CI** | LazyAF's test suite is a LazyAF pipeline, triggered by pushing to LazyAF. Every architectural change has to survive the platform executing it on itself before it lands. |

---

## Getting started

**You need:** Docker and Docker Compose. An `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` if you
want a hosted agent to do work — or your own ollama/vLLM box, or neither: cards, repos,
pipelines, shell/container steps, and the git server all run without any key.

The whole path, in order, is in **[QUICKSTART.md](QUICKSTART.md)**. The two steps people
miss:

```bash
python scripts/bootstrap_secrets.py   # MANDATORY. Creates .env and generates the two
                                      # shared secrets the backend refuses to start
                                      # without. Idempotent; never prints a value.
python scripts/build_images.py        # builds the six lazyaf-*:dev step images
                                      # (needs the docker SDK: pip install docker)
```

There is no default for `LAZYAF_STEP_AUTH_SECRET` or `LAZYAF_RUNNER_AUTH_SECRET`, in the
source or in either compose file. Both compose files fail fast with the command above if
they are unset, and `backend/app/config.py` treats the two constants LazyAF used to ship as
*unset* — so an inherited `.env` cannot quietly keep that hole open.

Then point LazyAF at a repo with the CLI:

```bash
pip install ./cli               # not on PyPI, and no release has been tagged yet
lazyaf ingest /path/to/your/repo --name my-project
```

That creates the repo in LazyAF, adds a git remote called `lazyaf` to your local clone, and
pushes to it. From then on it is ordinary git:

```bash
git push lazyaf main                          # send work in
git fetch lazyaf                              # pull agent branches back
lazyaf branches <repo-id>                     # see what the agents produced
lazyaf land <repo-id> --branch <branch>       # push one on to your real origin (--pr opens a PR)
```

Then open the UI (default `http://localhost:5173`), pick your repo, and either create a
card or add a `.lazyaf/pipelines/*.yaml` and push.

---

## Before you expose it: what this actually opens

**LazyAF has no authentication. None of it. Anyone who can open a TCP connection to it can
run arbitrary code as root on the machine hosting it.**

That is not a hardening to-do; it is the current design, and these three facts combine into
it:

1. **Every human-facing route is open.** No API key, no session, no login, on any router in
   `backend/app/routers/` — including the internal git server at `/git/{repo_id}.git`, which
   will serve a clone or accept a push from anyone. Three things authenticate, and all
   three protect LazyAF's internals from each other rather than protecting you from a
   caller: step containers reporting to `/api/steps/*` (a short-lived per-step JWT),
   runner agents enrolling at `/ws/runner` (the shared secret), and the debug terminal
   socket (a 15-minute JWT — which anyone can mint, from the open
   `POST /api/debug/{id}/join-token`).
2. **The backend holds the host Docker socket.** Both compose files bind-mount
   `/var/run/docker.sock` into the backend container, because that is how it spawns step
   containers. That is root-equivalent on the host. A step can also request the socket for
   itself with `needs: [docker]`.
3. **Nothing binds to loopback.** Both compose files publish with no host-IP prefix —
   `"8000:8000"` in `docker-compose.yml`, `"${LAZYAF_BACKEND_PORT:-8000}:8000"` in
   `docker-compose.release.yml` — which Docker publishes on `0.0.0.0`, every interface.
   Check yours with `docker compose config` and look for `host_ip`; if there is no
   `host_ip` line, the port is open to your whole network. The frontend port is a second
   door to the same API: its nginx reverse-proxies `/api` and `/ws` to the backend. The dev
   stack publishes a third port, `8099`, a mock OpenAI server.

Put together: an unauthenticated POST creates a pipeline, a pipeline step is a container
image plus a command on your daemon, and the daemon is the host's. On a coffee-shop Wi-Fi
or a cloud VM with a public IP, that is a takeover, not a misconfiguration.

**Bind it to loopback yourself. Nothing does it for you.** For the release stack, the port
variables are interpolated straight into the mapping, so a host IP in them works:

```bash
# .env
LAZYAF_BACKEND_PORT=127.0.0.1:8000
LAZYAF_FRONTEND_PORT=127.0.0.1:5173
```

`docker compose -f docker-compose.release.yml config` then shows `host_ip: 127.0.0.1` on
both. The dev stack (`docker-compose.yml`) hardcodes its mappings, so it needs a compose
override file or an edit. Either way, verify with `docker compose config` rather than
trusting this paragraph — and put a firewall in front of the host regardless.

Run it on a machine you trust, on a network you trust. Do not put it on the open internet.
There is no configuration that makes that safe today.

Two smaller notes, since they are easy to get wrong:

- `docker compose config` and `docker inspect` print the **interpolated** environment —
  your API keys and both shared secrets in plain text. Redact before pasting either into an
  issue. `docker compose logs`, `scripts/preflight.py` and `scripts/bootstrap_secrets.py`
  never print a secret value.
- A runner agent on a genuinely remote host must use `wss://`. The dispatch frame carries
  the step's JWT and its secret environment, and the agent refuses plaintext `ws://` to a
  non-loopback host unless you set `LAZYAF_RUNNER_ALLOW_INSECURE=1` — which the bundled
  same-host runners do, deliberately, because their frames never leave a private bridge on
  one machine.

---

## Core concepts

**Repo** — a repository ingested into LazyAF. LazyAF stores a bare clone on its internal
git server and serves it over git-smart-HTTP at `/git/{repo_id}.git`. Pushing to it can
fire pipeline triggers.

**Card** — a unit of work: title, description, an agent to do it. Statuses are `todo`,
`in_progress`, `in_review`, `done`, `failed`. Starting a card runs one agent step on a
fresh `lazyaf/<id>` branch; the diff shows up in the UI for approve/reject.

**Pipeline** — an ordered set of steps, or a DAG. Steps are `script` (a shell command in an
image), `docker` (a command in a named container image), or `agent`. Steps carry
`on_success` / `on_failure` (`next`, `stop`, `trigger:{id}`, `merge:{branch}`), a `timeout`,
and `continue_in_context` to keep the workspace for the next step. Pipelines can be defined
in the UI or in your repo under `.lazyaf/pipelines/`; repo-defined ones re-sync from the
pushed commit *before* triggers are matched, so a CI change takes effect on the push that
introduced it.

**Triggers** — `push` (with a branch filter) and `card_complete` (with `on_pass: merge` /
`on_fail: reject`). Plus manual runs from the UI or API. Two rapid pushes of the same
commit produce one run.

**Graph editor** — pipelines edited in the UI are drawn as a node graph: drag step nodes
onto a canvas, connect them, and label edges `success` / `failure` / `always`. A fixed start
node's outgoing edges define the entry points. Old array-form pipelines are auto-converted
to a vertical chain when you open them. *(Repo-defined YAML pipelines are view-and-run in
the UI, not editable there — the file is the source of truth.)*

**Agents** — an agent step names `claude-code`, `gemini`, `openai-harness` (any
OpenAI-compatible endpoint) or `mock` (zero-cost testing), plus a prompt. The prompt can be
inline (`prompt_template`, with `{{title}}` / `{{description}}` placeholders), or come from
an **Agent File** defined in the UI, or from `.lazyaf/agents/*.yaml` in your repo — repo
definitions override platform ones of the same name.

**Playground** — run one agent against a branch with a task and a prompt, watch the output
stream live, look at the resulting diff, and optionally save it to a branch. No card, no
pipeline. It is the fastest way to iterate on a prompt, and the fastest way to try a
self-hosted endpoint.

**Workspace** — the named Docker volume a run owns. The repo is cloned into
`/workspace/repo` by a helper container, `HOME` is `/workspace/home`, and both survive
across every step of the run. Volumes are cleaned up on success, on failure, and by a
periodic orphan sweep after a crash.

**Runners** — by default the backend spawns step containers itself on its own Docker
daemon. To run steps on *another* machine, start a **runner agent** (`lazyaf-runner`) there:
it dials out to the backend over a WebSocket, enrols with capability labels, and executes
the steps assigned to it on its own daemon. Pin a step to one with a `requires:` block:

```yaml
  - id: build-on-the-pi
    type: script
    config:
      image: "lazyaf-base:dev"
      requires:
        has: ["gpio"]
      command: ./build.sh
```

The backend never connects *in*, so a runner behind NAT works. See
[runner-agent/README.md](runner-agent/README.md) for the full configuration and security
posture. Runners are visible in the UI's sidebar panel with their labels, state, and
connection age.

**Model endpoints** — a registered OpenAI-compatible server (ollama, vLLM, …) with its
probed capabilities, health, and cost basis. `reach` says where it is reachable from:
`direct` (the backend can call it), `runner-local` (only the machine hosting it can), or
`proxy` (the backend brokers, and the upstream key never reaches a container).

**Spec layer** — Features contain user stories, which contain acceptance criteria. Cards
link to stories. Tests declare which criterion they cover via a pytest marker; when a step
finishes, the manifest is posted back and joined to the criterion along with the commit,
branch, model, and prompt. A user story cannot be marked `done` while any of its *required*
criteria lacks a currently-passing test run. Separately, a card only reaches `in_review` if
the tests tied back to its run are green — a red suite lands it in `failed`.

**Debug re-run** — re-run a failed pipeline with breakpoints. The run pauses before the
chosen step, a `lazyaf-debug-sidecar` container mounts the same workspace volume, and
`lazyaf debug attach` drops you into a shell there over a WebSocket. Fix it by hand,
`resume`, and the run continues. (A *remote* step pauses but cannot be attached to.)

---

## Architecture at a glance

```
        you                        LazyAF backend (FastAPI + SQLite)
   ┌──────────┐              ┌──────────────────────────────────────┐
   │ browser  │◀── WS /ws ──▶│  REST API  ·  internal git server    │
   └──────────┘              │  pipeline executor  ·  triggers      │
   ┌──────────┐  git push    │  workspace service  ·  spec + usage  │
   │ your git │─────────────▶│                                      │
   └──────────┘              └───┬──────────────────────┬───────────┘
                    spawns       │                      │  WS /ws/runner
                                 ▼                      ▼
                   ┌───────────────────────┐  ┌─────────────────────┐
                   │  step container       │  │  runner agent       │
                   │  (ephemeral)          │  │  (another machine)  │
                   │  /workspace/repo      │  │  spawns its own     │
                   │  /workspace/home      │  │  step containers    │
                   └───────────┬───────────┘  └──────────┬──────────┘
                               │                         │
                               └────────────┬────────────┘
                                            │
                                            ▼
                     POST /api/steps/{id}/status · logs · heartbeat ·
                     test-results · usage    — straight back to the
                     backend over HTTP, authenticated with a step JWT
```

The load-bearing detail: a step container reports **to the backend directly**, over HTTP,
authenticated with a short-lived per-step JWT. That is true whether the backend spawned it
or a runner agent on another host did. The runner's WebSocket carries only what is about
the *runner* and the *assignment* — registration, heartbeats, dispatch, ACK, cancel — so
remote execution needed no new reporting code.

Components:

| Path | What it is |
|---|---|
| `backend/` | FastAPI app: REST API, WebSocket broadcast, internal git server (Dulwich), pipeline executor, SQLite via SQLAlchemy + Alembic |
| `frontend/` | Svelte 5 SPA: board, pipelines + graph editor, specs, experiments, playground, endpoints, runner panel |
| `images/` | The `lazyaf-{base,debug-sidecar,agent-base,claude,gemini,test-runner}:dev` step images and the in-container control runtime |
| `runner-common/` | Shared agent-step runtime baked into the agent images — the executors, the OpenAI-compatible harness, and the `pytest-lazyaf` plugin |
| `runner-agent/` | `lazyaf-runner` — the remote runner agent |
| `cli/` | `lazyaf-cli` — ingest, land, list, branches, tests reconcile, debug |
| `tdd/` | The test suite, split into tiers |
| `.lazyaf/pipelines/` | LazyAF's own CI pipeline |

There is **no job queue and no runner pool polling for work.** An earlier architecture had
both; they were deleted. Steps are dispatched, not claimed.

### Interfaces

- **UI** — `http://localhost:5173`. Board, Pipelines (with a node-graph editor), Specs,
  Experiments, Playground, Endpoints.
- **REST API** — `http://localhost:8000/docs` (Swagger UI, generated from the code — it is
  the authoritative endpoint list). `GET /health` for liveness.
- **CLI** — `lazyaf ingest`, `land`, `list`, `branches`, `tests reconcile`, and
  `debug {rerun,list,status,attach,resume,abort,extend}`. Set `LAZYAF_SERVER` if the
  backend is not on `localhost:8000`.
- **MCP** — a Model Context Protocol server with 45 tools (repos, cards, pipelines, runs,
  agent files, diffs, branches, and the spec layer), so Claude Desktop or another MCP client
  can drive LazyAF. It runs standalone over stdio and talks to the backend over HTTP:

  ```json
  {
    "mcpServers": {
      "lazyaf": {
        "command": "uv",
        "args": ["run", "--project", "/path/to/lazyaf/backend", "python", "-m", "app.mcp"],
        "env": { "LAZYAF_BACKEND_URL": "http://localhost:8000" }
      }
    }
  }
  ```

---

## How far along this actually is

Status vocabulary: **COMPLETE** (landed and covered by the gate), **IN PROGRESS**,
**DESIGNED** (written down, no code).

**Solid — COMPLETE, and gated on every push.** The execution platform. Pipelines and the
graph editor, cards, the internal git server and its triggers, the workspace lifecycle,
the control layer that reports step status/logs/usage over HTTP, remote runner agents, the
spec layer, per-step effort telemetry, experiments, and debug re-run with an attachable
sidecar shell. `.lazyaf/pipelines/test-suite.yaml` runs three tiers plus a zero-cost mock
agent step, a step pinned to a remote runner, two self-hosted harness steps, and a final
step that asserts through the API that each step ran on the execution path its definition
asked for. The floors every tier must clear are committed in `tdd/tier_floors.json` --
that file is the honest number, because it is the one the gate actually enforces and the
one you can read without running anything. A change that silently stops running tests
fails against it. Tier-1 currently executes several thousand tests and grows most days,
so this README deliberately does not quote a count: the last one it quoted was stale
within hours, and it cited a `junit-t1.xml` that is gitignored and therefore unreadable
by anyone checking the claim.

**New — landed 2026-08-30, less travelled.** Self-hosted model endpoints and the agent
harness (see [above](#agents-can-run-on-your-own-hardware)). Verified against a real ollama
on one host and against a mock server in CI; `reach: runner-local` and `reach: proxy` are
implemented but have never been run against a second machine, and cards cannot select a
self-hosted endpoint yet.

**IN PROGRESS.** Retiring the v1 array pipeline format in favour of the graph. The first
two phases landed (`b79bb7f` — the graph gained terminal `merge:` / `trigger:` actions, so
it can now express everything the array could); the rest has not. Repo YAML still takes the
ordered-list form, and will until that work completes.

**DESIGNED, not built. Do not plan around these:**

- **The benchmark / eval layer (Milestone 13).** Zero implementation — a repo-wide grep for
  `BenchmarkCase`, `StrategyTemplate`, `TrialIteration`, `fail_to_pass` or `cost_to_solve`
  returns exactly one hit, and it is a comment. The design is in
  [docs/milestone-13/](docs/milestone-13/) and is worth reading; none of it exists.
- **Runner images with vLLM/ollama baked in, WSL2/RTX targeting, and yielding the GPU when
  you start a game (Phase 14.5).** Designed in `upcoming/wave9-145-runner-images.md`;
  `images/` contains no such image.

**Known limits of what does work:**

- **No authentication, and it holds your Docker socket.** See [the section
  above](#before-you-expose-it-what-this-actually-opens). This is a laptop/LAN tool today.
- **Single-node, single-worker.** The runner registry is per-process, so the backend must
  run one uvicorn worker. It logs a warning at startup if `WEB_CONCURRENCY` is higher.
- **Remote runners are exercised on loopback only.** The protocol, registry, dispatch, and
  remote workspace provisioning run on every push against a real runner-agent process — on
  the same host. Genuinely remote hardware is manual and less travelled.
- **No release has been published.** No `v*` tag exists in this repository yet, so there is
  no versioned image set and no CLI wheel to download. Build from source, or track the
  `main` image tag. See [QUICKSTART.md](QUICKSTART.md).

The roadmap, the design decisions, and the reasoning behind them live in
[PLAN.md](PLAN.md).

---

## Where this is going

The current work finishes the execution platform. The next milestone turns it into a
measuring instrument.

The question: **take a repo at a known commit, set an AI loop on a known task, and measure
what it actually cost to get to a working solution** — across models, prompts, and loop
shapes. Not "can an agent do this once", but "which way of working is effective, and at
what price".

The design that makes that tractable is that **a strategy is data, not code**. A strategy
(one-shot; write tests first; implement then fan out N reviewers then fix; an expensive
planner directing K cheap workers) is expressed as an ordinary pipeline graph with role
placeholders. It runs through the same executor as everything else, so adding a strategy to
the catalog is authoring JSON. Each iteration of a loop is a real, visible, costed pipeline
run.

The intended headline numbers are cost-to-solve, regression rate (how often a loop breaks
tests that were passing while fixing the target), and iterations-to-solve — each reported
with its variance, over a corpus of repos pinned at fixed commits with objective pass/fail
oracles, and exported as a bundle someone else can re-run.

Self-hosted endpoints are the reason this is now affordable to attempt: a strategy
comparison that costs hundreds of dollars against a hosted API costs electricity against
your own GPU.

The design docs: [docs/milestone-13/](docs/milestone-13/). **None of it is built yet.**

---

## Development

```bash
./scripts/test.sh unit           # fast
./scripts/test.sh integration    # API + DB (some need Docker)
./scripts/test.sh e2e-quick
./scripts/test.sh all
```

(`scripts/test.ps1` is the Windows equivalent.) Frontend: `npm run test:unit` (Vitest) and
`npm run test:e2e` (Playwright) in `frontend/`.

Steps run in the `lazyaf-*` images, which the backend resolves by their local `:dev` tag and
deliberately never pulls for you — a missing image fails the step loudly instead of
downloading something behind your back. From a source checkout, build them:

```bash
pip install docker                        # the build script drives the docker SDK
python scripts/build_images.py            # builds lazyaf-*:dev, skips anything unchanged
python scripts/build_images.py --check    # list missing/stale images without building
```

**The project gates itself with its own pipeline.** `.lazyaf/pipelines/test-suite.yaml` is
LazyAF's CI: pushing to LazyAF's internal remote runs the suite in three tiers (unit,
Docker-dependent integration, quick e2e), plus a zero-cost mock-agent step, a step pinned to
a remote runner, and two `openai-harness` steps against a mock OpenAI server. A final step
then asserts, through the API, that every step actually ran on the execution path its
definition asked for — so a silent fallback to a different path fails the run instead of
passing quietly.

GitHub Actions exists in this repo, but it **packages; it does not gate**: `images.yml`
publishes images to GHCR and `release.yml` builds the CLI wheel, both fired by a tag or a
push a human made after watching the dogfood pipeline go green. Test gating is LazyAF's job.

Two rules the suite enforces on itself, worth knowing before you send a patch: each tier has
a committed floor on how many tests must actually execute, and every skip must be in a
committed baseline of allowed skip reasons. A change that silently stops running tests fails
the gate.

Reading order for a new contributor: this file → [PLAN.md](PLAN.md) →
[tdd/README.md](tdd/README.md) → [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT. See [LICENSE](LICENSE).

---
