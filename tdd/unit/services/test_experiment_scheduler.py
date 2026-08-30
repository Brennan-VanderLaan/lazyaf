"""
Unit tests for the experiment pump: claiming, concurrency, completion,
abort, stall detection and resume (Phase 12.6.5).

Dispatch itself is stubbed here — `start_cell_run` is replaced by a fake that
creates a real `PipelineRun` row without executing anything — so these tests
pin the SCHEDULER's behaviour deterministically. The real dispatch path is
exercised end to end in tdd/integration/api/test_experiments_api.py.

The WS manager is the REAL one (R6): broadcasts to zero connections are a
no-op, so there is nothing to mock and nothing that can silently stop being
called.
"""
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Card, Pipeline, PipelineRun, Repo, RunStatus, StepRun, TestRef, TestRun
from app.models.experiment import (
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.services import experiment_service as svc


from tdd.unit.services.experiment_rows import (  # noqa: E402
    cells_by_status,
    clean_pump_state,  # noqa: F401  (autouse fixture)
    fake_dispatch,  # noqa: F401  (fixture)
    make_card,
    make_experiment,
    make_repo,
    make_run,
)


# -----------------------------------------------------------------------------
# Launch + concurrency
# -----------------------------------------------------------------------------

class TestLaunch:
    async def test_launch_creates_one_cell_per_matrix_point(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, models=2, prompts=2, repeat=3
        )

        created, dispatched = await svc.launch(db_session, experiment)

        assert created == 12
        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == experiment.id
                    )
                )
            ).scalars()
        )
        assert len(cells) == 12
        assert sorted(c.cell_index for c in cells) == list(range(12))
        assert len({c.variant_index for c in cells}) == 4

    async def test_launch_freezes_coordinates_on_every_cell(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(db_session, repo, card, models=2)

        await svc.launch(db_session, experiment)

        cells = {
            c.cell_index: c
            for c in (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == experiment.id
                    )
                )
            ).scalars()
        }
        assert cells[0].model == "m0"
        assert cells[1].model == "m1"
        assert all(c.agent == "mock" for c in cells.values())
        assert all(c.label for c in cells.values())

    async def test_launch_moves_the_experiment_to_running(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(db_session, repo, card)

        await svc.launch(db_session, experiment)

        assert experiment.status == ExperimentStatus.RUNNING.value
        assert experiment.launched_at is not None


class TestConcurrency:
    async def test_pump_respects_the_concurrency_ceiling(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=5
        )

        created, dispatched = await svc.launch(db_session, experiment)

        assert created == 5
        assert dispatched == 2
        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.RUNNING.value] == 2
        assert counts[ExperimentRunStatus.PENDING.value] == 3

    async def test_cells_dispatch_in_cell_index_order(self, db_session, fake_dispatch):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=4
        )

        await svc.launch(db_session, experiment)

        running = list(
            (
                await db_session.execute(
                    select(ExperimentRun.cell_index).where(
                        ExperimentRun.experiment_id == experiment.id,
                        ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                    )
                )
            ).scalars()
        )
        assert sorted(running) == [0, 1]

    async def test_live_count_is_read_from_the_database_not_memory(
        self, db_session, fake_dispatch
    ):
        """A backend restart must not lose the count, so a second pump on a
        full board dispatches nothing."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=3
        )
        await svc.launch(db_session, experiment)

        svc._pump_locks.clear()
        assert await svc.pump(db_session, experiment.id) == 0

    async def test_claim_is_compare_and_set_single_winner(self, db_session):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(db_session, repo, card, models=1)
        cell = ExperimentRun(
            id=str(uuid4()), experiment_id=experiment.id, cell_index=0,
            variant_index=0, agent="mock",
        )
        db_session.add(cell)
        await db_session.commit()

        first = await svc._claim(db_session, cell.id)
        second = await svc._claim(db_session, cell.id)

        assert first is True
        assert second is False, "read-then-write would have let both win"

    async def test_two_concurrent_pumps_do_not_double_dispatch(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=4, models=4
        )
        await svc.launch(db_session, experiment)
        # Reset the board so both pumps see pending work.
        for cell in (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id
                )
            )
        ).scalars():
            cell.status = ExperimentRunStatus.PENDING.value
        await db_session.commit()
        fake_dispatch.clear()
        svc._pump_locks.clear()

        await asyncio.gather(
            svc.pump(db_session, experiment.id), svc.pump(db_session, experiment.id)
        )

        dispatched_cells = [cell_id for _, cell_id in fake_dispatch]
        assert len(dispatched_cells) == len(set(dispatched_cells)) == 4


class TestReentrancy:
    async def test_synchronous_completion_does_not_recurse(self, db_session, monkeypatch):
        """start_pipeline can complete a run INSIDE the dispatch call, which
        re-enters the pump through on_cell_complete. Without the re-entrancy
        guard this recurses once per cell."""
        depth = {"current": 0, "max": 0}
        real_pump_once = svc._pump_once

        async def counting_pump_once(db, experiment_id):
            depth["current"] += 1
            depth["max"] = max(depth["max"], depth["current"])
            try:
                return await real_pump_once(db, experiment_id)
            finally:
                depth["current"] -= 1

        monkeypatch.setattr(svc, "_pump_once", counting_pump_once)

        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=6
        )

        async def _sync_failing_start(db, exp, cell):
            run = await make_run(
                db, repo, status=RunStatus.FAILED.value, trigger_ref=cell.id
            )
            # This is exactly what _complete_pipeline does inline.
            await svc.on_cell_complete(db, run, False)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _sync_failing_start)

        await svc.launch(db_session, experiment)

        assert depth["max"] == 1, "the pump re-entered itself"
        counts = await cells_by_status(db_session, experiment.id)
        assert counts.get(ExperimentRunStatus.PENDING.value, 0) == 0

    async def test_dispatch_failure_records_the_cell_and_keeps_going(
        self, db_session, monkeypatch
    ):
        """One bad cell never kills a matrix."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=4, models=3
        )

        calls = {"n": 0}

        async def _flaky(db, exp, cell):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("image preflight failed: lazyaf-agent-base:dev")
            return await make_run(db, repo, trigger_ref=cell.id)

        monkeypatch.setattr(svc, "start_cell_run", _flaky)
        await svc.launch(db_session, experiment)

        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.ERROR.value] == 1
        assert counts[ExperimentRunStatus.RUNNING.value] == 2
        errored = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status == ExperimentRunStatus.ERROR.value,
                )
            )
        ).scalar_one()
        assert "image preflight failed" in errored.error


# -----------------------------------------------------------------------------
# Completion + classification
# -----------------------------------------------------------------------------

class TestClassification:
    async def _cell_with_run(self, db, run_status, *, tests=None):
        repo = await make_repo(db)
        card = await make_card(db, repo)
        experiment = await make_experiment(db, repo, card, models=1)
        cell = ExperimentRun(
            id=str(uuid4()), experiment_id=experiment.id, cell_index=0,
            variant_index=0, agent="mock",
            status=ExperimentRunStatus.RUNNING.value,
        )
        db.add(cell)
        await db.commit()
        run = await make_run(db, repo, status=run_status, trigger_ref=cell.id)
        cell.pipeline_run_id = run.id
        await db.commit()

        for status in tests or []:
            ref = TestRef(id=str(uuid4()), lazyaf_test_id=f"t-{uuid4().hex[:6]}",
                          repo_id=repo.id, status="active")
            db.add(ref)
            await db.commit()
            db.add(
                TestRun(
                    id=str(uuid4()), test_ref_id=ref.id, pipeline_run_id=run.id,
                    commit_sha="", status=status, experiment_run_id=cell.id,
                )
            )
        await db.commit()
        return experiment, cell, run

    async def test_successful_run_is_passed(self, db_session):
        _, cell, run = await self._cell_with_run(db_session, RunStatus.PASSED.value)
        await svc.on_cell_complete(db_session, run, True)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.PASSED.value

    async def test_successful_run_with_no_tests_is_still_passed(self, db_session):
        _, cell, run = await self._cell_with_run(db_session, RunStatus.PASSED.value)
        await svc.on_cell_complete(db_session, run, True)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.PASSED.value

    async def test_failed_run_with_test_evidence_is_failed_not_error(self, db_session):
        """The suite came back red: that IS the measurement."""
        _, cell, run = await self._cell_with_run(
            db_session, RunStatus.FAILED.value, tests=["failed", "passed"]
        )
        await svc.on_cell_complete(db_session, run, False)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.FAILED.value

    async def test_failed_run_with_no_test_evidence_is_error_not_a_zero_score(
        self, db_session
    ):
        _, cell, run = await self._cell_with_run(db_session, RunStatus.FAILED.value)
        await svc.on_cell_complete(db_session, run, False)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.ERROR.value
        assert "nothing was measured" in cell.error

    async def test_cancelled_run_is_cancelled_even_when_told_it_succeeded(
        self, db_session
    ):
        _, cell, run = await self._cell_with_run(db_session, RunStatus.CANCELLED.value)
        await svc.on_cell_complete(db_session, run, True)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.CANCELLED.value

    async def test_completion_is_idempotent(self, db_session):
        _, cell, run = await self._cell_with_run(db_session, RunStatus.PASSED.value)
        await svc.on_cell_complete(db_session, run, True)
        first_completed_at = cell.completed_at
        await svc.on_cell_complete(db_session, run, False)
        await db_session.refresh(cell)
        assert cell.status == ExperimentRunStatus.PASSED.value
        assert cell.completed_at == first_completed_at

    async def test_completion_on_an_unknown_trigger_ref_is_a_noop(self, db_session):
        repo = await make_repo(db_session)
        run = await make_run(db_session, repo, trigger_ref=str(uuid4()))
        await svc.on_cell_complete(db_session, run, True)  # must not raise

    async def test_completion_never_raises(self, db_session, monkeypatch):
        """The caller's log-and-swallow is a backstop, not load-bearing."""
        _, cell, run = await self._cell_with_run(db_session, RunStatus.PASSED.value)

        async def _boom(*args, **kwargs):
            raise RuntimeError("classification exploded")

        monkeypatch.setattr(svc, "classify_cell", _boom)
        await svc.on_cell_complete(db_session, run, True)

    async def test_completion_backfills_the_pipeline_run_mirror(self, db_session):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(db_session, repo, card, models=1)
        cell = ExperimentRun(
            id=str(uuid4()), experiment_id=experiment.id, cell_index=0,
            variant_index=0, agent="mock",
            status=ExperimentRunStatus.DISPATCHING.value,
        )
        db_session.add(cell)
        await db_session.commit()
        run = await make_run(db_session, repo, status=RunStatus.PASSED.value,
                             trigger_ref=cell.id)

        await svc.on_cell_complete(db_session, run, True)

        await db_session.refresh(cell)
        assert cell.pipeline_run_id == run.id


class TestFinalization:
    async def test_experiment_completes_when_all_cells_terminal(
        self, db_session, monkeypatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=3
        )

        async def _immediate(db, exp, cell):
            run = await make_run(db, repo, status=RunStatus.PASSED.value,
                                 trigger_ref=cell.id)
            await svc.on_cell_complete(db, run, True)
            return run

        monkeypatch.setattr(svc, "start_cell_run", _immediate)
        await svc.launch(db_session, experiment)

        await db_session.refresh(experiment)
        assert experiment.status == ExperimentStatus.COMPLETE.value
        assert experiment.completed_at is not None
        counts = await cells_by_status(db_session, experiment.id)
        assert counts == {ExperimentRunStatus.PASSED.value: 3}

    async def test_running_experiment_is_not_finalized_early(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=3
        )
        await svc.launch(db_session, experiment)
        await db_session.refresh(experiment)
        assert experiment.status == ExperimentStatus.RUNNING.value
        assert experiment.completed_at is None


class TestAbort:
    async def test_abort_cancels_pending_and_leaves_running_alone(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=5
        )
        await svc.launch(db_session, experiment)

        cancelled, still_running = await svc.abort(db_session, experiment)

        assert cancelled == 3
        assert still_running == 2
        counts = await cells_by_status(db_session, experiment.id)
        assert counts[ExperimentRunStatus.CANCELLED.value] == 3
        assert counts[ExperimentRunStatus.RUNNING.value] == 2
        assert experiment.status == ExperimentStatus.ABORTED.value

    async def test_running_cells_of_an_aborted_experiment_still_count(
        self, db_session, fake_dispatch
    ):
        """Work already paid for is measurement; throwing it away is the
        expensive kind of tidy."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=3
        )
        await svc.launch(db_session, experiment)
        await svc.abort(db_session, experiment)

        live = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalar_one()
        run = await db_session.get(PipelineRun, live.pipeline_run_id)
        run.status = RunStatus.PASSED.value
        await db_session.commit()
        await svc.on_cell_complete(db_session, run, True)

        await db_session.refresh(live)
        await db_session.refresh(experiment)
        assert live.status == ExperimentRunStatus.PASSED.value
        assert experiment.status == ExperimentStatus.ABORTED.value
        assert experiment.completed_at is not None

    async def test_abort_does_not_dispatch_anything_new(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=4
        )
        await svc.launch(db_session, experiment)
        await svc.abort(db_session, experiment)
        before = len(fake_dispatch)

        svc._pump_locks.clear()
        assert await svc.pump(db_session, experiment.id) == 0
        assert len(fake_dispatch) == before


class TestStallAndResume:
    async def test_stalled_is_reported_when_the_pump_is_gone(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=4
        )
        await svc.launch(db_session, experiment)
        assert await svc.is_stalled(db_session, experiment) is False

        # Simulate the restart: the live cells are gone with the process.
        for cell in (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalars():
            cell.status = ExperimentRunStatus.PASSED.value
        await db_session.commit()

        assert await svc.is_stalled(db_session, experiment) is True

    async def test_a_complete_experiment_is_never_stalled(self, db_session):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, status=ExperimentStatus.COMPLETE.value
        )
        assert await svc.is_stalled(db_session, experiment) is False

    async def test_resume_dispatches_the_remaining_cells(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=2, models=4
        )
        await svc.launch(db_session, experiment)
        for cell in (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalars():
            cell.status = ExperimentRunStatus.PASSED.value
        await db_session.commit()
        svc._pump_locks.clear()

        dispatched, reset = await svc.resume(db_session, experiment)

        assert dispatched == 2
        assert reset == 0
        counts = await cells_by_status(db_session, experiment.id)
        assert counts.get(ExperimentRunStatus.PENDING.value, 0) == 0

    async def test_resume_returns_orphaned_dispatching_cells_to_pending(
        self, db_session, fake_dispatch
    ):
        """A cell left dispatching with no run never started - the run row
        would exist otherwise."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=2
        )
        await svc.launch(db_session, experiment)
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.cell_index == 0,
                )
            )
        ).scalar_one()
        cell.status = ExperimentRunStatus.DISPATCHING.value
        cell.pipeline_run_id = None
        await db_session.commit()
        svc._pump_locks.clear()

        dispatched, reset = await svc.resume(db_session, experiment)

        assert reset == 1
        assert dispatched >= 1

    async def test_resume_classifies_a_live_cell_whose_run_already_finished(
        self, db_session, fake_dispatch
    ):
        """The completion hook fired into a process that is gone."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1, models=1
        )
        await svc.launch(db_session, experiment)
        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        run = await db_session.get(PipelineRun, cell.pipeline_run_id)
        run.status = RunStatus.PASSED.value
        await db_session.commit()
        svc._pump_locks.clear()

        await svc.resume(db_session, experiment)

        await db_session.refresh(cell)
        await db_session.refresh(experiment)
        assert cell.status == ExperimentRunStatus.PASSED.value
        assert experiment.status == ExperimentStatus.COMPLETE.value
