# LazyAF

LazyAF is a self-hosted CI/CD platform in which AI coding agents are a step type.
You write a pipeline out of shell steps, container steps, and **agent** steps, and LazyAF
runs it in isolated Docker containers against your repo — which it keeps a copy of on its
own internal git server, so agents work on branches and never touch your origin.

It is not a chat window bolted onto a repo. It is a build system that can run Claude Code
or the Gemini CLI as a step, next to `pytest`, with the same logs, the same pass/fail
semantics, and the same isolation.

> **Status:** pre-1.0, single-node, and under active development — but real enough to run
> its own CI. Every push to LazyAF's internal remote runs LazyAF's ~3,000-test suite
> through LazyAF's own pipeline engine, and that run gates the branch. See
> [What is not done yet](#what-is-not-done-yet) for the honest gaps.

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
      agent: claude-code        # claude-code | gemini | mock
      task: "Check the test output above. If anything failed, fix it and re-run pytest."
    on_failure: stop
```

(In this flat list form the agent step runs either way. To run it *only* when the tests
fail, draw the pipeline in the UI's graph editor and give the edge a `failure` condition.)

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

## Why it exists

Most "AI coding" tools are a chat window, a diff, and a hope. Most CI systems treat an LLM
as a curiosity you shell out to. LazyAF is the position that agents belong in the same
place as your tests: in a pipeline, sandboxed, logged, and gated.

Concretely, what is different here:

| | |
|---|---|
| **Agents are steps, not a chatbot** | An agent step has a timeout, an exit code, `on_success`/`on_failure` edges, and logs — the same contract as a shell step. It can be one node in a graph, downstream of a build and upstream of a gate. |
| **Every run is isolated** | Each step runs in a fresh, ephemeral container. Containers are throwaway; the **workspace volume** underneath them persists for the run, so `HOME`, installed tools, and the checkout survive between steps. |
| **An internal git server** | `lazyaf ingest` pushes your repo to a bare repo LazyAF owns. Agents branch and push there. Nothing reaches your real remote until you run `lazyaf land`. |
| **A spec layer that ties tests to intent** | Features → user stories → acceptance criteria live in the database. Tests annotate themselves with `@pytest.mark.lazyaf_test_id(...)`, runs ship a manifest back, and `GET /api/criteria/{id}/history` gives you the pass/fail series for a criterion across commits and branches. |
| **Effort telemetry per step** | Every step writes a `StepUsage` row: tokens, dollars, wall-clock, container-seconds, and where the cost number came from. A step whose CLI reports nothing still records `cost_source: unknown` rather than quietly reporting $0. *(API only today — no UI yet.)* |
| **It runs its own CI** | LazyAF's test suite is a LazyAF pipeline, triggered by pushing to LazyAF. Every architectural change has to survive the platform executing it on itself before it lands. |

---

## Getting started

**You need:** Docker and Docker Compose. An `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` if you
want agents to do work — cards, repos, pipelines, shell/container steps, and the git server
all run fine with neither.

There are two ways to bring the stack up:

- **Pull prebuilt images** (`docker-compose.release.yml`) — no build step, images come from
  GHCR, pinned by a `LAZYAF_VERSION` you set in `.env`. This is the fast path if you just
  want to try it.
- **Build from source** (`docker-compose.yml`) — builds the backend and frontend locally
  and bind-mounts the source for live reload. This is the path if you are working *on*
  LazyAF.

Both read a `.env` you create from `.env.example`; the template documents every setting.
Bring up **one or the other**, not both — they share a fixed-name Docker network.

**Exact commands live in [QUICKSTART.md](QUICKSTART.md)** — it is short, and it is the file
that gets updated when the flow changes. The rest of this section is what happens after the
stack is up.

Point LazyAF at a repo with the CLI:

```bash
pip install ./cli               # not on PyPI yet; releases also ship a wheel
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

**Agents** — an agent step names a CLI (`claude-code`, `gemini`, or `mock` for zero-cost
testing) and a prompt. The prompt can be inline (`prompt_template`, with `{{title}}` /
`{{description}}` placeholders), or come from an **Agent File** defined in the UI, or from
`.lazyaf/agents/*.yaml` in your repo — repo definitions override platform ones of the same
name.

**Playground** — run one agent against a branch with a task and a prompt, watch the output
stream live, look at the resulting diff, and optionally save it to a branch. No card, no
pipeline. It is the fastest way to iterate on a prompt.

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

**Spec layer** — Features contain user stories, which contain acceptance criteria. Cards
link to stories. Tests declare which criterion they cover via a pytest marker; when a step
finishes, the manifest is posted back and joined to the criterion along with the commit,
branch, model, and prompt. A user story cannot be marked `done` while any of its *required*
criteria lacks a currently-passing test run. Separately, a card only reaches `in_review` if
the tests tied back to its run are green — a red suite lands it in `failed`.
There is a CRUD UI for features/stories/criteria; the test-history side is API-only so far.

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
| `frontend/` | Svelte 5 SPA: board, pipelines + graph editor, specs, playground, runner panel |
| `images/` | The `lazyaf-{base,agent-base,claude,gemini,test-runner}:dev` step images and the in-container control runtime |
| `runner-common/` | Shared agent-step runtime baked into the agent images, plus the `pytest-lazyaf` plugin |
| `runner-agent/` | `lazyaf-runner` — the remote runner agent |
| `cli/` | `lazyaf-cli` — ingest, land, list, branches, tests reconcile |
| `tdd/` | The test suite, split into tiers |
| `.lazyaf/pipelines/` | LazyAF's own CI pipeline |

There is **no job queue and no runner pool polling for work.** An earlier architecture had
both; they were deleted. Steps are dispatched, not claimed.

### Interfaces

- **UI** — `http://localhost:5173`. Five pages: Board, Pipelines (with a node-graph editor), Specs, Experiments, Playground.
- **REST API** — `http://localhost:8000/docs` (Swagger UI, generated from the code — it is
  the authoritative endpoint list). `GET /health` for liveness.
- **CLI** — `lazyaf ingest`, `land`, `list`, `branches`, `tests reconcile`. Set
  `LAZYAF_SERVER` if the backend is not on `localhost:8000`.
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

The design docs are written and worth reading if this is the interesting part:
[docs/milestone-13/](docs/milestone-13/). None of it is built yet.

### What is not done yet

Being plain about it:

- **Benchmarking, experiments, and leaderboards do not exist.** No models, no endpoints, no
  UI. Everything in the section above is design.
- **Effort telemetry has no UI.** Tokens and cost are recorded per step and rolled up per
  run over the API (`GET /api/pipeline-runs/{id}/usage`), but nothing in the frontend shows
  them yet.
- **The spec layer's test history has no UI.** You can create features, stories, and
  criteria in the UI; the criterion pass/fail history is API-only.
- **A running pipeline is shown as a linear timeline, not on the graph.** The graph editor
  can already render live step state; nothing wires it up yet.
- **Single-node, single-worker.** The runner registry is per-process, so the backend must
  run one uvicorn worker. It says so at startup if you set `WEB_CONCURRENCY` higher.
- **No authentication, and it holds your Docker socket.** The API, the UI, and the internal
  git server are unauthenticated, and the step and runner secrets ship with well-known
  defaults. The backend also has the host Docker socket, which it needs in order to spawn
  step containers — that is root-equivalent on the machine you run it on. This is a
  laptop/LAN tool today. Run it somewhere you trust, bound to localhost; do not put it on
  the open internet.
- **Remote runners are tested on loopback.** The protocol, registry, dispatch, and remote
  workspace provisioning are exercised on every push by a real runner agent process — but on
  the same host. Running against genuinely remote hardware is manual and less travelled.
- **Debug re-run mode** (pausing a failed run at a breakpoint and attaching a terminal) is
  designed but not built.

The roadmap, the design decisions, and the reasoning behind them live in
[PLAN.md](PLAN.md).

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
python scripts/build_images.py            # builds lazyaf-*:dev, skips anything unchanged
python scripts/build_images.py --check    # list missing/stale images without building
```

**The project gates itself with its own pipeline.** `.lazyaf/pipelines/test-suite.yaml` is
LazyAF's CI: pushing to LazyAF's internal remote runs the suite in three tiers (unit,
Docker-dependent integration, quick e2e), plus a zero-cost mock-agent step and a step pinned
to a remote runner. A final step then asserts, through the API, that every step actually ran
on the execution path its definition asked for — so a silent fallback to a different path
fails the run instead of passing quietly. There is no external CI; this is it.

Two rules the suite enforces on itself, worth knowing before you send a patch: each tier has
a committed floor on how many tests must actually execute, and every skip must be in a
committed baseline of allowed skip reasons. A change that silently stops running tests fails
the gate.

Reading order for a new contributor: this file → [PLAN.md](PLAN.md) ("What is LazyAF",
"Current Status", "Milestone 12 — Attempt #3 Roadmap" and its standing rules) →
[tdd/README.md](tdd/README.md).

---

## License

MIT. See [LICENSE](LICENSE).

---
