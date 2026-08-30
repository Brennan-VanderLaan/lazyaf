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

FIXED at the serialization boundary (``backend/app/schemas/_datetime.py``):
every datetime the API emits now carries an explicit ``+00:00`` offset. The
xfail(strict) markers below are gone and these are plain regression guards.
"""
from datetime import datetime, timedelta, timezone

import pytest

from .qa5_http import api, drop_repo, make_card, make_repo

#: A demo laptop in US Eastern Daylight Time. Any non-UTC zone reproduces it.
DEMO_BROWSER_UTC_OFFSET = timedelta(hours=-4)
DEMO_BROWSER = timezone(DEMO_BROWSER_UTC_OFFSET)


@pytest.fixture
def repo():
    r = make_repo()
    yield r
    drop_repo(r["id"])


@pytest.fixture
def card(repo):
    return make_card(repo["id"])


def test_repo_created_at_carries_a_timezone(repo):
    raw = repo["created_at"]
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, (
        f"created_at={raw!r} has no UTC designator; a browser parses it as "
        f"local time and renders it hours off"
    )


def test_card_timestamps_carry_a_timezone(card):
    for field in ("created_at", "updated_at"):
        parsed = datetime.fromisoformat(card[field])
        assert parsed.tzinfo is not None, f"{field}={card[field]!r} is naive"


def test_fresh_row_does_not_read_as_being_in_the_future(repo):
    """Reproduce exactly what the UI's formatDuration() computes.

    The frontend does ``new Date(start).getTime()``. What that yields depends
    entirely on whether the string carries a designator: with one it is the
    real instant; WITHOUT one the browser applies its own offset, which on a
    UTC-4 demo laptop put every fresh row four hours in the future and printed
    ``-14399s`` next to every running step.

    So the assertion is made from the demo browser's chair — ``DEMO_BROWSER``
    is the zone the QA pass measured on — and it is the naive case that fails
    it.
    """
    raw = repo["created_at"]
    as_the_browser_parses_it = datetime.fromisoformat(raw)
    assert as_the_browser_parses_it.tzinfo is not None, (
        f"created_at={raw!r} has no designator, so a browser at "
        f"{DEMO_BROWSER_UTC_OFFSET} reads it as local time and every duration "
        f"computed from it is off by that offset"
    )
    elapsed = (
        datetime.now(DEMO_BROWSER) - as_the_browser_parses_it
    ).total_seconds()
    assert elapsed >= 0, (
        f"UI would render a duration of {elapsed:.0f}s (negative) for a row "
        f"created moments ago; created_at={raw!r}"
    )


def test_agent_file_timestamps_carry_a_timezone():
    """The agent-file modal renders created_at with toLocaleString().

    Recorded separately because it is the surface where the wrong time is
    shown as literal text rather than folded into a duration.

    This used to document the defect and SKIP once it was fixed; it now
    asserts the fixed contract, so the surface stays covered instead of going
    quiet.
    """
    status, body = api(
        "POST", "/api/agent-files",
        {"name": f"qa5-tz-{datetime.now().timestamp():.0f}", "content": "# tz probe"},
    )
    assert status == 201 and isinstance(body, dict), (
        f"could not create agent file: {status} {body!r}"
    )
    try:
        parsed = datetime.fromisoformat(body["created_at"])
        assert parsed.tzinfo is not None, (
            f"agent-file created_at={body['created_at']!r} is naive; a non-UTC "
            f"browser renders it offset by its whole UTC offset"
        )
        skew = abs((datetime.now(timezone.utc) - parsed).total_seconds())
        assert skew < 300, (
            f"agent-file created_at={body['created_at']!r} is {skew:.0f}s away "
            f"from now; the instant itself is wrong, not just its label"
        )
    finally:
        api("DELETE", f"/api/agent-files/{body['id']}")
