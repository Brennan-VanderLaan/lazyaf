"""The error boundary: no bare 500s, and no dropped connections.

QA finding T3 (BLOCKER). Every 500 the adversarial pass produced was one
uncaught exception escaping the ASGI app, and each cost the UI TWO requests:

  * the response was the literal bytes ``Internal Server Error`` with
    ``content-type: text/plain``, so a frontend doing ``res.json()`` on the
    error path threw a second time and showed "Unknown error"; and
  * uvicorn then closed the transport, so the app's NEXT request died with
    ``RemoteDisconnected`` (measured 6/6, against 0/6 on a 200 control).

``app/main.py`` now registers handlers for ``IntegrityError``,
``StaleDataError`` and ``SQLAlchemyError``, a ``RequestValidationError``
handler that can always render its own body, and an ASGI-level last resort.

HOW THE KEEP-ALIVE CLAIM IS TESTED HERE. httpx's ``ASGITransport`` runs with
``raise_app_exceptions=True``: an exception that escapes the application
surfaces in the test as a raise, not as a response. That escape IS the thing
that closed the socket in production. So "the call returns a response at all"
is the in-process form of "the connection survived" — and every test below
then issues a FOLLOW-UP request on the same client to show the session is
still usable.
"""
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm.exc import StaleDataError

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentFile, Card  # noqa: E402

PROBE_PREFIX = "/__error_boundary_probe__"


@pytest.fixture
def probes():
    """Mount routes that raise the EXACT exceptions the QA pass hit.

    On the real application object, through the real middleware stack, with a
    real database session — so what is under test is the deployed boundary,
    not a re-creation of it. Removed again on teardown.
    """
    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    @app.get(f"{PROBE_PREFIX}/unique")
    async def _unique(db: AsyncSession = Depends(get_db)):
        name = f"probe-{uuid4().hex[:8]}"
        db.add(AgentFile(id=str(uuid4()), name=name, content="a"))
        db.add(AgentFile(id=str(uuid4()), name=name, content="b"))
        await db.commit()

    @app.get(f"{PROBE_PREFIX}/not-null")
    async def _not_null(db: AsyncSession = Depends(get_db)):
        db.add(Card(id=str(uuid4()), repo_id=str(uuid4()), title=None))
        await db.commit()

    @app.get(f"{PROBE_PREFIX}/stale")
    async def _stale():
        raise StaleDataError("expected to update 1 row(s); 0 were matched")

    @app.get(f"{PROBE_PREFIX}/locked")
    async def _locked():
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    @app.get(f"{PROBE_PREFIX}/pool")
    async def _pool():
        raise SATimeoutError("QueuePool limit of size 5 overflow 10 reached")

    @app.get(f"{PROBE_PREFIX}/boom")
    async def _boom():
        raise RuntimeError("a programming error, not a database one")

    yield PROBE_PREFIX

    app.router.routes = [
        route
        for route in app.router.routes
        if not getattr(route, "path", "").startswith(PROBE_PREFIX)
    ]
    app.openapi_schema = None


async def assert_session_still_usable(client):
    """The amplifier half of T3: one crash must not cost the NEXT request."""
    follow_up = await client.get("/health")
    assert follow_up.status_code == 200, (
        "the request after the error failed — this is the keep-alive kill that "
        "turned one server-side error into two broken UI calls"
    )


def assert_structured_json(response, *, status: int, error: str):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json"), (
        f"error body is {response.headers['content-type']!r}, not JSON; a "
        f"frontend doing res.json() on the error path throws a second time"
    )
    body = response.json()
    assert body["error"] == error, body
    assert isinstance(body.get("detail"), str) and body["detail"], body
    assert "Traceback" not in body["detail"], body


class TestIntegrityErrors:
    async def test_unique_violation_is_409_json(self, client, probes):
        response = await client.get(f"{probes}/unique")
        assert_structured_json(response, status=409, error="unique_violation")
        assert response.json()["field"] == "name"
        await assert_session_still_usable(client)

    async def test_not_null_violation_is_422_json_naming_the_column(
        self, client, probes
    ):
        response = await client.get(f"{probes}/not-null")
        assert_structured_json(response, status=422, error="not_null_violation")
        assert response.json()["field"] == "title"
        assert "title" in response.json()["detail"]
        await assert_session_still_usable(client)


class TestOtherDatabaseErrors:
    async def test_stale_data_is_409(self, client, probes):
        """`start` racing `delete` on one card: 3 of 6 trials in the QA pass."""
        response = await client.get(f"{probes}/stale")
        assert_structured_json(response, status=409, error="concurrent_modification")
        await assert_session_still_usable(client)

    async def test_database_locked_is_a_retryable_503(self, client, probes):
        response = await client.get(f"{probes}/locked")
        assert_structured_json(response, status=503, error="database_unavailable")
        assert response.headers["retry-after"] == "1"
        await assert_session_still_usable(client)

    async def test_pool_exhaustion_is_a_retryable_503(self, client, probes):
        """40 concurrent GET /api/pipeline-runs produced this 10 times."""
        response = await client.get(f"{probes}/pool")
        assert_structured_json(response, status=503, error="database_unavailable")
        await assert_session_still_usable(client)


class TestLastResort:
    async def test_a_programming_error_is_a_json_500_not_an_escape(
        self, client, probes
    ):
        """The single highest-leverage line in the triage: an exception that is
        nobody's constraint violation still must not escape the app."""
        response = await client.get(f"{probes}/boom")
        assert_structured_json(response, status=500, error="internal_error")
        await assert_session_still_usable(client)

    async def test_the_error_body_is_readable_cross_origin(self, client, probes):
        """CORS must still apply to the error response.

        An error response produced OUTSIDE CORSMiddleware carries no
        ``Access-Control-Allow-Origin`` and the browser refuses to let the app
        read it — reproducing the "Unknown error" symptom by a different route.
        This pins the middleware ORDER, which is easy to break by adding the
        boundary in the wrong place.
        """
        response = await client.get(
            f"{probes}/boom", headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 500
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://localhost:5173"
        )

    async def test_no_stack_trace_reaches_the_client(self, client, probes):
        response = await client.get(f"{probes}/boom")
        assert "a programming error" not in response.text, (
            "the exception's own message leaked into the response body"
        )
        assert "app/main.py" not in response.text
        assert "Traceback" not in response.text


class TestNonFiniteFloats:
    """``NaN`` / ``Infinity`` in a request body 500'd on EVERY endpoint.

    Python's json parser accepts those literals, Starlette's JSONResponse
    renders with ``allow_nan=False``, and FastAPI's own 422 echoes the offending
    input — so the validation error could not be serialized and became a 500.
    """

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    async def test_the_422_can_actually_be_rendered(self, client, literal):
        response = await client.post(
            "/api/repos",
            content=f'{{"name": "tz", "default_branch": {literal}}}',
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert isinstance(response.json()["detail"], list)
        await assert_session_still_usable(client)


class TestPatchWithExplicitNull:
    """End to end, the exact request that started the QA pass:
    ``s.patch(f"{B}/api/cards/{C}", json={"title": None})`` -> 500, and the very
    next request on that session raised ``RemoteDisconnected``."""

    @pytest.fixture
    async def card(self, client):
        repo = await client.post(
            "/api/repos", json={"name": f"nullpatch-{uuid4().hex[:8]}"}
        )
        assert repo.status_code == 201, repo.text
        card = await client.post(
            f"/api/repos/{repo.json()['id']}/cards", json={"title": "null probe"}
        )
        assert card.status_code == 201, card.text
        return card.json()

    @pytest.mark.parametrize(
        "field", ["title", "description", "status", "runner_type"]
    )
    async def test_null_on_a_required_field_is_a_422_naming_it(
        self, client, card, field
    ):
        response = await client.patch(
            f"/api/cards/{card['id']}", json={field: None}
        )
        assert response.status_code == 422, response.text
        locations = [error["loc"] for error in response.json()["detail"]]
        assert ["body", field] in locations, response.json()
        await assert_session_still_usable(client)

    async def test_the_card_is_left_untouched(self, client, card):
        await client.patch(f"/api/cards/{card['id']}", json={"title": None})
        after = await client.get(f"/api/cards/{card['id']}")
        assert after.status_code == 200
        assert after.json()["title"] == "null probe"

    async def test_a_normal_patch_still_works(self, client, card):
        """The guard must not have made PATCH stricter than it was."""
        response = await client.patch(
            f"/api/cards/{card['id']}", json={"title": "renamed"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "renamed"

    async def test_null_still_clears_a_nullable_link(self, client, card):
        """``feature_id`` IS nullable — explicit null is how the UI unlinks a
        card from its feature, and that must keep working."""
        response = await client.patch(
            f"/api/cards/{card['id']}", json={"feature_id": None}
        )
        assert response.status_code == 200, response.text
        assert response.json()["feature_id"] is None
