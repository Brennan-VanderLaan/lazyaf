"""QA-3: duplicate side effects from TOCTOU guards on card actions.

``POST /api/cards/{id}/start`` guards with a plain read-then-check:

    card = (await db.execute(select(Card)...)).scalar_one_or_none()
    if card.status != "todo":
        raise HTTPException(400, ...)
    ...                                  # several awaits
    card.status = "in_progress"
    await db.commit()

There is no ``with_for_update`` and no unique constraint behind it, so every
request released in the same instant reads ``todo``, passes the check, and
commits its own Job + ad-hoc PipelineRun. ``retry`` has the same shape.

Two clients pressing Start, or one client whose request was retried, produce
two agent runs on two branches for one card - and the card keeps only the
LAST branch name, so the other branches are orphaned.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa3_support import (  # noqa: E402
    api,
    ensure_repo,
    fire_together,
    make_card,
    require_stack,
    status_counts,
)


def _runs_for_card(card_id: str) -> list[dict]:
    status, body = api("GET", "/api/pipeline-runs?limit=100")
    if status != 200 or not isinstance(body, list):
        return []
    return [run for run in body if run.get("trigger_ref") == card_id]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-3 (BLOCKER): concurrent POST /api/cards/{id}/start "
        "all pass the `status != todo` check (no row lock), so N simultaneous "
        "requests create N Jobs and N PipelineRuns for one card. "
        "backend/app/routers/cards.py:296-354"
    ),
)
def test_simultaneous_card_start_creates_exactly_one_run():
    require_stack()
    repo_id = ensure_repo()
    card_id = make_card(repo_id, f"qa3-start-{uuid.uuid4().hex[:6]}")

    results = fire_together(5, lambda _i: api("POST", f"/api/cards/{card_id}/start"))
    counts = status_counts(results)
    accepted = counts.get(200, 0)

    time.sleep(3)
    runs = _runs_for_card(card_id)

    assert accepted == 1, (
        f"expected exactly one start to win, got {accepted} of 5 accepted "
        f"(codes {counts})"
    )
    assert len(runs) == 1, (
        f"expected exactly one pipeline run for the card, found {len(runs)}: "
        f"{[r['id'] for r in runs]}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-4 (MAJOR): POST /api/cards/{id}/retry has the same "
        "unguarded read-then-check as start; 10 simultaneous retries were "
        "all accepted and produced 10 extra runs for one card. "
        "backend/app/routers/cards.py:559"
    ),
)
def test_simultaneous_card_retry_creates_exactly_one_run():
    require_stack()
    repo_id = ensure_repo()
    card_id = make_card(repo_id, f"qa3-retry-{uuid.uuid4().hex[:6]}")

    api("POST", f"/api/cards/{card_id}/start")
    # Let the (runner-less) run reach its terminal state so retry is legal.
    deadline = time.time() + 60
    while time.time() < deadline:
        status, body = api("GET", f"/api/cards/{card_id}")
        if status == 200 and body.get("status") in ("failed", "in_review", "done"):
            break
        time.sleep(1)

    before = len(_runs_for_card(card_id))
    results = fire_together(5, lambda _i: api("POST", f"/api/cards/{card_id}/retry"))
    counts = status_counts(results)
    time.sleep(3)
    after = len(_runs_for_card(card_id))

    assert counts.get(200, 0) == 1, (
        f"expected exactly one retry to win, got {counts}"
    )
    assert after - before == 1, (
        f"expected one new run from the retry burst, got {after - before}"
    )


def test_simultaneous_agent_file_create_yields_exactly_one_row():
    """Control case - this one is CORRECT and must stay correct.

    Agent files are name-unique and the create path rejects the losers with a
    400 instead of racing. Kept as a guard so a future "fix" to the card path
    does not regress the endpoint that already got it right.
    """
    require_stack()
    name = f"qa3-af-{uuid.uuid4().hex[:8]}"

    results = fire_together(
        10, lambda i: api("POST", "/api/agent-files", {"name": name, "content": f"c{i}"})
    )
    counts = status_counts(results)

    status, body = api("GET", "/api/agent-files")
    assert status == 200
    rows = [row for row in body if row.get("name") == name]

    assert counts.get(201, 0) == 1, f"expected one winner, got {counts}"
    assert len(rows) == 1, f"expected one agent-file row, found {len(rows)}"
    assert 500 not in counts, f"a concurrent create returned 500: {counts}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA3-5 (MAJOR): concurrent POST /api/prompt-templates with "
        "the same name loses the pre-check race and the UNIQUE violation "
        "escapes as a bare 500 'Internal Server Error' instead of the 409 the "
        "sequential path returns."
    ),
)
def test_simultaneous_prompt_template_create_never_returns_500():
    require_stack()
    name = f"qa3-tpl-{uuid.uuid4().hex[:8]}"

    results = fire_together(
        10,
        lambda i: api("POST", "/api/prompt-templates", {"name": name, "content": f"c{i}"}),
    )
    counts = status_counts(results)

    assert counts.get(500, 0) == 0, (
        f"a concurrent duplicate-name create returned 500: {counts}"
    )
