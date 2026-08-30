"""Runners API - READ-ONLY over the registry (Phase 12.6, R2).

This module used to be the polling stack's entire control surface: a runner
POSTed `/register`, polled `GET /{id}/job` for work, POSTed `/{id}/heartbeat`,
`/{id}/logs` and `/{id}/complete`, and an operator could pull a `docker run`
line out of `/docker-command` to start one. All of it is gone.

A 12.6 runner is a `lazyaf_runner` agent that enrolls over `/ws/runner`, and
the WebSocket carries every one of those concerns: registration and identity,
heartbeats, assignment and ACK, runner-origin log lines, and the terminal step
outcome. There is nothing left for HTTP to do except SHOW what the registry
knows - which is what this router is now.

Two endpoints, both reads:

    GET /api/runners          the registry snapshot (the UI's mount fetch and
                              `verify_executor.py` assertion 9 read this)
    GET /api/runners/{id}     one row

The step container's own reporting (`POST /api/steps/{id}/status|logs|
heartbeat|test-results|usage`) is UNCHANGED and lives in routers/steps.py.
That is the whole 12.6 channel decision: the step JWT is location-independent,
so all five control channels work from another host with no new server code,
and the socket carries only what is about the RUNNER and the ASSIGNMENT.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.execution.runner_registry import runner_registry

router = APIRouter(prefix="/api/runners", tags=["runners"])


@router.get("", response_model=list[dict])
async def list_runners(db: AsyncSession = Depends(get_db)):
    """Every runner this backend knows about.

    Rows come from the registry: the DB row is the durable projection of a
    live connection, and `connection` is stamped from the process's actual
    socket table rather than from the row. That distinction is load-bearing -
    an "idle" row left behind by a crashed backend process is
    indistinguishable from a live one in the database alone, and dispatching
    at it is exactly the split-brain the field exists to make visible.
    """
    return await runner_registry.snapshot(db)


@router.get("/{runner_id}")
async def get_runner(runner_id: str, db: AsyncSession = Depends(get_db)):
    """One runner row, or 404. Same projection as the list."""
    for row in await runner_registry.snapshot(db):
        if row["id"] == runner_id:
            return row
    raise HTTPException(status_code=404, detail="Runner not found")
