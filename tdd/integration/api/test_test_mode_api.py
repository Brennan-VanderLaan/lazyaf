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
from datetime import datetime
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
async def _restore_singletons():
    """These tests exercise the real module-level singletons; leave them
    empty for the rest of the suite no matter how a test exits."""
    yield
    from app.services.execution.runner_dispatcher import runner_dispatcher
    from app.services.execution.runner_registry import runner_registry
    from app.services.playground_service import playground_service
    from app.services.trigger_service import trigger_deduplicator
    from app.services.websocket import manager

    await runner_registry.reset()
    await runner_dispatcher.reset()
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

    async def test_reset_names_the_runner_singletons_it_wiped(self, test_client):
        """R6: the reset endpoint must reset in-memory singletons.

        12.6 replaced the polling pool and its queue with the registry and the
        dispatcher, and BOTH have to be in `memory_reset` - the registry holds
        live sockets, the dispatcher holds waiters, in-flight assignments and
        a loop-bound wake event. A reset that wiped the DB and left either
        behind would hand the next test an assignment for a step row that no
        longer exists.

        The response's `memory_reset` derives from the resettable REGISTRY,
        not from a hand-maintained list, so this also pins that the two new
        singletons actually registered themselves.
        """
        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        reset = response.json()["memory_reset"]
        assert "runner_registry" in reset, reset
        assert "runner_dispatcher" in reset, reset
        # And the names they replaced are gone with their subsystems.
        assert "runner_pool" not in reset, reset
        assert "job_queue" not in reset, reset
        # M14: the capability probe holds one asyncio.Lock per endpoint id.
        # Those locks are loop-bound and keyed by rows the DB reset deletes -
        # the same failure_01 shape - so they register as a resettable too.
        assert "model_endpoint_probes" in reset, reset

    async def test_reset_closes_websockets(self, test_client):
        from app.services.websocket import manager

        stub = _StubWebSocket()
        manager.active_connections.append(stub)

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        assert manager.active_connections == []
        assert stub.closed is True

    async def test_reset_drops_a_registered_runner(self, test_client, db_session):
        """A runner row and its in-memory connection both go.

        The row is deleted with the rest of the DB; the registry's socket
        table is wiped by its own reset hook. Asserting only one of the two
        is how a reset leaves a ghost the next test can dispatch at.
        """
        from app.models import Runner
        from app.services.execution.runner_registry import runner_registry

        db_session.add(
            Runner(
                id="runner-doomed",
                name="doomed",
                runner_type="generic",
                status="idle",
                last_heartbeat=datetime.utcnow(),
            )
        )
        await db_session.commit()

        response = await test_client.post("/api/test/reset")
        assert_status_code(response, 200)

        assert await db_session.scalar(
            select(func.count()).select_from(Runner)
        ) == 0
        assert not runner_registry.is_connected("runner-doomed")

    async def test_reset_clears_playground_sessions(self, test_client):
        from app.services.playground_service import playground_service

        # The subject here is reset(), not the start path: since 12.5
        # start_test dispatches a real ad-hoc agent run and needs a db session
        # plus an ingested repo (that path is covered by
        # tdd/integration/api/test_playground_control_mode.py). A live session
        # object is all reset() needs to be exercised.
        from uuid import uuid4

        from app.services.playground_service import PlaygroundSession

        session = PlaygroundSession(
            id=str(uuid4()),
            repo_id="repo-doomed",
            branch="main",
            runner_type="any",
            status="running",
        )
        session_id = session.id
        playground_service._sessions[session_id] = session
        assert playground_service.get_session(session_id) is session

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
        # M14 added `model_endpoints`: the e2e lane gets the same two mock
        # OpenAI endpoints the dogfood pipeline uses, so a self-hosted step can
        # be exercised with NO GPU. The key set is pinned rather than merely
        # checked for the ones we care about, because a seed response that
        # quietly grows a field is a seed response consumers cannot rely on.
        assert set(body.keys()) == {
            "success", "repo", "pipeline", "cards", "model_endpoints",
        }
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

    async def test_seeded_review_card_branch_actually_exists(
        self, test_client, clean_git_repos
    ):
        """The in-review card's branch is a real ref, not just a string.

        It used to be only a string: the card row claimed
        'lazyaf/seed-review' and nothing ever created that ref, so the first
        thing a demo does on the seeded board - press Approve - merged a
        branch that did not exist and came back a red toast (T16).
        """
        from dulwich.repo import Repo as DulwichRepo

        from app.routers.test_api import SEED_REVIEW_BRANCH

        body = (await test_client.post("/api/test/seed")).json()
        git = DulwichRepo(str(clean_git_repos.get_repo_path(body["repo"]["id"])))

        review_ref = f"refs/heads/{SEED_REVIEW_BRANCH}".encode()
        assert review_ref in git.refs, (
            f"seed created no {SEED_REVIEW_BRANCH} ref; refs are "
            f"{sorted(git.refs.keys())}"
        )

        # And it is a DESCENDANT of the default branch, so the merge is real
        # work landing rather than a no-op fast-forward to the same commit.
        default_sha = git.refs[b"refs/heads/main"]
        review_sha = git.refs[review_ref]
        assert review_sha != default_sha
        assert git[review_sha].parents == [default_sha]

    async def test_seeded_review_card_can_be_approved(
        self, test_client, client, clean_git_repos
    ):
        """The demo's first Approve merges for real and moves the card to done."""
        body = (await test_client.post("/api/test/seed")).json()
        card = next(c for c in body["cards"] if c["status"] == "in_review")

        response = await client.post(f"/api/cards/{card['id']}/approve")

        assert_status_code(response, 200)
        assert response.json()["card"]["status"] == "done"

    async def test_seed_review_branch_sha_is_deterministic(
        self, test_client, clean_git_repos
    ):
        """Same fixed author/timestamps rule as the seed commit."""
        from dulwich.repo import Repo as DulwichRepo

        from app.routers.test_api import SEED_REVIEW_BRANCH

        ref = f"refs/heads/{SEED_REVIEW_BRANCH}".encode()

        def review_sha(body: dict) -> bytes:
            path = clean_git_repos.get_repo_path(body["repo"]["id"])
            return DulwichRepo(str(path)).refs[ref]

        first = review_sha((await test_client.post("/api/test/seed")).json())
        await test_client.post("/api/test/reset")
        second = review_sha((await test_client.post("/api/test/seed")).json())

        assert second == first

    async def test_seeding_twice_without_a_reset_still_succeeds(
        self, test_client, clean_git_repos
    ):
        """/seed is idempotent, not one-shot.

        Every other seeded row is keyed by uuid4(), but ModelEndpoint carries a
        UNIQUE index on name - so a second /seed used to write the repo, the
        pipeline and both cards successfully and then explode on that one
        table. Worse, the rescue that exists so endpoint seeding 'never fails
        a seed' called rollback(), which EXPIRED the rows already committed
        above, so building the response lazy-loaded repo.id outside a greenlet
        and raised MissingGreenlet. The handler whose job was to swallow the
        failure was the thing returning 500.
        """
        first = await test_client.post("/api/test/seed")
        assert_status_code(first, 200)

        second = await test_client.post("/api/test/seed")
        assert_status_code(second, 200)
        assert second.json()["success"] is True
        # The response is built from values read BEFORE endpoint seeding, so it
        # survives even if that half rolls back.
        assert second.json()["repo"]["id"]

    async def test_seeding_twice_does_not_duplicate_model_endpoints(
        self, test_client, db_session, clean_git_repos
    ):
        from sqlalchemy import func, select as sa_select

        from app.models.model_endpoint import ModelEndpoint

        await test_client.post("/api/test/seed")
        after_one = await db_session.scalar(
            sa_select(func.count()).select_from(ModelEndpoint)
        )
        await test_client.post("/api/test/seed")
        after_two = await db_session.scalar(
            sa_select(func.count()).select_from(ModelEndpoint)
        )

        assert after_one > 0, "seed created no model endpoints at all"
        assert after_two == after_one

    async def test_seeded_cards_use_a_step_type_that_can_actually_run(
        self, test_client, clean_git_repos
    ):
        """A seeded card whose only outcome is a red toast is not a fixture.

        12.4 removed docker and script execution from the runners, and
        create_card refuses those types - but these rows are written through
        the ORM, which bypasses that refusal. So the seed has to hold the rule
        itself or it hands a demo a broken button.
        """
        from app.routers.cards import DEPRECATED_CARD_STEP_TYPES
        from app.models import Card

        body = (await test_client.post("/api/test/seed")).json()
        for seeded in body["cards"]:
            card = await test_client.get(f"/api/cards/{seeded['id']}")
            if card.status_code != 200:
                continue
            assert card.json()["step_type"] not in DEPRECATED_CARD_STEP_TYPES, (
                f"seeded card {seeded['title']!r} uses step type "
                f"{card.json()['step_type']!r}, which Start refuses"
            )

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
