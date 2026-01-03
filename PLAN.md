# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

## What is LazyAF?

LazyAF is a local-first CI/CD platform that integrates AI agents as first-class citizens. Instead of writing GitHub Actions YAML, you define pipelines with a mix of:

- **Agent steps**: Claude or Gemini implements features, fixes tests, reviews code
- **Script steps**: Traditional shell commands (lint, test, build)
- **Docker steps**: Commands in isolated container images

The core workflow:
1. **Ingest** a repo via CLI (`lazyaf ingest /path/to/repo`)
2. **Create cards** describing features or tasks
3. **Start work** -> Runner clones, executes, pushes to internal git server
4. **Pipeline triggers** -> Tests run, AI fixes failures, auto-merge on success
5. **Land changes** to real remote when ready (`lazyaf land`)

---

## Project Structure

```
lazyaf/
|-- backend/
|   |-- app/
|   |   |-- main.py              # FastAPI app entry point
|   |   |-- config.py            # Settings
|   |   |-- database.py          # SQLAlchemy async setup
|   |   |-- models/              # SQLAlchemy models
|   |   |-- routers/             # API endpoints
|   |   |-- services/            # Business logic
|   |   |-- schemas/             # Pydantic models
|   |   +-- mcp/                 # MCP server for Claude Desktop
|   |-- git_repos/               # Internal bare git repos
|   |-- runner/
|   |   |-- Dockerfile
|   |   +-- entrypoint.py        # Runner execution logic
|   |-- pyproject.toml
|   +-- alembic/                 # DB migrations
|-- cli/                         # LazyAF CLI tool (ingest, land)
|   |-- pyproject.toml
|   +-- lazyaf/cli.py
|-- frontend/
|   |-- src/
|   |   |-- lib/
|   |   |   |-- components/      # Svelte components
|   |   |   |-- stores/          # State management
|   |   |   +-- api/             # API client
|   |   +-- routes/              # Pages
|   |-- package.json
|   +-- vite.config.ts
|-- historical-documents/        # Archived phase documentation
|-- docker-compose.yml
|-- PLAN.md                      # This file
+-- README.md
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | Svelte + Vite | Reactive UI, fast builds |
| Backend | FastAPI | Async Python API |
| Database | SQLite + SQLAlchemy | Simple persistence (PostgreSQL ready) |
| Queue | In-memory | Job management |
| Containers | Docker SDK for Python | Runner isolation |
| Real-time | WebSockets | Status updates |
| Git | Dulwich | Pure Python git server |
| MCP | FastMCP | Claude Desktop integration |

---

## Core Data Models

### Repo
```python
class Repo:
    id: UUID
    name: str
    remote_url: str | None       # Real remote (GitHub/GitLab)
    default_branch: str          # e.g., "dev" or "main"
    is_ingested: bool
```

### Card
```python
class CardStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"

class Card:
    id: UUID
    repo_id: UUID
    title: str
    description: str
    status: CardStatus
    branch_name: str | None
    step_type: StepType          # agent | script | docker
    step_config: dict            # Type-specific config
```

### Pipeline
```python
class Pipeline:
    id: UUID
    repo_id: UUID
    name: str
    steps: list[PipelineStep]    # Ordered execution
    triggers: list[TriggerConfig]

class PipelineStep:
    name: str
    type: StepType               # agent | script | docker
    config: dict
    on_success: str              # "next" | "stop" | "merge:{branch}"
    on_failure: str              # "next" | "stop" | "trigger:{card_id}"
    continue_in_context: bool    # Preserve workspace
```

---

## API Summary

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/repos` | Repo management |
| `POST /api/repos/ingest` | Ingest local repo |
| `GET/POST /api/repos/{id}/cards` | Card CRUD |
| `POST /api/cards/{id}/start` | Trigger agent work |
| `GET/POST /api/pipelines` | Pipeline CRUD |
| `POST /api/pipelines/{id}/run` | Run pipeline |
| `GET /api/pipeline-runs/{id}` | Run status |
| `/git/{id}.git/*` | Internal git server |
| `/ws` | WebSocket for real-time updates |

Full API: 31 MCP tools for Claude Desktop orchestration.

---

## Agent Guidelines for This Repo

When working on LazyAF, agents should:

1. **Understand the architecture**: Backend (FastAPI) + Frontend (Svelte) + Runners (Docker)
2. **Check existing patterns**: Look at similar routers/services before creating new ones
3. **Run tests after changes**: `pytest` for backend, `npm test` for frontend
4. **Use the internal git server**: Changes go to internal server, not GitHub
5. **Follow the step type model**: All work is agent/script/docker steps
6. **Reference historical docs**: See `historical-documents/` for completed phase details

---

## Completed Phases Summary

Detailed documentation for completed phases is in `historical-documents/`.

| Phase | Name | Status | Key Deliverable |
|-------|------|--------|-----------------|
| 1 | Project Foundation | COMPLETE | `docker-compose up` runs both services |
| 2 | Repo & Card Management | COMPLETE | Create cards on kanban board |
| 3-3.75 | Runner Pool & Git Server | COMPLETE | Internal git server, runner isolation |
| 4 | Agent Integration | COMPLETE | Cards trigger Claude Code |
| 5 | Review Flow | COMPLETE | Approve/reject workflow |
| 6 | Polish | ONGOING | Quality of life improvements |
| 7 | MCP Interface | COMPLETE | 31 tools for Claude Desktop |
| 8 | Test Result Capture | COMPLETE | Test results displayed in UI |
| 8.5 | CI/CD Foundation | COMPLETE | Script/docker step types |
| 9-9.1 | Pipelines | COMPLETE | Multi-step workflows with context |

---

## Current Status

**Completed**: Phases 1-11 (full pipeline and trigger system)
**Next**: Phase 12 (Runner Architecture Refactor) - start with 12.0 (Unify Entrypoints)

The target workflow is now fully functional:
1. Ingest repos via CLI
2. Create cards describing features (or CI steps: script/docker)
3. Start work -> runner clones repo, executes step
4. Card completes -> reaches "in_review" status
5. **Pipeline triggers automatically** (if configured with card_complete trigger)
6. Pipeline runs tests/validation steps
7. **On pass**: Card auto-merged and marked done
8. **On fail**: Card marked failed (user can retry)

---

## Phase 10: Events & Triggers (COMPLETE)

**Goal**: Enable the Card -> Pipeline -> Merge workflow

### 10a: Card Completion Trigger
- [x] Add `triggers` field to Pipeline model (JSON array of TriggerConfig)
- [x] Add `trigger_context` field to PipelineRun model
- [x] When card status -> done/in_review, check for matching pipelines
- [x] Auto-trigger pipeline with card context (branch, commit, card_id)
- [x] UI: Configure triggers in pipeline editor
- [x] Trigger actions: on_pass (merge/nothing), on_fail (fail/reject/nothing)

### 10b: Auto-Merge Action
- [x] Pipeline completion executes trigger actions from context
- [x] `on_pass: "merge"` - merge card branch to default branch
- [x] `on_fail: "fail"` - mark card as failed
- [x] `on_fail: "reject"` - reject card back to todo
- [x] Merge uses internal git server
- [x] Conflict handling: fail with clear error

### 10c: Push Triggers
- [x] Internal git server captures pushed refs
- [x] Push event fires trigger_service.on_push()
- [x] Pipeline trigger: `{type: "push", config: {branches: ["main", "dev"]}}`
- [x] Branch pattern matching with fnmatch

---

## Phase 11: Agent Playground (MVP COMPLETE)

**Goal**: Rapid experimentation with agent prompts without full card/job loops

### Architecture

```
+-----------------------------------------------------------------------+
|                      AGENT PLAYGROUND TAB                              |
+-----------------------------------------------------------------------+
|  +----------------------+  +--------------------------------------+   |
|  |   AGENT CONFIG       |  |      STREAMING AGENT OUTPUT          |   |
|  |  Repo: [dropdown]    |  |  I'll analyze the code...            |   |
|  |  Agent: [dropdown]   |  |  [Tool: Read] src/utils.ts           |   |
|  |  Runner: Claude/Gem  |  |  [Tool: Edit] src/utils.ts:42        |   |
|  |  Branch: [dropdown]  |  +--------------------------------------+   |
|  |  Task: [textarea]    |  +--------------------------------------+   |
|  |  [> Test Once]       |  |         DIFF PREVIEW                 |   |
|  |  [ ] Save to branch  |  |  - const old = "foo";                |   |
|  +----------------------+  |  + const new = "bar";                |   |
|                            +--------------------------------------+   |
+-----------------------------------------------------------------------+
```

### Completed Sub-phases

- [x] **11a**: Foundation - playground job queue, SSE streaming
- [x] **11b**: Frontend Test Once Mode - UI, log streaming
- [x] **11c**: Diff & Save - capture diff, save to branch
- [x] **11d**: Cancellation - cancel button, status indicators

### Pending

- [ ] **11e**: Polish - keyboard shortcuts, auto-scroll, copy diff, branch cleanup

---

## Phase 12: Runner Architecture Refactor

> **Vision**: Runners become execution targets (machines with capabilities), not execution environments. Steps run in ephemeral containers with a shared workspace.

### Current Problems

1. **Entrypoint divergence**: Claude and Gemini runners are ~1800 lines each, 95% duplicated
2. **Docker-in-Docker required**: For `type: docker` steps
3. **Workspace conflicts**: Multiple pipelines on same runner can collide
4. **No image flexibility**: Steps inherit the runner's environment
5. **No hardware affinity**: Can't route steps to specific hardware (GPUs, ARM devices)

### Target Architecture

```
MODE 1: LOCAL (Backend has Docker access) - Zero latency

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Local          | --> Docker API
    | Executor       |     | Router         |     | Executor       |
    +----------------+     +----------------+     +----------------+


MODE 2: REMOTE (Hardware/distributed runners) - WebSocket push

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Remote         | <-- WebSocket
    | Executor       |     | Router         |     | Executor       |
    +----------------+     +----------------+     +----------------+
                                                           |
    +------------------------------------------------------+
    | Runner Agents connected via WebSocket
    v
    +----------------+     +----------------+     +----------------+
    | Runner Agent   |     | Runner Agent   |     | Runner Agent   |
    | (Docker host)  |     | (Raspberry Pi) |     | (GPU server)   |
    | labels:        |     | labels:        |     | labels:        |
    |   arch=amd64   |     |   arch=arm64   |     |   has=cuda     |
    +----------------+     +----------------+     +----------------+
```

### Implementation Phases

#### Phase 12.0: Unify Runner Entrypoints (2-3 days)
**Goal**: Fix immediate pain, unblock future phases

- [ ] Create `runner-common/` package with shared utilities
  - `git_helpers.py` - clone, branch, push, commit
  - `context_helpers.py` - `.lazyaf-context/` management
  - `job_helpers.py` - heartbeat, logging, status reporting
- [ ] Create unified entrypoint that dispatches by agent type
- [ ] Reduce Claude/Gemini-specific code to ~100 lines each

**Outcome**: Maintainable entrypoints, foundation for new architecture

#### Phase 12.1: LocalExecutor + Step State Machine (~1.5 weeks)
**Goal**: Instant step execution with proper state management

- [ ] Create `step_executions` table with idempotency keys
- [ ] Implement Step state machine (pending -> running -> completed/failed)
- [ ] Create `LocalExecutor` service (spawns containers via Docker SDK)
- [ ] Real-time log streaming from container to pipeline executor
- [ ] Crash recovery: reattach to orphaned containers on restart

**Outcome**: Local dev is instant with proper state tracking

#### Phase 12.2: Workspace State Machine + Pipeline Integration (~1.5 weeks)
**Goal**: Proper workspace lifecycle with locking and cleanup

- [ ] Create `Workspace` model with state and use_count
- [ ] Implement workspace locking (shared for execution, exclusive for cleanup)
- [ ] Create `ExecutionRouter` (routes to Local or Remote executor)
- [ ] Trigger deduplication with `trigger_key`
- [ ] Orphan detection and cleanup

**Outcome**: Robust workspace lifecycle, exactly-once execution

#### Phase 12.3: Control Layer & Step Images (~1.5 weeks)
**Goal**: Proper container communication and base images

- [ ] Create control layer script (`/control/run.py`)
  - Reads config from `/workspace/.control/step_config.json`
  - Reports status, streams logs, sends heartbeats
- [ ] Create base image (`lazyaf-base`) with control layer
- [ ] Create agent images (`lazyaf-claude`, `lazyaf-gemini`)
- [ ] Configure HOME persistence (`/workspace/home`)

**Outcome**: Steps run in proper containers with backend communication

#### Phase 12.4: Migrate Script/Docker Steps (~1 week)
**Goal**: All non-agent steps use new architecture

- [ ] Pipeline executor routes script/docker steps through orchestrator
- [ ] Remove step execution from runner entrypoints
- [ ] Steps can specify custom images in pipeline YAML

**Outcome**: Script/docker steps don't need runners

#### Phase 12.5: Migrate Agent Steps (~1.5 weeks)
**Goal**: Agent steps also use new architecture

- [ ] Agent steps spawn ephemeral containers via orchestrator
- [ ] Agent wrapper handles CLI invocation
- [ ] Remove old runner polling infrastructure

**Outcome**: All step types use unified architecture

#### Phase 12.6: RemoteExecutor + Runner State Machine (~2 weeks)
**Goal**: Millisecond-latency job assignment with proper failure handling

- [ ] Implement Runner state machine (disconnected -> idle -> busy -> dead)
- [ ] WebSocket protocol for job push (not polling)
- [ ] ACK timeout and job reassignment
- [ ] Heartbeat monitoring (30s death timeout)
- [ ] Create `runner-agent` package for remote machines
- [ ] `NativeOrchestrator` for embedded devices (no Docker)

**Outcome**: Robust remote execution with push-based assignment

#### Phase 12.7: Debug Re-Run Mode (~2-3 weeks)
**Goal**: Re-run failed pipelines with breakpoints for interactive debugging

- [ ] `POST /api/pipeline-runs/{id}/debug-rerun` endpoint
- [ ] Breakpoint selector (pause before specific steps)
- [ ] Debug session state machine (waiting -> connected -> ended)
- [ ] CLI `lazyaf debug <session-id>` with WebSocket terminal
- [ ] Sidecar mode (inspect filesystem) and shell mode (exec into container)
- [ ] Session timeout management (1-4 hours)

**Outcome**: Operators can debug failed pipelines interactively

#### Phase 12.8: Cleanup & Polish (~1 week)
**Goal**: Remove legacy code, document new model

- [ ] Remove old runner entrypoints
- [ ] Remove `runner_pool.py` polling infrastructure
- [ ] Update docker-compose for new architecture
- [ ] Documentation and example Dockerfiles

**Outcome**: Clean, documented system

#### Phase 12.9: Kubernetes Orchestrator (Future)
**Goal**: Same code works on Kubernetes

- [ ] `KubernetesOrchestrator` implementation
- [ ] PersistentVolumeClaims for workspaces
- [ ] K8s Jobs for step execution
- [ ] Node selectors based on runner labels

**Outcome**: Production-ready K8s deployment

---

## Phase 9.5: Webhooks (DEFERRED)

**Goal**: External systems can trigger pipelines

- [ ] Webhook entity (token-authenticated URLs)
- [ ] Generic webhook: `POST /api/webhooks/{token}/trigger`
- [ ] Git push webhook: parses branch/commit from payload
- [ ] UI: Webhook management

---

## Phase 13: Reporting & Artifacts (FUTURE)

**Goal**: Visibility into CI health over time, build artifact storage

- [ ] Test result aggregation per repo over time
- [ ] Coverage trend tracking
- [ ] Artifact storage (build outputs, test reports)
- [ ] Dashboard with CI health metrics

---

## Roadmap Summary

| Phase | Status | Description |
|-------|--------|-------------|
| 1-9.1 | COMPLETE | Core platform, pipelines, triggers |
| 10 | COMPLETE | Events & triggers, auto-merge |
| 11 | MVP COMPLETE | Agent playground (polish pending) |
| **12** | **NEXT** | Runner architecture refactor |
| 9.5 | DEFERRED | Webhooks |
| 13 | FUTURE | Reporting & artifacts |

---

## Update & Migration Strategy

### Current State (Pre-Phase 12)

In-memory state (job queue, runner registrations) is lost on restart:

1. **Before restarting backend**: Wait for active jobs to complete
2. **Queued jobs**: Will be lost - re-queue via UI after restart
3. **Runner registrations**: Runners auto-reconnect and re-register

```bash
# Safe restart (current architecture)
docker compose up -d --build backend
# Runners reconnect automatically
```

### Target State (Post-Phase 12)

All state persisted to database:
- Alembic migrations run automatically on startup
- Graceful shutdown with drain period
- Orphan recovery on restart
- Runner resilience (re-register on 404, timeout on submit)

---

## Tech Debt

### TD1: Entrypoint Refactor
**Trigger**: Phase 12.0 addresses this
- Extract test detection, git operations, job helpers into shared package

### TD2: Feature Consistency
- Cards and pipeline steps use same agent configuration UI
- Unified step config schema across cards and pipelines

### TD3: Pipeline Workspace Cleanup
- Clean up old pipeline workspace directories
- Disk space monitoring and automatic cleanup thresholds
