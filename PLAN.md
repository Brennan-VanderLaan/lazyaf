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
- [ ] Add card filtering/search
- [ ] Runner scaling controls
- [ ] Basic auth (optional, local tool)
- [ ] Persist runner pool config

**Deliverable**: You're using this for real work

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

**Completed**: Phases 1-5
**Current**: Phase 6 (Polish)

The core workflow is functional:
1. Ingest repos via CLI
2. Create cards describing features
3. Start work → agent clones, implements, pushes
4. Review diffs in card modal
5. Approve/Reject to complete cycle

**Ready for Phase 6 polish items and real-world usage testing.**
