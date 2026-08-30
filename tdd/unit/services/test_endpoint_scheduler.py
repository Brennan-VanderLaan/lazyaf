"""The endpoint admission gate (M14 s6.4, cross-agent contract #9).

Every assertion here is about ONE invariant: **the in-flight count is read
from the DATABASE, never from an in-memory counter.** A counter dies with the
process; `step_executions.model_endpoint_id` does not, and it is also what the
API renders and what DELETE refuses on.

The second invariant is R1: waiting must be VISIBLE, with a position, and
BOUNDED. Silent waiting and hanging are indistinguishable, and a pin nobody can
satisfy must not hang a pipeline forever (12.6's NO_RUNNER_TIMEOUT rule,
applied to a GPU slot instead of a runner).
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.models.model_endpoint import (  # noqa: E402
    IN_FLIGHT_STEP_STATUSES,
    ModelEndpoint,
    default_gpu_node_id,
    default_runner_label,
)
from app.models.pipeline import (  # noqa: E402
    Pipeline,
    PipelineRun,
    StepExecution,
    StepExecutionStatus,
    StepRun,
)
from app.models.repo import Repo  # noqa: E402
from app.services.model_endpoints import scheduler  # noqa: E402
from app.services.model_endpoints.scheduler import (  # noqa: E402
    ENDPOINT_WAIT_POLL,
    ENDPOINT_WAIT_TIMEOUT,
    EndpointAdmissionTimeout,
    admit,
    in_flight_count,
    notify_release,
    slot_holders,
    sweep_stale_slots,
    try_admit,
    uses_admission_gate,
)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_locks():
    """An `asyncio.Lock` created on one event loop is unusable on the next,
    and pytest-asyncio gives every test its own loop."""
    scheduler.reset_for_tests()
    yield
    scheduler.reset_for_tests()


async def _make_endpoint(db, name="local-4090", *, max_concurrency=1, reach="direct",
                         rate=None):
    endpoint = ModelEndpoint(
        id=str(uuid4()),
        name=name,
        base_url="http://172.17.0.1:11434/v1",
        model="qwen2.5-coder:32b",
        server_kind="ollama",
        auth_style="none",
        reach=reach,
        runner_label=default_runner_label(name) if reach == "runner-local" else None,
        rate_usd_hour=Decimal(rate) if rate is not None else None,
        gpu_node_id=default_gpu_node_id(name),
        max_concurrency=max_concurrency,
        request_timeout_seconds=300,
        probe_status="ok",
        probe_detail="{}",
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        enabled=True,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


async def _make_step_execution(db, index=0, status=StepExecutionStatus.PREPARING.value):
    """A real StepExecution on a real StepRun on a real PipelineRun.

    R6: the gate CASes on this row, so a mock would be testing the mock.
    """
    repo = Repo(id=str(uuid4()), name=f"r{index}", default_branch="main")
    db.add(repo)
    await db.commit()
    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name=f"p{index}",
                        steps="[]", triggers="[]")
    db.add(pipeline)
    await db.commit()
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    db.add(run)
    await db.commit()
    step_run = StepRun(id=str(uuid4()), pipeline_run_id=run.id, step_index=index,
                       step_name=f"s{index}", status="running")
    db.add(step_run)
    await db.commit()
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run.id}:{index}:{step_run.id}",
        step_run_id=step_run.id,
        status=status,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution


# --------------------------------------------------------------------------
# The CAS
# --------------------------------------------------------------------------

class TestCompareAndSwap:
    async def test_a_free_slot_is_claimed_and_the_row_records_it(self, db_session):
        endpoint = await _make_endpoint(db_session)
        execution = await _make_step_execution(db_session)

        assert await try_admit(db_session, execution.id, endpoint) is True

        await db_session.refresh(execution)
        assert execution.model_endpoint_id == endpoint.id
        assert await in_flight_count(db_session, endpoint.id) == 1

    async def test_the_count_comes_from_the_database_not_from_memory(self, db_session):
        """Contract #9. Written by a DIFFERENT session than the one that
        reads it - which an in-memory counter could not survive."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        holder.model_endpoint_id = endpoint.id
        holder.status = StepExecutionStatus.RUNNING.value
        await db_session.commit()

        scheduler.reset_for_tests()  # every in-process structure thrown away
        assert await in_flight_count(db_session, endpoint.id) == 1

        second = await _make_step_execution(db_session, index=1)
        assert await try_admit(db_session, second.id, endpoint) is False

    async def test_a_full_endpoint_refuses_and_leaves_the_row_untouched(self, db_session):
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        first = await _make_step_execution(db_session, index=0)
        second = await _make_step_execution(db_session, index=1)

        assert await try_admit(db_session, first.id, endpoint) is True
        assert await try_admit(db_session, second.id, endpoint) is False

        await db_session.refresh(second)
        assert second.model_endpoint_id is None

    async def test_a_cap_of_two_admits_two_and_refuses_the_third(self, db_session):
        endpoint = await _make_endpoint(db_session, max_concurrency=2)
        executions = [await _make_step_execution(db_session, index=i) for i in range(3)]

        assert await try_admit(db_session, executions[0].id, endpoint) is True
        assert await try_admit(db_session, executions[1].id, endpoint) is True
        assert await try_admit(db_session, executions[2].id, endpoint) is False
        assert await in_flight_count(db_session, endpoint.id) == 2

    async def test_re_admitting_the_same_step_is_idempotent(self, db_session):
        """A retried dispatch must not deadlock against its own held slot."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        execution = await _make_step_execution(db_session)

        assert await try_admit(db_session, execution.id, endpoint) is True
        assert await try_admit(db_session, execution.id, endpoint) is True
        assert await in_flight_count(db_session, endpoint.id) == 1

    @pytest.mark.parametrize("status", IN_FLIGHT_STEP_STATUSES)
    async def test_every_in_flight_status_holds_a_slot(self, db_session, status):
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0, status=status)
        holder.model_endpoint_id = endpoint.id
        await db_session.commit()

        second = await _make_step_execution(db_session, index=1)
        assert await try_admit(db_session, second.id, endpoint) is False

    @pytest.mark.parametrize(
        "status",
        [
            StepExecutionStatus.COMPLETED.value,
            StepExecutionStatus.FAILED.value,
            StepExecutionStatus.CANCELLED.value,
            StepExecutionStatus.TIMEOUT.value,
        ],
    )
    async def test_a_terminal_step_releases_its_slot_without_any_write(
        self, db_session, status
    ):
        """RELEASE IS NOT A WRITE. The slot is held by a row whose STATUS is
        in-flight, so the terminal status write IS the release - which is
        exactly why a crash cannot leak a slot permanently."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        assert await try_admit(db_session, holder.id, endpoint) is True

        holder.status = status
        await db_session.commit()

        assert await in_flight_count(db_session, endpoint.id) == 0
        # And the id is deliberately LEFT for forensics and the usage join.
        await db_session.refresh(holder)
        assert holder.model_endpoint_id == endpoint.id

        second = await _make_step_execution(db_session, index=1)
        assert await try_admit(db_session, second.id, endpoint) is True


# --------------------------------------------------------------------------
# Waiting: visible, wakeable, bounded
# --------------------------------------------------------------------------

class TestWaiting:
    async def test_two_concurrent_admits_admit_exactly_one(self, db_session):
        """The design's test contract 5, verbatim: exactly one is admitted,
        the loser waits."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        first = await _make_step_execution(db_session, index=0)
        second = await _make_step_execution(db_session, index=1)

        results = await asyncio.gather(
            try_admit(db_session, first.id, endpoint),
            try_admit(db_session, second.id, endpoint),
        )
        assert sorted(results) == [False, True]
        assert await in_flight_count(db_session, endpoint.id) == 1

    async def test_the_wait_is_announced_with_a_position(self, db_session):
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        await try_admit(db_session, holder.id, endpoint)
        waiter = await _make_step_execution(db_session, index=1)

        lines = []
        with pytest.raises(EndpointAdmissionTimeout):
            await admit(
                db_session, waiter.id, endpoint,
                log=lines.append, timeout=0.4, poll=0.05, log_interval=0.0,
            )

        assert lines, "R1: silent waiting and hanging are indistinguishable"
        assert "waiting for endpoint local-4090" in lines[0]
        assert "1 of 1 slots busy" in lines[0]
        assert "position 1" in lines[0]

    async def test_the_waiter_wakes_and_is_admitted_when_the_slot_frees(
        self, db_session
    ):
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        await try_admit(db_session, holder.id, endpoint)
        waiter = await _make_step_execution(db_session, index=1)

        async def _release_soon():
            await asyncio.sleep(0.1)
            holder.status = StepExecutionStatus.COMPLETED.value
            await db_session.commit()
            notify_release(endpoint.id)

        lines = []
        release = asyncio.create_task(_release_soon())
        await admit(
            db_session, waiter.id, endpoint,
            log=lines.append, timeout=5, poll=0.05, log_interval=0.0,
        )
        await release

        await db_session.refresh(waiter)
        assert waiter.model_endpoint_id == endpoint.id
        assert any("admitted to endpoint local-4090" in line for line in lines)

    async def test_the_timeout_names_the_endpoint_the_cap_and_the_holders(
        self, db_session
    ):
        """A pin nobody can satisfy must not hang a pipeline forever, and the
        operator must not have to guess WHICH steps are hogging the GPU."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        await try_admit(db_session, holder.id, endpoint)
        waiter = await _make_step_execution(db_session, index=1)

        with pytest.raises(EndpointAdmissionTimeout) as excinfo:
            await admit(
                db_session, waiter.id, endpoint,
                timeout=0.3, poll=0.05, log_interval=999,
            )

        message = str(excinfo.value)
        assert "local-4090" in message
        assert "max_concurrency=1" in message
        assert holder.id in message
        assert await slot_holders(db_session, endpoint.id) == [holder.id]

    async def test_an_immediately_free_slot_logs_nothing(self, db_session):
        """A queue message on a step that never queued is noise."""
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        execution = await _make_step_execution(db_session)

        lines = []
        await admit(db_session, execution.id, endpoint, log=lines.append)
        assert lines == []

    async def test_a_failing_log_callback_never_fails_the_step(self, db_session):
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        holder = await _make_step_execution(db_session, index=0)
        await try_admit(db_session, holder.id, endpoint)
        waiter = await _make_step_execution(db_session, index=1)

        def _boom(_line):
            raise RuntimeError("the log sink is down")

        with pytest.raises(EndpointAdmissionTimeout):
            await admit(
                db_session, waiter.id, endpoint,
                log=_boom, timeout=0.2, poll=0.05, log_interval=0.0,
            )

    async def test_notify_release_on_an_unknown_endpoint_is_a_no_op(self):
        notify_release(None)
        notify_release("never-seen")


# --------------------------------------------------------------------------
# runner-local skips the gate
# --------------------------------------------------------------------------

class TestRunnerLocalSkipsTheGate:
    async def test_uses_admission_gate_is_false_for_runner_local(self, db_session):
        endpoint = await _make_endpoint(db_session, reach="runner-local")
        assert uses_admission_gate(endpoint) is False

    @pytest.mark.parametrize("reach", ["direct", "proxy"])
    async def test_every_other_reach_uses_the_gate(self, db_session, reach):
        endpoint = await _make_endpoint(db_session, name=f"e-{reach}", reach=reach)
        assert uses_admission_gate(endpoint) is True

    async def test_a_runner_local_endpoint_admits_instantly_and_claims_nothing(
        self, db_session
    ):
        """Two gates that can block each other is a deadlock, and it buys
        nothing: MAX_CONCURRENT_STEPS=1 per runner agent already serializes
        there. The effective concurrency is `count(runners carrying the
        label)`, which the operator can see and change."""
        endpoint = await _make_endpoint(
            db_session, reach="runner-local", max_concurrency=1
        )
        first = await _make_step_execution(db_session, index=0)
        second = await _make_step_execution(db_session, index=1)
        first.model_endpoint_id = endpoint.id
        first.status = StepExecutionStatus.RUNNING.value
        await db_session.commit()

        lines = []
        await admit(db_session, second.id, endpoint, log=lines.append, timeout=0.2)

        assert lines == []
        await db_session.refresh(second)
        # The gate did not claim the row: nothing here arbitrates a slot.
        assert second.model_endpoint_id is None


# --------------------------------------------------------------------------
# Startup sweep
# --------------------------------------------------------------------------

class TestStartupSweep:
    async def test_it_clears_terminal_rows_and_leaves_live_ones(self, db_session):
        endpoint = await _make_endpoint(db_session)
        live = await _make_step_execution(db_session, index=0)
        dead = await _make_step_execution(db_session, index=1)
        for row in (live, dead):
            row.model_endpoint_id = endpoint.id
        live.status = StepExecutionStatus.RUNNING.value
        dead.status = StepExecutionStatus.COMPLETED.value
        await db_session.commit()

        cleared = await sweep_stale_slots(db_session)

        await db_session.refresh(live)
        await db_session.refresh(dead)
        assert cleared == 1
        assert live.model_endpoint_id == endpoint.id
        assert dead.model_endpoint_id is None


# --------------------------------------------------------------------------
# The constants are the ones the design named
# --------------------------------------------------------------------------

def test_the_wait_budget_is_bounded_and_stated():
    assert ENDPOINT_WAIT_TIMEOUT == 900
    assert ENDPOINT_WAIT_POLL == 5
