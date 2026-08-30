"""Unit tests for the runner dispatcher (Phase 12.6).

failure_01 requeued steps to `pending` on runner death and then had nothing
that ever looked at `pending` again. Every requeue silently stranded the
pipeline. This module is the thing that was missing, so these tests are about
two questions:

1. Can two claimers ever assign one step twice? (cross-agent contract #8 -
   the DB compare-and-swap, `rowcount != 1` means someone else won)
2. Does a step that nobody can run FAIL, loudly, with something an operator
   can act on? (a typo'd `requires:` that hangs forever is indistinguishable
   from a hung backend)

Everything runs against a REAL SQLite session, the REAL RunnerRegistry and
the REAL RunnerStateMachine with a capturing transport (R6). The only stub is
the runner on the far end of the socket, which is the one thing a unit test
genuinely cannot have.
"""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.pipeline import (
    Pipeline,
    PipelineRun,
    StepExecution,
    StepExecutionStatus,
    StepRun,
)
from app.models.repo import Repo
from app.models.runner import Runner
from app.services.execution.job_recovery import JobRecoveryService
from app.services.execution.runner_dispatcher import (
    AssignmentOutcome,
    NoRunnerAvailable,
    RunnerDispatcher,
)
from app.services.execution.runner_protocol import RegisterMessage
from app.services.execution.runner_registry import RunnerRegistry
from app.services.execution.runner_state import RunnerState
from app.services.websocket import manager


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------

class CapturingSocket:
    """A real transport that records what actually went on the wire."""

    def __init__(self):
        self.frames: list[dict] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def of_type(self, message_type: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == message_type]


@pytest_asyncio.fixture
async def sessions(async_engine):
    """A factory for INDEPENDENT sessions.

    Concurrency claims have to run on separate connections or the "race" is
    a fiction: one session cannot lose a compare-and-swap to itself.
    """
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def registry():
    reg = RunnerRegistry()
    yield reg
    await reg.reset()


@pytest_asyncio.fixture
async def dispatcher(registry, sessions):
    """A dispatcher wired exactly as the lifespan wires it.

    `install_hooks()` is what makes the system event-driven: the registry
    wakes the dispatcher when a runner reaches IDLE and the recovery service
    wakes it after any requeue. A test that skipped it would be measuring the
    15s self-heal tick instead of the design.
    """
    disp = RunnerDispatcher(
        registry=registry, recovery=JobRecoveryService(), session_factory=sessions
    )
    disp.install_hooks()
    yield disp
    registry.set_dispatch_waker(None)
    disp.recovery.set_requeue_hook(None)
    await disp.reset()


@pytest_asyncio.fixture
async def chain(db_session):
    """Repo -> Pipeline -> PipelineRun -> StepRun, the parents a StepExecution needs."""
    repo = Repo(id=str(uuid4()), name="dispatch-repo")
    pipeline = Pipeline(
        id=str(uuid4()), repo_id=repo.id, name="dispatch-pipeline", steps="[]"
    )
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=0,
        step_name="pinned step",
        status="running",
    )
    db_session.add_all([repo, pipeline, run, step_run])
    await db_session.commit()
    return {"repo": repo, "pipeline": pipeline, "run": run, "step_run": step_run}


async def make_step(db, chain, requirements: dict | None = None, index: int = 0) -> str:
    """A PENDING StepExecution carrying durable requirements."""
    execution = StepExecution(
        id=str(uuid4()),
        step_run_id=chain["step_run"].id,
        execution_key=f"{chain['run'].id}:{index}:{uuid4()}",
        status=StepExecutionStatus.PENDING.value,
        runner_requirements=json.dumps(requirements or {}, sort_keys=True),
    )
    db.add(execution)
    await db.commit()
    return execution.id


async def connect_runner(
    registry, db, runner_id: str, labels: dict | None = None, runner_type: str = "generic"
) -> tuple[Runner, CapturingSocket]:
    socket = CapturingSocket()
    runner = await registry.connect(
        db,
        socket,
        RegisterMessage(
            runner_id=runner_id,
            name=runner_id,
            runner_type=runner_type,
            labels=labels or {},
        ),
    )
    return runner, socket


# -----------------------------------------------------------------------------
# Cross-agent contract #8: the CAS is the only double-assign detection
# -----------------------------------------------------------------------------

class TestCompareAndSwap:

    async def test_claim_assigns_the_step_and_writes_the_runner_pointer(
        self, dispatcher, registry, db_session, chain
    ):
        """`current_step_execution_id` is WRITTEN.

        failure_01 declared this column and never wrote it, which silently
        neutered the entire job-recovery subsystem: a dead runner's in-flight
        step was unfindable, so nothing was ever requeued. It is the single
        most consequential line in this file.
        """
        await connect_runner(registry, db_session, "runner-a")
        step_id = await make_step(db_session, chain)

        runner = await dispatcher.claim(db_session, step_id, {})

        assert runner is not None and runner.id == "runner-a"
        execution = await db_session.get(StepExecution, step_id)
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.ASSIGNED.value
        assert execution.runner_id == "runner-a"
        assert execution.assigned_at is not None

        row = await db_session.get(Runner, "runner-a")
        await db_session.refresh(row)
        assert row.current_step_execution_id == step_id
        assert row.status == RunnerState.ASSIGNED.value

    async def test_second_claimer_loses_and_writes_nothing(
        self, registry, sessions, db_session, chain
    ):
        """Two dispatchers, one step: the DB arbitrates, not a lock.

        Two SEPARATE dispatcher instances is the point - they share no
        in-process lock, so the only thing that can stop a double assignment
        is the guarded UPDATE.
        """
        await connect_runner(registry, db_session, "runner-a")
        await connect_runner(registry, db_session, "runner-b")
        step_id = await make_step(db_session, chain)

        first = RunnerDispatcher(registry=registry, session_factory=sessions)
        second = RunnerDispatcher(registry=registry, session_factory=sessions)

        # Independent sessions: two processes, two connections. A claimer
        # cannot lose a compare-and-swap to itself.
        async with sessions() as db_one:
            winner = await first.claim(db_one, step_id, {})
            winner_id = winner.id if winner else None
        async with sessions() as db_two:
            loser = await second.claim(db_two, step_id, {})

        assert winner_id is not None
        assert loser is None

        async with sessions() as db:
            execution = await db.get(StepExecution, step_id)
            assert execution.runner_id == winner_id

            # The loser's candidate is untouched: still idle, still free.
            other_id = "runner-b" if winner_id == "runner-a" else "runner-a"
            other = await db.get(Runner, other_id)
            assert other.status == RunnerState.IDLE.value
            assert other.current_step_execution_id is None

    async def test_concurrent_claims_on_one_step_assign_it_exactly_once(
        self, registry, sessions, db_session, chain
    ):
        """The same race, actually raced, on independent connections."""
        await connect_runner(registry, db_session, "runner-a")
        await connect_runner(registry, db_session, "runner-b")
        step_id = await make_step(db_session, chain)

        first = RunnerDispatcher(registry=registry, session_factory=sessions)
        second = RunnerDispatcher(registry=registry, session_factory=sessions)

        async def claim_with(disp):
            async with sessions() as db:
                return await disp.claim(db, step_id, {})

        results = await asyncio.gather(
            claim_with(first), claim_with(second), return_exceptions=True
        )
        winners = [r for r in results if isinstance(r, Runner)]
        assert len(winners) == 1, results

        async with sessions() as db:
            execution = await db.get(StepExecution, step_id)
            assert execution.status == StepExecutionStatus.ASSIGNED.value
            assert execution.runner_id == winners[0].id
            assigned = (
                await db.execute(
                    select(Runner).where(Runner.current_step_execution_id == step_id)
                )
            ).scalars().all()
            assert len(assigned) == 1

    async def test_one_runner_cannot_take_two_steps(
        self, dispatcher, registry, db_session, chain
    ):
        """The runner half of the CAS: `WHERE status='idle' AND step IS NULL`."""
        await connect_runner(registry, db_session, "solo")
        first_step = await make_step(db_session, chain, index=0)
        second_step = await make_step(db_session, chain, index=1)

        assert await dispatcher.claim(db_session, first_step, {}) is not None
        assert await dispatcher.claim(db_session, second_step, {}) is None

        second = await db_session.get(StepExecution, second_step)
        await db_session.refresh(second)
        assert second.status == StepExecutionStatus.PENDING.value
        assert second.runner_id is None

    async def test_claim_returns_none_when_no_runner_matches(
        self, dispatcher, registry, db_session, chain
    ):
        await connect_runner(registry, db_session, "amd-box", labels={"arch": "amd64"})
        step_id = await make_step(db_session, chain, {"arch": "arm64"})

        assert await dispatcher.claim(db_session, step_id, {"arch": "arm64"}) is None

        execution = await db_session.get(StepExecution, step_id)
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.PENDING.value

    async def test_claim_honors_a_runner_id_pin(
        self, dispatcher, registry, db_session, chain
    ):
        await connect_runner(registry, db_session, "runner-a")
        await connect_runner(registry, db_session, "pi-workshop-1")
        step_id = await make_step(db_session, chain, {"runner_id": "pi-workshop-1"})

        runner = await dispatcher.claim(
            db_session, step_id, {"runner_id": "pi-workshop-1"}
        )

        assert runner is not None and runner.id == "pi-workshop-1"

    async def test_a_non_pending_step_is_never_claimed(
        self, dispatcher, registry, db_session, chain
    ):
        """A step already running somewhere must not be handed out again."""
        await connect_runner(registry, db_session, "runner-a")
        step_id = await make_step(db_session, chain)
        execution = await db_session.get(StepExecution, step_id)
        execution.status = StepExecutionStatus.RUNNING.value
        await db_session.commit()

        assert await dispatcher.claim(db_session, step_id, {}) is None

    async def test_assignment_broadcasts_exactly_one_runner_status_frame(
        self, dispatcher, registry, db_session, chain
    ):
        ui = CapturingSocket()
        manager.active_connections.append(ui)
        try:
            await connect_runner(registry, db_session, "runner-a")
            step_id = await make_step(db_session, chain)
            before = len(ui.of_type("runner_status"))

            await dispatcher.claim(db_session, step_id, {})

            frames = ui.of_type("runner_status")[before:]
            assert len(frames) == 1
            assert frames[0]["payload"]["status"] == RunnerState.ASSIGNED.value
        finally:
            manager.active_connections.remove(ui)


# -----------------------------------------------------------------------------
# The waiting side: acquire(), the budget, and the actionable failure
# -----------------------------------------------------------------------------

class TestAcquire:

    async def test_acquire_returns_the_assigned_runner(
        self, dispatcher, registry, db_session, chain
    ):
        await connect_runner(registry, db_session, "runner-a")
        step_id = await make_step(db_session, chain)

        runner = await asyncio.wait_for(dispatcher.acquire(step_id, {}), timeout=5)

        assert runner.id == "runner-a"

    async def test_acquire_is_woken_by_a_runner_arriving_later(
        self, dispatcher, registry, sessions, db_session, chain
    ):
        """Event-driven, not polled: connecting a runner wakes the waiter.

        `RunnerRegistry.connect` calls the dispatch waker, which is exactly
        the hook `install_hooks` wires up.
        """
        dispatcher.install_hooks()
        step_id = await make_step(db_session, chain)

        waiting = asyncio.create_task(dispatcher.acquire(step_id, {}, timeout=10))
        await asyncio.sleep(0.05)
        assert not waiting.done()

        async with sessions() as db:
            await connect_runner(registry, db, "late-arrival")

        runner = await asyncio.wait_for(waiting, timeout=5)
        assert runner.id == "late-arrival"
        registry.set_dispatch_waker(None)

    async def test_no_matching_runner_fails_with_requirements_and_the_fleet(
        self, dispatcher, registry, db_session, chain
    ):
        """A `requires:` nobody can satisfy must FAIL, naming both sides.

        The overwhelmingly likely cause is a typo, and the only thing an
        operator can act on is the difference between what was asked for and
        what is actually connected. Hanging until someone notices is
        indistinguishable from a hung backend - which is precisely how
        failure_01's pipelines "just stopped".
        """
        await connect_runner(
            registry, db_session, "amd-box", labels={"arch": "amd64", "has": ["docker"]}
        )
        step_id = await make_step(db_session, chain, {"arch": "arm64"})

        with pytest.raises(NoRunnerAvailable) as excinfo:
            await dispatcher.acquire(step_id, {"arch": "arm64"}, timeout=0.2)

        message = str(excinfo.value)
        assert "arm64" in message
        assert "amd-box" in message
        assert "amd64" in message

    async def test_no_runners_at_all_says_so(self, dispatcher, db_session, chain):
        step_id = await make_step(db_session, chain, {"has": ["gpio"]})

        with pytest.raises(NoRunnerAvailable) as excinfo:
            await dispatcher.acquire(step_id, {"has": ["gpio"]}, timeout=0.2)

        assert "connected runners: none" in str(excinfo.value)

    async def test_a_disconnected_row_is_not_a_connected_runner(
        self, dispatcher, registry, db_session, chain
    ):
        """A row left `idle` by a crashed process is not somebody you can
        hand work to. The registry's own `_connections` is the authority."""
        db_session.add(
            Runner(
                id="ghost",
                name="ghost",
                runner_type="generic",
                status=RunnerState.IDLE.value,
            )
        )
        await db_session.commit()
        step_id = await make_step(db_session, chain)

        with pytest.raises(NoRunnerAvailable) as excinfo:
            await dispatcher.acquire(step_id, {}, timeout=0.2)

        assert "ghost" not in str(excinfo.value)

    async def test_the_dispatch_order_is_oldest_step_first(
        self, dispatcher, registry, db_session, chain
    ):
        """One runner, two waiting steps: the older one wins.

        The ORDER comes from the database (`created_at`), which is why
        `runner_requirements` is a durable column - a requeued step has to be
        re-matchable and re-prioritized after a restart, so its requirements
        cannot live only in a dispatch closure.
        """
        from datetime import datetime, timedelta

        older = await make_step(db_session, chain, index=0)
        newer = await make_step(db_session, chain, index=1)
        old_row = await db_session.get(StepExecution, older)
        old_row.created_at = datetime.utcnow() - timedelta(minutes=5)
        await db_session.commit()

        # Register the NEWER step first, so registration order and DB order
        # disagree and only the DB order can produce the expected answer.
        newer_wait = asyncio.create_task(dispatcher.acquire(newer, {}, timeout=10))
        await asyncio.sleep(0.05)
        older_wait = asyncio.create_task(dispatcher.acquire(older, {}, timeout=10))
        await asyncio.sleep(0.05)

        await connect_runner(registry, db_session, "only-runner")

        done, _pending = await asyncio.wait(
            {newer_wait, older_wait},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert older_wait in done
        assert newer_wait not in done

        newer_wait.cancel()
        older_wait.cancel()
        await asyncio.gather(newer_wait, older_wait, return_exceptions=True)


# -----------------------------------------------------------------------------
# The rendezvous: (step_id, runner_id), never step_id alone
# -----------------------------------------------------------------------------

class TestAssignmentRendezvous:

    async def test_ack_and_complete_resolve_the_open_assignment(self, dispatcher):
        assignment = dispatcher.register_assignment("step-1", "runner-a")

        assert dispatcher.notify_ack("step-1", "runner-a") is True
        assert await assignment.ack is True

        assert dispatcher.notify_complete("step-1", "runner-a", 0, None) is True
        outcome = await assignment.terminal
        assert outcome.exit_code == 0
        assert outcome.requeued is False

    async def test_a_frame_from_a_different_runner_is_inert(self, dispatcher):
        """The (step_id, runner_id) key is a second layer of the step gate.

        failure_01 keyed its ACK future on the step id alone, so a
        re-assignment of the same step after an ACK timeout collided with
        the future of the assignment that had already timed out.
        """
        assignment = dispatcher.register_assignment("step-1", "runner-a")

        assert dispatcher.notify_ack("step-1", "runner-b") is False
        assert dispatcher.notify_complete("step-1", "runner-b", 0, None) is False
        assert not assignment.ack.done()
        assert not assignment.terminal.done()

    async def test_a_frame_after_release_is_inert(self, dispatcher):
        dispatcher.register_assignment("step-1", "runner-a")
        dispatcher.release_assignment("step-1", "runner-a")

        assert dispatcher.notify_complete("step-1", "runner-a", 0, None) is False

    async def test_complete_without_ack_still_lands(self, dispatcher):
        """A runner that finishes without ACKing still did the work.

        Unblocking the ACK wait here is strictly better than letting the ACK
        timeout reassign a step that is already finished.
        """
        assignment = dispatcher.register_assignment("step-1", "runner-a")

        assert dispatcher.notify_complete("step-1", "runner-a", 3, "boom") is True

        assert assignment.ack.done()
        outcome = await assignment.terminal
        assert (outcome.exit_code, outcome.error) == (3, "boom")

    async def test_requeue_hook_resolves_the_assignment_as_requeued(self, dispatcher):
        """A requeue is NOT a terminal outcome - it tells the executor to
        re-dispatch rather than to yield a result."""
        assignment = dispatcher.register_assignment("step-1", "runner-a")

        dispatcher.on_step_requeued("step-1", "runner-a", "runner died")

        outcome = await assignment.terminal
        assert outcome.requeued is True
        assert outcome.reason == "runner died"

    async def test_recovery_requeue_reaches_the_dispatcher(
        self, dispatcher, registry, db_session, chain
    ):
        """The wiring end to end: a runner dies, recovery requeues, and the
        executor waiting on that assignment is told - no polling involved."""
        dispatcher.install_hooks()
        try:
            await connect_runner(registry, db_session, "doomed")
            step_id = await make_step(db_session, chain)
            await dispatcher.claim(db_session, step_id, {})
            assignment = dispatcher.register_assignment(step_id, "doomed")

            runner = await db_session.get(Runner, "doomed")
            await db_session.refresh(runner)
            await dispatcher.recovery.on_runner_death(db_session, runner)

            outcome = await asyncio.wait_for(assignment.terminal, timeout=2)
            assert outcome.requeued is True

            execution = await db_session.get(StepExecution, step_id)
            await db_session.refresh(execution)
            assert execution.status == StepExecutionStatus.PENDING.value
            assert execution.runner_id is None
        finally:
            registry.set_dispatch_waker(None)
            dispatcher.recovery.set_requeue_hook(None)


# -----------------------------------------------------------------------------
# Closing an assignment out
# -----------------------------------------------------------------------------

class TestReleaseRunner:

    async def test_release_returns_the_runner_to_idle_and_clears_the_pointer(
        self, dispatcher, registry, db_session, chain
    ):
        await connect_runner(registry, db_session, "runner-a")
        step_id = await make_step(db_session, chain)
        await dispatcher.claim(db_session, step_id, {})
        await registry.transition(db_session, "runner-a", RunnerState.BUSY)

        await dispatcher.release_runner(db_session, "runner-a", step_id)

        row = await db_session.get(Runner, "runner-a")
        await db_session.refresh(row)
        assert row.status == RunnerState.IDLE.value
        assert row.current_step_execution_id is None

    async def test_release_never_raises_for_a_runner_that_died_finishing(
        self, dispatcher, registry, db_session, chain
    ):
        """DEAD -> IDLE is illegal by design, and a completed step must not
        become a crashed one because of it."""
        await connect_runner(registry, db_session, "runner-a")
        step_id = await make_step(db_session, chain)
        await dispatcher.claim(db_session, step_id, {})
        await registry.transition(db_session, "runner-a", RunnerState.DEAD)

        await dispatcher.release_runner(db_session, "runner-a", step_id)

        row = await db_session.get(Runner, "runner-a")
        await db_session.refresh(row)
        assert row.status == RunnerState.DEAD.value
        assert row.current_step_execution_id is None


class TestNoRunnerAvailableMessage:
    """The exception is a diagnostic surface, so its text is a contract."""

    def test_message_names_requirements_and_every_connected_runner(self):
        exc = NoRunnerAvailable(
            {"has": ["gpio"], "arch": "arm64"},
            [
                {
                    "id": "pi-1",
                    "runner_type": "generic",
                    "labels": {"arch": "arm64", "has": ["camera"]},
                }
            ],
        )

        message = str(exc)
        assert '"has": ["gpio"]' in message
        assert "pi-1" in message
        assert "camera" in message

    def test_empty_fleet_is_stated_not_omitted(self):
        assert "connected runners: none" in str(NoRunnerAvailable({"arch": "arm64"}, []))


class TestAssignmentOutcome:

    def test_defaults_are_a_non_terminal_nothing_happened(self):
        outcome = AssignmentOutcome()
        assert outcome.exit_code is None
        assert outcome.requeued is False
        assert outcome.cancelled is False
