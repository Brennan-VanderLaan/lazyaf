import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings

# Configure logging to show INFO level for debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
from app.database import init_db
from app.routers import repos, cards, jobs, runners, agent_files, pipelines, lazyaf_files
from app.routers import git, playground, models, steps, spec
from app.services.websocket import manager

# Import models to ensure they're registered with Base before init_db
import app.models  # noqa: F401

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from app.database import engine, async_session
    from app.services.runner_pool import runner_pool
    from app.services.playground_service import playground_service
    from app.services.execution import recover_orphaned_executions
    from app.services.workspace.population import pre_pull_images
    from app.services.workspace_service import start_orphan_audit
    from app.services.control_layer import auth as step_auth

    # Step auth secret is settings-driven (LAZYAF_STEP_AUTH_SECRET); the
    # default equals the module's dev constant so tests without lifespan
    # see identical behavior.
    step_auth.set_secret_key(settings.step_auth_secret)

    await init_db()

    # Recover any orphaned step executions from previous crash/restart
    async with async_session() as session:
        recovered = await recover_orphaned_executions(session)
        if recovered:
            logging.getLogger(__name__).info(
                f"Recovered {len(recovered)} orphaned step executions on startup"
            )

    await runner_pool.start()
    await playground_service.start()
    # Periodic workspace orphan audit (Phase 12.2-INT). First sweep runs
    # immediately for startup crash recovery; the loop owns its DB sessions
    # and survives sweep failures (logged, retried next interval).
    orphan_audit_task = start_orphan_audit()()
    # Pre-pull the step/clone images in the background (non-blocking) so the
    # first pipeline run doesn't sit silently through an implicit pull.
    # pre_pull_images never raises; failures are logged and the implicit
    # pull on first use remains the safety net.
    pre_pull_task = asyncio.create_task(pre_pull_images(), name="image-pre-pull")
    yield
    for task in (pre_pull_task, orphan_audit_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await playground_service.stop()
    await runner_pool.stop()
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    description="Visual orchestrator for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(repos.router)
app.include_router(cards.router)
app.include_router(jobs.router)
app.include_router(runners.router)
app.include_router(agent_files.router)
app.include_router(pipelines.router)
app.include_router(lazyaf_files.router)
app.include_router(git.router)
app.include_router(playground.router)
app.include_router(playground.session_router)
app.include_router(models.router)
app.include_router(steps.router)
app.include_router(spec.router)

if settings.test_mode:
    # e2e-only reset/seed surface; module stays unimported when the flag is off
    from app.routers import test_api
    app.include_router(test_api.router)


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}


@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.app_name}", "docs": "/docs"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    import asyncio
    await manager.connect(websocket)
    try:
        while True:
            try:
                # Use wait_for with timeout to allow shutdown to interrupt
                message = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                if message["type"] == "websocket.disconnect":
                    break
            except asyncio.TimeoutError:
                # Send ping to keep alive, continue loop
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
            except asyncio.CancelledError:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.disconnect(websocket)
