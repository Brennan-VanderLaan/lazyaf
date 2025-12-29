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
