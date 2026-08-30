"""QA-5 finding 1 (BLOCKER): naive-UTC timestamps on the wire.

Backend models use ``default=datetime.utcnow`` (naive UTC) and FastAPI
serialises them with no ``Z`` and no offset::

    "created_at": "2026-08-30T10:49:27.360473"

Per the JS spec a date-TIME string with no designator is *local* time, so
``new Date(...)`` on a US-Eastern demo machine (UTC-4) reads a row created one
second ago as four hours in the FUTURE.

Observed live in the running UI on 2026-08-30:

  * AgentFileModal rendered ``Created: 8/30/2026, 10:51:13 AM`` while the
    browser's own clock read ``6:51:23 AM``.
  * The frontend's ``formatDuration(started_at, null)`` rendered ``-14396s``
    for a row created moments earlier — a NEGATIVE live duration, which is
    what a demo audience would see next to every running step.

Frontend consumers of these values:
  frontend/src/lib/pages/PipelinesPage.svelte:119-133
  frontend/src/lib/components/PipelineRunViewer.svelte:123-131
  frontend/src/lib/components/JobStatus.svelte:94-103
  frontend/src/lib/components/AgentFileModal.svelte:142,146
"""
from datetime import datetime, timedelta, timezone

import pytest

from .qa5_http import api, drop_repo, make_card, make_repo

#: A demo laptop in US Eastern Daylight Time. Any non-UTC zone reproduces it.
DEMO_BROWSER_UTC_OFFSET = timedelta(hours=-4)


@pytest.fixture
def repo():
    r = make_repo()
    yield r
    drop_repo(r["id"])


@pytest.fixture
def card(repo):
    return make_card(repo["id"])


@pytest.mark.xfail(
    strict=True,
    reason="QA finding 1 (BLOCKER): repo.created_at is serialised naive, so "
           "browsers parse it as local time",
)
def test_repo_created_at_carries_a_timezone(repo):
    raw = repo["created_at"]
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, (
        f"created_at={raw!r} has no UTC designator; a browser parses it as "
        f"local time and renders it hours off"
    )


@pytest.mark.xfail(
    strict=True,
    reason="QA finding 1 (BLOCKER): card timestamps are naive, same root cause",
)
def test_card_timestamps_carry_a_timezone(card):
    for field in ("created_at", "updated_at"):
        parsed = datetime.fromisoformat(card[field])
        assert parsed.tzinfo is not None, f"{field}={card[field]!r} is naive"


@pytest.mark.xfail(
    strict=True,
    reason="QA finding 1 (BLOCKER): a row created moments ago reads as being "
           "in the future, so every live duration in the UI renders negative",
)
def test_fresh_row_does_not_read_as_being_in_the_future(repo):
    """Reproduce exactly what the UI's formatDuration() computes.

    The frontend does ``new Date(start).getTime()`` with no timezone handling,
    so a browser at UTC-4 treats the naive UTC string as UTC-4 wall-clock —
    four hours ahead of the real instant.
    """
    raw = repo["created_at"]
    naive = datetime.fromisoformat(raw)
    as_the_browser_parses_it = naive.replace(
        tzinfo=timezone(DEMO_BROWSER_UTC_OFFSET)
    )
    elapsed = (datetime.now(timezone.utc) - as_the_browser_parses_it).total_seconds()
    assert elapsed >= 0, (
        f"UI would render a duration of {elapsed:.0f}s (negative) for a row "
        f"created moments ago; created_at={raw!r}"
    )


def test_agent_file_timestamps_have_the_same_defect():
    """The agent-file modal renders created_at with toLocaleString().

    Recorded separately because it is the surface where the wrong time is
    shown as literal text rather than folded into a duration.
    """
    status, body = api(
        "POST", "/api/agent-files",
        {"name": f"qa5-tz-{datetime.now().timestamp():.0f}", "content": "# tz probe"},
    )
    if status >= 400 or not isinstance(body, dict):
        pytest.skip(f"could not create agent file: {status} {body!r}")
    try:
        parsed = datetime.fromisoformat(body["created_at"])
        if parsed.tzinfo is not None:
            pytest.skip("timestamps are timezone-aware now; finding 1 is fixed")
        skew = abs(
            (datetime.utcnow() - parsed).total_seconds()
        )
        # Documents the *current* behaviour: the value is naive UTC. When
        # finding 1 is fixed this test skips instead of silently passing.
        assert skew < 300, (
            f"agent-file created_at={body['created_at']!r} is naive UTC; a "
            f"non-UTC browser renders it offset by its whole UTC offset"
        )
    finally:
        api("DELETE", f"/api/agent-files/{body['id']}")
