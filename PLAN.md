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

**Completed**: Phases 1-11, 12.0, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
**Next**: Phase 12.7 (Debug Re-Run Mode)

### Phase 12 Progress

| Phase | Name | Status | Tests |
|-------|------|--------|-------|
| 12.0 | Unify Runner Entrypoints | COMPLETE | - |
| 12.1 | LocalExecutor + Step State Machine | COMPLETE | 94 pass |
| 12.2 | Workspace & Pipeline State Machines | COMPLETE | 272 pass |
| 12.3 | Control Layer & Step Images | COMPLETE | 302 pass |
| 12.4 | Migrate Script/Docker Steps | COMPLETE | 302+ pass |
| 12.5 | Migrate Agent Steps | COMPLETE | 48+ pass |
| 12.6 | RemoteExecutor & Runner State Machine | COMPLETE | 200+ pass |

Phase 12.6 complete. Remote runners now use WebSocket push architecture for millisecond-latency job assignment (vs 5-second polling). Key deliverables:

- **Runner State Machine**: DISCONNECTED → CONNECTING → IDLE → ASSIGNED → BUSY → DEAD lifecycle
- **RemoteExecutor**: Manages WebSocket connections, pushes jobs immediately to idle runners
- **WebSocket Protocol**: Registration (10s timeout), ACK (5s timeout), Heartbeat (10s interval), Death (30s timeout)
- **Job Recovery**: Automatic requeue on runner death/disconnect, safe reconnection handling
- **Runner Agent Package**: `runner-agent/` with CLI, Docker orchestrator, WebSocket client
- **HTTP Polling Removed**: `runner_pool.py` and `/api/runners` endpoints removed (WebSocket only)

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

## Phase 11: Agent Playground ✅ (MVP Complete)

**Goal**: Rapid experimentation with agent prompts without going through full card/job development loops

**Problem Being Solved**: When developing or refining agent prompts, you need to iterate quickly. Currently, testing an agent requires creating a card, starting it, waiting for completion, reviewing the diff - a slow loop. The Agent Playground provides immediate feedback on agent behavior.

### Key Architecture Decision

**Use Existing Runner Infrastructure**: Direct Claude/Gemini API calls cannot modify files - they only return text. Real agent behavior (file changes, diffs) requires the existing Docker runner infrastructure (Claude Code CLI, aider, etc.). The Playground reuses `job_queue` with a special `is_playground=True` flag.

### MVP Scope (Phase 11a-11c)

- **Test Once mode only** (defer Test Continuous to later)
- **Branch input mode only** (test against real branch, like card execution)
- **Real-time streaming** (agent reasoning + tool calls via `--output-format stream-json`)
- **Branch creation opt-in** (run against source branch, save to new branch only if user wants)

### Architecture

```
+-----------------------------------------------------------------------+
|                      AGENT PLAYGROUND TAB                              |
+-----------------------------------------------------------------------+
|                                                                        |
|  +----------------------+  +--------------------------------------+   |
|  |   AGENT CONFIG       |  |      STREAMING AGENT OUTPUT          |   |
|  |                      |  |                                      |   |
|  |  Repo: [dropdown]    |  |  I'll analyze the code structure     |   |
|  |  Agent: [dropdown]   |  |  and make the requested changes.     |   |
|  |  Runner: Claude/Gem  |  |                                      |   |
|  |                      |  |  [Tool: Read] src/utils.ts           |   |
|  |  Branch: [dropdown]  |  |  [Tool: Edit] src/utils.ts:42        |   |
|  |                      |  |                                      |   |
|  |  +----------------+  |  |  The function now handles edge       |   |
|  |  | Task Override  |  |  |  cases for empty input...           |   |
|  |  | (optional)     |  |  |                                      |   |
|  |  +----------------+  |  +--------------------------------------+   |
|  |                      |                                             |
|  |  [> Test Once]       |  +--------------------------------------+   |
|  |  [x Cancel]          |  |         DIFF PREVIEW                 |   |
|  |                      |  |                                      |   |
|  |  [ ] Save to branch  |  |  src/utils.ts                        |   |
|  |  agent-test/...      |  |  - const old = "foo";                |   |
|  |                      |  |  + const new = "bar";                |   |
|  +----------------------+  +--------------------------------------+   |
|                                                                        |
+-----------------------------------------------------------------------+
```

### How It Works

1. User selects repo, branch, agent, runner type
2. Optionally overrides task description
3. Clicks "Test Once"
4. Backend creates a playground job (ephemeral, no DB card)
5. Runner picks up job, executes agent with **streaming output**:
   ```bash
   claude -p --output-format stream-json --include-partial-messages "..."
   ```
6. Runner reads stdout line-by-line, forwards JSON events to backend
7. Backend relays events to frontend via SSE (tokens, tool calls, reasoning)
8. On completion, backend captures diff from runner
9. Diff displayed in UI
10. If "Save to branch" checked, changes pushed to `agent-test/<name>-NNN`

### Streaming Architecture

Claude Code CLI supports `--output-format stream-json` which emits real-time JSON events:
- `content_block_delta` - streaming text tokens
- `tool_use` - tool calls (Edit, Bash, etc.)
- `message_stop` - completion

The runner captures these and forwards to the playground session, giving users visibility into agent reasoning as it happens.

### Backend Implementation

**Extend QueuedJob** in `job_queue.py`:
```python
@dataclass
class QueuedJob:
    # ... existing fields ...
    is_playground: bool = False  # True = ephemeral run, no card updates
    playground_session_id: str | None = None  # Links to SSE stream
    playground_save_branch: str | None = None  # If set, push changes here
```

**New Service**: `app/services/playground_service.py`
```python
class PlaygroundService:
    """
    Manages playground test runs using existing runner infrastructure.
    """

    # In-memory tracking (no DB persistence)
    active_runs: dict[str, PlaygroundRun]  # session_id -> run state

    async def start_test(
        self,
        repo_id: str,
        agent_id: str,
        runner_type: str,
        branch: str,
        task_override: str | None = None,
        save_branch: str | None = None,  # If set, save changes here
    ) -> str:
        """
        1. Load agent config
        2. Create QueuedJob with is_playground=True
        3. Enqueue job
        4. Return session_id for SSE streaming
        """

    async def stream_logs(self, session_id: str) -> AsyncGenerator:
        """SSE generator yielding runner logs as they arrive"""

    async def cancel_test(self, session_id: str) -> bool:
        """Cancel running playground job"""

    async def get_result(self, session_id: str) -> PlaygroundResult:
        """Get diff and status after completion"""
```

**New Router**: `app/routers/playground.py`
```python
@router.post("/repos/{repo_id}/playground/test")
async def start_test(repo_id: str, request: TestRequest) -> TestResponse:
    """Start a playground test, returns session_id"""

@router.get("/playground/{session_id}/stream")
async def stream_logs(session_id: str) -> EventSourceResponse:
    """SSE endpoint streaming runner logs"""

@router.post("/playground/{session_id}/cancel")
async def cancel_test(session_id: str) -> dict:
    """Cancel a running test"""

@router.get("/playground/{session_id}/result")
async def get_result(session_id: str) -> ResultResponse:
    """Get diff and completion status"""
```

**Runner Awareness**: Runners check `is_playground` flag:
- If `True`: Skip card status updates, stream logs to playground session
- If `playground_save_branch` set: Push changes to that branch
- Otherwise: Discard changes after capturing diff

### Frontend Implementation

**New Page**: `src/routes/PlaygroundPage.svelte`
- Route: `/playground`
- Tab alongside Board, Pipelines

**New Store**: `src/lib/stores/playground.ts`
```typescript
interface PlaygroundState {
  // Configuration
  repoId: string | null;
  agentId: string | null;
  runnerType: 'claude-code' | 'gemini';
  branch: string | null;
  taskOverride: string;
  saveToBranch: boolean;
  saveBranchName: string;

  // Execution state
  status: 'idle' | 'running' | 'cancelling' | 'complete' | 'error';
  sessionId: string | null;
  logs: string[];
  diff: string | null;
  filesChanged: string[];
  error: string | null;
}
```

**New Component**: `src/lib/components/Playground.svelte`
- Repo selector (existing component)
- Branch selector (existing component)
- Agent selector dropdown
- Runner type selector
- Task override textarea (optional)
- "Save to branch" checkbox + branch name input
- "Test Once" button
- Cancel button
- Log stream display (scrolling, like job logs)
- Diff preview panel

### Implementation Phases

#### Phase 11a: Foundation (MVP)
- [x] Add `is_playground`, `playground_session_id`, `playground_save_branch` to QueuedJob
- [x] Create `PlaygroundService` with `start_test()`, `stream_logs()`, `get_result()`
- [x] Create `/playground/*` REST endpoints
- [x] SSE streaming for runner logs
- [x] Runner checks `is_playground` flag, skips card updates if true (Claude only, Gemini TODO)

#### Phase 11b: Frontend - Test Once Mode
- [x] Add Playground tab to navigation
- [x] Create `PlaygroundPage.svelte`
- [x] Create `playground` store
- [x] Repo/branch/agent/runner selectors
- [x] Task override textarea
- [x] "Test Once" button
- [x] Log stream display (reuse LogViewer patterns)
- [x] Connect to SSE endpoint

#### Phase 11c: Diff & Save
- [x] Backend captures diff after runner completes
- [x] Diff preview panel in frontend
- [x] "Save to branch" checkbox
- [x] Branch name input with auto-generate (`agent-test/<agent>-NNN`)
- [x] Push changes to save branch on completion

#### Phase 11d: Cancellation
- [x] Backend: `cancel_test()` updates status (actual process kill TODO)
- [x] Frontend: Cancel button
- [x] Graceful cleanup
- [x] Status indicators

#### Phase 11e: Polish
- [ ] Keyboard shortcuts (Ctrl+Enter for Test Once)
- [ ] Log auto-scroll with pause on hover
- [ ] Copy diff to clipboard
- [ ] Branch cleanup UI (list/delete `agent-test/*` branches)
- [ ] Error handling and retry

### Future Enhancements (Not MVP)

- **Test Continuous mode**: Auto-run after typing stops (2.5s debounce)
- **Sample text input**: Quick prompt validation without full branch
- **File-specific input**: Test against single file
- **Side-by-side comparison**: Claude vs Gemini results
- **Run history**: Persist playground runs for comparison

### Files to Create

| File | Purpose |
|------|---------|
| `backend/app/services/playground_service.py` | Core service |
| `backend/app/routers/playground.py` | REST + SSE endpoints |
| `backend/app/schemas/playground.py` | Request/response models |
| `frontend/src/routes/PlaygroundPage.svelte` | Page component |
| `frontend/src/lib/stores/playground.ts` | State management |
| `frontend/src/lib/components/Playground.svelte` | Main UI |

### Files to Modify

| File | Changes |
|------|---------|
| `backend/app/services/job_queue.py` | Add playground fields to QueuedJob |
| `backend/app/main.py` | Mount playground router |
| `frontend/src/App.svelte` | Add Playground tab to nav |
| `frontend/src/lib/api/client.ts` | Add playground API methods |
| `frontend/src/lib/api/types.ts` | Add playground types |
| Runner code | Check `is_playground` flag, handle accordingly |

---

## Phase 12: Runner Architecture Refactor

> **Vision**: Runners become execution targets (machines with capabilities), not execution environments. Steps run in ephemeral containers with a shared workspace. Enables multi-image pipelines, hardware-specific runners, and future Kubernetes support.

### Current Problems

1. **Entrypoint divergence**: Claude and Gemini runners are ~1800 lines each, 95% duplicated, features diverging
2. **Docker-in-Docker required**: For `type: docker` steps, runners need Docker socket mounted (gross)
3. **Workspace conflicts**: Multiple pipelines on same runner can destroy each other's workspace
4. **No image flexibility**: Steps inherit the runner's environment, can't use custom images
5. **No hardware affinity**: Can't route steps to specific hardware (embedded devices, GPUs)

### Target Architecture

**Two execution modes:**

```
MODE 1: LOCAL (Backend has Docker access) - Zero latency

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Local          | --> Docker API
    | Executor       |     | Router         |     | Executor       |         |
    +----------------+     +----------------+     +----------------+         |
                                                                             v
                                                                    +----------------+
                                                                    | Step Container |
                                                                    +----------------+

MODE 2: REMOTE (Hardware/distributed runners) - WebSocket push, millisecond latency

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Remote         | <-- WebSocket
    | Executor       |     | Router         |     | Executor       |         |
    +----------------+     +----------------+     +----------------+         |
                                                                             |
            +----------------------------------------------------------------+
            | (push job immediately)
            v
    +----------------+     +----------------+     +----------------+
    | Runner Agent   |     | Runner Agent   |     | Runner Agent   |
    | (Docker host)  |     | (Raspberry Pi) |     | (GPU server)   |
    |                |     |                |     |                |
    | labels:        |     | labels:        |     | labels:        |
    |   arch=amd64   |     |   arch=arm64   |     |   arch=amd64   |
    |   type=docker  |     |   has=gpio     |     |   has=cuda     |
    |                |     |   has=camera   |     |                |
    | [Docker Orch.] |     | [Native Orch.] |     | [Docker Orch.] |
    +----------------+     +----------------+     +----------------+
```

### Key Design Decisions

**Event-driven, not polling**: Old runners polled every 5 seconds. New architecture:
- Local: Backend spawns containers directly (instant)
- Remote: Backend pushes jobs via WebSocket (milliseconds)

**OCI containers for everything** (except embedded hardware):
- System dependencies are container-level concerns
- Even "local" development uses Docker containers
- Native execution only for hardware that can't run Docker (GPIO, sensors)

### Core Concepts

**LocalExecutor**: Backend service that spawns containers directly via Docker SDK. No runner process, no polling - instant execution. This is the default for local development.

**RemoteExecutor**: Backend service that pushes jobs to connected runner agents via WebSocket. For remote Docker hosts, specialized hardware, distributed execution.

**Runner Agent**: Process that runs on remote machines, connects to backend via WebSocket, receives job assignments immediately. Has a local orchestrator (Docker or Native).

**Orchestrator**: How steps actually execute on a runner:
- `DockerOrchestrator`: Runs steps in containers (most common)
- `NativeOrchestrator`: Runs steps directly on host (embedded devices only)
- `KubernetesOrchestrator`: Runs steps as K8s Jobs (future)

**Workspace**: Per-pipeline-run working directory containing:
```
/workspace/
|-- repo/           # Git checkout
|-- home/           # Persistent $HOME (caches, .local/bin survive across steps)
+-- .control/       # Step config, logs, metadata
```

**Control Layer**: Thin wrapper in every step container handling heartbeat, log streaming, status reporting.

### Step Requirements (New Pipeline YAML)

```yaml
steps:
  - name: "Build firmware"
    type: docker
    config:
      image: "arm-toolchain:latest"
      command: "make firmware"
    requires:
      arch: arm64

  - name: "Run tests"
    type: docker
    config:
      image: "lazyaf-test-runner:latest"  # Pre-built with deps
      command: "pytest -v"

  - name: "Flash and test hardware"
    type: script
    config:
      command: "flash-firmware && run-hardware-tests"
    requires:
      has: gpio,camera
      runner_id: pi-workshop-1  # Pin to specific device
```

### Workspace Portability

| Scenario | Strategy |
|----------|----------|
| Same Docker host | Shared named volume (fast) |
| Different machines | Workspace tarball transfer |
| Kubernetes | PersistentVolumeClaim |

---

### Workspace Transfer Protocol

How workspaces move between steps, especially when steps run on different machines.

#### LocalExecutor (backend has Docker access)

Simplest case - all steps run on the same Docker host:

```
Step 1 container --> /workspace volume --> Step 2 container
                          |
                     Named volume persists
                     on Docker host
```

- Volume name: `lazyaf-ws-{pipeline_run_id}`
- All steps mount the same volume
- No transfer needed - it's already there
- Cleanup: Volume deleted when pipeline completes or after timeout

#### RemoteExecutor with DockerOrchestrator (remote Docker host)

Steps run on a remote machine with Docker. Same as LocalExecutor but on the remote host:

- Runner creates named volume on its local Docker
- All steps assigned to that runner share the volume
- **Affinity required**: Steps with `continue_in_context=true` MUST run on same runner

#### RemoteExecutor with NativeOrchestrator (embedded devices)

For devices that can't run Docker (Raspberry Pi GPIO work, bare metal, etc.):

- Workspace is a directory on the filesystem: `/var/lazyaf/workspaces/{pipeline_run_id}/`
- Runner manages the directory directly
- **Affinity required**: Steps with `continue_in_context=true` MUST run on same runner

#### Cross-Machine Workspace Transfer (Tarball Protocol)

When a step MUST run on a different machine than the previous step (different hardware requirements), the workspace is transferred as a tarball:

```
Runner A                      Backend                         Runner B
   |                             |                               |
   |-- step complete             |                               |
   |                             |                               |
   |-- POST /workspace-snapshot -->                              |
   |   (uploads tarball)         |                               |
   |                             |-- stores tarball              |
   |<-- 200 OK ------------------|                               |
   |                             |                               |
   |                             |   (step 2 assigned to B)      |
   |                             |                               |
   |                             |<-- GET /workspace-snapshot ---|
   |                             |   (B requests tarball)        |
   |                             |                               |
   |                             |--- tarball response --------->|
   |                             |                               |
   |                             |   (B extracts, runs step 2)   |
```

The runner's control layer handles upload/download - backend just stores the blob.

**API Endpoints:**
```
POST /api/pipeline-runs/{id}/workspace-snapshot
  Body: multipart/form-data with tarball
  Response: {snapshot_id, size_bytes}

GET /api/pipeline-runs/{id}/workspace-snapshot
  Response: application/gzip tarball

DELETE /api/pipeline-runs/{id}/workspace-snapshot
  (Called on pipeline completion)
```

**Tarball contents:**
```
workspace.tar.gz
|-- repo/              # Git checkout + uncommitted changes
|-- home/              # Persisted HOME (~/.local/bin, caches)
+-- .control/          # Step metadata, logs
```

**When transfer happens:**
- Only when next step has different `requires:` that forces a different runner
- NOT for normal `continue_in_context` on same runner (volume is faster)
- Backend detects machine boundary and triggers upload/download

**Size limits:**
- Default max: 500MB compressed
- Configurable per pipeline: `workspace_transfer_max_mb: 1000`
- Steps producing large artifacts should use dedicated artifact storage (future)

**Failure handling:**
- Upload fails: Step marked failed, pipeline can retry
- Download fails: Step marked failed, pipeline can retry
- Tarball corrupted: Checksum validation, retry from last good snapshot

---

### continue_in_context Semantics

The `continue_in_context` flag controls what persists between pipeline steps.

#### What IS Preserved

| Item | Location | Notes |
|------|----------|-------|
| Workspace files | `/workspace/repo/` | All files, tracked and untracked |
| Build artifacts | `/workspace/repo/` | node_modules, __pycache__, binaries |
| HOME directory | `/workspace/home/` | pip cache, npm cache, installed CLIs |
| Step logs | `/workspace/.lazyaf-context/` | Previous step outputs |
| Git state | `/workspace/repo/.git/` | Uncommitted changes preserved |

#### What is NOT Preserved

| Item | Why | Workaround |
|------|-----|------------|
| Container | Fresh container per step | Use same image if env matters |
| Environment variables | New process, new env | Set in step config or script |
| Running processes | Container dies between steps | Re-start in next step |
| Memory state | Fresh process | Serialize to file if needed |
| Network connections | Fresh container | Re-establish in next step |

#### Example: What Users Should Expect

```yaml
steps:
  - name: "Install deps"
    type: script
    config:
      command: |
        pip install pytest
        export MY_VAR=foo
        echo "done" > /tmp/marker
    continue_in_context: true

  - name: "Run tests"
    type: script
    config:
      command: |
        pytest -v           # Works - pytest in /workspace/home/.local/bin
        echo $MY_VAR        # Empty - env var not preserved
        cat /tmp/marker     # Fails - /tmp is container-local, not in workspace
```

#### Different Images Across Steps

Each step runs in its specified image. The workspace volume is mounted regardless:

```yaml
steps:
  - name: "Build Go binary"
    type: docker
    config:
      image: golang:1.21
      command: go build -o /workspace/repo/myapp
    continue_in_context: true

  - name: "Test with Python"
    type: docker
    config:
      image: python:3.12
      command: python /workspace/repo/integration_test.py
    # /workspace/repo/myapp binary is available!
```

This is intentional - allows heterogeneous pipelines. Caveat: architecture must match (can't build ARM binary on amd64 and run it).

#### Runner Affinity

> **Implementation Status** (as of Phase 11):
> - ✅ Basic affinity: `required_runner_id` enforced in job_queue.dequeue()
> - ✅ `continue_in_context` / `is_continuation` flags working in both runners
> - ✅ `previous_runner_id` passed between pipeline steps
> - ❌ `affinity_timeout` NOT implemented (jobs wait indefinitely for required runner)
> - ❌ No tests for affinity scenarios

When `continue_in_context: true`, the next step REQUIRES the same runner:

```
Step 1 (runner A, continue_in_context=true)
    |
    v
Step 2 (MUST be runner A - has the workspace volume)
```

**Affinity failure handling:**

| Scenario | Behavior |
|----------|----------|
| Runner A still connected | Step 2 assigned to A immediately |
| Runner A temporarily disconnected | Wait up to `affinity_timeout` (default 5 min) |
| Runner A dead (heartbeat timeout) | Pipeline FAILS with clear error |
| Runner A reconnects after timeout | Too late - pipeline already failed |

**Why not fall back to another runner?**
- Workspace state would be inconsistent
- Silent fallback causes confusing failures
- Explicit failure is better than subtle bugs

**Configurable timeout:**
```yaml
pipeline:
  affinity_timeout: 300  # seconds, default 5 minutes
```

#### Forcing Fresh Workspace

To explicitly NOT continue from previous step:

```yaml
steps:
  - name: "Build"
    continue_in_context: true  # Keep workspace

  - name: "Test in clean env"
    continue_in_context: false  # This step gets fresh clone
    # Previous workspace is discarded, fresh git clone
```

---

### Lifecycle State Machines

All state transitions are guarded by locks and idempotency keys to prevent race conditions and duplicate executions.

#### Centralized Locking (Backend is Source of Truth)

**Critical design decision**: All locking and state management happens in the backend database. Runners are stateless clients - they never hold locks or make decisions about state transitions.

**Two execution paths, same locking model:**

| Mode | How Steps Execute | Locking |
|------|-------------------|---------|
| **LocalExecutor** | Backend spawns containers directly via Docker SDK | DB row locks; container death detected via Docker API/exit codes |
| **RemoteExecutor** | Backend pushes jobs to runners via WebSocket | DB row locks + heartbeat timeout for runner death detection |

Both modes use the database as the single source of truth. The difference is how failure is detected:
- LocalExecutor: Docker SDK tells us immediately when a container dies
- RemoteExecutor: Heartbeat timeout (30s) tells us when a runner is unreachable

**RemoteExecutor flow (push-based, not polling):**

```
+-------------------+          +-------------------+          +-------------------+
|  Runner Agent A   |          |      BACKEND      |          |  Runner Agent B   |
|   (stateless)     |          | (source of truth) |          |   (stateless)     |
+-------------------+          +-------------------+          +-------------------+
        |                              |                              |
        |--- WebSocket connect ------->|                              |
        |--- register {labels} ------->|                              |
        |                              |-- DB: find pending step ---->|
        |                              |-- DB: SELECT FOR UPDATE ---->|
        |                              |<- lock acquired, assign -----|
        |<-- push: "execute step X" ---|                              |
        |--- ACK -------------------->|                              |
        |                              |                              |
        |--- heartbeat --------------->|                              |
        |--- heartbeat --------------->|                              |
        |                              |                              |
        |     (network dies)           |                              |
        |       X    X    X            |                              |
        |                              |-- 30s timeout: no heartbeat -|
        |                              |-- DB: mark step pending ---->|
        |                              |                              |
        |                              |<--- WebSocket connect -------|
        |                              |<--- register {labels} -------|
        |                              |-- DB: find pending step ---->|
        |                              |-- push: "execute step X" --->|
```

**Why this works:**
- Backend holds all state in PostgreSQL/SQLite with ACID guarantees
- `SELECT FOR UPDATE` prevents double-assignment race conditions
- Heartbeat timeout (30s) detects dead runners, releases their work
- Runners reconnecting check if their work was reassigned before resuming
- Idempotency keys prevent duplicate execution even with retries

**What runners do:**
- Connect via WebSocket, receive job assignments (pushed, not pulled)
- Send heartbeats every 10s to prove liveness
- Report status transitions (preparing -> running -> completed)
- Stream logs back to backend
- **Never** decide on their own whether to take or release work

**What backend does:**
- All assignment decisions (who gets what step)
- All state transitions (step status, workspace status)
- All lock management (row-level DB locks, not Redis)
- Heartbeat monitoring and dead runner detection
- Work re-queuing when runners die

**Locking implementation:**
- **Step assignment**: `SELECT ... FOR UPDATE` on step row
- **Idempotency**: Unique constraint on `execution_key` column
- **Workspace access**: PostgreSQL advisory locks (SQLite: file locking in dev)
- **No Redis required**: All coordination through the primary database

**Network partition handling:**
| Scenario | Backend Action |
|----------|----------------|
| Runner disconnects mid-job | Wait for heartbeat timeout (30s), return step to `pending` |
| Runner reconnects after timeout | Check if step was reassigned; if yes, abort local work |
| Runner reconnects before timeout | Continue normally, step still assigned to this runner |
| Backend restarts | Runners reconnect, re-register; backend resumes from DB state |
| Double-completion (race) | Idempotency key rejects second completion |
| LocalExecutor container dies | Docker SDK notifies backend immediately; step marked `failed` |

> **Note**: Current implementation uses 90s heartbeat timeout (generous for polling).
> WebSocket push model reduces this to 30s for faster failure detection.

**Debug mode integration**: Debug sessions (Phase 12.7) have extended timeouts (1-4 hours)
and their own state machine. When a step is at a debug breakpoint, the normal heartbeat
timeout is suspended - the debug session timeout applies instead.

#### Step Lifecycle

```
[pending] --> [assigned] --> [preparing] --> [running] --> [completing]
                                                 |              |
                                                 | exit_0       | finalized
                                                 v              v
                                            [timeout]      [completed]
                                                 |
                                                 v
[cancelled] <-- cancel (any state) ------- [failed]
```

| State | Description |
|-------|-------------|
| `pending` | Created, waiting for executor |
| `assigned` | Assigned to runner, awaiting ACK (remote only) |
| `preparing` | Pulling image, setting up workspace |
| `running` | Container executing |
| `completing` | Processing results |
| `completed` | Exit code 0, success |
| `failed` | Non-zero exit or exception |
| `cancelled` | User cancelled |
| `timeout` | Exceeded time limit |

**Idempotency**: Each step execution has an `execution_key = "{pipeline_run_id}:{step_index}:{attempt}"`. Duplicate requests return existing execution.

#### Workspace Lifecycle

```
[creating] --> [ready] <--> [in_use] --> [cleaning] --> [destroyed]
     |                          |
     | create_failed            | audit_detects_orphan
     v                          v
 [failed]                  [orphaned] --> manual cleanup --> destroyed
```

| State | Description |
|-------|-------------|
| `creating` | Volume being created, repo cloning |
| `ready` | Available, no active steps |
| `in_use` | Step(s) currently executing |
| `cleaning` | Pipeline done, destroying volume |
| `destroyed` | Cleaned up (terminal) |
| `orphaned` | Lost track, needs manual cleanup |
| `failed` | Creation failed |

**Locking**:
- Exclusive lock for creation/cleanup
- Shared lock for step execution (allows parallel steps)
- Use count tracks concurrent usage

#### Pipeline Run Lifecycle

```
[pending] --> [preparing] --> [running] --> [completing] --> [completed]
                   |              |
                   | prep_failed  | step_failed
                   v              v
              [failed] <----- [failed]
                   ^
                   | timeout
             [cancelled] <-- cancel (any non-terminal)
```

**Exactly-once step execution**: Pipeline executor skips steps with completed `execution_key`.

**Trigger deduplication**: Triggers have a `trigger_key` (e.g., `push:{repo}:{sha}`). Duplicates within 1 hour are ignored.

#### Runner Lifecycle (Remote Only)

```
[disconnected] --> [connecting] --> [idle] --> [assigned] --> [busy]
       ^                                            |            |
       |                                            | ack_timeout|
       |                                            v            |
       |                                        [dead] <---------+
       |                                            |
       +------- heartbeat_timeout -----------------+
       |                                            |
       +------------- reconnect -------------------+
```

| State | Description |
|-------|-------------|
| `disconnected` | No WebSocket connection |
| `connecting` | WebSocket open, registration pending |
| `idle` | Ready to accept jobs |
| `assigned` | Job sent, awaiting ACK |
| `busy` | Executing step |
| `dead` | Heartbeat timeout, presumed crashed |

**Job recovery**: When runner dies mid-job, step is re-queued if still in `running` state (prevents duplicate if completion was lost).

#### Synchronization Requirements

| Resource | Lock Type | Implementation |
|----------|-----------|----------------|
| Step assignment | Row lock | `SELECT FOR UPDATE` on step row |
| Step execution | Idempotency key | Unique constraint on `execution_key` |
| Workspace access | Shared/Exclusive | PostgreSQL advisory locks (SQLite file lock in dev) |
| Pipeline execution | Exclusive | Row lock on pipeline_run + single executor process |
| Trigger dedup | Time-windowed key | Unique constraint on `trigger_key` + created_at window |

> **No Redis required**: All synchronization uses the primary database. This simplifies
> deployment and eliminates a distributed systems failure mode.

#### Crash Recovery

On backend restart:
1. **Steps**: Find non-terminal steps, reattach to running containers or re-queue
2. **Pipelines**: Resume execution (idempotent - skips completed steps)
3. **Workspaces**: Audit for orphans, cleanup stale volumes
4. **Runners**: Mark as dead, wait for reconnection

---

### Phase 12 Prerequisites: Test Infrastructure

> **Goal**: Establish platform-level test hooks and fixtures BEFORE any Phase 12 implementation begins. Tests define interfaces and expected behavior - they are written FIRST, not as an afterthought.

**This must exist BEFORE any Phase 12 work begins.**

#### Test Fixtures (Create First)

- [ ] `tdd/conftest.py` with shared pytest fixtures:
  - `docker_client` - Connected Docker SDK client (skip tests if unavailable)
  - `test_database` - Fresh SQLite in-memory or temp file per test
  - `async_session` - Async SQLAlchemy session factory
  - `mock_websocket` - Fake WebSocket for protocol testing
  - `temp_workspace` - Creates and cleans up temp directories

#### Platform-Level Test Hooks

- [ ] Docker manipulation helpers (`tdd/shared/docker_helpers.py`):
  - `spawn_test_container(image, command)` - Create container, return handle
  - `kill_container(container_id)` - Force kill
  - `pause_container(container_id)` - Simulate hang
  - `disconnect_network(container_id)` - Network partition
- [ ] Process control helpers (`tdd/shared/process_helpers.py`):
  - `kill_process(pid)` - Simulate crash
  - `send_signal(pid, signal)` - SIGTERM, SIGKILL, SIGSTOP
- [ ] Time manipulation (`tdd/shared/time_helpers.py`):
  - `freeze_time(timestamp)` - For timeout testing
  - `advance_time(seconds)` - Fast-forward for heartbeat tests

#### Mock Infrastructure

- [ ] `MockDockerClient` - Fake Docker SDK for unit tests (no real Docker needed)
- [ ] `MockWebSocket` - Fake WebSocket for protocol tests
- [ ] `TestDatabase` - Fresh database per test with rollback
- [ ] `MockRunner` - Simulates runner behavior for RemoteExecutor tests

#### Chaos Test Infrastructure

- [ ] `ChaosController` class (`tdd/shared/chaos.py`):
  - `inject_failure(type, target, duration)` - Programmatic failure injection
  - Failure types: network_partition, process_kill, disk_full, slow_io
- [ ] Recovery verification helpers:
  - `wait_for_state(entity, expected_state, timeout)`
  - `assert_eventually(condition, timeout, interval)`

**Outcome**: All Phase 12 sub-phases can write tests immediately without infrastructure blockers.

---

### Phase 12.0: Unify Runner Entrypoints
**Goal**: Fix immediate pain, unblock future phases

The current entrypoints are ~1800 lines each with 95% duplication. This phase extracts common code into a shared package.

#### Tests First (Define Contracts)

Write these tests BEFORE implementing the shared modules:

**test_git_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_clone_creates_repo_at_path` | `clone(url, path) -> None` raises on failure |
| `test_checkout_branch_switches` | `checkout(path, branch) -> None` |
| `test_get_current_sha_returns_string` | `get_sha(path) -> str` |
| `test_clone_handles_auth_failure` | Returns specific exception type |

**test_context_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_create_context_dir_structure` | `.lazyaf-context/` has expected subdirs |
| `test_write_context_file_creates_json` | Files are valid JSON |
| `test_read_context_returns_parsed` | Read matches written |

**test_job_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_heartbeat_sends_to_backend` | `send_heartbeat(job_id)` hits correct endpoint |
| `test_heartbeat_timeout_raises` | Raises after N seconds |
| `test_status_report_formats_correctly` | Status payload structure |

- [ ] Write `test_git_helpers.py` (defines interface)
- [ ] Write `test_context_helpers.py` (defines interface)
- [ ] Write `test_job_helpers.py` (defines interface)

#### Implementation (Make Tests Pass)

- [ ] Create `runner-common/` package with shared utilities
  - `git_helpers.py` - clone, branch, push, commit operations (pass tests)
  - `context_helpers.py` - `.lazyaf-context/` management (pass tests)
  - `job_helpers.py` - heartbeat, logging, status reporting (pass tests)
  - `test_helpers.py` - test detection and execution
- [ ] Create unified entrypoint that dispatches by agent type
- [ ] Reduce Claude/Gemini-specific code to ~100 lines each (just CLI invocation)

#### Integration Validation

- [ ] `test_unified_entrypoint_dispatches.py`:
  - Claude agent type routes correctly
  - Gemini agent type routes correctly
  - Unknown agent type fails with clear error
- [ ] `test_existing_pipelines_still_work.py`:
  - Run actual pipeline with unified entrypoint
  - Compare output to baseline

#### Done Criteria

- [ ] All `test_*_helpers.py` tests pass
- [ ] Integration tests pass with both Claude and Gemini runners
- [ ] No regression in existing pipeline behavior

**Effort**: 2-3 days
**Risk**: Low
**Outcome**: Maintainable entrypoints, foundation for new architecture

---

### Phase 12.1: LocalExecutor + Step State Machine
**Goal**: Instant step execution with proper state management

**Status**: COMPLETE (94 tests passing)

The fast path - backend spawns containers directly, with full lifecycle tracking.

#### Tests First (Define Contracts) ✅

**test_step_state_machine.py** - 32 tests
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_assigned_valid` | Transition allowed |
| `test_pending_to_running_invalid` | Must go through assigned first |
| `test_running_to_completed_on_exit_0` | Exit code 0 = success |
| `test_running_to_failed_on_nonzero` | Exit code != 0 = failure |
| `test_running_to_timeout_on_deadline` | Timeout = specific state |
| `test_cancel_from_any_state` | Cancel always works |
| `test_completed_is_terminal` | No transitions from completed |
| `test_transition_records_timestamp` | State changes have timestamps |

**test_idempotency_keys.py** - 23 tests
| Test | Defines Contract |
|------|------------------|
| `test_execution_key_format` | Format: `{run_id}:{step}:{attempt}` |
| `test_same_key_returns_existing` | Duplicate request = same execution |
| `test_different_attempt_new_execution` | Retry = new execution |

**test_local_executor_contract.py** - 39 tests
| Test | Defines Contract |
|------|------------------|
| `test_execute_step_returns_generator` | `execute_step() -> AsyncGenerator` |
| `test_execute_step_idempotent` | Same key = same result |
| `test_execute_step_spawns_container` | Container created with correct image |
| `test_execute_step_mounts_workspace` | Volume mounted at /workspace |
| `test_execute_step_streams_logs` | Generator yields log lines |
| `test_timeout_kills_container` | Container killed after timeout |
| `test_crash_detection_fails_step` | Container crash = step failed |

- [x] Write `test_step_state_machine.py` (defines state transitions)
- [x] Write `test_idempotency_keys.py` (defines idempotency contract)
- [x] Write `test_local_executor_contract.py` (defines executor interface)

#### Database Migration ✅

- [x] Create `step_executions` table with Alembic migration
  ```python
  class StepExecution(Base):
      __tablename__ = "step_executions"
      id: str  # UUID
      execution_key: str  # "{pipeline_run_id}:{step_index}:{attempt}" - UNIQUE constraint
      step_run_id: str  # FK to step_runs
      status: str  # pending, preparing, running, completing, completed, failed, cancelled
      runner_id: str | None  # Which runner is executing (remote only)
      container_id: str | None  # Docker container ID (local only)
      exit_code: int | None
      started_at: datetime | None
      completed_at: datetime | None
  ```
- [x] Add unique index on `execution_key` for idempotency

#### Implementation (Make Tests Pass) ✅

- [x] Implement Step state machine (`step_state.py`) - 32 tests pass
- [x] Implement Idempotency tracking (`idempotency.py`) - 23 tests pass
- [x] Create `LocalExecutor` service (`local_executor.py`) - 39 tests pass
- [x] Add Docker SDK (`docker` + `aiodocker`) to backend dependencies
- [x] Timeout handling with automatic container kill
- [x] Container crash detection and proper state transition to `failed`
- [x] Real-time log streaming from container to pipeline executor

#### Files Created

| File | Purpose | Tests |
|------|---------|-------|
| `backend/app/services/execution/step_state.py` | Step lifecycle state machine | 32 |
| `backend/app/services/execution/idempotency.py` | Execution idempotency tracking | 23 |
| `backend/app/services/execution/local_executor.py` | Local Docker execution | 39 |
| `backend/app/models/step_execution.py` | StepExecution DB model | - |
| `backend/alembic/versions/5b8e88c7c2ce_add_step_executions_table.py` | DB migration | - |

#### Integration Validation (Deferred to Phase 12.6)

- [ ] `test_local_executor_real_docker.py` (requires Docker):
  - Actually spawns container
  - Actually streams logs
  - Actually detects exit codes
- [ ] `test_local_executor_recovery.py`:
  - Kill backend mid-execution
  - Restart backend
  - Verify orphaned steps are failed/reattached

#### Chaos Tests (Deferred to Phase 12.6)

- [ ] `test_container_oom_handled.py` - Container OOM = step failed
- [ ] `test_docker_unavailable_graceful.py` - Docker down = clear error

#### Done Criteria

- [x] All state machine unit tests pass (32/32)
- [x] All idempotency tests pass (23/23)
- [x] LocalExecutor contract tests pass (39/39)
- [ ] Integration tests pass with real Docker (deferred)
- [ ] Recovery test passes (deferred)

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Local dev is instant with proper state tracking and crash recovery

---

### Phase 12.2: Workspace State Machine & Pipeline Integration
**Goal**: Proper workspace lifecycle with locking and cleanup

**Status**: COMPLETE (272 tests passing, including integration tests)

#### Tests First (Define Contracts) ✅

**test_workspace_state_machine.py** - 42 tests
| Test | Defines Contract |
|------|------------------|
| `test_creating_to_ready_on_success` | Volume created = ready |
| `test_creating_to_failed_on_error` | Volume creation fails = failed |
| `test_ready_to_in_use_increments_count` | use_count tracks concurrent access |
| `test_in_use_to_ready_decrements_count` | Step completes = decrement |
| `test_cleaning_requires_zero_use_count` | Can't clean while in use |
| `test_orphaned_detection` | Workspace with no pipeline = orphaned |

**test_workspace_locking.py** - 24 tests
| Test | Defines Contract |
|------|------------------|
| `test_exclusive_lock_for_create` | Only one creator |
| `test_exclusive_lock_for_cleanup` | Only one cleaner |
| `test_shared_lock_for_execution` | Multiple steps can run |
| `test_lock_timeout_returns_false` | Don't block forever |

**test_execution_router.py** - 31 tests
| Test | Defines Contract |
|------|------------------|
| `test_routes_to_local_when_no_requirements` | Default = LocalExecutor |
| `test_routes_to_remote_when_hardware_required` | `requires: {has: gpio}` = remote |
| `test_returns_executor_handle` | Caller gets async generator |

**test_pipeline_state_machine.py** - 53 tests
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_preparing` | Pipeline starts |
| `test_preparing_to_running` | Workspace ready |
| `test_running_to_completing` | All steps done |
| `test_completing_to_completed` | Cleanup done |
| `test_step_failure_fails_pipeline` | One step fails = pipeline fails |

**test_trigger_deduplication.py** - 19 tests
| Test | Defines Contract |
|------|------------------|
| `test_same_trigger_key_within_window_ignored` | Duplicate = no new run |
| `test_same_trigger_key_after_window_allowed` | Window expired = new run |
| `test_trigger_key_format` | Format: `{type}:{repo}:{ref}` |

- [x] Write `test_workspace_state_machine.py` (defines workspace lifecycle)
- [x] Write `test_workspace_locking.py` (defines locking semantics)
- [x] Write `test_execution_router.py` (defines routing contract)
- [x] Write `test_pipeline_state_machine.py` (defines pipeline lifecycle)
- [x] Write `test_trigger_deduplication.py` (defines dedup contract)

#### Implementation (Make Tests Pass)

- [x] Implement Workspace state machine (`workspace_state.py`) - 42 tests pass
- [x] Create `Workspace` model with state and use_count (DB persistence)
  ```python
  class Workspace:
      id: str  # "lazyaf-ws-{pipeline_run_id}"
      status: WorkspaceStatus
      use_count: int  # For concurrent step access
      pipeline_run_id: str
  ```
- [x] Implement workspace locking (`workspace_locking.py`) - 24 tests pass
- [x] Idempotent workspace creation (`get_or_create_workspace`)
- [x] Create `ExecutionRouter` (`router.py`) - 31 tests pass
- [x] Update `pipeline_executor.py` to use workspace lifecycle
- [x] Implement pipeline state machine (`pipeline_state.py`) - 53 tests pass
- [x] Implement trigger deduplication (`trigger_dedup.py`) - 19 tests pass
- [x] Workspace cleanup on pipeline completion
- [x] Orphan detection: periodic audit finds abandoned workspaces

#### Files Created

| File | Purpose | Tests |
|------|---------|-------|
| `backend/app/services/execution/workspace_state.py` | Workspace lifecycle state machine | 42 |
| `backend/app/services/execution/workspace_locking.py` | Shared/exclusive workspace locks | 24 |
| `backend/app/services/execution/router.py` | Routes steps to Local/Remote executor | 31 |
| `backend/app/services/execution/pipeline_state.py` | Pipeline run lifecycle state machine | 53 |
| `backend/app/services/execution/trigger_dedup.py` | Prevents duplicate pipeline triggers | 19 |
| `backend/app/models/workspace.py` | Workspace DB model | - |
| `backend/app/services/workspace_service.py` | Workspace lifecycle service | - |
| `tdd/integration/execution/test_workspace_integration.py` | Integration tests | 9 |

#### Integration Validation ✅

- [x] `test_workspace_integration.py`:
  - Workspace volume creation
  - State machine lifecycle
  - Concurrent shared locks
  - Multi-step workspace sharing
  - Orphan detection
  - Cleanup removes volume

#### Chaos Tests (Deferred to Phase 12.6)

- [ ] `test_concurrent_workspace_access.py` - Multiple steps, same workspace
- [ ] `test_orphan_workspace_recovery.py` - Backend dies, workspace orphaned, recovered on restart

#### Done Criteria

- [x] Workspace state machine tests pass (42/42)
- [x] Locking tests pass (24/24)
- [x] ExecutionRouter tests pass (31/31)
- [x] Pipeline state machine tests pass (53/53)
- [x] Trigger deduplication tests pass (19/19)
- [x] Workspace integration tests pass (9/9)
- [x] Workspace DB model created with migration
- [x] Pipeline executor uses workspace lifecycle

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Robust workspace lifecycle, exactly-once execution, no orphaned resources

---

### Phase 12.3: Control Layer & Step Images
**Goal**: Proper container communication and base images

**Status**: COMPLETE (302 tests passing)

#### Tests First (Define Contracts) ✅

**test_step_api_endpoints.py** - 21 tests
| Test | Defines Contract |
|------|------------------|
| `test_post_status_updates_step` | Status endpoint updates DB |
| `test_post_logs_appends` | Logs endpoint appends to step logs |
| `test_post_heartbeat_updates_timestamp` | Heartbeat extends timeout |
| `test_auth_required` | Endpoints require step token |
| `test_token_generation_validation` | Token service generates/validates tokens |

**test_control_layer_protocol.py** - 18 tests
| Test | Defines Contract |
|------|------------------|
| `test_reads_config_from_control_dir` | Config at `/workspace/.control/step_config.json` |
| `test_reports_status_on_start` | POST to `/api/steps/{id}/status` with `running` |
| `test_reports_status_on_complete` | POST with `completed` and exit code |
| `test_streams_logs_to_backend` | POST to `/api/steps/{id}/logs` |
| `test_heartbeat_during_execution` | POST to `/api/steps/{id}/heartbeat` periodically |
| `test_handles_backend_unavailable` | Retries with exponential backoff |

- [x] Write `test_step_api_endpoints.py` (defines API contract) - 21 tests
- [x] Write `test_control_layer_protocol.py` (defines control layer contract) - 18 tests

#### Implementation (Make Tests Pass) ✅

- [x] Create API endpoints - 21 tests pass
  - `POST /api/steps/{step_id}/status` - Update step status
  - `POST /api/steps/{step_id}/logs` - Append log lines (batched)
  - `POST /api/steps/{step_id}/heartbeat` - Extend timeout, prove liveness
  - Token-based authentication via `step_token.py`
- [x] Create control layer scripts - 18 tests pass
  - `config.py` - Parses `/workspace/.control/step_config.json`
  - `backend_client.py` - HTTP client with retry logic (exponential backoff)
  - `heartbeat.py` - Background heartbeat thread
  - `executor.py` - Command execution with log streaming
  - `run.py` - Main entrypoint
- [x] Create base image (`lazyaf-base`)
  - Python 3.12-slim + git + curl + sudo
  - Control layer at `/control/`
  - `ENTRYPOINT ["python", "/control/run.py"]`
  - `HOME=/workspace/home` for cache persistence
  - Non-root `lazyaf` user with sudo access
- [x] Create agent images inheriting from base
  - `lazyaf-claude`: base + Node.js 20 + Claude Code CLI
  - `lazyaf-gemini`: base + Gemini SDK
- [x] Update `LocalExecutor` with control layer support
  - Added `use_control_layer`, `backend_url`, `heartbeat_interval` to `ExecutionConfig`
  - Added `_prepare_control_directory()` to create `.control/step_config.json`
  - Updated `_create_container()` for control layer mode

#### Files Created

| File | Purpose | Tests |
|------|---------|-------|
| `backend/app/schemas/steps.py` | Pydantic schemas for step API | - |
| `backend/app/services/execution/step_token.py` | Token generation/validation | - |
| `backend/app/routers/steps.py` | Step API endpoints | 21 |
| `images/base/Dockerfile` | Base image with control layer | - |
| `images/base/requirements.txt` | Control layer dependencies | - |
| `images/base/control/__init__.py` | Control layer package | - |
| `images/base/control/config.py` | Step config parser | - |
| `images/base/control/backend_client.py` | HTTP client with retry | - |
| `images/base/control/heartbeat.py` | Background heartbeat | - |
| `images/base/control/executor.py` | Command execution | - |
| `images/base/control/run.py` | Main entrypoint | 18 |
| `images/claude/Dockerfile` | Claude agent image | - |
| `images/gemini/Dockerfile` | Gemini agent image | - |
| `tdd/unit/execution/test_step_api_endpoints.py` | API contract tests | 21 |
| `tdd/unit/execution/test_control_layer_protocol.py` | Protocol tests | 18 |

#### Files Modified

| File | Changes |
|------|---------|
| `backend/app/main.py` | Mount steps router |
| `backend/app/services/execution/local_executor.py` | Control layer support in ExecutionConfig |

#### Integration Validation (Deferred to Phase 12.6)

- [ ] `test_agent_script_agent_pipeline.py`:
  - Agent step installs tool via pip
  - Script step uses that tool
  - Agent step sees script output
- [ ] `test_control_layer_reports_failure.py`:
  - Command exits non-zero
  - Control layer reports failed status
  - Backend marks step failed

#### Done Criteria

- [x] API endpoint tests pass (21/21)
- [x] Control layer protocol tests pass (18/18)
- [x] Base image Dockerfile created
- [x] Agent image Dockerfiles created
- [x] LocalExecutor updated with control layer support
- [ ] Integration tests with real Docker (deferred to Phase 12.6)

**Effort**: 1-1.5 weeks
**Risk**: Medium
**Outcome**: Steps run in proper containers with backend communication

---

### Phase 12.4: Migrate Script/Docker Steps
**Goal**: All non-agent steps use new architecture

**Status**: COMPLETE (302+ tests passing)

#### Tests First (Define Contracts) ✅

**test_step_routing_contract.py** - 15+ tests
| Test | Defines Contract |
|------|------------------|
| `test_script_step_routes_to_local` | type=script uses LocalExecutor |
| `test_docker_step_routes_to_local` | type=docker uses LocalExecutor |
| `test_custom_image_preserved` | `image: foo:bar` preserved in routing |
| `test_script_with_hardware_goes_remote` | Hardware requirements route remotely |
| `test_decision_includes_step_type` | RoutingDecision includes step info |

**test_migration_compatibility.py** - 15+ tests
| Test | Defines Contract |
|------|------------------|
| `test_script_step_without_image` | Old format uses default image |
| `test_docker_step_existing_format` | Existing docker steps work |
| `test_step_with_environment_variables` | Environment preserved |
| `test_command_wrapping` | Script commands wrapped in bash |

- [x] Write `test_step_routing_contract.py` (defines routing behavior)
- [x] Write `test_migration_compatibility.py` (defines backward compat)

#### Implementation (Make Tests Pass) ✅

- [x] Pipeline executor routes script/docker steps through LocalExecutor
- [x] Created `config_builder.py` for building ExecutionConfig from step_config
- [x] Added `step_type` and `step_config` to RoutingDecision
- [x] Steps can specify custom images in pipeline YAML
- [x] Created `test-suite-v2.yaml` using pre-built image
- [x] Created `lazyaf-test-runner` Dockerfile with uv + deps

#### Files Created

| File | Purpose |
|------|---------|
| `backend/app/services/execution/config_builder.py` | Builds ExecutionConfig from step_type/step_config |
| `images/test-runner/Dockerfile` | Pre-built image with uv, pytest, Node.js |
| `.lazyaf/pipelines/test-suite-v2.yaml` | Example pipeline using pre-built image |
| `tdd/unit/execution/test_step_routing_contract.py` | Routing contract tests |
| `tdd/unit/execution/test_migration_compatibility.py` | Backward compatibility tests |

#### Files Modified

| File | Changes |
|------|---------|
| `backend/app/services/execution/router.py` | Added step_type/step_config to RoutingDecision |
| `backend/app/services/execution/local_executor.py` | Support for Docker volume names |
| `backend/app/services/pipeline_executor.py` | LocalExecutor integration, routing logic |

#### Configuration

- **Environment Variable**: `LAZYAF_USE_LOCAL_EXECUTOR=1` enables local execution
- Default is disabled (safe for tests without Docker)
- When enabled, script/docker steps bypass job queue for instant execution

#### Done Criteria

- [x] Routing tests pass
- [x] Backward compatibility tests pass
- [x] All existing tests pass (302+)

**Effort**: 1 week
**Risk**: Medium (migration path)
**Outcome**: Script/docker steps can use LocalExecutor for instant execution

---

### Phase 12.5: Migrate Agent Steps
**Goal**: Agent steps also use new architecture

**Status**: COMPLETE (48+ tests passing)

#### Tests First (Define Contracts) ✅

**test_agent_step_contract.py** - 22 tests
| Test | Defines Contract |
|------|------------------|
| `test_agent_step_routes_to_local_executor` | Agent routes locally by default |
| `test_agent_step_uses_correct_image_claude` | `lazyaf-claude:latest` used |
| `test_agent_step_uses_correct_image_gemini` | `lazyaf-gemini:latest` used |
| `test_agent_wrapper_invokes_wrapper` | Wrapper script invoked, not direct CLI |
| `test_agent_config_includes_*` | Title, description, model, agent files, etc. |
| `test_claude_agent_has_anthropic_api_key` | API key injected |
| `test_gemini_agent_has_gemini_api_key` | API key injected |

**test_polling_removal.py** - 10 tests
| Test | Defines Contract |
|------|------------------|
| `test_agent_step_bypasses_job_queue` | No job_queue.enqueue() for local agents |
| `test_runners_not_registered_for_local_execution` | No runner registration needed |
| `test_no_heartbeat_required_from_runners` | LocalExecutor handles liveness |
| `test_job_queue_still_works_for_legacy` | Backward compat preserved |

**test_card_local_execution.py** - 16 tests
| Test | Defines Contract |
|------|------------------|
| `test_agent_card_routes_to_local_when_enabled` | Cards use LocalExecutor |
| `test_card_status_updates` | Status transitions work |
| `test_websocket_broadcasts` | Real-time updates via WebSocket |

- [x] Write `test_agent_step_contract.py` (defines agent execution)
- [x] Write `test_polling_removal.py` (defines what's removed)
- [x] Write `test_card_local_execution.py` (defines card execution)

#### Implementation (Make Tests Pass) ✅

- [x] Created `agent_wrapper.py` - Handles git clone, branch, CLI invocation, commit/push
- [x] Updated `config_builder.py` - Wrapper invocation, API key injection
- [x] Updated `local_executor.py` - Agent config in control directory
- [x] Updated `pipeline_executor.py` - Routes agent steps through LocalExecutor
- [x] Created `card_executor.py` - Local execution for standalone cards
- [x] Updated `cards.py` router - Routes agent cards through LocalExecutor

#### Files Created

| File | Purpose | Tests |
|------|---------|-------|
| `images/base/control/agent_wrapper.py` | Agent execution wrapper (~350 lines) | - |
| `backend/app/services/card_executor.py` | Local card execution helper | - |
| `tdd/unit/execution/test_agent_step_contract.py` | Agent step tests | 22 |
| `tdd/unit/execution/test_polling_removal.py` | Polling removal tests | 10 |
| `tdd/unit/execution/test_card_local_execution.py` | Card execution tests | 16 |

#### Files Modified

| File | Changes |
|------|---------|
| `backend/app/services/execution/config_builder.py` | Wrapper command, API key injection |
| `backend/app/services/execution/local_executor.py` | Agent config in control directory |
| `backend/app/services/pipeline_executor.py` | Route agents through LocalExecutor |
| `backend/app/routers/cards.py` | Route agent cards through LocalExecutor |

#### Configuration

- **Environment Variable**: `LAZYAF_USE_LOCAL_EXECUTOR=1` enables local execution
- Agent steps now bypass job queue when enabled
- Both pipeline steps AND standalone cards use LocalExecutor
- Continuations still use job queue (preserved for Phase 12.6 remote runners)

#### Done Criteria

- [x] Agent step contract tests pass (22/22)
- [x] Polling removal tests pass (10/10)
- [x] Card local execution tests pass (16/16)
- [x] All existing tests still pass

**Effort**: 1 week
**Risk**: Higher (changes agent execution model)
**Outcome**: All step types use unified architecture

---

### Phase 12.6: RemoteExecutor & Runner State Machine
**Goal**: Millisecond-latency job assignment with proper connection lifecycle

**Status**: COMPLETE (200+ tests passing)

Event-driven architecture - no polling, backend pushes jobs immediately.

#### Tests First (Define Contracts) ✅

**test_runner_state_machine.py** - 63 tests for runner lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_disconnected_to_connecting` | WebSocket opens |
| `test_connecting_to_idle_on_register` | Registration succeeds |
| `test_idle_to_assigned_on_job` | Job pushed to runner |
| `test_assigned_to_busy_on_ack` | Runner acknowledges |
| `test_busy_to_dead_on_timeout` | Heartbeat missed |
| `test_dead_to_connecting_on_reconnect` | Runner reconnects |

**test_websocket_protocol.py** - 55 tests for protocol messages
| Test | Defines Contract |
|------|------------------|
| `test_register_message_format` | `{"type": "register", "runner_id": ..., "labels": ...}` |
| `test_execute_step_message_format` | `{"type": "execute_step", ...}` |
| `test_ack_required_within_timeout` | 5s ACK timeout |
| `test_heartbeat_interval` | Heartbeat every 10s |
| `test_death_timeout` | 30s without heartbeat = dead |

**test_remote_executor_contract.py** - Contract tests for RemoteExecutor
| Test | Defines Contract |
|------|------------------|
| `test_register_runner_stores_in_db` | Runner record created |
| `test_execute_step_pushes_via_websocket` | Job pushed immediately |
| `test_ack_timeout_reassigns` | No ACK = try another runner |
| `test_heartbeat_extends_deadline` | Heartbeat resets death timer |
| `test_death_requeues_step` | Dead runner = step back to pending |

**test_job_recovery.py** - Recovery scenario tests
| Test | Defines Contract |
|------|------------------|
| `test_runner_dies_mid_job_requeues` | Step re-queued |
| `test_runner_reconnects_resumes` | Same runner picks up |
| `test_reconnect_after_reassign_aborts` | Too late = abort local work |

- [x] Write `test_runner_state_machine.py` (defines runner lifecycle) - 63 tests
- [x] Write `test_websocket_protocol.py` (defines protocol contract) - 55 tests
- [x] Write `test_remote_executor_contract.py` (defines executor interface)
- [x] Write `test_job_recovery.py` (defines recovery contract)

#### Database Migration ✅

- [x] Created migration `a1b2c3d4e5f6_enhance_runners_table.py` adding:
  - `name` (String 255)
  - `runner_type` (String 50, default "claude-code")
  - `labels` (Text/JSON)
  - `current_step_execution_id` (FK to step_executions)
  - `websocket_id` (String 64)
  - `connected_at` (DateTime)
  - `created_at` (DateTime)
- [x] Updated `RunnerStatus` enum: disconnected, connecting, idle, assigned, busy, dead, offline

#### Implementation (Make Tests Pass) ✅

- [x] Implement Runner state machine (`runner_state.py`) - 63 tests pass
- [x] Remove in-memory `runner_pool.py` (WebSocket only, no backward compat)
- [x] Implement WebSocket protocol (`runner_protocol.py`) - 55 tests pass
- [x] Create `RemoteExecutor` service (`remote_executor.py`)
- [x] WebSocket endpoint for runner connections (`/ws/runner` in `ws_runners.py`)
  - Registration with auth timeout (10s)
  - ACK required for job assignment (5s timeout)
  - Heartbeat monitoring (30s death timeout)
  - Background timeout monitor task
- [x] Implement job recovery (`job_recovery.py`)
- [x] Reconnection handling in RemoteExecutor
- [x] Create `runner-agent` package:
  - `lazyaf_runner/agent.py` - Main RunnerAgent class
  - `lazyaf_runner/config.py` - Config from env vars
  - `lazyaf_runner/docker_orch.py` - DockerOrchestrator
  - `lazyaf_runner/cli.py` - CLI entry point
  - `runner-agent/Dockerfile` - Container image

#### Files Created

| File | Purpose | Tests |
|------|---------|-------|
| `tdd/unit/execution/test_runner_state_machine.py` | State machine tests | 63 |
| `tdd/unit/execution/test_websocket_protocol.py` | Protocol tests | 55 |
| `tdd/unit/execution/test_remote_executor_contract.py` | Executor tests | ~30 |
| `tdd/unit/execution/test_job_recovery.py` | Recovery tests | ~20 |
| `backend/alembic/versions/a1b2c3d4e5f6_enhance_runners_table.py` | DB migration | - |
| `backend/app/services/execution/runner_state.py` | State machine | - |
| `backend/app/services/execution/runner_protocol.py` | Protocol constants | - |
| `backend/app/services/execution/remote_executor.py` | RemoteExecutor | - |
| `backend/app/services/execution/job_recovery.py` | Recovery service | - |
| `backend/app/routers/ws_runners.py` | WebSocket endpoint | - |
| `runner-agent/pyproject.toml` | Package config | - |
| `runner-agent/lazyaf_runner/*.py` | Runner agent code | - |
| `runner-agent/Dockerfile` | Container image | - |

#### Files Removed

| File | Reason |
|------|--------|
| `backend/app/services/runner_pool.py` | Replaced by RemoteExecutor |
| `backend/app/routers/runners.py` | Replaced by ws_runners.py |
| `tdd/unit/services/test_runner_pool.py` | Tests for removed module |
| `tdd/integration/api/test_runners_api.py` | Tests for removed endpoints |

#### Done Criteria ✅

- [x] Runner state machine tests pass (63/63)
- [x] WebSocket protocol tests pass (55/55)
- [x] RemoteExecutor contract tests pass
- [x] Job recovery tests pass
- [x] All existing tests still pass

#### Deferred to Later

- [ ] NativeOrchestrator for embedded devices (focus on Docker first)
- [ ] Chaos tests (network partition, kills)
- [ ] Integration tests with real runner agents

**Effort**: 2 weeks
**Risk**: Medium-High
**Outcome**: Robust remote execution with proper failure handling

**Example runner deployment:**
```bash
# On Raspberry Pi
export LAZYAF_BACKEND_URL="http://192.168.1.100:8000"
export LAZYAF_RUNNER_ID="pi-workshop-1"
export LAZYAF_LABELS="arch=arm64,has=gpio,has=camera"
export LAZYAF_ORCHESTRATOR="native"

python -m lazyaf_runner  # Connects via WebSocket, receives jobs immediately
```

**WebSocket Protocol:**
```
Runner -> Backend: {"type": "register", "runner_id": "...", "labels": {...}}
Backend -> Runner: {"type": "execute_step", "step_id": "...", "image": "...", ...}
Runner -> Backend: {"type": "log", "step_id": "...", "line": "..."}
Runner -> Backend: {"type": "step_complete", "step_id": "...", "exit_code": 0}
```

---

### Phase 12.7: Debug Re-Run Mode
**Goal**: Re-run failed pipelines with breakpoints for interactive debugging

The primary use case: someone points you at a failed pipeline and you need to figure out what went wrong. Debug mode lets you re-run with breakpoints, inspect state, and iterate.

#### Tests First (Define Contracts)

**test_debug_session_state_machine.py** - Write BEFORE implementing debug lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_waiting_on_breakpoint` | Breakpoint hit = waiting |
| `test_waiting_to_connected_on_join` | CLI connects = connected |
| `test_connected_to_ended_on_resume` | Resume = continue |
| `test_timeout_from_waiting` | No connect = timeout |
| `test_timeout_from_connected` | Idle too long = timeout |

**test_debug_api_contract.py** - Write BEFORE implementing API endpoints
| Test | Defines Contract |
|------|------------------|
| `test_create_debug_rerun_returns_session` | POST returns session ID |
| `test_get_debug_session_returns_info` | GET returns commit, runtime, logs |
| `test_resume_continues_pipeline` | POST resume = pipeline continues |
| `test_abort_cancels_pipeline` | POST abort = pipeline cancelled |

**test_breakpoint_execution.py** - Write BEFORE implementing breakpoint behavior
| Test | Defines Contract |
|------|------------------|
| `test_pipeline_pauses_at_breakpoint` | Execution stops |
| `test_workspace_preserved_at_breakpoint` | Files accessible |
| `test_multiple_breakpoints_work` | Can set many breakpoints |

**test_terminal_connection.py** - Write BEFORE implementing terminal
| Test | Defines Contract |
|------|------------------|
| `test_sidecar_mode_spawns_container` | Sidecar container created |
| `test_shell_mode_execs_into_running` | Exec into step container |
| `test_special_commands_work` | @resume, @abort, @status |
| `test_token_required` | Auth enforced |

- [ ] Write `test_debug_session_state_machine.py` (defines debug lifecycle)
- [ ] Write `test_debug_api_contract.py` (defines API contract)
- [ ] Write `test_breakpoint_execution.py` (defines breakpoint behavior)
- [ ] Write `test_terminal_connection.py` (defines terminal protocol)

#### Debug Re-Run Workflow

```
1. User sees failed pipeline → clicks "Debug Re-run"
2. Modal shows:
   - Step list with checkboxes for breakpoints (dynamic, not YAML)
   - Commit selection: "Same as failure (abc123)" OR "Different branch/commit"
3. User starts debug run
4. Pipeline executes until breakpoint
5. UI shows rich context + CLI join command
6. User connects via CLI, inspects, continues or aborts
7. Repeat until done or pipeline completes
```

#### UI at Breakpoint

When a breakpoint is hit, the UI displays:

| Field | Description |
|-------|-------------|
| **Current Commit** | SHA + message of commit being tested |
| **Runtime Info** | Host, orchestrator type, container image, image SHA |
| **Step Info** | Current step name, index, type |
| **Logs** | Full job/pipeline logs up to this point |
| **Join Command** | Pre-populated CLI command to copy/paste |
| **Controls** | Resume, Abort buttons |

#### Two Connection Modes

The CLI supports two ways to connect, depending on what you need:

**1. Sidecar Mode** (inspect filesystem only)
```bash
lazyaf debug <session-id> --sidecar
```
- Spawns a debug sidecar container with workspace volume mounted
- Read-only inspection of checkout, build artifacts, logs
- Useful when step container has exited or you just need to look at files
- Full shell with common tools (vim, git, htop, etc.)

**2. Live Shell Mode** (process in running container)
```bash
lazyaf debug <session-id> --shell
```
- Creates a new process inside the current step container
- Access to full runtime environment (same image, env vars, installed packages)
- Can run the same commands the step would run
- Only available when step container is still running (at breakpoint)

Both modes use WebSocket transport (not SSH) for simplicity.

#### CLI Commands

```bash
# Connect to debug session (default: sidecar if container stopped, shell if running)
lazyaf debug <session-id> --token <token>

# Explicit mode selection
lazyaf debug <session-id> --sidecar --token <token>
lazyaf debug <session-id> --shell --token <token>

# Control commands (from within debug shell or separately)
lazyaf debug <session-id> --resume      # Continue to next breakpoint
lazyaf debug <session-id> --abort       # Cancel the debug run
lazyaf debug <session-id> --status      # Show current state
```

Inside a debug shell, special commands:
```
@resume    # Continue pipeline (alias for --resume)
@abort     # Cancel debug run (alias for --abort)
@status    # Show breakpoint info
@help      # List available commands
```

#### API Endpoints

```
# Start debug re-run from failed pipeline
POST /api/pipeline-runs/{id}/debug-rerun
  Body: {
    breakpoints: ["step-id-1", "step-id-2"],  # Steps to break before
    use_original_commit: bool,                 # true = same commit as failure
    commit_sha: string | null,                 # if use_original_commit=false
    branch: string | null                      # if use_original_commit=false
  }
  Returns: { run_id, debug_session_id }

# Get debug session info (for UI display)
GET /api/debug/{session_id}
  Returns: {
    status: "waiting" | "connected" | "timeout" | "ended",
    current_step: { name, index, type },
    commit: { sha, message },
    runtime: { host, orchestrator, image, image_sha },
    logs: string,
    join_command: string,
    token: string
  }

# Control debug session
POST /api/debug/{session_id}/resume     # Continue to next breakpoint
POST /api/debug/{session_id}/abort      # Cancel debug run
POST /api/debug/{session_id}/extend     # Extend timeout

# WebSocket endpoint for terminal
WS /api/debug/{session_id}/terminal?mode=sidecar|shell&token=<token>
```

#### Debug Session States

```
[pending] --> [waiting_at_bp] --> [connected] --> [ended]
                    |                   |
                    | timeout           | timeout/disconnect
                    v                   v
               [timeout]           [timeout]
```

| State | Description |
|-------|-------------|
| `pending` | Debug run started, executing before first breakpoint |
| `waiting_at_bp` | At breakpoint, waiting for user to connect |
| `connected` | User connected via CLI |
| `timeout` | Session timed out (default 1hr, max 4hr) |
| `ended` | User resumed/aborted, or pipeline completed |

#### Pipeline Run States (Extended)

| State | Description |
|-------|-------------|
| `debug_pending` | Debug re-run created, not yet started |
| `debug_running` | Executing between breakpoints |
| `debug_waiting` | At breakpoint, waiting for user |
| `debug_connected` | User connected, inspecting |

#### Sidecar Container

```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y \
    vim nano less \
    git curl wget \
    htop tree jq \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# WebSocket terminal server
COPY debug-terminal-server /usr/local/bin/
ENTRYPOINT ["debug-terminal-server"]
```

Mounts workspace volume at `/workspace`. Lightweight, starts fast.

#### Security

- **One-time tokens**: Generated per debug session, single use
- **Session timeout**: Default 1 hour, max 4 hours, extendable
- **Token expiry**: Tokens expire with session
- **Resource limits**: Debug containers have CPU/memory limits
- **No SSH**: WebSocket only, simpler attack surface
- **Future**: Integrate with auth system when available

#### Implementation Phases

**Phase 12.7a: Core Debug Re-Run (MVP)**
- [ ] `POST /api/pipeline-runs/{id}/debug-rerun` endpoint
- [ ] `DebugSession` model and service
- [ ] Pipeline executor honors breakpoints, pauses execution
- [ ] `GET /api/debug/{session_id}` for session info
- [ ] Resume/abort endpoints
- [ ] UI: "Debug Re-run" button on failed pipelines
- [ ] UI: Breakpoint selector modal
- [ ] UI: Commit selector (original vs custom)
- [ ] UI: Debug panel showing context when at breakpoint

**Phase 12.7b: CLI Connection**
- [ ] `lazyaf debug` command structure
- [ ] WebSocket terminal client in CLI
- [ ] Sidecar mode: spawn debug container, connect
- [ ] Shell mode: exec into running container
- [ ] Special commands (@resume, @abort, @status)
- [ ] Token-based authentication

**Phase 12.7c: Polish**
- [ ] Session timeout management
- [ ] Reconnection handling (resume interrupted session)
- [ ] UI improvements (better log display, status indicators)
- [ ] `--extend` to add time to session
- [ ] Cleanup: remove debug containers on session end

#### Integration Validation

- [ ] `test_e2e_debug_workflow.py`:
  - Pipeline fails
  - Create debug re-run with breakpoint
  - Connect via CLI
  - Inspect workspace
  - Resume
  - Pipeline completes

#### Done Criteria

- [ ] Debug session state machine tests pass
- [ ] API contract tests pass
- [ ] Breakpoint execution tests pass
- [ ] Terminal connection tests pass
- [ ] E2E workflow test passes

**Effort**: 2-3 weeks
**Risk**: Medium
**Outcome**: Operators can re-run failed pipelines with breakpoints and connect via CLI to debug

---

### Phase 12.8: Cleanup & Polish
**Goal**: Remove legacy code, document new model

#### Tests First (Regression Focus)

**test_no_legacy_code.py** - Verify removal is complete
| Test | Validates |
|------|-----------|
| `test_old_entrypoints_removed` | Files don't exist |
| `test_runner_pool_removed` | No polling infrastructure |
| `test_no_docker_in_docker` | No socket mounting in runners |

**test_full_regression_suite.py** - Everything still works

*Pipeline Execution Paths*
| Test | Validates |
|------|-----------|
| `test_pipeline_with_single_step_completes` | Minimal pipeline executes end-to-end |
| `test_pipeline_with_multiple_steps_sequential` | Steps execute in order |
| `test_pipeline_on_success_next_continues` | `on_success: next` advances to next step |
| `test_pipeline_on_success_stop_completes` | `on_success: stop` ends pipeline with passed status |
| `test_pipeline_on_failure_stop_halts` | `on_failure: stop` ends pipeline with failed status |
| `test_pipeline_on_failure_next_continues` | `on_failure: next` continues despite step failure |
| `test_pipeline_cancel_stops_execution` | Cancel marks run cancelled, stops steps |

*Step Type Variations*
| Test | Validates |
|------|-----------|
| `test_script_step_executes_command` | `type: script` runs shell command |
| `test_docker_step_uses_specified_image` | `type: docker` pulls and runs in specified image |
| `test_agent_step_invokes_ai_runner` | `type: agent` dispatches to Claude/Gemini runner |
| `test_step_timeout_enforced` | Step exceeding timeout is killed |
| `test_step_config_passed_to_executor` | step_config JSON reaches executor |

*Executor Modes*
| Test | Validates |
|------|-----------|
| `test_local_executor_spawns_container` | LocalExecutor creates Docker container |
| `test_remote_executor_pushes_via_websocket` | RemoteExecutor sends job over WebSocket |
| `test_execution_router_selects_correct_executor` | Router picks Local vs Remote based on requirements |

*Workspace Continuity*
| Test | Validates |
|------|-----------|
| `test_continue_in_context_preserves_workspace` | `continue_in_context: true` keeps files |
| `test_is_continuation_skips_cleanup` | Continuation step does not reset workspace |
| `test_previous_step_logs_passed_to_next` | Agent sees previous step output |
| `test_different_images_share_workspace` | Step 1 in golang, Step 2 in python, both see files |

*Trigger Mechanisms*
| Test | Validates |
|------|-----------|
| `test_card_complete_trigger_fires` | Card -> done triggers pipeline |
| `test_push_trigger_on_branch_match` | Push to matching branch triggers |
| `test_trigger_disabled_does_not_fire` | enabled: false suppresses trigger |

*WebSocket Broadcasts*
| Test | Validates |
|------|-----------|
| `test_pipeline_run_status_broadcast` | pipeline_run_status event sent |
| `test_step_run_status_broadcast` | step_run_status event sent |
| `test_card_updated_broadcast` | card_updated on status change |

*Error Handling*
| Test | Validates |
|------|-----------|
| `test_step_failure_captured_in_error_field` | Failed step has error message |
| `test_job_failure_updates_card_status` | Failed job -> card status = failed |
| `test_tests_failed_marks_card_failed` | tests_passed=false -> card failed |
| `test_runner_death_requeues_step` | Runner dies -> step returns to pending |

*Recovery Scenarios*
| Test | Validates |
|------|-----------|
| `test_backend_restart_resumes_pipelines` | Running pipelines resume after restart |
| `test_orphan_containers_cleaned_on_startup` | Stale containers killed |
| `test_orphan_steps_marked_failed` | Abandoned steps get failed status |

- [ ] Write `test_no_legacy_code.py` (verifies cleanup)
- [ ] Write `test_full_regression_suite.py` (validates everything works - 30+ tests above)

#### Implementation

- [ ] Remove old runner entrypoints (archive for reference)
- [ ] Update docker-compose for new architecture
- [ ] Remove `runner_pool.py` polling infrastructure
- [ ] Documentation: runner deployment, custom images, step requirements
- [ ] Example Dockerfiles for common step images

#### Done Criteria

- [ ] Legacy removal tests pass
- [ ] Full regression suite passes
- [ ] Documentation reviewed

**Effort**: 1 week
**Outcome**: Clean, documented system

---

### Phase 12.9: Kubernetes Orchestrator (Future)
**Goal**: Same code works on Kubernetes

#### Tests First (Define Contracts)

**test_k8s_orchestrator_contract.py** - Write BEFORE implementing K8s
| Test | Defines Contract |
|------|------------------|
| `test_creates_k8s_job_for_step` | Job resource created |
| `test_uses_pvc_for_workspace` | PVC mounted |
| `test_node_selector_from_labels` | Labels -> node selector |
| `test_job_completion_detected` | Job status watched |

- [ ] Write `test_k8s_orchestrator_contract.py` (defines K8s behavior)

#### Implementation (Make Tests Pass)

- [ ] Implement `KubernetesOrchestrator` (make tests pass)
- [ ] PersistentVolumeClaims for workspaces
- [ ] K8s Jobs for step execution
- [ ] Node selectors based on runner labels
- [ ] Integration tests in K8s environment

#### Done Criteria

- [ ] K8s orchestrator tests pass (mocked)
- [ ] Integration tests pass (real K8s)

**Effort**: 2-3 weeks when needed
**Outcome**: Production-ready K8s deployment
