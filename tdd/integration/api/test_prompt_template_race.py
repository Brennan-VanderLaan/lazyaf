"""Concurrent creates of one prompt-template name.

QA finding T3: 20 simultaneous ``POST /api/prompt-templates`` with one name
produced **4-11 bare 500s per trial and ZERO rows** — the winner's insert died
along with the losers'. ``_ensure_template_name_unique`` is a SELECT and
``prompt_templates.name`` is UNIQUE, so between the check and the INSERT a
concurrent request can take the name and the constraint fires at COMMIT.

The fix in ``app/routers/spec.py`` absorbs that with the rollback/re-select
idiom the codebase already uses in usage and test-results ingestion: the loser
of the race gets the same 409 a sequential duplicate gets, and the winner's row
survives.

Two tests, and they prove different things — stated rather than implied:

* ``TestTheAbsorber`` forces the interleaving, so the constraint DOES fire and
  the absorber is what produces the answer.
* ``TestUnderRealConcurrency`` runs real simultaneous requests on independent
  sessions. It proves the user-visible contract (one row, one 201, no 5xx)
  without controlling which of the two paths answered.
"""
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.spec import PromptTemplate  # noqa: E402
from app.routers import spec as spec_router  # noqa: E402


async def count_named(session, name: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(PromptTemplate)
            .where(PromptTemplate.name == name)
        )
    ).scalar_one()


class TestTheAbsorber:
    """The pre-check misses, the constraint fires, the absorber answers."""

    @pytest.fixture
    def blind_first_check(self, monkeypatch):
        """Make the NEXT uniqueness pre-check see nothing.

        That is exactly the state a concurrent request creates: our SELECT ran
        before their INSERT committed. Only the first call is blinded, so the
        absorber's re-select — the thing under test — runs for real.
        """
        real = spec_router._ensure_template_name_unique
        calls = {"n": 0}

        async def blinded(db, name):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await real(db, name)

        monkeypatch.setattr(spec_router, "_ensure_template_name_unique", blinded)
        return calls

    async def test_the_loser_gets_409_not_500(
        self, client, db_session, blind_first_check
    ):
        name = f"race-{uuid4().hex[:8]}"
        db_session.add(PromptTemplate(id=str(uuid4()), name=name, content="winner"))
        await db_session.commit()

        response = await client.post(
            "/api/prompt-templates", json={"name": name, "content": "loser"}
        )

        assert response.status_code == 409, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert name in response.json()["detail"]
        assert blind_first_check["n"] == 2, (
            "the absorber's re-select never ran, so this test did not exercise "
            "the path it claims to"
        )

    async def test_the_winners_row_survives_and_is_not_duplicated(
        self, client, db_session, blind_first_check
    ):
        name = f"race-{uuid4().hex[:8]}"
        db_session.add(PromptTemplate(id=str(uuid4()), name=name, content="winner"))
        await db_session.commit()

        await client.post(
            "/api/prompt-templates", json={"name": name, "content": "loser"}
        )

        assert await count_named(db_session, name) == 1
        listed = await client.get("/api/prompt-templates")
        assert listed.status_code == 200
        survivor = next(t for t in listed.json() if t["name"] == name)
        assert survivor["content"] == "winner"

    async def test_the_next_request_still_works(
        self, client, db_session, blind_first_check
    ):
        """The T3 amplifier: an escaped exception used to cost the UI the
        NEXT request too."""
        name = f"race-{uuid4().hex[:8]}"
        db_session.add(PromptTemplate(id=str(uuid4()), name=name, content="winner"))
        await db_session.commit()

        await client.post(
            "/api/prompt-templates", json={"name": name, "content": "loser"}
        )

        follow_up = await client.get("/health")
        assert follow_up.status_code == 200

    async def test_the_name_being_freed_mid_race_still_creates(
        self, client, blind_first_check
    ):
        """Nobody took the name after all: the pre-check missed, nothing
        conflicted, and the create succeeds on the first pass."""
        name = f"race-{uuid4().hex[:8]}"
        response = await client.post(
            "/api/prompt-templates", json={"name": name, "content": "body"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["name"] == name


class TestRenameRace:
    """The same check-then-write shape one row later, on PATCH."""

    async def test_renaming_onto_a_taken_name_is_409(self, client):
        first = await client.post(
            "/api/prompt-templates", json={"name": f"a-{uuid4().hex[:8]}"}
        )
        second = await client.post(
            "/api/prompt-templates", json={"name": f"b-{uuid4().hex[:8]}"}
        )
        assert first.status_code == 201 and second.status_code == 201

        response = await client.patch(
            f"/api/prompt-templates/{second.json()['id']}",
            json={"name": first.json()["name"]},
        )
        assert response.status_code == 409, response.text

        follow_up = await client.get("/health")
        assert follow_up.status_code == 200


class TestUnderRealConcurrency:
    """Real simultaneous requests, each on its own session — the shape the QA
    pass ran, at a size a test tier can afford."""

    RACERS = 8

    @pytest.fixture
    async def racing_client(self, async_engine):
        """A client whose every request gets a FRESH session, as in production.

        The shared ``client`` fixture hands one session to every request, which
        would serialize exactly the thing under test.
        """
        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )

        async def override_get_db():
            async with factory() as session:
                yield session

        previous = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, factory
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    async def test_one_row_one_201_and_no_500s(self, racing_client):
        ac, factory = racing_client
        name = f"stampede-{uuid4().hex[:8]}"

        responses = await asyncio.gather(
            *(
                ac.post("/api/prompt-templates", json={"name": name, "content": "x"})
                for _ in range(self.RACERS)
            )
        )
        codes = [response.status_code for response in responses]

        assert not [code for code in codes if code >= 500], (
            f"{len([c for c in codes if c >= 500])} of {self.RACERS} concurrent "
            f"creates returned a server error: {codes}"
        )
        assert codes.count(201) == 1, f"expected exactly one winner, got {codes}"
        assert set(codes) == {201, 409}, f"unexpected statuses: {codes}"

        async with factory() as session:
            assert await count_named(session, name) == 1, (
                "the winner's row did not survive the stampede — the original "
                "defect wrote ZERO rows out of 20 concurrent creates"
            )

    async def test_every_loser_gets_a_readable_json_body(self, racing_client):
        ac, _ = racing_client
        name = f"stampede-{uuid4().hex[:8]}"

        responses = await asyncio.gather(
            *(
                ac.post("/api/prompt-templates", json={"name": name})
                for _ in range(self.RACERS)
            )
        )

        for response in responses:
            if response.status_code == 201:
                continue
            assert response.headers["content-type"].startswith("application/json")
            assert name in response.json()["detail"]
