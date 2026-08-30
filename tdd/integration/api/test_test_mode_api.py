"""
Integration tests for the env-gated test-mode API (Phase 0c).

Covers the standing-rule R6 requirement that the e2e reset endpoint resets
in-memory singletons too, not just the database:
- /api/test/reset clears all DB tables AND job queue, runner pool, WS
  connections, playground sessions, and the trigger-dedup cache
- /api/test/seed creates deterministic fixtures and returns a stable shape
- the router is NOT registered on the app when LAZYAF_TEST_MODE is off
  (404), and endpoints 403 as defense in depth even if mounted
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import assert_status_code


@pytest.fixture(autouse=True)
def _restore_singletons():
    """These tests exercise the real module-level singletons; leave them
    empty for the rest of the suite no matter how a test exits."""
    yield
    from app.services.job_queue import job_queue
    from app.services.playground_service import playground_service
    from app.services.runner_pool import runner_pool
    from app.services.trigger_service import trigger_deduplicator
    from app.services.websocket import manager

    job_queue._jobs.clear()
    job_queue._pending.clear()
    runner_pool._runners.clear()
    manager.active_connections = []
    playground_service._sessions.clear()
    trigger_deduplicator._triggers.clear()


@pytest.fixture
def test_mode_enabled(monkeypatch):
    """Flip the cached Settings instance's flag for the duration of a test."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "test_mode", True)


@pytest_asyncio.fixture
async def test_api_app(db_session):
    """A fresh app with the test router mounted, regardless of the env flag
    (registration-on-flag is asserted separately against the real app)."""
    from fastapi import FastAPI

    from app.database import get_db
    from app.routers import test_api

    app = FastAPI()
    app.include_router(test_api.router)

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_client(test_api_app, test_mode_enabled):
    transport = ASGITransport(app=test_api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class _StubWebSocket:
    """Capturing stand-in for a live connection on the real manager."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class TestGating:
    """The router must not exist at all unless LAZYAF_TEST_MODE is set."""

    async def test_router_absent_from_main_app_when_flag_off(self, client):
        """The suite runs without LAZYAF_TEST_MODE: no /api/test/* routes.

        Prefix is '/api/test/' (trailing slash): the 12.2.6 test tie-back
        surface (/api/test-refs/*) legitimately shares the shorter prefix
        and is NOT gated by the flag."""
        from app.main import app as main_app

        test_paths = [
            r.path for r in main_app.routes if r.path.startswith("/api/test/")
        ]
        assert test_paths == []

        response = await client.post("/api/test/reset")
        assert_status_code(response, 404)
        response = await client.post("/api/test/seed")
        assert_status_code(response, 404)

    def test_router_registered_when_flag_on(self):
        """A fresh process with LAZYAF_TEST_MODE=true mounts the router."""
        code = (
            "from app.main import app;"
            "print(sorted(r.path for r in app.routes"
            " if r.path.startswith('/api/test')))"
        )
        env = {**os.environ, "LAZYAF_TEST_MODE": "true"}
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(backend_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "/api/test/reset" in result.stdout
        assert "/api/test/seed" in result.stdout

    async def test_endpoints_403_when_flag_off_even_if_mounted(
        self, test_api_app
    ):
        """Defense in depth: a mounted router still refuses without the flag."""
        transport = ASGITransport(app=test_api_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            for path in ("/api/test/reset", "/api/test/seed"):
                response = await ac.post(path)
                assert_status_code(response, 403)


class TestReset:
    """POST /api/test/reset clears the DB and every in-memory singleton."""

    async def test_reset_clears_db_rows(self, test_client, db_session):
        from app.database import Base
        from app.models import Card, Pipeline, PipelineRun, Repo

        repo = Repo(name="doomed-repo")
        db_session.add(repo)
        await db_session.flush()
        pipeline = Pipeline(repo_id=repo.id, name="doomed-pipeline", steps="[]")
        db_session.add(pipeline)
        await db_session.flush()
        db_session.add(PipelineRun(pipeline_id=pipeline.id))
        db_session.add(Card(repo_id=repo.id, title="doomed-card"))
        await db_session.commit()

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)
        body = response.json()
        assert body["success"] is True
        # Every mapped table is covered, order-aware
        assert set(body["tables_cleared"]) == {
            t.name for t in Base.metadata.sorted_tables
        }

        for model in (Repo, Card, Pipeline, PipelineRun):
            count = await db_session.scalar(select(func.count()).select_from(model))
            assert count == 0, f"{model.__name__} rows survived reset"

    async def test_reset_clears_enqueued_job(self, test_client):
        """A job enqueued before reset must be gone from the queue."""
        from app.services.job_queue import QueuedJob, job_queue

        job = QueuedJob(
            id="job-doomed",
            card_id="card-doomed",
            repo_id="repo-doomed",
            repo_url="",
            base_branch="main",
            card_title="Doomed",
            card_description="Enqueued before reset",
        )
        await job_queue.enqueue(job)
        assert job_queue.queue_size == 1

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        assert job_queue.queue_size == 0
        assert job_queue.pending_count == 0
        assert await job_queue.dequeue() is None

    async def test_reset_clears_runner_pool_and_websockets(self, test_client):
        from app.services.runner_pool import runner_pool
        from app.services.websocket import manager

        runner_pool.register(runner_id="runner-doomed", name="doomed")
        assert runner_pool.runner_count == 1

        stub = _StubWebSocket()
        manager.active_connections.append(stub)

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)
        assert "runner_pool" in response.json()["memory_reset"]

        assert runner_pool.runner_count == 0
        assert runner_pool.get_runner("runner-doomed") is None
        assert manager.active_connections == []
        assert stub.closed is True

    async def test_reset_clears_playground_sessions(self, test_client):
        from app.services.playground_service import playground_service

        session_id = await playground_service.start_test(
            repo_id="repo-doomed", branch="main", runner_type="any"
        )
        session = playground_service.get_session(session_id)
        assert session is not None

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        assert playground_service.get_session(session_id) is None
        # Live sessions are cancelled so open SSE streams terminate
        assert session.status == "cancelled"

    async def test_reset_clears_trigger_dedup_cache(self, test_client):
        from app.services.trigger_service import trigger_deduplicator

        assert await trigger_deduplicator.should_trigger("push:r1:k1", 60.0)
        await trigger_deduplicator.record_trigger("push:r1:k1", "run-1")
        assert not await trigger_deduplicator.should_trigger("push:r1:k1", 60.0)

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        # Same key triggers again: the dedup window no longer applies
        assert await trigger_deduplicator.should_trigger("push:r1:k1", 60.0)

    async def test_reset_deletes_internal_git_storage(
        self, test_client, db_session, clean_git_repos
    ):
        from app.models import Repo

        repo = Repo(name="doomed-git-repo", is_ingested=True)
        db_session.add(repo)
        await db_session.commit()
        clean_git_repos.create_bare_repo(repo.id)
        assert clean_git_repos.repo_exists(repo.id)

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        assert not clean_git_repos.repo_exists(repo.id)


class TestSeed:
    """POST /api/test/seed creates deterministic fixtures with stable shape."""

    async def test_seed_returns_stable_shape(
        self, test_client, db_session, clean_git_repos
    ):
        from app.models import Card, Pipeline, Repo

        response = await test_client.post("/api/test/seed")
        assert_status_code(response, 200)
        body = response.json()

        assert body["success"] is True
        assert set(body.keys()) == {"success", "repo", "pipeline", "cards"}
        assert body["repo"]["name"] == "e2e-seed-repo"
        assert body["repo"]["default_branch"] == "main"
        assert body["repo"]["git_initialized"] is True
        assert body["pipeline"]["name"] == "e2e-seed-pipeline"
        assert [c["status"] for c in body["cards"]] == ["todo", "in_review"]

        # Returned ids point at real rows
        repo = await db_session.get(Repo, body["repo"]["id"])
        assert repo is not None and repo.is_ingested is True
        assert await db_session.get(Pipeline, body["pipeline"]["id"]) is not None
        for card in body["cards"]:
            assert await db_session.get(Card, card["id"]) is not None

        # Internal git repo actually exists on disk
        assert clean_git_repos.repo_exists(body["repo"]["id"])

    async def test_seed_git_commit_is_deterministic(
        self, test_client, clean_git_repos
    ):
        """Fixed author/timestamps: every seeded repo has the same HEAD sha."""
        from dulwich.repo import Repo as DulwichRepo

        def head_sha(body: dict) -> bytes:
            repo_path = clean_git_repos.get_repo_path(body["repo"]["id"])
            return DulwichRepo(str(repo_path)).refs[b"refs/heads/main"]

        first = (await test_client.post("/api/test/seed")).json()
        first_sha = head_sha(first)
        await test_client.post("/api/test/reset")
        second = (await test_client.post("/api/test/seed")).json()

        assert head_sha(second) == first_sha

    async def test_reset_seed_cycle_is_repeatable(
        self, test_client, db_session, clean_git_repos
    ):
        from app.models import Repo

        def shape(body: dict) -> dict:
            return {
                "repo": body["repo"]["name"],
                "pipeline": body["pipeline"]["name"],
                "cards": [(c["title"], c["status"]) for c in body["cards"]],
            }

        first = (await test_client.post("/api/test/seed")).json()
        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)
        second = (await test_client.post("/api/test/seed")).json()

        assert shape(first) == shape(second)
        # No accumulation: exactly one repo after the second cycle
        count = await db_session.scalar(select(func.count()).select_from(Repo))
        assert count == 1
