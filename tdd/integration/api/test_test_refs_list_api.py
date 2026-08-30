"""
Integration tests for GET /api/test-refs (Phase 12.4 tie-back surface).

The MCP tool `list_test_refs` has always called this route; until now the
route did not exist, so the tool was dead on arrival (every call returned
"Failed to list test refs: {"detail":"Not Found"}").

Contract:
- Filters: repo_id, criterion_id, status; paging via limit/offset.
- Ordering is (repo_id, lazyaf_test_id) — pinned contract #1 makes that PAIR
  the identity of a TestRef, so two repos declaring the same marker string
  list as two distinct rows and never collapse.
- Unknown repo_id -> 404 (an empty list would read as "this repo declares no
  tests", which is a materially different answer).
- Unknown status -> 400 naming the vocabulary.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (
    AcceptanceCriterion,
    Feature,
    Repo,
    TestRef,
    UserStory,
)


@pytest.fixture
async def repo_a(db_session):
    repo = Repo(id=str(uuid4()), name="list-repo-a", is_ingested=True)
    db_session.add(repo)
    await db_session.commit()
    return repo


@pytest.fixture
async def repo_b(db_session):
    repo = Repo(id=str(uuid4()), name="list-repo-b", is_ingested=True)
    db_session.add(repo)
    await db_session.commit()
    return repo


@pytest.fixture
async def criterion(db_session):
    feature = Feature(title="F", repo_ids="[]")
    db_session.add(feature)
    await db_session.flush()
    story = UserStory(feature_id=feature.id, title="S")
    db_session.add(story)
    await db_session.flush()
    crit = AcceptanceCriterion(user_story_id=story.id, text="measurable")
    db_session.add(crit)
    await db_session.commit()
    return crit


@pytest.fixture
async def refs(db_session, repo_a, repo_b, criterion):
    """Two repos; repo_a has one linked active, one unlinked orphan; repo_b
    declares the SAME marker string as repo_a (contract #1)."""
    rows = [
        TestRef(
            id=str(uuid4()),
            lazyaf_test_id="auth.login",
            repo_id=repo_a.id,
            file_path="tests/test_auth.py",
            criterion_id=criterion.id,
            status="active",
        ),
        TestRef(
            id=str(uuid4()),
            lazyaf_test_id="auth.zz_removed",
            repo_id=repo_a.id,
            file_path="tests/test_auth.py",
            status="orphan",
        ),
        TestRef(
            id=str(uuid4()),
            lazyaf_test_id="auth.login",
            repo_id=repo_b.id,
            file_path="spec/auth_spec.py",
            status="active",
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return rows


class TestListTestRefsRouteExists:
    async def test_route_is_registered(self, client, refs):
        """The MCP tool's route resolves (it 404'd as 'Not Found' before)."""
        response = await client.get("/api/test-refs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_unfiltered_lists_every_repo(self, client, refs):
        ids = {(r["repo_id"], r["lazyaf_test_id"]) for r in (await client.get("/api/test-refs")).json()}
        assert len(ids) == 3


class TestRepoScoping:
    async def test_filters_to_one_repo(self, client, refs, repo_a):
        rows = (await client.get(f"/api/test-refs?repo_id={repo_a.id}")).json()
        assert len(rows) == 2
        assert {r["repo_id"] for r in rows} == {repo_a.id}

    async def test_same_marker_in_two_repos_are_two_rows(
        self, client, refs, repo_a, repo_b
    ):
        """Contract #1: identity is (repo_id, lazyaf_test_id)."""
        a = (await client.get(f"/api/test-refs?repo_id={repo_a.id}")).json()
        b = (await client.get(f"/api/test-refs?repo_id={repo_b.id}")).json()
        a_login = [r for r in a if r["lazyaf_test_id"] == "auth.login"]
        b_login = [r for r in b if r["lazyaf_test_id"] == "auth.login"]
        assert len(a_login) == 1 and len(b_login) == 1
        assert a_login[0]["id"] != b_login[0]["id"]
        assert a_login[0]["file_path"] != b_login[0]["file_path"]

    async def test_unknown_repo_is_404_not_empty_list(self, client, refs):
        response = await client.get("/api/test-refs?repo_id=nope")
        assert response.status_code == 404

    async def test_ordering_is_repo_then_test_id(self, client, refs, repo_a):
        rows = (await client.get(f"/api/test-refs?repo_id={repo_a.id}")).json()
        assert [r["lazyaf_test_id"] for r in rows] == [
            "auth.login",
            "auth.zz_removed",
        ]


class TestFilters:
    async def test_filter_by_status_active(self, client, refs, repo_a):
        rows = (
            await client.get(f"/api/test-refs?repo_id={repo_a.id}&status=active")
        ).json()
        assert [r["lazyaf_test_id"] for r in rows] == ["auth.login"]

    async def test_filter_by_status_orphan(self, client, refs, repo_a):
        rows = (
            await client.get(f"/api/test-refs?repo_id={repo_a.id}&status=orphan")
        ).json()
        assert [r["lazyaf_test_id"] for r in rows] == ["auth.zz_removed"]

    async def test_invalid_status_is_400_naming_vocabulary(self, client, refs):
        response = await client.get("/api/test-refs?status=flaky")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "active" in detail and "orphan" in detail

    async def test_filter_by_criterion(self, client, refs, criterion):
        rows = (
            await client.get(f"/api/test-refs?criterion_id={criterion.id}")
        ).json()
        assert len(rows) == 1
        assert rows[0]["criterion_id"] == criterion.id

    async def test_filters_compose(self, client, refs, repo_a, criterion):
        rows = (
            await client.get(
                f"/api/test-refs?repo_id={repo_a.id}"
                f"&criterion_id={criterion.id}&status=active"
            )
        ).json()
        assert len(rows) == 1
        assert rows[0]["lazyaf_test_id"] == "auth.login"

    async def test_criterion_filter_matching_nothing_is_empty(self, client, refs):
        rows = (await client.get("/api/test-refs?criterion_id=nope")).json()
        assert rows == []


class TestPaging:
    async def test_limit_and_offset(self, client, refs, repo_a):
        first = (
            await client.get(f"/api/test-refs?repo_id={repo_a.id}&limit=1")
        ).json()
        second = (
            await client.get(
                f"/api/test-refs?repo_id={repo_a.id}&limit=1&offset=1"
            )
        ).json()
        assert len(first) == 1 and len(second) == 1
        assert first[0]["id"] != second[0]["id"]

    async def test_limit_out_of_range_is_422(self, client, refs):
        assert (await client.get("/api/test-refs?limit=0")).status_code == 422


class TestResponseShape:
    async def test_row_carries_the_identity_pair_and_link(
        self, client, refs, repo_a, criterion
    ):
        rows = (
            await client.get(f"/api/test-refs?repo_id={repo_a.id}&status=active")
        ).json()
        row = rows[0]
        for key in (
            "id",
            "lazyaf_test_id",
            "repo_id",
            "file_path",
            "criterion_id",
            "status",
            "created_at",
            "updated_at",
        ):
            assert key in row, key
        assert row["repo_id"] == repo_a.id
        assert row["criterion_id"] == criterion.id
