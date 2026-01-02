from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import repos, cards, jobs, runners, agent_files, pipelines, lazyaf_files
from app.routers import git, playground, models
from app.services.websocket import manager

# Import models to ensure they're registered with Base before init_db
import app.models  # noqa: F401

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import engine
    from app.services.runner_pool import runner_pool
    from app.services.playground_service import playground_service

    await init_db()
    await runner_pool.start()
    await playground_service.start()
    yield
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
