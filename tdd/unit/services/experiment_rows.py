"""
Shared row builders and fixtures for the Phase 12.6.5 experiment unit suites.

Not a test module (no `test_` prefix): it is imported by
`test_experiment_scheduler.py`, `test_experiment_budget.py` and
`test_prompt_version_freeze.py`, following the
`tdd/unit/control_runtime/*_contract.py` convention for shared, non-collected
helpers.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (
    Card,
    Pipeline,
    PipelineRun,
    Repo,
    RunStatus,
    StepExecution,
    StepRun,
    StepUsage,
)
from app.models.experiment import Experiment, ExperimentRun, ExperimentStatus
from app.services import experiment_service as svc


async def make_repo(db) -> Repo:
    repo = Repo(id=str(uuid4()), name=f"repo-{uuid4().hex[:6]}", is_ingested=True)
    db.add(repo)
    await db.commit()
    return repo


async def make_card(db, repo) -> Card:
    card = Card(
        id=str(uuid4()),
        repo_id=repo.id,
        title="Add the widget",
        description="It should widget.",
    )
    db.add(card)
    await db.commit()
    return card


def matrix_json(models=2, prompts=1, repeat=1) -> str:
    return json.dumps(
        {
            "models": [
                {"agent": "mock", "model": f"m{i}", "label": f"m{i}"}
                for i in range(models)
            ],
            "prompts": [
                {"prompt_template_id": None, "label": f"p{i}"} for i in range(prompts)
            ],
            "repeat": repeat,
        }
    )


async def make_experiment(
    db,
    repo,
    card,
    *,
    budget="10.00",
    concurrency=2,
    models=2,
    prompts=1,
    repeat=1,
    status=ExperimentStatus.DRAFT.value,
    matrix=None,
) -> Experiment:
    experiment = Experiment(
        id=str(uuid4()),
        name="opus vs haiku",
        description="",
        target_type="card",
        target_id=card.id,
        repo_id=repo.id,
        matrix=matrix if matrix is not None else matrix_json(models, prompts, repeat),
        budget_usd=Decimal(budget),
        max_concurrency=concurrency,
        status=status,
    )
    db.add(experiment)
    await db.commit()
    return experiment


async def make_run(db, repo, *, status=RunStatus.RUNNING.value, trigger_ref=None):
    pipeline = Pipeline(
        id=str(uuid4()), repo_id=repo.id, name=f"p-{uuid4().hex[:6]}", steps="[]"
    )
    db.add(pipeline)
    await db.commit()
    run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status=status,
        trigger_type=svc.TRIGGER_EXPERIMENT,
        trigger_ref=trigger_ref,
    )
    db.add(run)
    await db.commit()
    return run


async def add_usage(
    db, pipeline_run_id, *, cost="1.00", source="cli-reported", model="m0",
    wall_clock_ms=1000, input_tokens=100, output_tokens=50,
):
    """A StepUsage row against a run, through the real FK chain."""
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=pipeline_run_id,
        step_index=0,
        step_name="agent",
        status=RunStatus.PASSED.value,
    )
    db.add(step_run)
    await db.commit()
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{pipeline_run_id}:0:{uuid4().hex[:8]}",
        step_run_id=step_run.id,
    )
    db.add(execution)
    await db.commit()
    usage = StepUsage(
        id=str(uuid4()),
        step_execution_id=execution.id,
        step_run_id=step_run.id,
        pipeline_run_id=pipeline_run_id,
        provider="self-hosted",
        model=model,
        cost_usd=Decimal(cost) if cost is not None else None,
        cost_source=source,
        wall_clock_ms=wall_clock_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        determinism="{}",
    )
    db.add(usage)
    await db.commit()
    return usage


async def cells_by_status(db, experiment_id) -> dict[str, int]:
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(ExperimentRun.status).where(
                ExperimentRun.experiment_id == experiment_id
            )
        )
    ).scalars()
    counts: dict[str, int] = {}
    for status in rows:
        counts[status] = counts.get(status, 0) + 1
    return counts


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Replace `start_cell_run` with a row-creating stub.

    Returns the list of `(experiment_id, cell_id)` it was called with, so a
    test can assert exactly which cells left the gate.
    """
    calls: list[tuple[str, str]] = []

    async def _start(db, experiment, cell):
        calls.append((experiment.id, cell.id))
        return await make_run(
            db, await db.get(Repo, experiment.repo_id), trigger_ref=cell.id
        )

    monkeypatch.setattr(svc, "start_cell_run", _start)
    return calls


@pytest.fixture(autouse=True)
def clean_pump_state():
    """The pump's locks are process-global; a leaked one would make the next
    test's first pump a silent no-op."""
    svc._pump_locks.clear()
    svc._repump.clear()
    yield
    svc._pump_locks.clear()
    svc._repump.clear()
