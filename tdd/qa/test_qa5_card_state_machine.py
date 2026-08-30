"""QA-5 finding 4 (MAJOR): the board lets a user walk a card into a state
where "Approve" reports success without merging anything.

Reproduction through the UI alone, no API calls needed:

  1. Create a repo and a card. It lands in TO DO.
  2. DRAG the card from TO DO straight onto the IN REVIEW column.
     ``Board.svelte`` ``handleDrop`` sends ``PATCH {status: "in_review"}`` for
     every transition except todo -> in_progress, and the backend accepts it.
     The card is now "in review" with ``branch_name = null``.
  3. Open the card and click "✓ Approve".
     ``approve_card`` only merges ``if card.branch_name and repo.is_ingested``
     (backend/app/routers/cards.py:435). With no branch that whole block is
     skipped, the card is still marked ``done``, the modal closes, and no
     error is raised anywhere.

Demo result: a card sitting in DONE that never ran, never branched and never
merged — and nothing in the UI says so.

Note: the same silent-approve hole is reachable from TO DO directly; a peer QA
lane covers that entry point. These tests pin the drag-through-IN-REVIEW path
that the board exposes to a mouse.
"""
import pytest

from .qa5_http import api, drop_repo, make_card, make_repo


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
    reason="QA finding 4a (MAJOR): backend accepts an arbitrary todo -> "
           "in_review jump, which the board's drag-and-drop performs directly",
)
def test_card_cannot_jump_from_todo_straight_to_in_review(card):
    status, body = api("PATCH", f"/api/cards/{card['id']}", {"status": "in_review"})
    branch = body.get("branch_name") if isinstance(body, dict) else "?"
    assert status >= 400, (
        f"todo -> in_review was accepted ({status}); the card is now in review "
        f"with branch_name={branch!r}, so the review panel has nothing to show"
    )


@pytest.mark.xfail(
    strict=True,
    reason="QA finding 4b (MAJOR): approving a branchless card silently "
           "reports success and marks it done without merging anything",
)
def test_approving_a_card_with_no_branch_is_refused(card):
    moved_status, moved = api(
        "PATCH", f"/api/cards/{card['id']}", {"status": "in_review"}
    )
    if moved_status >= 400 or not isinstance(moved, dict):
        pytest.skip("could not stage the in_review state")
    assert moved["branch_name"] is None, "precondition: card has no branch"

    status, body = api("POST", f"/api/cards/{card['id']}/approve", {})
    final = body.get("card", {}).get("status") if isinstance(body, dict) else body
    assert status >= 400, (
        f"approve returned {status} for a card with no branch; card status is "
        f"now {final!r} and nothing was merged"
    )


def test_diff_for_a_branchless_card_is_a_clean_error(card):
    """VERIFIED NOT A BUG — pinned so a refactor keeps the clean 400.

    The review panel asks for a diff; with no branch the backend answers
    ``400 {"detail": "Card has no branch"}`` instead of leaking a traceback
    into the UI's error alert.
    """
    api("PATCH", f"/api/cards/{card['id']}", {"status": "in_review"})
    status, body = api("GET", f"/api/cards/{card['id']}/diff")
    assert status == 400, f"expected a clean 400, got {status}: {body!r}"
    assert isinstance(body, dict) and "detail" in body
    detail = str(body["detail"])
    assert "Traceback" not in detail and 'File "' not in detail, (
        f"diff error leaked internals into a user-facing string: {detail!r}"
    )


def test_html_in_a_card_title_round_trips_unescaped_on_the_wire(repo):
    """VERIFIED NOT A BUG — the API stores titles verbatim and Svelte escapes
    them at render time. Confirmed in the browser: a title containing
    ``<script>alert('xss')</script><img src=x onerror=alert(1)>`` renders as
    literal text, fires no dialog and loads no image.

    Pinned so nobody "fixes" this by HTML-escaping on the server (which would
    double-escape in the UI) or by rendering titles with {@html}.
    """
    payload = "qa5 <script>alert('xss')</script> <img src=x onerror=alert(1)>"
    card = make_card(repo["id"], title=payload)
    assert card["title"] == payload, "server must store the title verbatim"
    status, fetched = api("GET", f"/api/cards/{card['id']}")
    assert status == 200 and fetched["title"] == payload
