"""The startup recovery split: local executions FAIL, remote ones REQUEUE.

`recover_orphaned_executions` used to fail everything in a non-terminal state
and its own docstring flagged the divergence it knew was coming. Phase 12.6
closes it, and the two branches need OPPOSITE answers for a reason worth
stating in a test name:

    local   the container died with the backend. There is nothing to
            reconnect to and nothing to reassign.
    remote  a runner genuinely can reconnect and the step genuinely can be
            reassigned. Failing it throws away work a live agent on another
            host is perfectly able to do.

One test per branch, no shared assertion - a single test covering both would
pass while one branch quietly did the other's job.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest_asyncio

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
from app.services.execution.recovery import recover_orphaned_executions
from app.services.execution.runner_state import RunnerState


@pytest_asyncio.fixture
async def chain(db_session):
    repo = Repo(id=str(uuid4()), name="recovery-split-repo")
    pipeline = Pipeline(
        id=str(uuid4()), repo_id=repo.id, name="recovery-split", steps="[]"
    )
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=0,
        step_name="step",
        status="running",
    )
    db_session.add_all([repo, pipeline, run, step_run])
    await db_session.commit()
    return {"run": run, "step_run": step_run}


async def add_execution(db, chain, *, status, runner_id=None) -> StepExecution:
    execution = StepExecution(
        id=str(uuid4()),
        step_run_id=chain["step_run"].id,
        execution_key=f"{chain['run'].id}:0:{uuid4()}",
        status=status,
        runner_id=runner_id,
    )
    db.add(execution)
    await db.commit()
    return execution


async def add_runner(db, runner_id, status) -> Runner:
    runner = Runner(
        id=runner_id, name=runner_id, runner_type="generic", status=status
    )
    db.add(runner)
    await db.commit()
    return runner


class TestLocalBranch:

    async def test_a_local_execution_is_failed(self, db_session, chain):
        """runner_id IS NULL is the local path by definition - only the
        remote CAS ever writes that column."""
        execution = await add_execution(
            db_session, chain, status=StepExecutionStatus.RUNNING.value
        )

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.FAILED.value
        assert execution.error == "Execution interrupted by backend restart"
        assert execution.completed_at is not None

    async def test_a_remote_step_that_was_never_assigned_is_failed_too(
        self, db_session, chain
    ):
        """A `pending` row armed for dispatch but never claimed has no runner
        to reconnect: its generator died with the backend, so it is exactly
        as unrecoverable as a local one."""
        execution = await add_execution(
            db_session, chain, status=StepExecutionStatus.PENDING.value
        )
        execution.runner_requirements = '{"arch": "arm64"}'
        await db_session.commit()

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.FAILED.value


class TestRemoteBranch:

    async def test_a_remote_execution_is_requeued_not_failed(
        self, db_session, chain
    ):
        """The whole point: a runner can come back, so the step goes back to
        `pending` for the dispatcher instead of being thrown away."""
        await add_runner(db_session, "gone-runner", RunnerState.DISCONNECTED.value)
        execution = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.RUNNING.value,
            runner_id="gone-runner",
        )

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id not in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.PENDING.value
        assert execution.runner_id is None
        assert execution.error is None

    async def test_a_dead_runners_step_is_requeued(self, db_session, chain):
        await add_runner(db_session, "dead-runner", RunnerState.DEAD.value)
        execution = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.PREPARING.value,
            runner_id="dead-runner",
        )

        await recover_orphaned_executions(db_session)

        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.PENDING.value

    async def test_a_step_pointing_at_a_runner_row_that_does_not_exist_is_requeued(
        self, db_session, chain
    ):
        """"Gone" includes "never heard of it" - a stale DB or a deleted
        runner must not strand the step forever."""
        execution = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.RUNNING.value,
            runner_id="never-registered",
        )

        await recover_orphaned_executions(db_session)

        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.PENDING.value

    async def test_a_step_on_a_still_connected_runner_is_left_alone(
        self, db_session, chain
    ):
        """Neither branch touches it: not local (it has a runner) and not
        lost (its runner is idle). The startup bootstrap makes this state
        unreachable at startup, which is exactly why the sweep can be safely
        re-run at any time without stealing live work."""
        await add_runner(db_session, "live-runner", RunnerState.IDLE.value)
        execution = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.RUNNING.value,
            runner_id="live-runner",
        )

        recovered = await recover_orphaned_executions(db_session)

        assert execution.id not in recovered
        await db_session.refresh(execution)
        assert execution.status == StepExecutionStatus.RUNNING.value
        assert execution.runner_id == "live-runner"


class TestReturnValue:

    async def test_only_failed_ids_are_returned(self, db_session, chain):
        """A requeued step is not a failure. Conflating them would make a
        healthy remote handover read as a crash in the startup log."""
        await add_runner(db_session, "gone", RunnerState.DEAD.value)
        local = await add_execution(
            db_session, chain, status=StepExecutionStatus.RUNNING.value
        )
        remote = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.RUNNING.value,
            runner_id="gone",
        )

        recovered = await recover_orphaned_executions(db_session)

        assert recovered == [local.id]
        await db_session.refresh(remote)
        assert remote.status == StepExecutionStatus.PENDING.value

    async def test_terminal_executions_are_untouched_on_both_branches(
        self, db_session, chain
    ):
        await add_runner(db_session, "gone", RunnerState.DEAD.value)
        done_local = await add_execution(
            db_session, chain, status=StepExecutionStatus.COMPLETED.value
        )
        done_remote = await add_execution(
            db_session,
            chain,
            status=StepExecutionStatus.COMPLETED.value,
            runner_id="gone",
        )

        assert await recover_orphaned_executions(db_session) == []

        await db_session.refresh(done_local)
        await db_session.refresh(done_remote)
        assert done_local.status == StepExecutionStatus.COMPLETED.value
        assert done_remote.status == StepExecutionStatus.COMPLETED.value
