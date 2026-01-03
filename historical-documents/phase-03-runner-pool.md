# Phase 3: Runner Pool (including 3.5 and 3.75)

> **Status**: COMPLETE
> **Goal**: Docker-based runner pool that can execute commands

---

## Phase 3: Runner Pool Core

### Completed Tasks

- [x] Create runner Docker image
- [x] Implement Runner model and pool manager
- [x] Runner registration/heartbeat system
- [x] Job queue (in-memory for now)
- [x] Job assignment logic
- [x] Runner status API

### Deliverable

Can spawn runners and assign dummy jobs

---

## Phase 3.5: Runner UI/UX

> **Goal**: Visibility into runner pool and easy agent management

### Completed Tasks

- [x] Add runner types and API client to frontend
- [x] Create runner store with 2s polling
- [x] Build RunnerPanel component (pool stats, +/- scaling)
- [x] Add individual runner list with status badges
- [x] Backend endpoint for docker run command generation
- [x] Docker command modal (placeholders + copy with secrets button)

### Deliverable

Can see runner status, scale pool, and copy docker commands to spin up runners

---

## Phase 3.75: Internal Git Server

> **Goal**: Host repos internally for iteration isolation - don't pollute real remotes until ready

### Motivation

During active development/iteration, we don't want to push experimental branches and PRs to GitHub/GitLab. The internal git server lets agents work in isolation. When satisfied with results, users can "land" changes to the real remote.

### Architecture

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

### Flows

1. **Ingest**: User runs CLI -> pushes local repo -> stored in internal git server
2. **Agent Work**: Runner clones from internal server -> makes changes -> pushes back
3. **Land**: User pushes approved branch to real remote (GitHub/GitLab)

### Sub-phases

**Phase 3.75a: Git Server Foundation** (COMPLETE)
- [x] Add dulwich dependency (pure Python git library)
- [x] Create `git_server.py` service for bare repo management
- [x] Create `git.py` router with HTTP smart protocol endpoints
- [x] Update Repo model (add `is_ingested`, remove `path` requirement)
- [x] Add `/api/repos/ingest` endpoint

**Phase 3.75b: Ingest CLI** (COMPLETE)
- [x] Create `cli/` package with Click
- [x] `lazyaf ingest /path/to/repo --name NAME` command
- [x] CLI calls ingest API, then `git push` to internal server
- [x] Support `--branch` flag to specify branch to push
- [x] Support `--all-branches` flag to push all branches

**Phase 3.75c: Agent Integration** (COMPLETE)
- [x] Update runner entrypoint to clone from internal git URL
- [x] Runner pushes feature branches back to internal server (not origin)
- [x] Skip PR creation when working against internal server
- [x] Track branches per repo via /repos/{id}/branches endpoint

**Phase 3.75d: Land Flow** (COMPLETE)
- [x] CLI `lazyaf land {repo_id} --branch {branch}` command
- [x] Fetches from internal, pushes to real remote
- [x] Optional `--pr` flag to create PR via gh CLI
- [ ] UI button to trigger land flow (later - Phase 6)

### Tech Stack

- `dulwich` - Pure Python git implementation, embeds in FastAPI
- HTTP smart protocol - Works through firewalls, supports auth headers
- Bare repos stored in `backend/git_repos/`

### Deliverable

Can ingest local repos, agents work in isolation, land changes when ready
