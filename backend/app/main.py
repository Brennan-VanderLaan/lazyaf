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
from app.routers import git, playground, models, steps, spec, test_results
from app.routers import experiments, debug
from app.routers import ws_runners
from app.services.websocket import manager

# Import models to ensure they're registered with Base before init_db
import app.models  # noqa: F401

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    import os

    from app.database import engine, async_session
    from app.services.playground_service import playground_service
    from app.services.execution import recover_orphaned_executions
    from app.services.execution.runner_dispatcher import runner_dispatcher
    from app.services.execution.runner_registry import runner_registry
    from app.services.workspace.population import pre_pull_images
    from app.services.workspace_service import start_orphan_audit
    from app.services.control_layer import auth as step_auth

    log = logging.getLogger(__name__)

    # Step auth secret is settings-driven (LAZYAF_STEP_AUTH_SECRET); the
    # default equals the module's dev constant so tests without lifespan
    # see identical behavior.
    step_auth.set_secret_key(settings.step_auth_secret)

    await init_db()

    # The runner registry is per-PROCESS: a multi-worker uvicorn would route
    # assignments to a worker holding no socket for the target runner. That
    # is a stated limit, not a hidden one - say so at startup rather than
    # letting an operator discover it as intermittent "no runner matched".
    try:
        concurrency = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        concurrency = 1
    if concurrency > 1:
        log.warning(
            "WEB_CONCURRENCY=%s: the runner registry and dispatcher are "
            "per-process, so runner assignments will only work on the worker "
            "that holds each runner's WebSocket. Run single-worker until the "
            "registry.send() fan-out seam is implemented.",
            concurrency,
        )

    async with async_session() as session:
        # ORDER MATTERS. Bootstrap FIRST: no connection survives a restart,
        # so every runner row is forced `disconnected` with a NULL
        # websocket_id before any socket can connect. Only then does the
        # orphan sweep run - it decides a remote step is stranded by looking
        # at its runner's status, and a stale "busy" row left over from the
        # previous process would hide the very steps that need requeueing.
        disconnected = await runner_registry.bootstrap(session)
        if disconnected:
            log.info(
                f"Marked {disconnected} runner row(s) disconnected on startup"
            )

        # Local executions are failed (their containers died with us); remote
        # ones go back to pending for the dispatcher.
        recovered = await recover_orphaned_executions(session)
        if recovered:
            log.info(
                f"Recovered {len(recovered)} orphaned step executions on startup"
            )

        # 12.7 contract C20: a paused debug gate is an in-process task, so a
        # restart leaves the session and its run half-alive forever. End
        # them honestly and remove any stray sidecar. Never raises on a
        # Docker-less host - the sidecar sweep logs and returns 0.
        from app.services.execution.debug_session_service import (
            debug_session_service,
        )

        swept = await debug_session_service.sweep_paused_sessions(session)
        if swept:
            log.info(
                f"Ended {swept} debug session(s) paused across a restart"
            )

    # The dispatcher installs its wake hook on the registry and its requeue
    # hook on the job recovery service, then owns the assignment loop.
    await runner_dispatcher.start(async_session)

    await playground_service.start()

    # 12.6.5: re-pump any experiment a restart stalled. Runs AFTER the
    # dispatcher and playground service are up because it dispatches real
    # pipeline runs. Never raises; POST /api/experiments/{id}/resume
    # remains the guaranteed path.
    from app.services.experiment_service import resume_stalled_experiments

    resumed = await resume_stalled_experiments()
    if resumed:
        log.info(f"Resumed {resumed} experiment(s) stalled by a restart")
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
    # Symmetric shutdown: tell every runner to finish up and go away BEFORE
    # the dispatcher stops handing out work, so a drain never races a fresh
    # assignment onto a socket that is about to close.
    try:
        await runner_registry.drain("backend shutting down")
    except Exception:
        log.exception("runner drain failed during shutdown")
    await runner_dispatcher.stop()
    await playground_service.stop()
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
app.include_router(test_results.router)
app.include_router(experiments.router)
# The debug router carries both the 12.7 HTTP surface and the terminal
# WebSocket (/ws/debug/...); like ws_runners it declares its own paths.
app.include_router(debug.router)
# The runner WebSocket (/ws/runner). Registered here rather than under an
# /api prefix: it is a transport, not a REST surface.
app.include_router(ws_runners.router)

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
