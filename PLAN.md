# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           FRONTEND                                   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Svelte App                                │   │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │   │
│  │  │ To Do   │ │In Prog  │ │In Review│ │  Done   │           │   │
│  │  │  ┌───┐  │ │  ┌───┐  │ │  ┌───┐  │ │  ┌───┐  │           │   │
│  │  │  │   │  │ │  │   │  │ │  │   │  │ │  │   │  │           │   │
│  │  │  └───┘  │ │  └───┘  │ │  └───┘  │ │  └───┘  │           │   │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘           │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              │ HTTP/WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           BACKEND                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Server                            │   │
│  │                                                              │   │
│  │  • REST API (cards, repos, jobs)                            │   │
│  │  • WebSocket (real-time status updates)                     │   │
│  │  • Runner Pool Manager                                       │   │
│  │  • Job Queue                                                 │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│                              ▼                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    SQLite Database                           │   │
│  │  • repos, cards, jobs, runners                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              │ Docker API
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        RUNNER POOL                                   │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐                       │
│  │  Runner 1 │  │  Runner 2 │  │  Runner N │                       │
│  │ ┌───────┐ │  │ ┌───────┐ │  │ ┌───────┐ │                       │
│  │ │Docker │ │  │ │Docker │ │  │ │Docker │ │                       │
│  │ │  +    │ │  │ │  +    │ │  │ │  +    │ │                       │
│  │ │Claude │ │  │ │Claude │ │  │ │Claude │ │                       │
│  │ │ Code  │ │  │ │ Code  │ │  │ │ Code  │ │                       │
│  │ └───────┘ │  │ └───────┘ │  │ └───────┘ │                       │
│  └───────────┘  └───────────┘  └───────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | Svelte + Vite | Reactive UI, fast builds |
| Backend | FastAPI | Async Python API |
| Database | SQLite + SQLAlchemy | Simple persistence |
| Queue | In-memory (upgrade to Redis later) | Job management |
| Containers | Docker SDK for Python | Runner isolation |
| Real-time | WebSockets (FastAPI native) | Status updates |

## Data Models

### Repo
```python
class Repo:
    id: UUID
    name: str
    remote_url: str | None       # Real remote (GitHub/GitLab) for landing changes
    default_branch: str          # e.g., "dev" or "main"
    is_ingested: bool            # True if repo content is in internal git server
    created_at: datetime

    @property
    def internal_git_url(self) -> str:
        """URL to clone from internal git server: /git/{id}.git"""
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
    branch_name: str | None      # Created when work starts
    pr_url: str | None           # Populated when PR created
    job_id: UUID | None          # Current/last job
    created_at: datetime
    updated_at: datetime
```

### Job
```python
class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class Job:
    id: UUID
    card_id: UUID
    runner_id: UUID | None
    status: JobStatus
    logs: str                    # Captured output
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
```

### Runner
```python
class RunnerStatus(Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"

class Runner:
    id: UUID
    container_id: str | None
    status: RunnerStatus
    current_job_id: UUID | None
    last_heartbeat: datetime
```

## API Endpoints

### Repos
```
GET    /api/repos              # List all repos
POST   /api/repos              # Create a repo record
POST   /api/repos/ingest       # Create repo + init internal git storage
GET    /api/repos/{id}         # Get repo details
GET    /api/repos/{id}/clone-url  # Get internal git clone URL
DELETE /api/repos/{id}         # Delete repo (and git storage)
```

### Git (Internal Git Server)
```
GET    /git/{id}.git/info/refs?service=git-upload-pack   # Clone/fetch refs
GET    /git/{id}.git/info/refs?service=git-receive-pack  # Push refs
POST   /git/{id}.git/git-upload-pack                     # Clone/fetch data
POST   /git/{id}.git/git-receive-pack                    # Push data
GET    /git/{id}.git/HEAD                                # HEAD reference
```

### Cards
```
GET    /api/repos/{repo_id}/cards           # List cards for repo
POST   /api/repos/{repo_id}/cards           # Create card
GET    /api/cards/{id}                       # Get card details
PATCH  /api/cards/{id}                       # Update card
DELETE /api/cards/{id}                       # Delete card
POST   /api/cards/{id}/start                 # Trigger agent work
POST   /api/cards/{id}/approve               # Approve PR, move to done
POST   /api/cards/{id}/reject                # Reject, back to todo
```

### Jobs
```
GET    /api/jobs/{id}                        # Get job details
GET    /api/jobs/{id}/logs                   # Stream job logs
POST   /api/jobs/{id}/cancel                 # Cancel running job
```

### Runners
```
GET    /api/runners                          # List all runners
POST   /api/runners/scale                    # Scale pool up/down
```

### WebSocket
```
WS     /ws                                   # Real-time updates
       → card_updated, job_status, runner_status events
```

## Docker Runner Architecture

### Runner Image
```dockerfile
FROM python:3.12-slim

# Install git, Claude Code CLI dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN curl -fsSL https://claude.ai/install.sh | sh

# Working directory for repos
WORKDIR /workspace

# Entry script that:
# 1. Clones/checks out repo
# 2. Runs Claude Code with provided prompt
# 3. Creates PR
# 4. Reports status back
COPY runner_entrypoint.py /runner_entrypoint.py
ENTRYPOINT ["python", "/runner_entrypoint.py"]
```

### Runner Flow
1. Backend assigns job to runner
2. Container starts with environment:
   - `REPO_URL` - Git remote URL
   - `BRANCH_NAME` - Feature branch to create
   - `BASE_BRANCH` - Branch to base off (e.g., dev)
   - `CARD_TITLE` - Card title
   - `CARD_DESCRIPTION` - Card description
   - `CALLBACK_URL` - URL to report status
   - `ANTHROPIC_API_KEY` - For Claude Code
3. Runner clones repo, creates branch
4. Runner invokes Claude Code with structured prompt
5. Claude Code implements feature, commits
6. Runner pushes branch, creates PR via `gh` CLI
7. Runner reports completion to callback URL

### Claude Code Prompt Template
```
You are implementing a feature for this project.

## Feature Request
Title: {card_title}

Description:
{card_description}

## Instructions
1. Implement this feature following existing code patterns
2. Write tests if a test framework is present
3. Commit your changes with a clear message
4. Do not modify unrelated code

## Repository Context
{readme_content_if_available}
```

## Frontend Components

### Svelte Structure
```
src/
├── lib/
│   ├── components/
│   │   ├── Board.svelte           # Kanban board container
│   │   ├── Column.svelte          # Single column (To Do, etc.)
│   │   ├── Card.svelte            # Draggable card
│   │   ├── CardModal.svelte       # Create/edit card
│   │   ├── RepoSelector.svelte    # Repo picker
│   │   ├── JobStatus.svelte       # Job progress indicator
│   │   └── RunnerPool.svelte      # Runner status display
│   ├── stores/
│   │   ├── cards.ts               # Card state
│   │   ├── repos.ts               # Repo state
│   │   ├── jobs.ts                # Job state
│   │   └── websocket.ts           # WS connection
│   └── api/
│       └── client.ts              # API client
├── routes/
│   ├── +page.svelte               # Main board view
│   └── +layout.svelte             # App layout
└── app.html
```

## Implementation Phases

### Phase 1: Project Foundation
**Goal**: Basic project structure, can run both frontend and backend

- [x] Initialize Python project with uv
- [x] Set up FastAPI with basic health endpoint
- [x] Initialize Svelte project with Vite
- [x] Configure CORS, proxy for dev
- [x] Set up SQLite with SQLAlchemy models
- [x] Docker Compose for local dev

**Deliverable**: `docker-compose up` runs both services

### Phase 2: Repo & Card Management
**Goal**: Can attach repos and create/view cards

- [x] Implement Repo CRUD endpoints
- [x] Implement Card CRUD endpoints
- [x] Build RepoSelector component
- [x] Build Board + Column + Card components
- [x] Card drag-and-drop between columns
- [x] CardModal for create/edit

**Deliverable**: Can create cards on a board for an attached repo

### Phase 3: Runner Pool
**Goal**: Docker-based runner pool that can execute commands

- [x] Create runner Docker image
- [x] Implement Runner model and pool manager
- [x] Runner registration/heartbeat system
- [x] Job queue (in-memory for now)
- [x] Job assignment logic
- [x] Runner status API

**Deliverable**: Can spawn runners and assign dummy jobs

### Phase 3.5: Runner UI/UX
**Goal**: Visibility into runner pool and easy agent management

- [x] Add runner types and API client to frontend
- [x] Create runner store with 2s polling
- [x] Build RunnerPanel component (pool stats, +/- scaling)
- [x] Add individual runner list with status badges
- [x] Backend endpoint for docker run command generation
- [x] Docker command modal (placeholders + copy with secrets button)

**Deliverable**: Can see runner status, scale pool, and copy docker commands to spin up runners

### Phase 3.75: Internal Git Server ✅
**Goal**: Host repos internally for iteration isolation - don't pollute real remotes until ready

**Motivation**: During active development/iteration, we don't want to push experimental branches and PRs to GitHub/GitLab. The internal git server lets agents work in isolation. When satisfied with results, users can "land" changes to the real remote.

#### Architecture
```
┌─────────────────────────────────────────────────────────────────────┐
│                        INTERNAL GIT SERVER                          │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    HTTP Smart Protocol                       │   │
│  │  GET  /git/{repo_id}.git/info/refs   (clone discovery)      │   │
│  │  POST /git/{repo_id}.git/git-upload-pack   (fetch)          │   │
│  │  POST /git/{repo_id}.git/git-receive-pack  (push)           │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│                              ▼                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Bare Git Repos                            │   │
│  │  backend/git_repos/{repo_id}.git/                           │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

#### Flows
1. **Ingest**: User runs CLI → pushes local repo → stored in internal git server
2. **Agent Work**: Runner clones from internal server → makes changes → pushes back
3. **Land**: User pushes approved branch to real remote (GitHub/GitLab)

#### Implementation

**Phase 3.75a: Git Server Foundation** ✅
- [x] Add dulwich dependency (pure Python git library)
- [x] Create `git_server.py` service for bare repo management
- [x] Create `git.py` router with HTTP smart protocol endpoints
- [x] Update Repo model (add `is_ingested`, remove `path` requirement)
- [x] Add `/api/repos/ingest` endpoint

**Phase 3.75b: Ingest CLI** ✅
- [x] Create `cli/` package with Click
- [x] `lazyaf ingest /path/to/repo --name NAME` command
- [x] CLI calls ingest API, then `git push` to internal server
- [x] Support `--branch` flag to specify branch to push
- [x] Support `--all-branches` flag to push all branches

**Phase 3.75c: Agent Integration** ✅
- [x] Update runner entrypoint to clone from internal git URL
- [x] Runner pushes feature branches back to internal server (not origin)
- [x] Skip PR creation when working against internal server
- [x] Track branches per repo via /repos/{id}/branches endpoint

**Phase 3.75d: Land Flow** ✅
- [x] CLI `lazyaf land {repo_id} --branch {branch}` command
- [x] Fetches from internal, pushes to real remote
- [x] Optional `--pr` flag to create PR via gh CLI
- [ ] UI button to trigger land flow (later - Phase 6)

**Tech Stack**:
- `dulwich` - Pure Python git implementation, embeds in FastAPI
- HTTP smart protocol - Works through firewalls, supports auth headers
- Bare repos stored in `backend/git_repos/`

**Deliverable**: Can ingest local repos, agents work in isolation, land changes when ready

### Phase 4: Agent Integration ✅
**Goal**: Cards trigger Claude Code, results in PRs (or internal branches)

- [x] Runner entrypoint script (clone from internal server, branch, invoke Claude)
- [x] Claude Code invocation with card context
- [x] Push results back to internal git server
- [x] Job status callbacks (running/completed/failed)
- [x] Link branches back to cards
- [x] WebSocket status updates (job_status, card_updated broadcasts)
- [x] Jobs store in frontend for real-time tracking
- [x] JobStatus component (spinner, duration, logs viewer)
- [x] Background heartbeat thread during long Claude operations
- [x] Streaming output from Claude Code
- [x] Runner auto-reconnect when backend restarts
- [x] Workspace cleanup between job runs
- [x] Handle branch already exists on retry (fetch + merge)

**Deliverable**: Creating a card and clicking "Start" produces changes in the internal repo

### Phase 5: Review Flow ✅
**Goal**: Complete the human review loop

- [x] Job logs viewer (real-time polling, expandable panel)
- [x] Error handling and retry (retry button on failed cards)
- [x] Git graph visualization (commit history per branch)
- [x] Branch selector in RepoInfo panel
- [x] Diff viewer in CardModal for code review
- [x] "Approve" action moves card to done
- [x] "Reject" action resets card to todo
- [ ] "Approve" actually merges branch to default (later - requires merge logic)

**Deliverable**: Full loop from card → agent work → review → approve/reject works

### Phase 6: Polish
**Goal**: Usable daily driver

- [ ] Improve error states and messaging
- [x] Add card filtering/search
- [ ] Runner scaling controls
- [ ] Basic auth (optional, local tool)
- [ ] Persist runner pool config

**Deliverable**: You're using this for real work

### Phase 7: MCP Interface ✅
**Goal**: Allow LLM clients (Claude Desktop, etc.) to orchestrate LazyAF via Model Context Protocol

**Problem Being Solved**: Want to interact with LazyAF from chat interfaces - "create a card for adding dark mode" without opening the UI.

**MVP Scope** (31 tools total):

**Core Tools**:
- [x] `list_repos` - returns repo names, IDs, and ingest status
- [x] `list_cards` - returns cards for a repo with status, branch, PR info
- [x] `create_card` - creates a card given repo_id, title, description
- [x] `get_card` - returns full card details including job logs
- [x] `start_card` - triggers agent work on a card
- [x] `get_job_logs` - fetch logs from a job run
- [x] `get_runner_status` - check runner pool availability

**Card Actions**:
- [x] `approve_card` - approve and merge a card
- [x] `reject_card` - reject card back to todo
- [x] `retry_card` - retry a failed card
- [x] `update_card` - update card details
- [x] `delete_card` - delete a card

**Pipeline Tools** (with trigger support):
- [x] `list_pipelines` - list pipelines for a repo
- [x] `create_pipeline` - create pipeline with steps and triggers
- [x] `get_pipeline` - get pipeline details
- [x] `update_pipeline` - update pipeline including triggers
- [x] `delete_pipeline` - delete a pipeline
- [x] `run_pipeline` - trigger a pipeline run
- [x] `get_pipeline_run` - get run status with step details
- [x] `list_pipeline_runs` - list runs with filters
- [x] `cancel_pipeline_run` - cancel a running pipeline
- [x] `get_step_logs` - get logs for a specific step

**Agent Files**:
- [x] `list_agent_files` - list platform agent files
- [x] `get_agent_file` - get agent file content
- [x] `create_agent_file` - create new agent file
- [x] `update_agent_file` - update agent file
- [x] `delete_agent_file` - delete agent file

**Git/Branch Tools**:
- [x] `list_branches` - list repo branches
- [x] `get_diff` - get diff between branches

**Repo-Defined Assets**:
- [x] `list_repo_agents` - list .lazyaf/agents/
- [x] `list_repo_pipelines` - list .lazyaf/pipelines/

**Resource**: `repos://list` - formatted repo list

**Explicit Non-Goals (This Phase)**:
- HTTP/SSE transport (stdio only for MVP)
- Runner management via MCP
- Complex auth (local tool assumption)

**Key Files**:
```
backend/
├── mcp/
│   ├── __init__.py
│   ├── __main__.py       # Entry point for stdio transport
│   └── server.py         # MCP server implementation (all tools)
```

**Tech Stack**:
- `mcp` Python package with FastMCP for server implementation
- stdio transport for Claude Desktop compatibility
- Thin wrapper around existing FastAPI services

**Deliverable**: Full LazyAF orchestration from Claude Desktop - create cards, manage pipelines, configure triggers, review and approve work

### Phase 8: Test Result Capture ✅
**Goal**: Visibility into whether agent-produced code passes tests

**Problem Being Solved**: Agents complete work but may have broken tests. No visibility into test results, and manual checking is tedious. Want LazyAF to be the source of truth for test status, not GHA.

**MVP Scope**:
- [x] Runner detects test framework (package.json scripts, pytest.ini, etc.)
- [x] Runner runs tests after Claude Code completes (similar to docker entrypoint script)
- [x] Test results stored on Job model (pass_count, fail_count, output)
- [x] Test summary displayed in CardModal when viewing completed cards
- [x] Cards with failing tests get "failed" status with test output visible
- [x] Test output included in job logs

**Explicit Non-Goals (This Phase)**:
- Separate test dashboard (use existing UI)
- Automatic retry on test failure (manual retry exists)
- Coverage reports
- Test trend analysis
- External CI integration (this IS the CI)

**Key Files**:
- `backend/runner/entrypoint.py` - Add test detection and execution
- `backend/app/models/job.py` - Add test result fields
- `frontend/src/lib/components/CardModal.svelte` - Display test results

**Test Framework Detection**:
```python
# Priority order for detection:
1. package.json → scripts.test → "npm test"
2. pytest.ini / pyproject.toml [tool.pytest] → "pytest"
3. Cargo.toml → "cargo test"
4. go.mod → "go test ./..."
5. Makefile with test target → "make test"
```

**Decision Points**:
- If no tests detected: treat as "no tests" (not pass or fail)
- Test failure blocks card from going to "in_review" (stays as "failed")
- Test timeout: 5 minutes default, configurable per repo

**Deliverable**: After agent work, see "Tests: 42 passed, 3 failed" in card modal. Failed tests = failed card.

---

## CI/CD Pipeline System (Phases 8.5 - 11)

> **Vision**: Replace GitHub Actions with a local-first, controllable CI/CD system that seamlessly integrates AI agents as just another step type. Fast, testable locally, no YAML maze.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            PIPELINE SYSTEM                                   │
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │   TRIGGER   │───▶│  PIPELINE   │───▶│    STEPS    │───▶│   RESULTS   │  │
│  │             │    │             │    │             │    │             │  │
│  │ • Manual    │    │ • Ordered   │    │ • Agent     │    │ • Pass/Fail │  │
│  │ • Webhook   │    │ • Branching │    │ • Script    │    │ • Logs      │  │
│  │ • Git Push  │    │ • Parallel  │    │ • Docker    │    │ • Artifacts │  │
│  │ • Schedule  │    │   (future)  │    │             │    │             │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘  │
│                                                                              │
│  Step Types:                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  AGENT          │  SCRIPT           │  DOCKER                        │   │
│  │  Claude/Gemini  │  Shell commands   │  Command in specified image    │   │
│  │  implements     │  lint, test,      │  isolated builds, custom       │   │
│  │  features       │  coverage, build  │  environments                  │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### New Data Models

```python
class StepType(Enum):
    AGENT = "agent"      # Current Claude/Gemini behavior
    SCRIPT = "script"    # Shell command execution
    DOCKER = "docker"    # Command in specified container image

class Pipeline:
    id: UUID
    repo_id: UUID
    name: str
    description: str | None
    steps: list[PipelineStep]  # JSON - ordered steps
    is_template: bool          # Can be reused across repos
    created_at: datetime
    updated_at: datetime

class PipelineStep:
    name: str
    type: StepType
    config: dict               # Type-specific config (see below)
    on_success: str            # "next" | "stop" | "trigger:{card_id}" | "merge:{branch}"
    on_failure: str            # "next" | "stop" | "trigger:{card_id}"
    timeout: int               # Seconds, default 300

# Step config by type:
# AGENT:  {card_id, runner_type}
# SCRIPT: {command, working_dir?}
# DOCKER: {image, command, env?, volumes?}

class PipelineRun:
    id: UUID
    pipeline_id: UUID
    status: RunStatus          # pending/running/passed/failed/cancelled
    trigger_type: str          # "manual" | "webhook" | "card" | "push" | "schedule"
    trigger_ref: str | None    # webhook_id, card_id, branch, etc.
    current_step: int
    steps_completed: int
    steps_total: int
    started_at: datetime
    completed_at: datetime | None

class StepRun:
    id: UUID
    pipeline_run_id: UUID
    step_index: int
    status: RunStatus
    logs: str
    error: str | None
    started_at: datetime
    completed_at: datetime | None

class Webhook:
    id: UUID
    pipeline_id: UUID
    name: str
    token: str                 # Secret for authentication
    type: str                  # "generic" | "git_push"
    last_triggered: datetime | None
    created_at: datetime
```

### Phase 8.5: CI/CD Foundation (Non-AI Steps) ✅
**Goal**: Runners can execute scripts and docker commands, not just AI agents

**Problem Being Solved**: Currently all "work" must go through an AI agent. Want deterministic CI steps (lint, test, build) that run reliably every time.

**MVP Scope**:
- [x] Add `step_type` enum: `agent | script | docker`
- [x] Extend Card and Job models with step type and config fields
- [x] Update runner entrypoint to handle all three step types
- [x] Script steps: run shell command in cloned repo, capture output
- [x] Docker steps: run command in specified container image, capture output
- [x] UI: Cards can specify step type with config inputs (command, image, working_dir)
- [x] MCP: `create_card` accepts `step_type`, `command`, `image`, `working_dir` parameters

**Step Type Implementations**:
```python
# AGENT (existing behavior)
job.step_type = "agent"
job.config = {"runner_type": "claude-code"}
# Runner invokes Claude Code CLI

# SCRIPT (new)
job.step_type = "script"
job.config = {"command": "npm run lint && npm test"}
# Runner executes command directly, captures stdout/stderr

# DOCKER (new)
job.step_type = "docker"
job.config = {"image": "node:20", "command": "npm run build"}
# Runner pulls image, runs command in container, captures output
```

**Explicit Non-Goals (This Phase)**:
- Pipeline orchestration (just individual steps)
- Webhooks
- Step chaining

**Deliverable**: Can create a card with step_type=script or docker that runs commands directly without AI. UI has step type selector with config inputs. MCP create_card supports all step types.

### Phase 9: Pipelines ✅
**Goal**: Chain steps with conditional logic into reusable workflows

**Problem Being Solved**: Want multi-step workflows: lint → test → build, with AI fixes on failure

**MVP Scope**:
- [x] Pipeline entity with ordered steps
- [x] Steps can be inline (script/docker) or reference cards (agent)
- [x] Success/failure branching per step:
  - `next` - continue to next step
  - `stop` - halt pipeline (success or failure based on step result)
  - `trigger:{card_id}` - spawn AI agent card to fix issues
  - `trigger:pipeline:{pipeline_id}` - Run target pipeline
  - `merge:{branch}` - auto-merge on success
- [x] Extend Script step to allow for multi-line bash scripts
- [x] Pipeline execution engine (runs steps sequentially)
- [x] Pipeline run tracking (status, current step, logs per step)
- [x] UI: Pipeline builder/editor
- [x] UI: Pipeline runs viewer with step-by-step status
- [x] MCP tools: `create_pipeline`, `run_pipeline`, `get_pipeline_run`

**Example Pipeline**:
```json
{
  "name": "PR Validation",
  "steps": [
    {"name": "Lint", "type": "script", "config": {"command": "npm run lint"}, "on_failure": "trigger:ai-fix-lint"},
    {"name": "Test", "type": "script", "config": {"command": "npm test"}, "on_failure": "trigger:ai-fix-tests"},
    {"name": "Build", "type": "docker", "config": {"image": "node:20", "command": "npm run build"}, "on_failure": "stop"}
  ]
}
```

**Explicit Non-Goals (This Phase)**:
- Parallel step execution
- Webhook triggers
- Scheduled runs

**Deliverable**: Can define and run multi-step pipelines with AI fallback on failures

### Phase 9.5: Webhooks
**Goal**: External systems can trigger pipelines

**Problem Being Solved**: Want to trigger CI pipelines from git push, external services, or other automation

**MVP Scope**:
- [ ] Webhook entity (token-authenticated URLs)
- [ ] Generic webhook: `POST /api/webhooks/{token}/trigger`
- [ ] Git push webhook: `POST /api/webhooks/{token}/git-push` (parses branch/commit)
- [ ] Webhook payload available to pipeline as env vars
- [ ] UI: Webhook management (create, view URL, regenerate token, delete)
- [ ] MCP tools: `create_webhook`, `list_webhooks`, `delete_webhook`

**Webhook Endpoints**:
```
POST /api/webhooks/{token}/trigger
  Body: {params: {key: value}}
  → Triggers associated pipeline with params as env vars

POST /api/webhooks/{token}/git-push
  Body: {ref: "refs/heads/main", commits: [...], repository: {...}}
  → Parses git push payload, triggers pipeline with GIT_BRANCH, GIT_COMMIT, etc.
```

**Explicit Non-Goals (This Phase)**:
- GitHub/GitLab specific integrations (use generic git-push format)
- Webhook signature verification (token auth only for MVP)

**Deliverable**: Can get a webhook URL, configure Gitea/GitHub to POST to it, pipeline runs automatically

### Phase 10: Events & Triggers
**Goal**: Pipelines trigger automatically from various events

**Problem Being Solved**: Don't want to manually trigger or set up external webhooks for common cases

**MVP Scope**:
- [ ] Internal git server emits push events
- [ ] Pipeline trigger config: on_push (branch filter), on_card_complete, on_schedule
- [ ] Card completion can trigger downstream pipelines
- [ ] Cron-style scheduling for periodic runs
- [ ] UI: Trigger configuration in pipeline editor
- [ ] MCP: Trigger config in pipeline CRUD tools

**Trigger Types**:
```python
class PipelineTrigger:
    type: str  # "manual" | "webhook" | "push" | "card_complete" | "schedule"
    config: dict

# Examples:
{"type": "push", "config": {"branches": ["main", "dev"]}}
{"type": "card_complete", "config": {"card_status": "in_review"}}
{"type": "schedule", "config": {"cron": "0 0 * * *"}}  # Daily at midnight
```

**Explicit Non-Goals (This Phase)**:
- Complex event filtering
- Event replay/history

**Deliverable**: Push to internal git → pipeline automatically runs → AI fixes failures → auto-merge

### Phase 11: Reporting & Artifacts (Future)
**Goal**: Visibility into CI health over time, build artifact storage

**Problem Being Solved**: Want test trends, coverage reports, and a place to store build outputs

**Scope** (rough):
- [ ] Test result aggregation per repo over time
- [ ] Coverage trend tracking
- [ ] Artifact storage (build outputs, test reports)
- [ ] Pipeline run history with filtering
- [ ] Dashboard with CI health metrics
- [ ] MCP tools: `get_test_trends`, `get_coverage`, `list_artifacts`

**Explicit Non-Goals (This Phase)**:
- External artifact storage (S3, etc.) - local storage only
- Complex analytics

**Deliverable**: Dashboard showing test pass rates over time, coverage trends, downloadable build artifacts

---

### MCP Integration (CI/CD)

All CI/CD features exposed via MCP for Claude Desktop orchestration:

```python
# Pipeline Management
list_pipelines(repo_id?) -> list[Pipeline]
create_pipeline(repo_id, name, steps[]) -> Pipeline
get_pipeline(pipeline_id) -> Pipeline
update_pipeline(pipeline_id, steps[]) -> Pipeline
delete_pipeline(pipeline_id) -> bool

# Pipeline Execution
run_pipeline(pipeline_id, params?) -> PipelineRun
get_pipeline_run(run_id) -> PipelineRun
list_pipeline_runs(pipeline_id?, status?) -> list[PipelineRun]
cancel_pipeline_run(run_id) -> bool
get_step_logs(run_id, step_index) -> str

# Webhooks
create_webhook(pipeline_id, name, type) -> Webhook  # Returns URL + token
list_webhooks(repo_id?) -> list[Webhook]
delete_webhook(webhook_id) -> bool

# Resources
pipelines://list
pipelines://{id}/runs
webhooks://list
```

**Example Claude Desktop interactions**:
- "Create a pipeline for this repo that lints, tests, and builds"
- "Show me the last 5 pipeline runs for lazyaf"
- "Give me a webhook URL to trigger CI from Gitea"
- "Add an AI fix step to the test pipeline that triggers on failure"

---

## Project Structure

```
lazyaf/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app
│   │   ├── config.py            # Settings
│   │   ├── database.py          # SQLAlchemy setup
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── repo.py
│   │   │   ├── card.py
│   │   │   ├── job.py
│   │   │   └── runner.py
│   │   ├── routers/
│   │   │   ├── __init__.py
│   │   │   ├── repos.py
│   │   │   ├── cards.py
│   │   │   ├── jobs.py
│   │   │   ├── runners.py
│   │   │   └── git.py           # Git HTTP smart protocol
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── runner_pool.py
│   │   │   ├── job_queue.py
│   │   │   ├── websocket.py
│   │   │   └── git_server.py    # Bare repo management
│   │   ├── mcp/                  # MCP server (Phase 7)
│   │   │   ├── __init__.py
│   │   │   ├── server.py         # MCP server entry point
│   │   │   └── tools.py          # Tool definitions
│   │   └── schemas/
│   │       ├── __init__.py
│   │       └── ...              # Pydantic models
│   ├── git_repos/               # Internal bare git repos
│   │   └── {repo_id}.git/
│   ├── runner/
│   │   ├── Dockerfile
│   │   └── entrypoint.py
│   ├── pyproject.toml
│   └── alembic/                 # DB migrations
├── cli/                         # LazyAF CLI tool
│   ├── pyproject.toml
│   └── lazyaf/
│       └── cli.py               # ingest, land commands
├── frontend/
│   ├── src/
│   │   └── ...                  # Svelte app
│   ├── package.json
│   └── vite.config.ts
├── docker-compose.yml
├── dream.txt                    # Vision doc
├── PLAN.md                      # This file
└── README.md
```

## Open Questions / Future Scope

**Deferred to later phases:**
- Test failure triggers (requires test runner integration)
- Bug report ingestion (requires external integration)
- Multi-user support
- Dev → QA → Prod promotion
- External Trello/GitHub Issues sync
- Multiple repos simultaneously
- Agent progress streaming

**Questions to answer during implementation:**
- How to handle Claude Code auth in containers? (mount API key? OAuth?)
- How to handle GitHub auth for PR creation? (gh CLI auth, PAT?)
- What happens if agent takes too long? (timeout + cancel)
- How to handle merge conflicts? (fail and notify, or auto-resolve?)

---

## Current Status

**Completed**: Phases 1-10 (full pipeline and trigger system)
**Next**: Phase 9.5 (Webhooks) or Phase 11 (Reporting & Artifacts)

The target workflow is now fully functional:
1. Ingest repos via CLI
2. Create cards describing features (or CI steps: script/docker)
3. Start work → runner clones repo, executes step (AI agent, shell script, or docker command)
4. Card completes → reaches "in_review" status
5. **Pipeline triggers automatically** (if configured with card_complete trigger)
6. Pipeline runs tests/validation steps
7. **On pass**: Card auto-merged and marked done
8. **On fail**: Card marked failed (user can retry)

**Target Workflow (Card → Test → Merge)** - ✅ COMPLETE:
```
Card created → Agent implements → Card in_review → Pipeline triggers → Tests pass → Auto-merge to main
```

**Also Supported**:
- Push triggers: Pipeline runs when code pushed to matching branches
- Manual pipeline runs: Run any pipeline on-demand
- Trigger actions: Configure what happens on pass/fail per trigger

---

## Phase 9.1: Pipeline Polish ✅
**Goal**: Make pipelines production-ready for the target workflow

### 9.1a: Context Sharing Clarity ✅
- [x] `continue_in_context` flag on steps (workspace preserved)
- [x] `is_continuation` flag (skip clone on subsequent steps)
- [x] `previous_step_logs` passed to agent steps
- [x] Document workspace behavior in UI (tooltip on checkbox) - `PipelineEditor.svelte:481-489`
- [x] Log clearly what context each step receives - `entrypoint.py:508-517`

### 9.1b: Pipeline & Agent File Persistence ✅
**Goal**: Store pipelines AND agents in repo as `.lazyaf/` directory

This allows repos to bring custom agents along, version-controlled with the repo.

- [x] Define YAML schema for pipeline definitions - `backend/app/schemas/lazyaf_yaml.py`
- [x] Define YAML schema for agent definitions - `backend/app/schemas/lazyaf_yaml.py`
- [x] API endpoint to read `.lazyaf/` from git tree (branch-aware) - `backend/app/routers/lazyaf_files.py`
- [x] UI: Repo-defined agents tagged as "from repo" (vs platform agents) - `PipelineEditor.svelte:385-391`
- [x] Agents referenced by name, repo overrides platform on lookup - `backend/app/services/agent_resolver.py`
- [x] Pipelines read from working branch (matches the code being worked on)

**Critical: Git-Native Behavior** ✅
The `.lazyaf/` directory is **live from the repo**, not a one-time import:
- [x] UI reads `.lazyaf/` from HEAD of selected branch (via git server)
- [x] Pipelines/agents update automatically when branch HEAD changes
- [x] External commits (manual edits, other tools) reflected in UI immediately
- [x] No CLI export/import commands - just edit files in repo directly
- [x] Repo IS the source of truth (no DB copies of repo-defined items)

**Lookup Precedence**:
When resolving agent references (e.g., `agent: "test-fixer"`):
1. Check `.lazyaf/agents/test-fixer.yaml` in repo (working branch)
2. Fall back to platform-defined agent if not found in repo

This means:
- Switch branches → see that branch's pipelines/agents
- Push a commit adding `.lazyaf/agents/new-agent.yaml` → UI shows it immediately
- Different branches can have different pipeline/agent definitions

**Directory Structure**:
```
.lazyaf/
├── pipelines/
│   ├── test-suite.yaml       # One pipeline per file
│   ├── deploy.yaml
│   └── nightly-build.yaml
└── agents/
    ├── test-fixer.yaml       # One agent per file
    ├── code-reviewer.yaml
    └── unity-specialist.yaml
```

Benefits:
- Each pipeline/agent is a separate file (cleaner git diffs)
- Filename = identifier (no name collisions within a file)
- Easier to copy/share individual pipelines or agents
- Can delete/add without editing a monolithic file

**Pipeline Schema** (`.lazyaf/pipelines/test-suite.yaml`):
```yaml
# Filename is the identifier: "test-suite"
name: "Test Suite"                    # Display name
description: "Run tests on feature branches"
steps:
  - id: "tests"                       # Optional: stable ID for context file references
    name: "Install & Test"
    type: script
    config:
      command: |
        pip install -e ".[test]"
        pytest -v
    continue_in_context: true
  - id: "fix"
    name: "Fix Failures"
    type: agent
    config:
      title: "Fix Test Failures"
      description: "Review test output and fix failing tests"
      agent: "test-fixer"             # References agent by filename
    on_failure: stop
```

**Agent Schema** (`.lazyaf/agents/test-fixer.yaml`):
```yaml
# Filename is the identifier: "test-fixer"
name: "Test Fixer"                    # Display name
description: "Specialized agent for fixing test failures"
prompt_template: |
  You are a test specialist. Review the failing tests and fix them.

  Focus on:
  - Understanding why the test failed
  - Fixing the code, not the test (unless the test is wrong)
  - Maintaining existing test coverage

  ## Task
  {{description}}
```

**Another Agent** (`.lazyaf/agents/unity-specialist.yaml`):
```yaml
name: "Unity Specialist"
description: "Agent specialized in Unity C# development"
prompt_template: |
  You are a Unity game development specialist.

  ## Guidelines
  - Follow Unity best practices
  - Use proper component architecture
  - Consider performance for mobile builds

  ## Task
  {{description}}
```

**Variable Substitution**:
Prompt templates use `{{variable}}` syntax (consistent with existing entrypoint code):
- `{{description}}` - Card/step description
- `{{title}}` - Card/step title
- `{{branch_name}}` - Current branch name

---

### 9.1d: Pipeline Context Directory ✅
**Goal**: Enable chain-of-thought across pipeline steps via committed context

This is experimental - if agents benefit from reading/writing shared context files, this enables multi-step reasoning where agents build on each other's work.

- [x] Pipeline executor creates `.lazyaf-context/` on first step - `entrypoint.py:330-348`
- [x] Step logs written with naming based on step ID (if set) or index - `entrypoint.py:351-368`
- [x] Metadata written to `.lazyaf-context/metadata.json` - `entrypoint.py:371-388`
- [x] Context directory committed after each step - `entrypoint.py:390-426`
- [x] Merge action adds cleanup commit (removes `.lazyaf-context/`) - `pipeline_executor.py:642-651`
- [x] Users can squash-merge to keep upstream history clean (user behavior, not code)

**Step IDs for Stable References**:
Steps can have an optional `id` field for stable log file references:
```yaml
steps:
  - id: "tests"              # Optional stable identifier
    name: "Run Tests"
    type: script
    # ...
  - name: "Quick Lint"       # No ID - uses index-based naming
    type: script
    # ...
```

**Log File Naming**:
- With ID: `.lazyaf-context/id_<id>_NNN.log` (e.g., `id_tests_001.log`)
- Without ID: `.lazyaf-context/step_NNN_<name>.log` (e.g., `step_002_quick_lint.log`)

IDs enable stable references in agent prompts even if steps are reordered:
```yaml
prompt_template: |
  Review the test output in `.lazyaf-context/id_tests_*.log` and fix failures.
```

**Directory Structure** (committed to feature branch):
```
.lazyaf-context/
├── id_tests_001.log               # Step with id="tests" (stable reference)
├── step_002_quick_lint.log        # Step without ID (index-based)
├── id_analyze_003.log             # Step with id="analyze"
├── id_analyze_003.md              # Agent's notes (optional, agent-written)
├── test_results.json              # Structured test data (if available)
├── decisions.md                   # Accumulated decisions/learnings (agents can append)
└── metadata.json                  # {pipeline_run_id, steps_completed, step_id_map, ...}
```

**Key Behaviors**:
- Agents can READ from context (previous step logs, notes from earlier agents)
- Agents can WRITE to context (leave notes, analysis, decisions for future steps)
- Context accumulates across commits on the feature branch
- **On merge**: pipeline adds a final commit that removes `.lazyaf-context/`
- **User choice**: squash-merge to collapse all commits (including cleanup) into one clean commit

**Example Flow**:
```yaml
steps:
  - id: "tests"
    name: "Run Tests"
    type: script
    config:
      command: pytest -v  # Output → .lazyaf-context/id_tests_001.log
    continue_in_context: true

  - id: "fix"
    name: "Analyze & Fix"
    type: agent
    config:
      agent: "test-fixer"  # Reads id_tests_*.log, writes id_fix_002.md notes
    continue_in_context: true

  - name: "Generate Changelog"  # No ID - uses step_003_generate_changelog.log
    type: agent
    config:
      agent: "changelog-writer"  # Reads accumulated context, writes CHANGELOG entry
    on_success: "merge:main"  # Adds cleanup commit, then merges
```

**Merge Behavior**:
```yaml
# Default: add cleanup commit then merge (user can squash-merge)
on_success: "merge:main"

# Keep context (skip cleanup commit) - make sure to warn user to make sure they know what they are doing
on_success: "merge:main:keep-context"

# Process context before merge
- name: "Archive Context"
  type: script
  config:
    command: |
      # Do something creative with accumulated context
      cat .lazyaf-context/decisions.md >> docs/AI_DECISIONS.md
  on_success: "merge:main"  # Cleanup commit still added after this step
```

**Why Committed (not workspace-only)**:
- Context persists if pipeline is interrupted/resumed
- Git history shows agent reasoning evolution
- Can inspect context at any commit via git
- Agents can reference earlier context across multiple runs on same branch

**Concurrent Runs**:
Each pipeline run works on its own feature branch, so `.lazyaf-context/` is isolated per branch. Conflicts are unlikely since branches diverge before pipelines run.

**Explicit Non-Goals (This Phase)**:
- Default/golden-path agents that auto-use context (let patterns emerge first)
- Platform-level context templates
- Cross-pipeline context sharing

### 9.1c: Pipeline/Card Agent Parity ✅
**Goal**: Pipeline agent steps should have same capabilities as cards

- [x] Add `agent_file_ids` to pipeline step config (select agents)
- [x] Add `prompt_template` to pipeline step config
- [x] Update PipelineEditor to show agent selector for agent steps
- [x] Update pipeline executor to pass agent files to job

---

## Phase 10: Events & Triggers ✅
**Goal**: Enable the Card → Pipeline → Merge workflow

### 10a: Card Completion Trigger ✅
- [x] Add `triggers` field to Pipeline model (JSON array of TriggerConfig)
- [x] Add `trigger_context` field to PipelineRun model
- [x] TriggerConfig schema: `{type, config, enabled, on_pass, on_fail}`
- [x] When card status → done/in_review, check for matching pipelines
- [x] Auto-trigger pipeline with card context (branch, commit, card_id)
- [x] UI: Configure triggers in pipeline editor (type, status filter, actions)
- [x] Trigger actions: on_pass (merge/nothing), on_fail (fail/reject/nothing)

### 10b: Auto-Merge Action ✅
- [x] Pipeline completion executes trigger actions from context
- [x] `on_pass: "merge"` - merge card branch to default branch, mark card done
- [x] `on_pass: "merge:{branch}"` - merge to specific branch
- [x] `on_fail: "fail"` - mark card as failed (user can retry)
- [x] `on_fail: "reject"` - reject card back to todo
- [x] Merge uses internal git server
- [x] Conflict handling: fail with clear error

### 10c: Push Triggers ✅
- [x] Internal git server captures pushed refs after receive-pack
- [x] Push event fires trigger_service.on_push()
- [x] Pipeline trigger: `{type: "push", config: {branches: ["main", "dev"]}}`
- [x] Branch pattern matching with fnmatch (supports wildcards)
- [x] Push triggers don't show on_pass/on_fail UI (no card to act on)

---

## Phase 11: Agent Playground

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
┌─────────────────────────────────────────────────────────────────────┐
│                      AGENT PLAYGROUND TAB                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────────────┐  ┌──────────────────────────────────────┐│
│  │   AGENT CONFIG       │  │      STREAMING AGENT OUTPUT          ││
│  │                      │  │                                      ││
│  │  Repo: [dropdown]    │  │  I'll analyze the code structure     ││
│  │  Agent: [dropdown]   │  │  and make the requested changes.     ││
│  │  Runner: Claude/Gem  │  │                                      ││
│  │                      │  │  [Tool: Read] src/utils.ts           ││
│  │  Branch: [dropdown]  │  │  [Tool: Edit] src/utils.ts:42        ││
│  │                      │  │                                      ││
│  │  ┌────────────────┐  │  │  The function now handles edge       ││
│  │  │ Task Override  │  │  │  cases for empty input...█           ││
│  │  │ (optional)     │  │  │                                      ││
│  │  └────────────────┘  │  └──────────────────────────────────────┘│
│  │                      │                                          │
│  │  [▶ Test Once]       │  ┌──────────────────────────────────────┐│
│  │  [■ Cancel]          │  │         DIFF PREVIEW                 ││
│  │                      │  │                                      ││
│  │  ☐ Save to branch    │  │  src/utils.ts                        ││
│  │  agent-test/...      │  │  - const old = "foo";                ││
│  │                      │  │  + const new = "bar";                ││
│  └──────────────────────┘  └──────────────────────────────────────┘│
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
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
- [ ] Add `is_playground`, `playground_session_id`, `playground_save_branch` to QueuedJob
- [ ] Create `PlaygroundService` with `start_test()`, `stream_logs()`, `get_result()`
- [ ] Create `/playground/*` REST endpoints
- [ ] SSE streaming for runner logs
- [ ] Runner checks `is_playground` flag, skips card updates if true

#### Phase 11b: Frontend - Test Once Mode
- [ ] Add Playground tab to navigation
- [ ] Create `PlaygroundPage.svelte`
- [ ] Create `playground` store
- [ ] Repo/branch/agent/runner selectors
- [ ] Task override textarea
- [ ] "Test Once" button
- [ ] Log stream display (reuse LogViewer patterns)
- [ ] Connect to SSE endpoint

#### Phase 11c: Diff & Save
- [ ] Backend captures diff after runner completes
- [ ] Diff preview panel in frontend
- [ ] "Save to branch" checkbox
- [ ] Branch name input with auto-generate (`agent-test/<agent>-NNN`)
- [ ] Push changes to save branch on completion

#### Phase 11d: Cancellation
- [ ] Backend: `cancel_test()` kills runner process
- [ ] Frontend: Cancel button
- [ ] Graceful cleanup
- [ ] Status indicators

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

## Tech Debt (After Core Workflow Works)

### TD1: Entrypoint Refactor
**Trigger**: If entrypoint becomes hard to modify during Phase 10
- [ ] Extract test detection → `test_runner.py`
- [ ] Extract git operations → `git_helpers.py`
- [ ] Keep entrypoint as orchestrator

### TD2: Feature Consistency
- [ ] Cards and pipeline steps use same agent configuration UI
- [ ] Unified step config schema across cards and pipelines

### TD3: Pipeline Workspace Cleanup
- [ ] Clean up old pipeline workspace directories (`/workspace/{pipeline_run_id[:8]}/`)
- [ ] Add runner housekeeping to purge stale workspaces after pipeline completion
- [ ] Consider disk space monitoring and automatic cleanup thresholds

---

**Roadmap**:
- Phase 6: Polish - ongoing (quality of life)
- Phase 7: MCP Interface - ✅ COMPLETE (31 tools)
- Phase 8: Test Result Capture - ✅ COMPLETE
- Phase 8.5: CI/CD Foundation - ✅ COMPLETE
- Phase 9: Pipelines - ✅ COMPLETE
- Phase 9.1: Pipeline Polish - ✅ COMPLETE
  - 9.1a: Context Sharing - ✅ COMPLETE
  - 9.1b: File Persistence - ✅ COMPLETE
  - 9.1c: Agent Parity - ✅ COMPLETE
  - 9.1d: Context Directory - ✅ COMPLETE
- Phase 10: Events & Triggers - ✅ COMPLETE (card triggers, push triggers, auto-merge)
- **Phase 11: Agent Playground - NEXT** (rapid agent experimentation)
  - 11a: Foundation (backend service, SSE streaming, runner integration)
  - 11b: Frontend - Test Once mode
  - 11c: Diff capture & Save to branch
  - 11d: Cancellation support
  - 11e: Polish
  - Future: Test Continuous mode, sample text input, file-specific input
- Phase 9.5: Webhooks - deferred (external triggers from GitHub/Gitea)
- Phase 12: Reporting & Artifacts - future
