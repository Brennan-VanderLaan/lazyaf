
---

## Update & Migration Strategy

> **Goal**: Push updates with minimal impact to user flows. Migrations apply automatically. Runners survive backend restarts.

> **Scope**: This strategy describes the **target state** after Phase 12 is complete, where all
> job/runner state is persisted to the database. For the **current implementation** (pre-Phase 12),
> in-memory state (job queue, runner registrations) is lost on restart - use the "Interim Strategy"
> below until Phase 12 is implemented.

### Interim Strategy (Pre-Phase 12)

Until Phase 12 moves state to the database:

1. **Before restarting backend**: Wait for active jobs to complete (check UI)
2. **Queued jobs**: Will be lost - re-queue via UI after restart
3. **Runner registrations**: Runners auto-reconnect and re-register
4. **Use named volumes**: DB and git repos survive restart

```bash
# Safe restart (current architecture)
# 1. Check UI - wait for jobs to finish
# 2. Restart
docker compose up -d --build backend
# 3. Runners reconnect automatically
# 4. Re-queue any jobs that were pending
```

### Current Problems

1. **DB lost frequently**: `docker compose down` destroys volumes, losing all data
2. **Too aggressive restarts**: `docker compose up --build` rebuilds everything, no graceful shutdown
3. **Runner blocks forever**: If backend restarts mid-job, runner tries to submit work, backend doesn't recognize it, runner blocks waiting to submit before reconnecting
4. **No schema migrations**: Using `create_all()` which silently ignores schema changes on existing tables
5. **In-memory state lost**: Job queue, runner registrations, job assignments all vanish on restart

### Target Workflow

```bash
# Safe update (preserves data, runners can finish)
lazyaf update              # Or: docker compose pull && docker compose up -d

# What happens:
# 1. New containers start alongside old ones (rolling)
# 2. Backend drains: stops accepting new jobs, waits for active jobs to complete
# 3. Alembic migrations run automatically on startup
# 4. Old containers stop after drain timeout
# 5. Runners reconnect seamlessly to new backend
```

### Implementation

#### 1. Persistent Volumes (Don't Lose Data)

```yaml
# docker-compose.yml (SQLite mode - current)
volumes:
  lazyaf-data:      # SQLite DB + git repos + artifacts

services:
  backend:
    volumes:
      - lazyaf-data:/app/data
```

```yaml
# docker-compose.yml (PostgreSQL mode - future)
volumes:
  lazyaf-postgres:  # PostgreSQL data
  lazyaf-data:      # Git repos + artifacts (no SQLite)

services:
  postgres:
    image: postgres:16
    volumes:
      - lazyaf-postgres:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: lazyaf
      POSTGRES_USER: lazyaf
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U lazyaf"]
      interval: 5s
      timeout: 5s
      retries: 5

  backend:
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+asyncpg://lazyaf:${POSTGRES_PASSWORD}@postgres:5432/lazyaf
    volumes:
      - lazyaf-data:/app/data  # Git repos + artifacts only
```

**Rule**: `docker compose down` does NOT remove volumes. Use `docker compose down -v` only for intentional reset.

#### 2. Alembic Auto-Migration on Startup

```python
# backend/app/main.py
@app.on_event("startup")
async def startup():
    # Run migrations before anything else
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    # Then initialize services...
```

**Migration workflow for developers:**
```bash
# After changing models:
alembic revision --autogenerate -m "Add foo column to bars"
alembic upgrade head  # Test locally

# On deployment: migrations run automatically
```

#### 3. Runner Resilience (Don't Block Forever)

Current bug: Runner tries to submit completed work, backend doesn't recognize it, runner blocks.

**Fix in runner entrypoint:**
```python
def submit_job_result(job_id, result):
    """Submit with timeout and fallback."""
    for attempt in range(3):
        try:
            response = session.post(
                f"{BACKEND_URL}/api/runners/{RUNNER_ID}/complete",
                json=result,
                timeout=10
            )
            if response.status_code == 404:
                # Backend doesn't know us - re-register and retry
                log("Backend lost our registration, re-registering...")
                register_runner()
                continue
            return response
        except requests.exceptions.Timeout:
            log(f"Submit timeout, attempt {attempt + 1}/3")

    # After 3 failures, log locally and move on
    log(f"Failed to submit job {job_id}, saving locally for recovery")
    save_failed_submission(job_id, result)
    return None
```

**Key behaviors:**
- Timeout on submit (don't block forever)
- Re-register if backend returns 404
- Save failed submissions locally for manual recovery
- Move on to next job (don't get stuck)

#### 4. Graceful Backend Shutdown

```python
# backend/app/main.py
shutdown_event = asyncio.Event()

@app.on_event("shutdown")
async def shutdown():
    log("Shutdown initiated, draining...")
    shutdown_event.set()

    # Stop accepting new jobs
    job_queue.stop_accepting()

    # Wait for active jobs to complete (max 5 min)
    await runner_pool.drain(timeout=300)

    log("Drain complete, shutting down")
```

**Docker Compose config:**
```yaml
services:
  backend:
    stop_grace_period: 5m  # Give time for drain
    # SIGTERM triggers graceful shutdown
```

#### 5. Orphan Recovery on Startup

```python
@app.on_event("startup")
async def recover_orphans():
    """Mark stale in-progress items as failed."""
    cutoff = datetime.utcnow() - timedelta(minutes=30)

    # Find jobs that were "running" before we restarted
    stale_jobs = await db.execute(
        select(Job).where(
            Job.status == "running",
            Job.updated_at < cutoff
        )
    )

    for job in stale_jobs:
        job.status = "failed"
        job.error = "Backend restarted during execution"
        log(f"Marked orphaned job {job.id} as failed")

    # Same for pipeline runs, cards, etc.
    await db.commit()
```

#### 6. Execution Mode Recovery

**LocalExecutor (backend spawns containers via Docker SDK):**

On backend restart:
1. Query Docker for containers with label `lazyaf.step_execution_id`
2. For each container:
   - If `step_executions` row exists and status=`running`: reattach log stream, resume monitoring
   - If container exited while we were down: update status based on exit code
   - If no DB row exists: orphan container - kill and log warning
3. Resume timeout tracking from persisted `started_at` timestamp

```python
async def recover_local_containers():
    """Reattach to containers that were running when we died."""
    containers = docker.containers.list(filters={"label": "lazyaf.managed=true"})
    for container in containers:
        step_id = container.labels.get("lazyaf.step_execution_id")
        step = await db.get(StepExecution, step_id)
        if not step:
            log(f"Orphan container {container.id}, killing")
            container.kill()
        elif container.status == "exited":
            step.status = "completed" if container.attrs["State"]["ExitCode"] == 0 else "failed"
        else:
            # Still running - reattach
            await reattach_log_stream(container, step)
```

**RemoteExecutor (WebSocket push to runners):**

WebSocket connections die on backend restart. Recovery:
1. On restart, mark all runners as `disconnected` in database
2. Runners detect WebSocket close, enter reconnect loop (exponential backoff)
3. On reconnect: runner re-registers with labels via WebSocket
4. Backend checks for in-progress work assigned to that runner

```
Runner                           Backend
  |                                 |
  |  (backend restarts)             |
  |                                 |
  |  (WebSocket dies)               |
  |                                 |
  |--- WebSocket connect ---------->|
  |<-- connection accepted ---------|
  |                                 |
  |--- register {id, labels} ------>|
  |                                 |
  |    (backend checks: was this    |
  |     runner mid-job?)            |
  |                                 |
  |<-- "resume step X" or "idle" ---|
  |                                 |
  |  (back to normal)               |
```

**In-progress step handling:**
| Step Status | Runner Reconnects | Runner Doesn't Reconnect (30s) |
|-------------|-------------------|-------------------------------|
| `assigned` | Resume or reassign | Reassign to another runner |
| `running` | Runner continues, reports completion | Mark failed, re-queue if retries remain |
| `completing` | Wait for completion report | Mark failed |

#### 7. Debug Session Recovery

Debug sessions can last up to 4 hours and have special handling:

**State persistence:**
- `DebugSession` model persisted to database (session_id, state, expiry_at)
- Sidecar containers labeled with `lazyaf.debug_session_id`
- WebSocket terminal connections are ephemeral (client must reconnect)

**On backend restart:**
1. Query Docker for containers with label `lazyaf.debug_session_id`
2. Match to `DebugSession` rows in database
3. Resume timeout countdown from persisted `expiry_at`
4. Mark sessions as `waiting` (user must reconnect CLI)

```python
async def recover_debug_sessions():
    """Rediscover active debug sessions after restart."""
    containers = docker.containers.list(filters={"label": "lazyaf.debug_session_id"})
    for container in containers:
        session_id = container.labels["lazyaf.debug_session_id"]
        session = await db.get(DebugSession, session_id)
        if session and session.expiry_at > datetime.utcnow():
            session.status = "waiting_at_bp"  # User must reconnect
            log(f"Recovered debug session {session_id}")
        else:
            container.kill()  # Expired or orphaned
```

**Graceful shutdown with debug sessions:**
- If active debug sessions exist: warn user, extend drain timeout
- Or: `--force` flag to terminate debug sessions immediately
- Default: wait up to `min(remaining_session_time, 30min)`

#### 8. Workspace Snapshot Persistence

Workspace tarballs for cross-machine transfer are stored at:
`${LAZYAF_DATA_DIR}/snapshots/{pipeline_run_id}.tar.gz`

**This directory MUST be on a persistent volume.**

On backend restart:
- Existing snapshots remain available for download
- Interrupted uploads leave incomplete files - runner will retry
- Cleanup runs on pipeline completion (scheduled task)

```yaml
# Ensure snapshots survive restart
volumes:
  lazyaf-data:  # Contains: db/, git_repos/, snapshots/
```

#### 9. Frontend & External Connections

**Frontend WebSocket:**
- Frontend detects disconnect via WebSocket `onclose` event
- UI shows "Reconnecting..." indicator
- Exponential backoff: 1s, 2s, 4s, ... max 30s
- On reconnect: frontend refreshes state from REST API
- No data loss - all state is server-side

**MCP Server (Claude Desktop):**
- MCP runs in backend process - connections die on restart
- Claude Desktop handles reconnection automatically
- In-flight tool calls fail, Claude retries

**Webhooks during restart:**
- Webhooks arriving during restart window receive 503/connection refused
- GitHub/GitLab retry failed webhooks (typically 3x with backoff)
- For critical webhooks: consider nginx retry buffer (out of scope)
- `trigger_key` deduplication is persisted - survives restart

**Scheduled triggers:**
- Scheduler runs in backend process
- On restart: check for missed triggers since last run
- Fire missed triggers if within grace period (5 min default)
- Or: skip missed triggers, wait for next scheduled time

### Docker Compose Best Practices

```yaml
# docker-compose.yml
version: "3.8"

volumes:
  lazyaf-db:
  lazyaf-git:

services:
  backend:
    build: ./backend
    volumes:
      - lazyaf-db:/app/data
      - lazyaf-git:/app/git_repos
      - /var/run/docker.sock:/var/run/docker.sock  # For LocalExecutor
    stop_grace_period: 5m
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  runner-claude:
    build: ./runner-claude
    depends_on:
      backend:
        condition: service_healthy
    deploy:
      restart_policy:
        condition: on-failure
        delay: 5s
```

**Commands:**
```bash
# Normal update (preserves volumes)
docker compose pull
docker compose up -d --build

# Rebuild just backend (runners keep running)
docker compose up -d --build backend

# View what's running
docker compose ps

# Full reset (DESTROYS ALL DATA)
docker compose down -v  # Only when intentional!
```

### Migration Versioning

For breaking changes during early development:

```python
# backend/app/config.py
SCHEMA_VERSION = "2024.01.15"  # Bump on breaking changes

# backend/app/main.py
@app.on_event("startup")
async def check_schema_version():
    stored = await db.get_meta("schema_version")
    if stored and stored != SCHEMA_VERSION:
        if is_breaking_migration(stored, SCHEMA_VERSION):
            log(f"Breaking schema change {stored} -> {SCHEMA_VERSION}")
            log("Run: lazyaf db reset  (or docker compose down -v)")
            sys.exit(1)
```

### Database Compatibility (SQLite → PostgreSQL)

The strategy is designed to work with both SQLite (current) and PostgreSQL (future):

| Component | SQLite | PostgreSQL | Notes |
|-----------|--------|------------|-------|
| Alembic migrations | ✅ | ✅ | Same migrations work for both |
| Async driver | `aiosqlite` | `asyncpg` | Swap via `DATABASE_URL` |
| Row locking | File-level | `SELECT FOR UPDATE` | PostgreSQL enables true row locks |
| Advisory locks | File locking | `pg_advisory_lock()` | Better workspace locking with PG |
| Connection pooling | Not needed | Required | Add `pool_size`, `max_overflow` |
| JSON columns | TEXT with JSON | Native JSONB | Better query performance |

**Migration path (SQLite → PostgreSQL):**

```bash
# 1. Export data from SQLite
lazyaf db export --format json > backup.json

# 2. Switch DATABASE_URL to PostgreSQL
export DATABASE_URL=postgresql+asyncpg://...

# 3. Run migrations on fresh PostgreSQL
alembic upgrade head

# 4. Import data
lazyaf db import backup.json

# 5. Verify
lazyaf db verify
```

**Code patterns that work with both:**

```python
# Good: Use SQLAlchemy abstractions
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

async def upsert_runner(runner_id: str, data: dict):
    # SQLite fallback
    if db.dialect.name == "sqlite":
        existing = await db.get(Runner, runner_id)
        if existing:
            await db.execute(update(Runner).where(...).values(data))
        else:
            db.add(Runner(**data))
    else:
        # PostgreSQL native upsert
        stmt = pg_insert(Runner).values(data).on_conflict_do_update(...)
        await db.execute(stmt)

# Good: Advisory locks that work on both
async def with_workspace_lock(workspace_id: str):
    if db.dialect.name == "postgresql":
        await db.execute(text(f"SELECT pg_advisory_lock({hash(workspace_id)})"))
    else:
        # SQLite: use file-based lock or skip (single-writer anyway)
        pass
```

**When to switch to PostgreSQL:**
- Multiple backend instances needed (SQLite is single-writer)
- Row-level locking becomes critical (high concurrency)
- Need better JSON querying (labels, step config)
- Connection pooling for many runners

### Implementation Checklist

**Phase 1: Foundation (Do First)**
- [ ] Set up Alembic with async SQLAlchemy
- [ ] Generate initial migration from existing models
- [ ] Add auto-migration to startup
- [ ] Add named volumes to docker-compose.yml
- [ ] Document `docker compose down` vs `docker compose down -v`
- [ ] Use `DATABASE_URL` env var (enables SQLite/PostgreSQL switching)

**Phase 2: Runner Resilience**
- [ ] Add timeout to job submission
- [ ] Re-register on 404 response
- [ ] Save failed submissions locally
- [ ] Don't block forever - move on after retries

**Phase 3: Graceful Shutdown**
- [ ] Add drain logic to backend shutdown
- [ ] Set `stop_grace_period` in docker-compose
- [ ] Add `job_queue.stop_accepting()`
- [ ] Wait for active jobs before exit

**Phase 4: Orphan Recovery**
- [ ] Scan for stale "running" items on startup
- [ ] Mark as failed with clear error message
- [ ] Log what was recovered

**Effort**: 1-2 weeks total
**Outcome**: Updates don't lose data, runners survive restarts, no more blocking forever