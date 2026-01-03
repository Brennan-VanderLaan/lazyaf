"""
Model factories for creating test data.

These factories create SQLAlchemy model instances for use in tests.
They can be used directly in unit tests or with database sessions
in integration tests.
"""
import sys
from datetime import datetime
from pathlib import Path

import factory
from faker import Faker

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Card, CardStatus, Job, JobStatus, Repo, Runner, RunnerStatus

from .base import BaseFactory, generate_branch_name, generate_uuid

fake = Faker()


class RepoFactory(BaseFactory):
    """Factory for creating Repo instances."""

    class Meta:
        model = Repo

    id = factory.LazyFunction(generate_uuid)
    name = factory.LazyFunction(lambda: fake.word().capitalize() + "Project")
    path = factory.LazyFunction(lambda: f"/repos/{fake.slug()}")
    remote_url = factory.LazyFunction(
        lambda: f"https://github.com/{fake.user_name()}/{fake.slug()}.git"
    )
    default_branch = "main"
    created_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating repos in specific states."""

        local_only = factory.Trait(
            remote_url=None,
        )
        with_dev_branch = factory.Trait(
            default_branch="dev",
        )


class CardFactory(BaseFactory):
    """Factory for creating Card instances."""

    class Meta:
        model = Card

    id = factory.LazyFunction(generate_uuid)
    repo_id = factory.LazyFunction(generate_uuid)
    title = factory.LazyFunction(lambda: fake.sentence(nb_words=4).rstrip("."))
    description = factory.LazyFunction(lambda: fake.paragraph(nb_sentences=3))
    status = CardStatus.TODO.value
    branch_name = None
    pr_url = None
    job_id = None
    created_at = factory.LazyFunction(datetime.utcnow)
    updated_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating cards in specific states."""

        in_progress = factory.Trait(
            status=CardStatus.IN_PROGRESS.value,
            branch_name=factory.LazyAttribute(
                lambda o: generate_branch_name(o.title)
            ),
        )
        in_review = factory.Trait(
            status=CardStatus.IN_REVIEW.value,
            branch_name=factory.LazyAttribute(
                lambda o: generate_branch_name(o.title)
            ),
            pr_url=factory.LazyFunction(
                lambda: f"https://github.com/org/repo/pull/{fake.random_int(1, 1000)}"
            ),
        )
        done = factory.Trait(
            status=CardStatus.DONE.value,
            branch_name=factory.LazyAttribute(
                lambda o: generate_branch_name(o.title)
            ),
            pr_url=factory.LazyFunction(
                lambda: f"https://github.com/org/repo/pull/{fake.random_int(1, 1000)}"
            ),
        )
        failed = factory.Trait(
            status=CardStatus.FAILED.value,
        )


class JobFactory(BaseFactory):
    """Factory for creating Job instances."""

    class Meta:
        model = Job

    id = factory.LazyFunction(generate_uuid)
    card_id = factory.LazyFunction(generate_uuid)
    runner_id = None
    status = JobStatus.QUEUED.value
    logs = ""
    error = None
    started_at = None
    completed_at = None
    created_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating jobs in specific states."""

        running = factory.Trait(
            status=JobStatus.RUNNING.value,
            runner_id=factory.LazyFunction(generate_uuid),
            started_at=factory.LazyFunction(datetime.utcnow),
            logs="Starting job...\n",
        )
        completed = factory.Trait(
            status=JobStatus.COMPLETED.value,
            runner_id=factory.LazyFunction(generate_uuid),
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
            logs="Job completed successfully.\n",
        )
        failed = factory.Trait(
            status=JobStatus.FAILED.value,
            runner_id=factory.LazyFunction(generate_uuid),
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
            error="Task failed: unexpected error",
            logs="Error occurred during execution.\n",
        )


class RunnerFactory(BaseFactory):
    """Factory for creating Runner instances."""

    class Meta:
        model = Runner

    id = factory.LazyFunction(generate_uuid)
    container_id = factory.LazyFunction(
        lambda: fake.hexify(text="^^^^^^^^^^^^", upper=False)
    )
    status = RunnerStatus.IDLE.value
    current_job_id = None
    last_heartbeat = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating runners in specific states."""

        busy = factory.Trait(
            status=RunnerStatus.BUSY.value,
            current_job_id=factory.LazyFunction(generate_uuid),
        )
        offline = factory.Trait(
            status=RunnerStatus.OFFLINE.value,
            container_id=None,
        )
        no_container = factory.Trait(
            container_id=None,
        )


# Import Pipeline models
from app.models.pipeline import Pipeline, PipelineRun, StepRun, RunStatus
from app.models.step_execution import StepExecution, ExecutionStatus


class PipelineFactory(BaseFactory):
    """Factory for creating Pipeline instances."""

    class Meta:
        model = Pipeline

    id = factory.LazyFunction(generate_uuid)
    repo_id = factory.LazyFunction(generate_uuid)
    name = factory.LazyFunction(lambda: fake.word().capitalize() + " Pipeline")
    description = factory.LazyFunction(lambda: fake.sentence())
    steps = "[]"  # JSON string of steps
    is_template = False
    created_at = factory.LazyFunction(datetime.utcnow)
    updated_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating pipelines in specific states."""

        with_steps = factory.Trait(
            steps='[{"name": "Test", "type": "script", "config": {"command": "npm test"}, "on_success": "next", "on_failure": "stop", "timeout": 300}]',
        )
        multi_step = factory.Trait(
            steps='[{"name": "Lint", "type": "script", "config": {"command": "npm run lint"}, "on_success": "next", "on_failure": "stop", "timeout": 300}, {"name": "Test", "type": "script", "config": {"command": "npm test"}, "on_success": "next", "on_failure": "stop", "timeout": 300}, {"name": "Build", "type": "script", "config": {"command": "npm run build"}, "on_success": "stop", "on_failure": "stop", "timeout": 300}]',
        )


class PipelineRunFactory(BaseFactory):
    """Factory for creating PipelineRun instances."""

    class Meta:
        model = PipelineRun

    id = factory.LazyFunction(generate_uuid)
    pipeline_id = factory.LazyFunction(generate_uuid)
    status = RunStatus.PENDING.value
    trigger_type = "manual"
    trigger_ref = None
    current_step = 0
    steps_completed = 0
    steps_total = 0
    started_at = None
    completed_at = None
    created_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating pipeline runs in specific states."""

        running = factory.Trait(
            status=RunStatus.RUNNING.value,
            started_at=factory.LazyFunction(datetime.utcnow),
        )
        passed = factory.Trait(
            status=RunStatus.PASSED.value,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
        )
        failed = factory.Trait(
            status=RunStatus.FAILED.value,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
        )
        cancelled = factory.Trait(
            status=RunStatus.CANCELLED.value,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
        )


class StepRunFactory(BaseFactory):
    """Factory for creating StepRun instances."""

    class Meta:
        model = StepRun

    id = factory.LazyFunction(generate_uuid)
    pipeline_run_id = factory.LazyFunction(generate_uuid)
    step_index = 0
    step_name = factory.LazyFunction(lambda: fake.word().capitalize() + " Step")
    status = RunStatus.PENDING.value
    job_id = None
    logs = ""
    error = None
    started_at = None
    completed_at = None

    class Params:
        """Parameters for creating step runs in specific states."""

        running = factory.Trait(
            status=RunStatus.RUNNING.value,
            started_at=factory.LazyFunction(datetime.utcnow),
        )
        passed = factory.Trait(
            status=RunStatus.PASSED.value,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
            logs="Step completed successfully.\n",
        )
        failed = factory.Trait(
            status=RunStatus.FAILED.value,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
            error="Step failed: test error",
            logs="Error occurred.\n",
        )
        with_job = factory.Trait(
            job_id=factory.LazyFunction(generate_uuid),
        )


class StepExecutionFactory(BaseFactory):
    """Factory for creating StepExecution instances."""

    class Meta:
        model = StepExecution

    id = factory.LazyFunction(generate_uuid)
    execution_key = factory.LazyFunction(
        lambda: f"run-{generate_uuid()[:8]}:0:1"
    )
    step_run_id = factory.LazyFunction(generate_uuid)
    status = ExecutionStatus.PENDING.value
    runner_id = None
    container_id = None
    exit_code = None
    started_at = None
    completed_at = None
    created_at = factory.LazyFunction(datetime.utcnow)

    class Params:
        """Parameters for creating step executions in specific states."""

        preparing = factory.Trait(
            status=ExecutionStatus.PREPARING.value,
            container_id=factory.LazyFunction(
                lambda: fake.hexify(text="^^^^^^^^^^^^", upper=False)
            ),
        )
        running = factory.Trait(
            status=ExecutionStatus.RUNNING.value,
            container_id=factory.LazyFunction(
                lambda: fake.hexify(text="^^^^^^^^^^^^", upper=False)
            ),
            started_at=factory.LazyFunction(datetime.utcnow),
        )
        completed = factory.Trait(
            status=ExecutionStatus.COMPLETED.value,
            exit_code=0,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
        )
        failed = factory.Trait(
            status=ExecutionStatus.FAILED.value,
            exit_code=1,
            started_at=factory.LazyFunction(datetime.utcnow),
            completed_at=factory.LazyFunction(datetime.utcnow),
        )
        cancelled = factory.Trait(
            status=ExecutionStatus.CANCELLED.value,
        )
        with_runner = factory.Trait(
            runner_id=factory.LazyFunction(generate_uuid),
        )
        with_container = factory.Trait(
            container_id=factory.LazyFunction(
                lambda: fake.hexify(text="^^^^^^^^^^^^", upper=False)
            ),
        )
