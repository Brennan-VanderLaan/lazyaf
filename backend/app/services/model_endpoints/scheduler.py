"""The endpoint admission gate (M14, wave8 s6.4).

**One ollama process serving four requests on one GPU does not go 4x faster.**
It goes roughly 1x with 4x the latency, 4x the KV-cache pressure and a real
chance of an OOM that kills all four. That is why `max_concurrency` defaults to
1: an unlimited default would make M13's wall-clock and speedup numbers - the
whole reason wall-clock is a co-headline metric - fiction.

CROSS-AGENT CONTRACT #9, and the single most important property of this module:
**the in-flight count is READ FROM THE DATABASE, never from an in-memory
counter.** A counter dies with the process; `step_executions.model_endpoint_id`
does not, and it is also what `GET /api/model-endpoints` renders and what
`DELETE` refuses on. One fact, one writer, one reader.

The shape is 12.6's runner assignment verbatim:

    SELECT ... FROM model_endpoints WHERE id = :eid FOR UPDATE   -- the mutex
    SELECT count(*) FROM step_executions
     WHERE model_endpoint_id = :eid AND status IN (<in-flight>)
    -- if count < max_concurrency:
    UPDATE step_executions SET model_endpoint_id = :eid
     WHERE id = :sid AND model_endpoint_id IS NULL
    -- rowcount != 1 is the ONLY acceptable contention detector

SQLite renders no `FOR UPDATE` (SQLAlchemy's dialect emits an empty clause) and
serializes writers instead, which is sufficient for the single-process backend
LazyAF runs today; the statement is written so Postgres gets a real row lock the
day it is used. `_endpoint_mutex` is a per-process `asyncio.Lock` that makes the
read-then-write pair atomic WITHIN this process - it is a mutex, not a counter,
and removing it would leave the invariant to the database alone (correct on
Postgres, racy on SQLite's deferred transactions).

**RELEASE IS NOT A WRITE.** A slot is held by a row whose STATUS is in
`IN_FLIGHT_STEP_STATUSES`; the moment the step reaches a terminal status - by
the /api/steps router, by reconciliation, or by the startup orphan sweep - the
count stops including it. `model_endpoint_id` is deliberately left behind for
forensics, for the usage join and for the `probe-result` fence. That is why a
crash cannot leak a slot permanently and why there is no compensating delete to
forget.

**`runner-local` endpoints SKIP this gate entirely** (s6.4). Two gates that can
block each other - runner availability and endpoint slots - is a deadlock
waiting to be discovered in production, and it buys nothing:
`MAX_CONCURRENT_STEPS = 1` per runner agent already serializes there. The
effective concurrency of a runner-local endpoint is `count(runners carrying its
label)`, a number the operator can see in the runner panel and change by
starting another agent.
"""
import asyncio
import logging
from collections import deque
from typing import Callable, Deque, Optional

from sqlalchemy import func, select, update

from app.models.model_endpoint import IN_FLIGHT_STEP_STATUSES, ModelEndpoint
from app.models.pipeline import StepExecution

logger = logging.getLogger(__name__)

#: How long a step waits for a slot before FAILING LOUDLY. The same rule as
#: 12.6's `NO_RUNNER_TIMEOUT`: a pin nobody can satisfy must not hang a
#: pipeline forever.
ENDPOINT_WAIT_TIMEOUT = 900

#: Poll cadence. A waiter also wakes immediately on `notify_release`, so this
#: is the backstop for a slot freed by a path that never notified (a terminal
#: status written by the /api/steps router in another task, reconciliation, a
#: restart).
ENDPOINT_WAIT_POLL = 5

#: How often a waiting step SAYS SO in its own log. R1: silent waiting and
#: hanging are indistinguishable, and a fan-out that is serializing must look
#: like a queue rather than a hang.
ENDPOINT_WAIT_LOG_INTERVAL = 30


class EndpointAdmissionTimeout(RuntimeError):
    """A step waited `ENDPOINT_WAIT_TIMEOUT` and never got a slot.

    The message names the endpoint, the cap AND the step execution ids holding
    the slots, because "timed out waiting for an endpoint" without them is one
    round trip short of useful.
    """


#: Per-endpoint mutex + wakeup. Process-local by construction (an asyncio
#: primitive cannot be otherwise); the DATABASE remains the arbiter.
_endpoint_locks: dict[str, asyncio.Lock] = {}
_endpoint_conditions: dict[str, asyncio.Condition] = {}
#: Per-endpoint FIFO of waiter tokens, so a waiting step can report its
#: POSITION rather than just "still waiting".
_endpoint_waiters: dict[str, Deque[object]] = {}


def _endpoint_mutex(endpoint_id: str) -> asyncio.Lock:
    lock = _endpoint_locks.get(endpoint_id)
    if lock is None:
        lock = asyncio.Lock()
        _endpoint_locks[endpoint_id] = lock
    return lock


def _condition(endpoint_id: str) -> asyncio.Condition:
    condition = _endpoint_conditions.get(endpoint_id)
    if condition is None:
        condition = asyncio.Condition()
        _endpoint_conditions[endpoint_id] = condition
    return condition


def _waiters(endpoint_id: str) -> Deque[object]:
    queue = _endpoint_waiters.get(endpoint_id)
    if queue is None:
        queue = deque()
        _endpoint_waiters[endpoint_id] = queue
    return queue


def uses_admission_gate(endpoint: ModelEndpoint) -> bool:
    """Does this endpoint's reach mode go through the gate at all?

    `direct` and `proxy` do. `runner-local` does NOT - see the module
    docstring; two gates that can block each other is a deadlock.
    """
    return endpoint is not None and endpoint.reach != "runner-local"


async def in_flight_count(db, endpoint_id: str) -> int:
    """Slots held right now, READ FROM THE DATABASE (contract #9)."""
    result = await db.execute(
        select(func.count())
        .select_from(StepExecution)
        .where(
            StepExecution.model_endpoint_id == endpoint_id,
            StepExecution.status.in_(IN_FLIGHT_STEP_STATUSES),
        )
    )
    return int(result.scalar_one() or 0)


async def slot_holders(db, endpoint_id: str) -> list[str]:
    """The step execution ids currently holding this endpoint's slots.

    Named in the timeout message: an operator who has to guess WHICH steps are
    hogging a single-slot GPU has been told nothing useful.
    """
    result = await db.execute(
        select(StepExecution.id).where(
            StepExecution.model_endpoint_id == endpoint_id,
            StepExecution.status.in_(IN_FLIGHT_STEP_STATUSES),
        )
    )
    return [row[0] for row in result.all()]


async def _already_admitted(db, step_execution_id: str, endpoint_id: str) -> bool:
    """Is this step already holding a slot on this endpoint?

    Re-entrancy matters: a retried dispatch of the same StepExecution must be
    idempotent rather than deadlock against its own held slot.
    """
    result = await db.execute(
        select(StepExecution.model_endpoint_id).where(
            StepExecution.id == step_execution_id
        )
    )
    current = result.scalar_one_or_none()
    return current == endpoint_id


async def try_admit(db, step_execution_id: str, endpoint: ModelEndpoint) -> bool:
    """ONE compare-and-swap attempt. True when this step now holds a slot.

    Everything is inside the per-endpoint mutex so the count and the claim
    cannot interleave with another admit in this process, and the UPDATE's
    `rowcount` is the only contention detector (contract #9): a row whose
    `model_endpoint_id` is no longer NULL was claimed by someone else, and no
    amount of re-reading would have made that safe.
    """
    cap = max(int(endpoint.max_concurrency or 1), 1)
    async with _endpoint_mutex(endpoint.id):
        if await _already_admitted(db, step_execution_id, endpoint.id):
            return True

        # The mutex row. SQLite renders no FOR UPDATE and serializes writers
        # instead; Postgres takes a real row lock here the day it is used.
        await db.execute(
            select(ModelEndpoint.id)
            .where(ModelEndpoint.id == endpoint.id)
            .with_for_update()
        )

        held = await in_flight_count(db, endpoint.id)
        if held >= cap:
            # COMMIT, never rollback. Nothing was written, so this only ends
            # the read transaction (releasing the Postgres row lock) - and
            # `rollback()` would EXPIRE every ORM instance the caller's session
            # holds, whose next attribute read under asyncio is not a slow
            # query but `MissingGreenlet` (usage_ingestion's F3.2 lesson).
            await db.commit()
            return False

        result = await db.execute(
            update(StepExecution)
            .where(
                StepExecution.id == step_execution_id,
                StepExecution.model_endpoint_id.is_(None),
            )
            .values(model_endpoint_id=endpoint.id)
        )
        if result.rowcount != 1:
            # Contention, or the row is already pointed somewhere else. Either
            # way this attempt did not win; the caller re-reads and retries.
            # `rowcount != 1` is the ONLY acceptable contention detector
            # (contract #9) - re-reading and hoping is how a race gets a
            # second chance to be wrong.
            await db.commit()
            return False
        await db.commit()
        return True


async def admit(
    db,
    step_execution_id: str,
    endpoint: ModelEndpoint,
    *,
    log: Optional[Callable[[str], object]] = None,
    timeout: float = ENDPOINT_WAIT_TIMEOUT,
    poll: float = ENDPOINT_WAIT_POLL,
    log_interval: float = ENDPOINT_WAIT_LOG_INTERVAL,
) -> None:
    """Block until this step holds one of the endpoint's slots.

    `runner-local` endpoints return immediately (module docstring). Every other
    reach mode waits, VISIBLY: `log` is called once at entry when a slot is not
    free and every `log_interval` seconds thereafter with a line naming the
    endpoint, the occupancy and this step's position in the local queue.

    Raises `EndpointAdmissionTimeout` after `timeout` seconds, naming the
    endpoint, the cap and the step execution ids holding the slots.
    """
    if not uses_admission_gate(endpoint):
        logger.debug(
            "endpoint %s has reach=runner-local; the endpoint admission gate "
            "is skipped (the runner's own MAX_CONCURRENT_STEPS=1 serializes it)",
            endpoint.name,
        )
        return

    if await try_admit(db, step_execution_id, endpoint):
        return

    cap = max(int(endpoint.max_concurrency or 1), 1)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    condition = _condition(endpoint.id)
    queue = _waiters(endpoint.id)
    token = object()
    queue.append(token)
    last_logged = -1.0

    def _emit(position: int, held: int) -> None:
        line = (
            f"[executor] waiting for endpoint {endpoint.name} "
            f"({held} of {cap} slots busy, position {position})"
        )
        logger.info(
            "step %s: %s", step_execution_id[:8], line[len("[executor] "):]
        )
        if log is not None:
            try:
                log(line)
            except Exception:  # a log line must never fail a step
                logger.exception("endpoint wait log callback failed")

    try:
        while True:
            now = loop.time()
            if now >= deadline:
                holders = await slot_holders(db, endpoint.id)
                holder_text = (
                    ", ".join(holders)
                    if holders
                    else "(none - the cap may have been lowered under a "
                    "running step)"
                )
                raise EndpointAdmissionTimeout(
                    f"waited {int(timeout)}s for a slot on endpoint "
                    f"'{endpoint.name}' (max_concurrency={cap}) and none came "
                    f"free. Steps holding the slots: {holder_text}. Raise "
                    f"max_concurrency, cancel a holder, or wait for the "
                    f"fan-out to drain."
                )

            held = await in_flight_count(db, endpoint.id)
            position = (queue.index(token) + 1) if token in queue else 1
            if last_logged < 0 or (now - last_logged) >= log_interval:
                _emit(position, held)
                last_logged = now

            # Wake on a release, or poll - whichever comes first. The poll is
            # the backstop for a slot freed by a path that never notified
            # (the /api/steps router in another task, reconciliation, a
            # restart), because a missed notify must cost latency, not a hang.
            wait_for = min(poll, max(0.0, deadline - loop.time()))
            try:
                async with condition:
                    await asyncio.wait_for(condition.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                pass

            if await try_admit(db, step_execution_id, endpoint):
                if last_logged >= 0:
                    waited = int(timeout - max(0.0, deadline - loop.time()))
                    message = (
                        f"[executor] admitted to endpoint {endpoint.name} "
                        f"after waiting {waited}s"
                    )
                    logger.info("step %s: admitted after %ss", step_execution_id[:8], waited)
                    if log is not None:
                        try:
                            log(message)
                        except Exception:
                            logger.exception("endpoint admit log callback failed")
                return
    finally:
        try:
            queue.remove(token)
        except ValueError:  # pragma: no cover - defensive
            pass


def notify_release(endpoint_id: str | None) -> None:
    """Wake every waiter on this endpoint. Fire-and-forget, never raises.

    Called when a step that HELD a slot reaches its terminal state. The slot
    itself was released by the status write (see the module docstring); this
    only saves the next waiter up to `ENDPOINT_WAIT_POLL` seconds.
    """
    if not endpoint_id:
        return
    condition = _endpoint_conditions.get(endpoint_id)
    if condition is None:
        return

    async def _wake() -> None:
        async with condition:
            condition.notify_all()

    try:
        asyncio.get_running_loop().create_task(_wake())
    except RuntimeError:  # pragma: no cover - no loop (sync test teardown)
        logger.debug("no running loop; endpoint %s wakeup skipped", endpoint_id)


async def sweep_stale_slots(db) -> int:
    """Clear `model_endpoint_id` on step executions that are already terminal.

    A NO-OP for correctness and a tidy-up for forensics: the gate counts rows
    by STATUS, so a terminal row never held a slot in the first place. It
    exists because the design asks for it and because leaving the column
    populated on ancient rows makes `DELETE /api/model-endpoints/{id}`'s
    409 message harder to read. Returns the number of rows cleared.

    NOT called from anywhere yet - see the requested edit in agent C's report
    for the one line in `main.py`'s startup block.
    """
    result = await db.execute(
        update(StepExecution)
        .where(
            StepExecution.model_endpoint_id.is_not(None),
            StepExecution.status.not_in(IN_FLIGHT_STEP_STATUSES),
            StepExecution.status.not_in(("pending",)),
        )
        .values(model_endpoint_id=None)
    )
    cleared = int(result.rowcount or 0)
    if cleared:
        await db.commit()
        logger.info("cleared %s stale endpoint slot reference(s) at startup", cleared)
    return cleared


def reset_for_tests() -> None:
    """Drop the per-process mutexes/conditions/queues.

    Test-only: an `asyncio.Lock` created on one event loop is unusable on the
    next, and pytest-asyncio gives every test its own loop.
    """
    _endpoint_locks.clear()
    _endpoint_conditions.clear()
    _endpoint_waiters.clear()
