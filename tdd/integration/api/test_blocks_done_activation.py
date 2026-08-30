"""
Integration tests for the blocks-done activation (Phase 12.2.6).

A UserStory cannot transition to 'done' while any REQUIRED acceptance
criterion lacks a passing TestRun (joined via TestRef.criterion_id). This
was shipped stubbed in 12.2.5 and activates here, now that TestRef/TestRun
exist (migration 0004_test_tieback).
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import assert_status_code


@pytest_asyncio.fixture
async def feature(client):
    response = await client.post(
        "/api/features",
        json={"title": "Blocks-done Feature", "description": "12.2.6 activation"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def story(client, feature):
    response = await client.post(
        f"/api/features/{feature['id']}/stories",
        json={"title": "Verified Story", "narrative": "When done, then verified"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_criterion(client, story_id: str, text: str, required: bool = True) -> dict:
    response = await client.post(
        f"/api/user-stories/{story_id}/criteria",
        json={"text": text, "required": required},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_test_run(
    db_session,
    repo_id: str,
    criterion_id: str | None,
    status: str = "passed",
    lazyaf_test_id: str | None = None,
):
    """Insert a TestRef (linked to criterion_id) plus one TestRun with the
    given status, directly through the models."""
    from app.models import TestRef, TestRun

    ref = TestRef(
        lazyaf_test_id=lazyaf_test_id or f"blocksdone.{uuid4().hex[:12]}",
        repo_id=repo_id,
        file_path="tdd/integration/api/test_blocks_done_activation.py",
        criterion_id=criterion_id,
        status="active",
    )
    db_session.add(ref)
    await db_session.flush()
    run = TestRun(
        test_ref_id=ref.id,
        pipeline_run_id=str(uuid4()),
        commit_sha="deadbeefcafe",
        branch="main",
        status=status,
        duration_ms=42,
    )
    db_session.add(run)
    await db_session.commit()
    return ref, run


async def _patch_done(client, story_id: str):
    return await client.patch(
        f"/api/user-stories/{story_id}", json={"status": "done"}
    )


class TestBlocksDoneActivation:
    """PATCH /api/user-stories/{id} status=done vs required criteria."""

    async def test_required_criterion_without_any_testrun_blocks(
        self, client, story
    ):
        await _add_criterion(client, story["id"], "Unverified requirement")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 409)
        # story unchanged
        current = (await client.get(f"/api/user-stories/{story['id']}")).json()
        assert current["status"] != "done"

    async def test_required_criterion_with_passing_testrun_allows_done(
        self, client, db_session, story, repo
    ):
        criterion = await _add_criterion(client, story["id"], "Verified requirement")
        await _add_test_run(db_session, repo["id"], criterion["id"], status="passed")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 200)
        assert response.json()["status"] == "done"

    async def test_failed_testrun_does_not_unblock(
        self, client, db_session, story, repo
    ):
        criterion = await _add_criterion(client, story["id"], "Failing requirement")
        await _add_test_run(db_session, repo["id"], criterion["id"], status="failed")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 409)

    async def test_skipped_testrun_does_not_unblock(
        self, client, db_session, story, repo
    ):
        criterion = await _add_criterion(client, story["id"], "Skipped requirement")
        await _add_test_run(db_session, repo["id"], criterion["id"], status="skipped")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 409)

    async def test_non_required_criterion_never_blocks(self, client, story):
        await _add_criterion(
            client, story["id"], "Nice to have", required=False
        )
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 200)
        assert response.json()["status"] == "done"

    async def test_story_with_no_criteria_can_be_done(self, client, story):
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 200)

    async def test_one_unverified_required_criterion_still_blocks(
        self, client, db_session, story, repo
    ):
        verified = await _add_criterion(client, story["id"], "Verified one")
        await _add_criterion(client, story["id"], "Unverified one")
        await _add_test_run(db_session, repo["id"], verified["id"], status="passed")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 409)

    async def test_passing_run_on_unlinked_ref_does_not_unblock(
        self, client, db_session, story, repo
    ):
        """A passing TestRun on an ORPHAN-style ref (criterion_id None)
        verifies nothing."""
        await _add_criterion(client, story["id"], "Still unverified")
        await _add_test_run(db_session, repo["id"], None, status="passed")
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 409)

    async def test_failed_then_passing_run_unblocks(
        self, client, db_session, story, repo
    ):
        """Any passing run for the criterion satisfies the rule, even if
        other runs failed (history, not latest-only, per contract)."""
        criterion = await _add_criterion(client, story["id"], "Flaky then fixed")
        ref, _ = await _add_test_run(db_session, repo["id"], criterion["id"], status="failed")

        from app.models import TestRun

        db_session.add(
            TestRun(
                test_ref_id=ref.id,
                pipeline_run_id=str(uuid4()),
                commit_sha="deadbeefcaf2",
                branch="main",
                status="passed",
                duration_ms=7,
            )
        )
        await db_session.commit()
        response = await _patch_done(client, story["id"])
        assert_status_code(response, 200)

    async def test_non_done_transitions_unaffected(self, client, story):
        """The rule gates only the 'done' transition."""
        await _add_criterion(client, story["id"], "Unverified requirement")
        response = await client.patch(
            f"/api/user-stories/{story['id']}", json={"status": "in_progress"}
        )
        assert_status_code(response, 200)
        assert response.json()["status"] == "in_progress"
