"""
QA-6 — backend-side regressions behind the first-run / demo-polish findings.

These talk HTTP to a running QA stack (never the dev stack on :8000 or the e2e
lane on :8765):

    docker compose -p lazyaf-qa -f docker-compose.qa.yml up -d
    LAZYAF_QA_URL=http://localhost:8790 pytest tdd/qa/test_demo_polish_api.py

Base URL comes from $LAZYAF_QA_URL and defaults to http://localhost:8790.

Tests marked `xfail(strict=True)` encode a CONFIRMED defect: they fail today on
purpose, and pytest turns them into a FAILURE the moment the defect is fixed,
so nobody quietly closes the finding without deleting the marker.

Every row these create is prefixed `qa6-` so a shared stack stays greppable.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE_URL = os.environ.get("LAZYAF_QA_URL", "http://localhost:8790").rstrip("/")
TIMEOUT = 30

#: An ISO-8601 instant that carries its zone: trailing Z, or +HH:MM / -HH:MM.
#: This is the whole finding in one regex — a timestamp without one of these is
#: ambiguous the moment it leaves the process that made it.
TZ_AWARE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _name(kind: str) -> str:
    return f"qa6-{kind}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def require_stack():
    """Fail loudly with the startup command rather than erroring 30 times."""
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure means "no stack"
        pytest.fail(
            f"No QA stack at {BASE_URL} ({exc}).\n"
            "Start it with:\n"
            "  docker compose -p lazyaf-qa -f docker-compose.qa.yml up -d"
        )


@pytest.fixture
def repo():
    """A plain (un-ingested) repo. Enough for every schema-level assertion."""
    resp = requests.post(
        f"{BASE_URL}/api/repos", json={"name": _name("repo")}, timeout=TIMEOUT
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# FINDING 1 (BLOCKER) — timestamps go out over the wire with no timezone.
# ---------------------------------------------------------------------------


def test_repo_created_at_carries_a_timezone(repo):
    assert TZ_AWARE.search(repo["created_at"]), (
        f"created_at={repo['created_at']!r} has no timezone designator; "
        "a JS client will read it as local time"
    )


def test_pipeline_timestamps_carry_a_timezone(repo):
    resp = requests.post(
        f"{BASE_URL}/api/repos/{repo['id']}/pipelines",
        json={
            "name": _name("pipe"),
            "steps": [{"name": "s", "type": "script", "config": {"command": "true"}}],
        },
        timeout=TIMEOUT,
    )
    assert resp.status_code == 201, resp.text
    pipeline = resp.json()
    for field in ("created_at", "updated_at"):
        assert TZ_AWARE.search(pipeline[field]), f"{field}={pipeline[field]!r} is naive"


def test_timestamp_is_the_right_instant_not_just_a_relabelled_one():
    """
    Pin down WHICH way the bug pointed, so a "fix" that merely relabels local
    wall-clock time as UTC cannot slip through.

    The stored value is UTC, so the emitted instant must land within seconds of
    `now(timezone.utc)`. A relabelled local time would be off by the host's
    whole UTC offset and fail here even though it carries a designator.
    """
    before = datetime.now(timezone.utc)
    resp = requests.post(
        f"{BASE_URL}/api/repos", json={"name": _name("tz")}, timeout=TIMEOUT
    )
    assert resp.status_code == 201, resp.text
    raw = resp.json()["created_at"]

    created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert created.tzinfo is not None, f"created_at={raw!r} is still naive"

    drift = abs((created - before).total_seconds())
    assert drift < 120, f"created_at={raw!r} is {drift}s away from now(UTC)"


# ---------------------------------------------------------------------------
# FINDING 3 (MAJOR) — no length bound on user-supplied names.
# FINDING 6 (MAJOR) — blank / whitespace-only names accepted.
#
# BOTH FIXED at the schema (``backend/app/schemas/_strings.py``): the ``Name``
# alias — ``strip_whitespace=True, min_length=1, max_length=200`` — is applied
# to the ``*Create``/``*Update`` INPUT schemas of repos, pipelines and the rest,
# so a blank, whitespace-only or 5000-character name is a 422 naming
# ``body.name`` instead of a row that later wrecks a card header or renders as
# an invisible sidebar entry. The ``*Read`` schemas keep a bare ``str`` on
# purpose, so rows written before the bound still serialize.
#
# The xfail(strict) markers below are gone; these three are regression guards.
# ---------------------------------------------------------------------------


def test_pipeline_name_is_length_bounded(repo):
    resp = requests.post(
        f"{BASE_URL}/api/repos/{repo['id']}/pipelines",
        json={
            "name": "Q" * 5000,
            "description": "D" * 5000,
            "steps": [{"name": "s", "type": "script", "config": {"command": "true"}}],
        },
        timeout=TIMEOUT,
    )
    assert resp.status_code == 422, (
        f"a 5000-character pipeline name was accepted with {resp.status_code}"
    )


def test_repo_name_cannot_be_empty():
    resp = requests.post(f"{BASE_URL}/api/repos", json={"name": ""}, timeout=TIMEOUT)
    assert resp.status_code == 422, (
        f"an empty repo name was accepted with {resp.status_code}"
    )


def test_repo_name_cannot_be_whitespace_only():
    resp = requests.post(
        f"{BASE_URL}/api/repos", json={"name": "   \t  "}, timeout=TIMEOUT
    )
    assert resp.status_code == 422, (
        f"a whitespace-only repo name was accepted with {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Guard rails — verified-correct behaviour that must not regress.
# ---------------------------------------------------------------------------


def test_zero_step_pipeline_cannot_be_run(repo):
    """
    The frontend divides by `steps_total` to size the progress bar
    (PipelineRunViewer.svelte:154), so a run with steps_total == 0 would render
    `width: NaN%`. The backend refuses to create one — keep it that way.
    """
    ingested = requests.post(
        f"{BASE_URL}/api/repos/ingest", json={"name": _name("zero")}, timeout=TIMEOUT
    )
    assert ingested.status_code == 201, ingested.text
    repo_id = ingested.json()["id"]

    created = requests.post(
        f"{BASE_URL}/api/repos/{repo_id}/pipelines",
        json={"name": _name("zero-steps"), "steps": []},
        timeout=TIMEOUT,
    )
    assert created.status_code == 201, created.text

    run = requests.post(
        f"{BASE_URL}/api/pipelines/{created.json()['id']}/run", json={}, timeout=TIMEOUT
    )
    assert run.status_code == 400, f"expected a refusal, got {run.status_code}"
    assert "no steps" in run.text.lower()


def test_error_bodies_do_not_leak_internals():
    """A 404 must be a sentence, not a traceback or a SQL statement."""
    for path in (
        "/api/repos/not-a-uuid",
        "/api/pipeline-runs/00000000-0000-0000-0000-000000000000",
        "/api/pipelines/00000000-0000-0000-0000-000000000000",
    ):
        resp = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
        body = resp.text
        for leak in ("Traceback", "sqlalchemy", "SELECT ", "/app/", "site-packages"):
            assert leak not in body, f"{path} leaked {leak!r}: {body[:300]}"


def test_malformed_json_is_a_422_not_a_500(repo):
    resp = requests.post(
        f"{BASE_URL}/api/repos/{repo['id']}/cards",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 422, resp.text
    assert "Traceback" not in resp.text


def test_duplicate_prompt_template_name_is_409_not_500():
    """
    Regression pin: this returned a bare 500 (`sqlite3.IntegrityError: UNIQUE
    constraint failed`) earlier in the QA window and now returns a clean 409.
    """
    name = _name("tmpl")
    first = requests.post(
        f"{BASE_URL}/api/prompt-templates", json={"name": name}, timeout=TIMEOUT
    )
    assert first.status_code == 201, first.text

    second = requests.post(
        f"{BASE_URL}/api/prompt-templates", json={"name": name}, timeout=TIMEOUT
    )
    assert second.status_code == 409, (
        f"duplicate name should be 409, got {second.status_code}: {second.text[:300]}"
    )
    assert "Traceback" not in second.text


def test_pipeline_run_list_rejects_a_nonsense_limit():
    for limit in ("-5", "0", "abc", "999999999999999999999"):
        resp = requests.get(
            f"{BASE_URL}/api/pipeline-runs", params={"limit": limit}, timeout=TIMEOUT
        )
        assert resp.status_code in (200, 422), (
            f"limit={limit} -> {resp.status_code}: {resp.text[:200]}"
        )
        assert "Traceback" not in resp.text
