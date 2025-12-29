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
    path: str                    # Local path to repo
    remote_url: str | None       # GitHub remote
    default_branch: str          # e.g., "dev" or "main"
    created_at: datetime
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
POST   /api/repos              # Attach a repo
GET    /api/repos/{id}         # Get repo details
DELETE /api/repos/{id}         # Detach repo
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

- [ ] Create runner Docker image
- [ ] Implement Runner model and pool manager
- [ ] Runner registration/heartbeat system
- [ ] Job queue (in-memory for now)
- [ ] Job assignment logic
- [ ] Runner status API

**Deliverable**: Can spawn runners and assign dummy jobs

### Phase 4: Agent Integration
**Goal**: Cards trigger Claude Code, results in PRs

- [ ] Runner entrypoint script (clone, branch, invoke Claude)
- [ ] Claude Code invocation with card context
- [ ] PR creation via gh CLI
- [ ] Job status callbacks
- [ ] Link PRs back to cards
- [ ] WebSocket status updates

**Deliverable**: Creating a card and clicking "Start" produces a real PR

### Phase 5: Review Flow
**Goal**: Complete the human review loop

- [ ] "Approve" action merges PR
- [ ] "Reject" action closes PR, resets card
- [ ] Job logs viewer
- [ ] Error handling and retry

**Deliverable**: Full loop from card → PR → merge works

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
│   │   │   └── runners.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── runner_pool.py
│   │   │   ├── job_queue.py
│   │   │   └── websocket.py
│   │   └── schemas/
│   │       ├── __init__.py
│   │       └── ...              # Pydantic models
│   ├── runner/
│   │   ├── Dockerfile
│   │   └── entrypoint.py
│   ├── pyproject.toml
│   └── alembic/                 # DB migrations
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

## Next Steps

1. Review this plan
2. Approve or request changes
3. Begin Phase 1 implementation
