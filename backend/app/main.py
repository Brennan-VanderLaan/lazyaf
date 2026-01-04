from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import repos, cards, jobs, agent_files, pipelines, lazyaf_files
from app.routers import git, playground, models, steps, ws_runners, debug
from app.routers import test_api
from app.services.websocket import manager

# Import models to ensure they're registered with Base before init_db
import app.models  # noqa: F401

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import engine
    from app.services.playground_service import playground_service
    from app.services.execution.remote_executor import get_remote_executor
    from app.services.execution.job_recovery import get_job_recovery_service

    await init_db()

    # Start RemoteExecutor timeout monitor
    remote_executor = get_remote_executor()
    await remote_executor.start_monitor()

    await playground_service.start()

    # Recover orphaned steps on startup
    # (Commented out until we have proper DB session management)
    # job_recovery = get_job_recovery_service()
    # async with get_db_session() as db:
    #     await job_recovery.recover_orphaned_steps(db)

    yield

    await playground_service.stop()
    await remote_executor.stop_monitor()
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
app.include_router(agent_files.router)
app.include_router(pipelines.router)
app.include_router(lazyaf_files.router)
app.include_router(git.router)
app.include_router(playground.router)
app.include_router(playground.session_router)
app.include_router(models.router)
app.include_router(steps.router)
app.include_router(ws_runners.router)  # Phase 12.6: WebSocket runner endpoint
app.include_router(debug.router)  # Phase 12.7: Debug re-run endpoints

# Mount test API router only in test mode
if settings.test_mode:
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
