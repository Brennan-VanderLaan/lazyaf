# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

## Architecture Overview

```
+-----------------------------------------------------------------------+
|                           FRONTEND                                     |
|   +---------------------------------------------------------------+   |
|   |                    Svelte App                                  |   |
|   |   [To Do]    [In Prog]   [In Review]   [Done]                 |   |
|   |    [card]     [card]       [card]      [card]                 |   |
|   +---------------------------------------------------------------+   |
+-----------------------------------------------------------------------+
                              | HTTP/WebSocket
                              v
+-----------------------------------------------------------------------+
|                           BACKEND                                      |
|   +---------------------------------------------------------------+   |
|   |                    FastAPI Server                              |   |
|   |  - REST API (cards, repos, jobs)                              |   |
|   |  - WebSocket (real-time status updates)                       |   |
|   |  - Runner Pool Manager                                        |   |
|   |  - Job Queue                                                  |   |
|   +---------------------------------------------------------------+   |
|                              |                                         |
|                              v                                         |
|   +---------------------------------------------------------------+   |
|   |                    SQLite Database                             |   |
|   |  - repos, cards, jobs, runners                                |   |
|   +---------------------------------------------------------------+   |
+-----------------------------------------------------------------------+
                              | Docker API
                              v
+-----------------------------------------------------------------------+
|                        RUNNER POOL                                     |
|   +-----------+   +-----------+   +-----------+                       |
|   | Runner 1  |   | Runner 2  |   | Runner N  |                       |
|   | [Docker + |   | [Docker + |   | [Docker + |                       |
|   |  Claude]  |   |  Claude]  |   |  Claude]  |                       |
|   +-----------+   +-----------+   +-----------+                       |
+-----------------------------------------------------------------------+
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
|-- lib/
|   |-- components/
|   |   |-- Board.svelte           # Kanban board container
|   |   |-- Column.svelte          # Single column (To Do, etc.)
|   |   |-- Card.svelte            # Draggable card
|   |   |-- CardModal.svelte       # Create/edit card
|   |   |-- RepoSelector.svelte    # Repo picker
|   |   |-- JobStatus.svelte       # Job progress indicator
|   |   +-- RunnerPool.svelte      # Runner status display
|   |-- stores/
|   |   |-- cards.ts               # Card state
|   |   |-- repos.ts               # Repo state
|   |   |-- jobs.ts                # Job state
|   |   +-- websocket.ts           # WS connection
|   +-- api/
|       +-- client.ts              # API client
|-- routes/
|   |-- +page.svelte               # Main board view
|   +-- +layout.svelte             # App layout
+-- app.html
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
+-----------------------------------------------------------------------+
|                        INTERNAL GIT SERVER                             |
|   +---------------------------------------------------------------+   |
|   |                    HTTP Smart Protocol                         |   |
|   |  GET  /git/{repo_id}.git/info/refs   (clone discovery)        |   |
|   |  POST /git/{repo_id}.git/git-upload-pack   (fetch)            |   |
|   |  POST /git/{repo_id}.git/git-receive-pack  (push)             |   |
|   +---------------------------------------------------------------+   |
|                              |                                         |
|                              v                                         |
|   +---------------------------------------------------------------+   |
|   |                    Bare Git Repos                              |   |
|   |  backend/git_repos/{repo_id}.git/                             |   |
|   +---------------------------------------------------------------+   |
+-----------------------------------------------------------------------+
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
+-- mcp/
    |-- __init__.py
    |-- __main__.py       # Entry point for stdio transport
    +-- server.py         # MCP server implementation (all tools)
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
+-----------------------------------------------------------------------------+
|                            PIPELINE SYSTEM                                   |
|                                                                              |
|  +-------------+    +-------------+    +-------------+    +-------------+   |
|  |   TRIGGER   | -> |  PIPELINE   | -> |    STEPS    | -> |   RESULTS   |   |
|  |             |    |             |    |             |    |             |   |
|  | - Manual    |    | - Ordered   |    | - Agent     |    | - Pass/Fail |   |
|  | - Webhook   |    | - Branching |    | - Script    |    | - Logs      |   |
|  | - Git Push  |    | - Parallel  |    | - Docker    |    | - Artifacts |   |
|  | - Schedule  |    |   (future)  |    |             |    |             |   |
|  +-------------+    +-------------+    +-------------+    +-------------+   |
|                                                                              |
|  Step Types:                                                                 |
|  +-------------------------------------------------------------------------+ |
|  |  AGENT          |  SCRIPT           |  DOCKER                          | |
|  |  Claude/Gemini  |  Shell commands   |  Command in specified image      | |
|  |  implements     |  lint, test,      |  isolated builds, custom         | |
|  |  features       |  coverage, build  |  environments                    | |
|  +-------------------------------------------------------------------------+ |
+-----------------------------------------------------------------------------+
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
|-- backend/
|   |-- app/
|   |   |-- __init__.py
|   |   |-- main.py              # FastAPI app
|   |   |-- config.py            # Settings
|   |   |-- database.py          # SQLAlchemy setup
|   |   |-- models/
|   |   |   |-- __init__.py
|   |   |   |-- repo.py
|   |   |   |-- card.py
|   |   |   |-- job.py
|   |   |   +-- runner.py
|   |   |-- routers/
|   |   |   |-- __init__.py
|   |   |   |-- repos.py
|   |   |   |-- cards.py
|   |   |   |-- jobs.py
|   |   |   |-- runners.py
|   |   |   +-- git.py           # Git HTTP smart protocol
|   |   |-- services/
|   |   |   |-- __init__.py
|   |   |   |-- runner_pool.py
|   |   |   |-- job_queue.py
|   |   |   |-- websocket.py
|   |   |   +-- git_server.py    # Bare repo management
|   |   |-- mcp/                  # MCP server (Phase 7)
|   |   |   |-- __init__.py
|   |   |   |-- server.py         # MCP server entry point
|   |   |   +-- tools.py          # Tool definitions
|   |   +-- schemas/
|   |       |-- __init__.py
|   |       +-- ...              # Pydantic models
|   |-- git_repos/               # Internal bare git repos
|   |   +-- {repo_id}.git/
|   |-- runner/
|   |   |-- Dockerfile
|   |   +-- entrypoint.py
|   |-- pyproject.toml
|   +-- alembic/                 # DB migrations
|-- cli/                         # LazyAF CLI tool
|   |-- pyproject.toml
|   +-- lazyaf/
|       +-- cli.py               # ingest, land commands
|-- frontend/
|   |-- src/
|   |   +-- ...                  # Svelte app
|   |-- package.json
|   +-- vite.config.ts
|-- docker-compose.yml
|-- dream.txt                    # Vision doc
|-- PLAN.md                      # This file
+-- README.md
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
**Next**: Phase 12 (Runner Architecture Refactor) - start with 12.0 (Unify Entrypoints)

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
|-- pipelines/
|   |-- test-suite.yaml       # One pipeline per file
|   |-- deploy.yaml
|   +-- nightly-build.yaml
+-- agents/
    |-- test-fixer.yaml       # One agent per file
    |-- code-reviewer.yaml
    +-- unity-specialist.yaml
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
|-- id_tests_001.log               # Step with id="tests" (stable reference)
|-- step_002_quick_lint.log        # Step without ID (index-based)
|-- id_analyze_003.log             # Step with id="analyze"
|-- id_analyze_003.md              # Agent's notes (optional, agent-written)
|-- test_results.json              # Structured test data (if available)
|-- decisions.md                   # Accumulated decisions/learnings (agents can append)
+-- metadata.json                  # {pipeline_run_id, steps_completed, step_id_map, ...}
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
| Different machines | Git-based sync (push after step, pull before) |
| Kubernetes | PersistentVolumeClaim |

---

### Lifecycle State Machines

All state transitions are guarded by locks and idempotency keys to prevent race conditions and duplicate executions.

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
| Step execution | Idempotency key | Unique `execution_key` |
| Workspace access | Shared/Exclusive | Redis or DB advisory lock |
| Pipeline execution | Exclusive | One executor per pipeline run |
| Trigger dedup | Time-windowed key | Unique `trigger_key` within window |

#### Crash Recovery

On backend restart:
1. **Steps**: Find non-terminal steps, reattach to running containers or re-queue
2. **Pipelines**: Resume execution (idempotent - skips completed steps)
3. **Workspaces**: Audit for orphans, cleanup stale volumes
4. **Runners**: Mark as dead, wait for reconnection

---

### Phase 12.0: Unify Runner Entrypoints
**Goal**: Fix immediate pain, unblock future phases

The current entrypoints are ~1800 lines each with 95% duplication. This phase extracts common code into a shared package.

- [ ] Create `runner-common/` package with shared utilities
  - `git_helpers.py` - clone, branch, push, commit operations
  - `context_helpers.py` - `.lazyaf-context/` management
  - `job_helpers.py` - heartbeat, logging, status reporting
  - `test_helpers.py` - test detection and execution
- [ ] Create unified entrypoint that dispatches by agent type
- [ ] Reduce Claude/Gemini-specific code to ~100 lines each (just CLI invocation)
- [ ] Verify existing pipelines still work

**Effort**: 2-3 days
**Risk**: Low
**Outcome**: Maintainable entrypoints, foundation for new architecture

---

### Phase 12.1: LocalExecutor + Step State Machine
**Goal**: Instant step execution with proper state management

The fast path - backend spawns containers directly, with full lifecycle tracking.

- [ ] Implement Step state machine (pending → preparing → running → completing → completed/failed)
- [ ] Create `StepExecution` model with `execution_key` for idempotency
  ```python
  class StepExecution:
      execution_key: str  # "{pipeline_run_id}:{step_index}:{attempt}" - UNIQUE
      status: StepStatus  # State machine state
      container_id: str | None
      exit_code: int | None
  ```
- [ ] Create `LocalExecutor` service in `backend/app/services/execution/`
  ```python
  class LocalExecutor:
      """Spawns containers directly via Docker SDK. Zero latency."""
      async def execute_step(request: StepExecutionRequest) -> AsyncGenerator[str, StepResult]
      # Idempotent - returns existing execution if execution_key exists
  ```
- [ ] Add Docker SDK (`docker` package) to backend dependencies
- [ ] Mount Docker socket to backend container in docker-compose
- [ ] Timeout handling with automatic container kill
- [ ] Container crash detection and proper state transition to `failed`
- [ ] Real-time log streaming from container to pipeline executor
- [ ] Crash recovery: on startup, find orphaned steps and re-queue or fail them
- [ ] Test: idempotency (same request twice returns same result)

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Local dev is instant with proper state tracking and crash recovery

---

### Phase 12.2: Workspace State Machine & Pipeline Integration
**Goal**: Proper workspace lifecycle with locking and cleanup

- [ ] Implement Workspace state machine (creating → ready ↔ in_use → cleaning → destroyed)
- [ ] Create `Workspace` model with state and use_count
  ```python
  class Workspace:
      id: str  # "lazyaf-ws-{pipeline_run_id}"
      status: WorkspaceStatus
      use_count: int  # For concurrent step access
      pipeline_run_id: str
  ```
- [ ] Workspace locking (exclusive for create/cleanup, shared for step execution)
- [ ] Idempotent workspace creation (`get_or_create_workspace`)
- [ ] Create `ExecutionRouter` that routes steps to appropriate executor
- [ ] Update `pipeline_executor.py` to use ExecutionRouter instead of job queue
- [ ] Pipeline run state machine (pending → preparing → running → completing → completed)
- [ ] Trigger deduplication with `trigger_key` (prevents duplicate pipeline runs)
- [ ] Exactly-once step execution (skip steps with completed `execution_key`)
- [ ] Workspace cleanup on pipeline completion
- [ ] Orphan detection: periodic audit finds abandoned workspaces
- [ ] Test: multi-step pipeline with different images, crash recovery

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Robust workspace lifecycle, exactly-once execution, no orphaned resources

---

### Phase 12.3: Control Layer & Step Images
**Goal**: Proper container communication and base images

- [ ] Create control layer script (`/control/run.py`)
  - Reads step config from `/workspace/.control/step_config.json`
  - Reports status to backend (running, completed, failed)
  - Streams logs to backend
  - Heartbeat during long operations
- [ ] Create base image (`lazyaf-base`)
  - Python 3.12-slim + git + curl + control layer
  - `ENTRYPOINT ["python", "/control/run.py"]`
- [ ] Create agent images inheriting from base
  - `lazyaf-claude`: base + Claude CLI + agent wrapper
  - `lazyaf-gemini`: base + Gemini CLI + agent wrapper
- [ ] Workspace HOME persistence (`HOME=/workspace/home`)
  - pip/npm/uv caches persist across steps
  - `~/.local/bin` for user-installed tools
- [ ] New API endpoints for step containers
  - `POST /api/steps/{step_id}/status`
  - `POST /api/steps/{step_id}/logs`
  - `POST /api/steps/{step_id}/heartbeat`
- [ ] Test: agent step → script step → agent step with shared HOME

**Effort**: 1-1.5 weeks
**Risk**: Medium
**Outcome**: Steps run in proper containers with backend communication

---

### Phase 12.4: Migrate Script/Docker Steps
**Goal**: All non-agent steps use new architecture

- [ ] Pipeline executor routes script/docker steps through orchestrator
- [ ] Remove `execute_script_step` and `execute_docker_step` from runner entrypoints
- [ ] Steps can specify custom images in pipeline YAML
- [ ] Migrate test-suite.yaml to use pre-built image
- [ ] Create example `lazyaf-test-runner` Dockerfile with uv + deps
- [ ] Test: existing pipelines work, new multi-image pipelines work

**Effort**: 1 week
**Risk**: Medium (migration path)
**Outcome**: Script/docker steps don't need runners

---

### Phase 12.5: Migrate Agent Steps
**Goal**: Agent steps also use new architecture

- [ ] Agent steps spawn ephemeral containers via orchestrator
- [ ] Agent wrapper script handles Claude/Gemini CLI invocation
- [ ] Remove old runner polling infrastructure
- [ ] Runners no longer long-lived - spawned per step
- [ ] Test: Claude → script → Gemini pipeline works

**Effort**: 1-1.5 weeks
**Risk**: Higher (changes agent execution model)
**Outcome**: All step types use unified architecture

---

### Phase 12.6: RemoteExecutor & Runner State Machine
**Goal**: Millisecond-latency job assignment with proper connection lifecycle

Event-driven architecture - no polling, backend pushes jobs immediately.

- [ ] Implement Runner state machine (disconnected → connecting → idle → assigned → busy)
- [ ] Create `Runner` model with state tracking
  ```python
  class Runner:
      id: str
      status: RunnerStatus  # State machine state
      labels: dict  # {"arch": "arm64", "has": "gpio,camera"}
      current_step_id: str | None
      last_heartbeat: datetime
  ```
- [ ] Create `RemoteExecutor` service in backend
  ```python
  class RemoteExecutor:
      """Pushes jobs to remote runners via WebSocket."""
      async def register_runner(runner_id, labels, websocket) -> None
      async def execute_step(runner_id, step_config) -> AsyncGenerator[str, StepResult]
      # Handles ACK timeout, heartbeat monitoring, death detection
  ```
- [ ] WebSocket endpoint for runner connections (`/ws/runner`)
  - Registration with auth timeout (10s)
  - ACK required for job assignment (5s timeout)
  - Heartbeat monitoring (30s death timeout)
  - Graceful drain for shutdown
- [ ] Job recovery on runner death
  - If runner dies mid-job, re-queue step if still in `running` state
  - Prevents duplicate execution if completion message was lost
- [ ] Reconnection handling
  - Same runner_id can reconnect after death
  - Rejects duplicate connections from same runner_id
- [ ] Create `runner-agent` package (runs on target machines)
  - Connects to backend via WebSocket (NAT-friendly)
  - Sends ACK on job receipt
  - Heartbeat thread during execution
  - Auto-reconnect on disconnect
- [ ] `NativeOrchestrator` for embedded devices
  - Runs steps directly on host (no Docker)
  - Git-based workspace sync
- [ ] Test: runner death mid-job → step re-queued → new runner picks up

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

**Effort**: 2-3 weeks
**Risk**: Medium
**Outcome**: Operators can re-run failed pipelines with breakpoints and connect via CLI to debug

---

### Phase 12.8: Cleanup & Polish
**Goal**: Remove legacy code, document new model

- [ ] Remove old runner entrypoints (archive for reference)
- [ ] Update docker-compose for new architecture
- [ ] Remove `runner_pool.py` polling infrastructure
- [ ] Documentation: runner deployment, custom images, step requirements
- [ ] Example Dockerfiles for common step images

**Effort**: 1 week
**Outcome**: Clean, documented system

---

### Phase 12.9: Kubernetes Orchestrator (Future)
**Goal**: Same code works on Kubernetes

- [ ] Implement `KubernetesOrchestrator`
- [ ] PersistentVolumeClaims for workspaces
- [ ] K8s Jobs for step execution
- [ ] Node selectors based on runner labels
- [ ] Test in K8s environment

**Effort**: 2-3 weeks when needed
**Outcome**: Production-ready K8s deployment

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
- Phase 11: Agent Playground - deferred (rapid agent experimentation)
- **Phase 12: Runner Architecture Refactor - NEXT** (multi-image pipelines, hardware runners)
  - 12.0: Unify Runner Entrypoints (2-3 days) - fix immediate pain
  - 12.1: LocalExecutor + Step State Machine (~1.5 weeks) - zero latency + idempotency
  - 12.2: Workspace State Machine + Pipeline Integration (~1.5 weeks) - locking, cleanup, exactly-once
  - 12.3: Control Layer & Step Images (~1.5 weeks) - proper container communication
  - 12.4: Migrate Script/Docker Steps (~1 week) - cut over to new architecture
  - 12.5: Migrate Agent Steps (~1.5 weeks) - agents as ephemeral containers
  - 12.6: RemoteExecutor + Runner State Machine (~2 weeks) - WebSocket push, failure recovery
  - 12.7: Debug Mode (~2-3 weeks) - SSH/web terminal into workspaces, breakpoints, auto-debug on failure
  - 12.8: Cleanup & Polish (~1 week)
  - 12.9: Kubernetes Orchestrator (future)
- Phase 9.5: Webhooks - deferred (external triggers from GitHub/Gitea)
- Phase 13: Reporting & Artifacts - future
