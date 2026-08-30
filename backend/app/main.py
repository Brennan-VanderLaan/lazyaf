import logging
import math
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm.exc import StaleDataError

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
from app.routers import model_endpoints
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


# =============================================================================
# The error boundary
# =============================================================================
#
# Before this existed, an exception that escaped a route escaped the whole
# ASGI app, and that cost the UI TWO requests, not one:
#
#   1. uvicorn answered the failed request with the literal bytes
#      ``Internal Server Error`` and ``content-type: text/plain``. A frontend
#      on the error path does ``res.json()``, which throws a SECOND time - so
#      the user is shown "Unknown error" instead of what actually failed.
#   2. uvicorn then CLOSED the transport. The response carried no
#      ``Connection: close``, so the client's next request went out on a socket
#      the server had already torn down and died with ``RemoteDisconnected``.
#      Measured 6/6 by the QA pass (0/6 on the 200 control).
#
# So every handler below has two jobs: say something structured and true, and
# keep the connection alive. Registered ``exception_handler``s run inside
# Starlette's ExceptionMiddleware and satisfy both. A handler registered for
# bare ``Exception`` does NOT - Starlette routes that one to ServerErrorMiddleware,
# which re-raises after responding and lands back on the transport-closing path
# above - so the last-resort net is an ASGI middleware instead.
#
# Nothing here swallows anything: every branch logs the exception with its full
# traceback server-side before answering. What the client stops receiving is the
# stack trace, not the fact of the error.
#
# ORDERING: the boundary is registered BEFORE CORSMiddleware so that CORS ends
# up OUTERMOST (``add_middleware`` prepends). An error response produced outside
# CORS carries no ``Access-Control-Allow-Origin``, and the browser refuses to
# let the app read it - which would reproduce the "Unknown error" symptom by a
# different route.

logger = logging.getLogger(__name__)


class UnhandledErrorBoundary:
    """Last resort: turn an escaped exception into a JSON 500, in-band.

    A plain ASGI middleware rather than ``BaseHTTPMiddleware``: no task group,
    no interaction with background tasks or WebSockets, and it can tell whether
    the response had already started (in which case the only honest thing left
    is to let the exception through and let the server close the stream).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            logger.exception(
                "Unhandled exception serving %s %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
            )
            if response_started:
                # Headers are already on the wire; there is no status left to
                # change. Re-raise so the server tears down the half-sent body
                # rather than silently truncating it.
                raise
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "error": "internal_error",
                },
            )
            await response(scope, receive, _send)


app.add_middleware(UnhandledErrorBoundary)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# SQLite spells its constraint failures "<KIND> constraint failed: table.column".
# The column name is part of the public API surface (it is the field the client
# sent), the table name and the driver's wording are not - so only the column
# travels, and only when the message actually matches.
_CONSTRAINT_RE = re.compile(
    r"(?P<kind>NOT NULL|UNIQUE|CHECK|FOREIGN KEY) constraint failed"
    r"(?::\s*(?P<table>\w+)\.(?P<column>\w+))?",
    re.IGNORECASE,
)

# kind -> (status, error code, message template). A NOT NULL or CHECK violation
# means the client sent a value the column cannot hold: 422. UNIQUE and FOREIGN
# KEY mean the row conflicts with the rest of the database: 409.
_CONSTRAINT_RULES = {
    "NOT NULL": (422, "not_null_violation", "must not be null"),
    "CHECK": (422, "check_violation", "failed a database constraint"),
    "UNIQUE": (409, "unique_violation", "is already taken"),
    "FOREIGN KEY": (409, "foreign_key_violation", "references a row that does not exist"),
}


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    """A constraint violation is the CLIENT's error, not a server crash.

    Sources the QA pass hit, all of which were bare 500s: ``PATCH`` with an
    explicit ``null`` on a NOT NULL column (nine endpoints), and concurrent
    creates that lose the check-then-insert race on a unique name
    (prompt-templates, agent-files).
    """
    logger.warning(
        "IntegrityError serving %s %s: %s",
        request.method,
        request.url.path,
        exc.orig,
        exc_info=exc,
    )
    match = _CONSTRAINT_RE.search(str(exc.orig or exc))
    kind = match.group("kind").upper() if match else None
    status, code, phrase = _CONSTRAINT_RULES.get(
        kind, (409, "constraint_violation", "violates a database constraint")
    )
    column = match.group("column") if match else None
    body = {
        "detail": f"'{column}' {phrase}" if column else f"Request {phrase}",
        "error": code,
    }
    if column:
        body["field"] = column
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(StaleDataError)
async def stale_data_handler(request: Request, exc: StaleDataError):
    """Someone else changed or deleted the row mid-flight.

    ``expected to update 1 row(s); 0 were matched`` — e.g. `start` racing
    `delete` on the same card. 409, because retrying against fresh state is the
    action that can succeed.
    """
    logger.warning(
        "Concurrent modification serving %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": "The record changed while this request was in flight; "
                      "re-read it and try again.",
            "error": "concurrent_modification",
        },
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    """Everything else the database layer can raise.

    ``OperationalError: database is locked`` and ``TimeoutError: QueuePool
    limit ... reached`` are transient and retryable, so they answer 503 with a
    ``Retry-After``; anything else is ours to fix and answers 500. Either way
    the traceback is logged in full here and none of it reaches the client.
    """
    transient = isinstance(exc, (OperationalError, SATimeoutError))
    logger.error(
        "Database error serving %s %s (%s)",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc_info=exc,
    )
    if transient:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The database is temporarily unavailable. Retry shortly.",
                "error": "database_unavailable",
            },
            headers={"Retry-After": "1"},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Database error", "error": "database_error"},
    )


def _json_safe(value):
    """Replace non-finite floats so a 422 body can actually be rendered.

    Starlette's ``JSONResponse`` renders with ``allow_nan=False``. A request
    body containing the JSON literal ``NaN`` / ``Infinity`` (which Python's
    json parser accepts) therefore made FastAPI's own validation handler blow
    up while echoing the offending value back — turning a would-be 422 into a
    500 on EVERY endpoint. The value is preserved as its text so the message
    still names what was rejected.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """FastAPI's own 422, with non-finite floats scrubbed from the echo.

    Same status and same body shape as the default handler — this exists only
    so the body can always be serialized.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
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
# M14: the model endpoint registry, its probe, its usage rollup and the
# reach=proxy broker. Registered here so the operator API can reach it at all;
# without this line the whole milestone is dark.
app.include_router(model_endpoints.router)
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
