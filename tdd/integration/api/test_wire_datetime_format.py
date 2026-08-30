"""Every router's datetimes, on the actual wire.

QA finding T1 (BLOCKER). ``tdd/unit/schemas/test_utc_wire_format.py`` proves
the annotation is right; this file proves the BYTES are right, one
representative endpoint per router that emits a datetime, because the schema
is not the only way a timestamp reaches a client - a router that hand-builds
its response dict bypasses the schema entirely, and one of them still does
(see ``TestKnownGaps``).

The assertion is the arithmetic the UI does: parse what came back and check
that a row created moments ago is not in the future. On a naive string that
is exactly what fails, and it is what put ``-14399s`` next to every running
step.

WIRE FORMAT (pinned here and in app/schemas/_datetime.py):

    "created_at": "2026-08-30T12:06:32.695487+00:00"
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Card, Job, Repo  # noqa: E402
from app.models.runner import Runner  # noqa: E402
from app.models.testref import TestRef  # noqa: E402

#: A demo laptop in US Eastern Daylight Time. Any non-UTC zone reproduces the
#: original defect; this one is the zone the QA pass measured it on.
DEMO_BROWSER = timezone(timedelta(hours=-4))


def assert_utc_wire(value, where: str):
    """One timestamp, checked the way the browser reads it."""
    assert isinstance(value, str), f"{where}: expected a string, got {value!r}"
    assert value.endswith("+00:00"), (
        f"{where}={value!r} carries no UTC offset. A browser parses a "
        f"designator-less date-time as LOCAL time (ECMA-262), so this renders "
        f"hours off and every live duration computed from it goes negative."
    )
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0), f"{where}={value!r} is not UTC"


def assert_not_in_the_future(value, where: str):
    """The `-14399s` assertion, from the demo laptop's point of view."""
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    assert elapsed >= 0, (
        f"{where}={value!r} reads {elapsed:.0f}s in the FUTURE; the UI would "
        f"render that as a negative duration"
    )
    assert elapsed < 3600, f"{where}={value!r} reads {elapsed:.0f}s in the past"


def assert_timestamps(body: dict, *fields: str, label: str):
    assert fields, "a case with no fields to check would be a vacuous pass"
    for field in fields:
        value = body.get(field)
        assert value is not None, f"{label}: {field} missing from {sorted(body)}"
        assert_utc_wire(value, f"{label}.{field}")
        assert_not_in_the_future(value, f"{label}.{field}")


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
async def repo(client):
    response = await client.post(
        "/api/repos", json={"name": f"tz-{uuid4().hex[:8]}", "default_branch": "main"}
    )
    assert response.status_code == 201, response.text
    return response.json()


# -----------------------------------------------------------------------------
# One representative endpoint per router
# -----------------------------------------------------------------------------

class TestEveryRouterEmitsUtc:
    """Named per router so a regression says which surface broke."""

    async def test_repos(self, client):
        response = await client.post(
            "/api/repos", json={"name": f"tz-{uuid4().hex[:8]}"}
        )
        assert response.status_code == 201, response.text
        assert_timestamps(response.json(), "created_at", label="POST /api/repos")

        listed = await client.get("/api/repos")
        assert listed.status_code == 200
        assert_timestamps(listed.json()[0], "created_at", label="GET /api/repos")

    async def test_cards(self, client, repo):
        response = await client.post(
            f"/api/repos/{repo['id']}/cards", json={"title": "tz card"}
        )
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(),
            "created_at",
            "updated_at",
            label="POST /api/repos/{id}/cards",
        )

    async def test_jobs(self, client, db_session, repo):
        card = Card(id=str(uuid4()), repo_id=repo["id"], title="tz job card")
        job = Job(
            id=str(uuid4()),
            card_id=card.id,
            status="running",
            started_at=datetime.utcnow(),
        )
        db_session.add_all([card, job])
        await db_session.commit()

        response = await client.get(f"/api/jobs/{job.id}")
        assert response.status_code == 200, response.text
        assert_timestamps(
            response.json(), "created_at", "started_at", label="GET /api/jobs/{id}"
        )

    async def test_agent_files(self, client):
        response = await client.post(
            "/api/agent-files",
            json={"name": f"tz-{uuid4().hex[:8]}", "content": "# tz probe"},
        )
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(),
            "created_at",
            "updated_at",
            label="POST /api/agent-files",
        )

    async def test_pipelines(self, client, repo):
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={
                "name": "tz pipeline",
                "steps": [
                    {"name": "one", "type": "script", "config": {"command": "true"}}
                ],
            },
        )
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(),
            "created_at",
            "updated_at",
            label="POST /api/repos/{id}/pipelines",
        )

    async def test_spec_features(self, client):
        response = await client.post("/api/features", json={"title": "tz feature"})
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(), "created_at", "updated_at", label="POST /api/features"
        )

    async def test_spec_prompt_templates(self, client):
        response = await client.post(
            "/api/prompt-templates", json={"name": f"tz-{uuid4().hex[:8]}"}
        )
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(),
            "created_at",
            "updated_at",
            label="POST /api/prompt-templates",
        )

    async def test_test_results(self, client, db_session, repo):
        ref = TestRef(
            id=str(uuid4()),
            lazyaf_test_id=f"tz-{uuid4().hex[:8]}",
            repo_id=repo["id"],
        )
        db_session.add(ref)
        await db_session.commit()

        response = await client.get("/api/test-refs", params={"repo_id": repo["id"]})
        assert response.status_code == 200, response.text
        assert_timestamps(
            response.json()[0],
            "created_at",
            "updated_at",
            label="GET /api/test-refs",
        )

    async def test_experiments(self, client, db_session, repo):
        card = Card(id=str(uuid4()), repo_id=repo["id"], title="tz experiment card")
        db_session.add(card)
        await db_session.commit()

        response = await client.post(
            "/api/experiments",
            json={
                "name": "tz experiment",
                "target_type": "card",
                "target_id": card.id,
                "matrix": {
                    "models": [{"agent": "mock", "model": "mock-a", "label": "a"}],
                    "prompts": [{"prompt_template_id": None, "label": "default"}],
                    "repeat": 1,
                },
                "budget_usd": "5.00",
                "max_concurrency": 1,
            },
        )
        assert response.status_code == 201, response.text
        assert_timestamps(
            response.json(),
            "created_at",
            "updated_at",
            label="POST /api/experiments",
        )


class TestKnownGaps:
    """Surfaces that hand-build a payload instead of going through a
    response_model, so the field annotation cannot reach them and they have to
    call ``utc_isoformat`` themselves. Kept as their own class because that is
    the class of bug: one wire contract, two halves, only one of them typed."""

    async def test_runners_snapshot_emits_utc(self, client, db_session):
        db_session.add(
            Runner(
                id=str(uuid4()),
                name="tz-runner",
                status="idle",
                connected_at=datetime.utcnow(),
                last_heartbeat=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
        )
        await db_session.commit()

        response = await client.get("/api/runners")
        assert response.status_code == 200, response.text
        row = next(r for r in response.json() if r["name"] == "tz-runner")
        assert_timestamps(
            row,
            "connected_at",
            "last_heartbeat",
            "created_at",
            label="GET /api/runners",
        )


class TestTheBrowserArithmetic:
    """The symptom, reproduced end to end: what the UI computes from a real
    response for a row it just created."""

    async def test_a_row_created_now_yields_a_non_negative_duration(self, client):
        response = await client.post(
            "/api/repos", json={"name": f"tz-{uuid4().hex[:8]}"}
        )
        created_at = response.json()["created_at"]

        # `new Date(created_at).getTime()` in a UTC-4 browser. With a naive
        # string this lands four hours ahead of the real instant.
        as_the_browser_reads_it = datetime.fromisoformat(created_at)
        seconds = (
            datetime.now(DEMO_BROWSER) - as_the_browser_reads_it
        ).total_seconds()

        assert seconds >= 0, (
            f"formatDuration() would render {seconds:.0f}s for a row created "
            f"this instant (created_at={created_at!r})"
        )
