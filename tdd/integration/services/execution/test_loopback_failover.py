"""
LOOPBACK FAILOVER (Phase 12.6, R7) - a runner can die mid-step and the work
still lands.

This is the assertion that separates 12.6 from the attempt it replaces. That
one had a reconnect protocol on paper and, in the code, a `current_step_execution_id`
that was never written - so JobRecovery had nothing to recover, requeued
steps had no dispatcher to pick them up, and a runner dying mid-step lost the
work silently while the pipeline hung. Every one of those defects is
invisible to a happy-path test.

So this suite kills a BUSY agent with SIGKILL - no drain, no close frame, no
`disconnected`, exactly what a yanked cable looks like - and requires that:

  1. the death monitor notices by HEARTBEAT TIMEOUT (backend time only; no
     runner-supplied timestamp is ever compared to a backend deadline);
  2. `on_runner_death` requeues the step to `pending` and clears the runner's
     `current_step_execution_id`;
  3. the dispatcher hands it to the SURVIVING agent;
  4. the run completes, with exactly ONE terminal StepRun - a second
     `step_complete` from the dead assignment must change nothing.

Budget: DEATH_TIMEOUT + DISPATCH_SWEEP_INTERVAL plus the step's own runtime.

TIER: T2 (Docker-real). See test_loopback_runner.py's module docstring for
why this lives under services/execution rather than the design's path.
"""
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

_repo_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "backend"))

from app.models.pipeline import RunStatus, StepExecution  # noqa: E402
from app.models.runner import Runner  # noqa: E402
from app.services.execution import runner_protocol  # noqa: E402

# The lane fixture, the agent spawner, the git-seeded repo and the polling
# helpers are all shared with the happy-path suite - one definition, so the
# two cannot drift into two different "loopback lanes".
from tdd.integration.services.execution.test_loopback_runner import (  # noqa: E402,F401
    PROOF_FILE,
    STEP_IMAGE,
    fetch_json,
    fetch_run,
    lane,
    make_pipeline,
    step_image,
    wait_for_runner_state,
)

pytestmark = [pytest.mark.integration, pytest.mark.local_exec, pytest.mark.slow]

#: Long enough that the kill lands mid-step with room to spare, short enough
#: that the surviving runner's re-execution does not dominate the suite.
STEP_SLEEP_SECONDS = 25


def failover_step(marker: str) -> dict:
    """Pinned by CAPABILITY, not by runner id.

    The pin has to match BOTH agents or there is nothing to fail over TO -
    which is precisely why the requirement grammar has label matching and
    not only `runner_id`.
    """
    return {
        "name": "Failover probe",
        "type": "script",
        "timeout": 300,
        "config": {
            "image": STEP_IMAGE,
            "requires": {"has": ["failover-lane"]},
            "command": (
                f"echo {marker}-start\n"
                f"ls /workspace/repo/{PROOF_FILE}\n"
                f"sleep {STEP_SLEEP_SECONDS}\n"
                f"echo {marker}-done\n"
            ),
        },
    }


async def wait_for_busy_runner(lane, runner_ids: list[str], timeout: float = 90.0) -> str:
    """Poll until one of `runner_ids` is executing the step.

    Through `fetch_json`, which hands the blocking read to a thread: the test
    server shares this event loop, so a synchronous urlopen here deadlocks the
    very request it is waiting on.
    """
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = await fetch_json(f"{lane.local_url}/api/runners")
        for row in last:
            if row["id"] in runner_ids and row["status"] in {"assigned", "busy"}:
                return row["id"]
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"no runner in {runner_ids} became busy within {timeout}s: {last}"
    )


class TestLoopbackFailover:
    async def test_a_killed_runner_loses_the_step_and_the_other_finishes_it(
        self, lane
    ):
        """Agent E contract 2.

        Two agents carry the same capability label. The step is dispatched to
        one of them; that one is SIGKILLed while the container is still
        sleeping. The work must land anyway, on the survivor, without the
        pipeline executor ever learning that remoteness exists - the generator
        owns the step until terminal and re-dispatches internally, exactly as
        LocalExecutor owns its container until wait() returns.
        """
        labels = "has=docker,has=failover-lane"
        first_id = f"failover-a-{uuid4().hex[:6]}"
        second_id = f"failover-b-{uuid4().hex[:6]}"
        first = lane.spawn_agent(first_id, labels=labels)
        second = lane.spawn_agent(second_id, labels=labels)
        await wait_for_runner_state(lane, first_id, {"idle"})
        await wait_for_runner_state(lane, second_id, {"idle"})

        marker = f"failover-{uuid4().hex[:8]}"
        repo, pipeline = await make_pipeline(lane, [failover_step(marker)])

        async with lane.factory() as db:
            run = await lane.executor.start_pipeline(
                db=db, pipeline=pipeline, repo=repo
            )
            run_id = run.id
        lane.run_ids.append(run_id)

        busy_id = await wait_for_busy_runner(lane, [first_id, second_id])
        victim = first if busy_id == first_id else second
        survivor_id = second_id if busy_id == first_id else first_id

        # SIGKILL: no drain, no close frame. The backend must discover this by
        # heartbeat timeout, which is the only signal a yanked cable gives.
        victim.kill()

        # Budget: the death monitor's own tick plus the dispatcher's self-heal
        # sweep, plus a full re-execution of the step on the survivor.
        budget = (
            runner_protocol.DEATH_TIMEOUT
            + runner_protocol.DISPATCH_SWEEP_INTERVAL
            + STEP_SLEEP_SECONDS
            + 120
        )
        await asyncio.wait_for(lane.executor.wait_for_run(run_id), timeout=budget)

        run = await fetch_run(lane, run_id)
        step = run.step_runs[0]
        assert run.status == RunStatus.PASSED.value, (
            f"the run did not survive a mid-step runner death: "
            f"status={step.status} error={step.error!r}\n"
            f"--- step logs ---\n{step.logs}\n"
            f"--- victim ({busy_id}) ---\n{victim.output()}\n"
            f"--- survivor ({survivor_id}) ---\n"
            f"{(second if victim is first else first).output()}"
        )
        assert f"{marker}-done" in step.logs

        # EXACTLY ONE terminal StepRun. A late `step_complete` from the dead
        # assignment must be dropped by the step gate rather than writing a
        # second terminal state over the real one.
        assert len(run.step_runs) == 1, [
            (s.step_index, s.status) for s in run.step_runs
        ]
        assert step.executor == "remote"

        # The final assignment belongs to the SURVIVOR. The victim's execution
        # row (if a second attempt row exists) must not be the terminal one.
        async with lane.factory() as db:
            executions = list(
                (
                    await db.execute(
                        select(StepExecution)
                        .where(StepExecution.step_run_id == step.id)
                        .order_by(StepExecution.created_at)
                    )
                )
                .scalars()
                .all()
            )
        terminal = [e for e in executions if e.status == "completed"]
        assert terminal, [(e.status, e.runner_id) for e in executions]
        assert terminal[-1].runner_id == survivor_id, (
            "the completed execution is still attributed to the killed runner: "
            + str([(e.status, e.runner_id) for e in executions])
        )

        # And the survivor is idle and holding nothing.
        row = await wait_for_runner_state(lane, survivor_id, {"idle"}, timeout=60)
        assert row["current_step_execution_id"] is None

    async def test_the_dead_runner_is_marked_dead_and_holds_no_step(self, lane):
        """The registry's own bookkeeping after a death.

        `current_step_execution_id` being written on EVERY assignment is what
        makes recovery possible at all - it was the single omission that
        neutered the whole recovery service in the salvaged attempt. So this
        asserts the field's full lifecycle: set on assignment, CLEARED on
        death, with the step back in `pending` for the dispatcher.
        """
        labels = "has=docker,has=failover-lane"
        runner_id = f"solo-{uuid4().hex[:6]}"
        agent = lane.spawn_agent(runner_id, labels=labels)
        await wait_for_runner_state(lane, runner_id, {"idle"})

        marker = f"solo-{uuid4().hex[:8]}"
        repo, pipeline = await make_pipeline(lane, [failover_step(marker)])
        async with lane.factory() as db:
            run = await lane.executor.start_pipeline(
                db=db, pipeline=pipeline, repo=repo
            )
            run_id = run.id
        lane.run_ids.append(run_id)

        await wait_for_busy_runner(lane, [runner_id])
        async with lane.factory() as db:
            busy = (
                await db.execute(select(Runner).where(Runner.id == runner_id))
            ).scalar_one()
            held = busy.current_step_execution_id
        assert held, (
            "the registry never recorded which step the runner holds - "
            "recovery has nothing to recover from"
        )

        agent.kill()
        await wait_for_runner_state(
            lane,
            runner_id,
            {"dead", "disconnected"},
            timeout=runner_protocol.DEATH_TIMEOUT + 45,
        )

        async with lane.factory() as db:
            dead = (
                await db.execute(select(Runner).where(Runner.id == runner_id))
            ).scalar_one()
            execution = (
                await db.execute(
                    select(StepExecution).where(StepExecution.id == held)
                )
            ).scalar_one()
        assert dead.current_step_execution_id is None
        assert execution.status == "pending", (
            "the orphaned step was not requeued; with no other runner it will "
            "sit until NO_RUNNER_TIMEOUT, but it must be PENDING, not stuck "
            f"in {execution.status!r}"
        )
        assert execution.runner_id is None

        # Nothing else is expected to pick it up - let the run end however it
        # ends, and make sure the suite does not leave a container behind.
        try:
            await asyncio.wait_for(
                lane.executor.wait_for_run(run_id),
                timeout=runner_protocol.NO_RUNNER_TIMEOUT + 60,
            )
        except asyncio.TimeoutError:  # pragma: no cover - diagnostic only
            pytest.fail(
                "the run never reached a terminal state after its only runner "
                "died: a step nobody can execute must FAIL, not hang "
                f"(agent log)\n{agent.output()}"
            )
        run = await fetch_run(lane, run_id)
        assert run.status in {RunStatus.FAILED.value, RunStatus.PASSED.value}
