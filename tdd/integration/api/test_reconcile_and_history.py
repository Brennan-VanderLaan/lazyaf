"""
Integration tests for the test tie-back operator APIs (Phase 12.2.6,
pinned contract #6):

- POST /api/test-refs/reconcile — {repo_id, refs: [{lazyaf_test_id,
  file_path}]}: listed refs upsert to active with file_path; previously
  active refs for that repo absent from the list flip to orphan; response
  counts. Per-repo scoped.
- GET /api/criteria/{id}/history — TestRun series joined via TestRef,
  newest first, query params limit/branch.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import (
    AcceptanceCriterion,
    Feature,
    Repo,
    TestRef,
    TestRun,
    UserStory,
)


@pytest.fixture
async def repo_row(db_session):
    repo = Repo(id=str(uuid4()), name="reconcile-repo", is_ingested=True)
    db_session.add(repo)
    await db_session.commit()
    return repo


@pytest.fixture
async def other_repo_row(db_session):
    repo = Repo(id=str(uuid4()), name="other-repo", is_ingested=True)
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


async def _get_ref(db_session, lazyaf_test_id, repo_id=None):
    """A ref by identity. lazyaf_test_id alone is NOT an identity any more
    (contract #1), so pass repo_id whenever two repos share the id."""
    query = select(TestRef).where(TestRef.lazyaf_test_id == lazyaf_test_id)
    if repo_id is not None:
        query = query.where(TestRef.repo_id == repo_id)
    return (await db_session.execute(query)).scalar_one()


class TestReconcile:
    async def test_unknown_repo_404(self, client):
        response = await client.post(
            "/api/test-refs/reconcile", json={"repo_id": "nope", "refs": []}
        )
        assert response.status_code == 404

    async def test_creates_missing_refs_as_active(self, client, db_session, repo_row):
        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [
                    {"lazyaf_test_id": "a.one", "file_path": "tests/a.py"},
                    {"lazyaf_test_id": "a.two", "file_path": "tests/a.py"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json() == {"created": 2, "updated": 0, "orphaned": 0}

        ref = await _get_ref(db_session, "a.one")
        assert ref.status == "active"
        assert ref.repo_id == repo_row.id
        assert ref.file_path == "tests/a.py"

    async def test_listed_orphan_flips_back_to_active(
        self, client, db_session, repo_row
    ):
        """The orphan-TestRef lifecycle: an ingestion-auto-created orphan is
        promoted (with its file_path refreshed) once declared."""
        db_session.add(
            TestRef(
                lazyaf_test_id="was.orphan",
                repo_id=repo_row.id,
                status="orphan",
                file_path=None,
            )
        )
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [{"lazyaf_test_id": "was.orphan", "file_path": "tests/o.py"}],
            },
        )
        assert response.json() == {"created": 0, "updated": 1, "orphaned": 0}

        ref = await _get_ref(db_session, "was.orphan")
        await db_session.refresh(ref)
        assert ref.status == "active"
        assert ref.file_path == "tests/o.py"

    async def test_absent_active_refs_flip_to_orphan(
        self, client, db_session, repo_row
    ):
        """Previously-active refs for the repo that are absent from the
        declared list are orphaned (test deleted from the repo)."""
        db_session.add(
            TestRef(lazyaf_test_id="kept.ref", repo_id=repo_row.id, status="active")
        )
        db_session.add(
            TestRef(lazyaf_test_id="gone.ref", repo_id=repo_row.id, status="active")
        )
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [{"lazyaf_test_id": "kept.ref", "file_path": "tests/k.py"}],
            },
        )
        assert response.json() == {"created": 0, "updated": 1, "orphaned": 1}

        gone = await _get_ref(db_session, "gone.ref")
        await db_session.refresh(gone)
        assert gone.status == "orphan"
        kept = await _get_ref(db_session, "kept.ref")
        await db_session.refresh(kept)
        assert kept.status == "active"

    async def test_already_orphan_absent_refs_not_recounted(
        self, client, db_session, repo_row
    ):
        db_session.add(
            TestRef(lazyaf_test_id="long.gone", repo_id=repo_row.id, status="orphan")
        )
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile", json={"repo_id": repo_row.id, "refs": []}
        )
        assert response.json() == {"created": 0, "updated": 0, "orphaned": 0}

    async def test_reconcile_is_per_repo_scoped(
        self, client, db_session, repo_row, other_repo_row
    ):
        """Reconciling repo A never touches repo B's refs (PLAN
        test_reconcile_per_repo_scoped)."""
        db_session.add(
            TestRef(
                lazyaf_test_id="other.active",
                repo_id=other_repo_row.id,
                status="active",
            )
        )
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [{"lazyaf_test_id": "mine.only", "file_path": "t.py"}],
            },
        )
        assert response.json() == {"created": 1, "updated": 0, "orphaned": 0}

        other = await _get_ref(db_session, "other.active")
        await db_session.refresh(other)
        assert other.status == "active"
        assert other.repo_id == other_repo_row.id

    async def test_listed_id_owned_by_another_repo_is_created_not_rehomed(
        self, client, db_session, repo_row, other_repo_row
    ):
        """Contract #1: identity is (repo_id, lazyaf_test_id). Repo A
        declaring a marker string that repo B already registered creates A's
        OWN ref - B's row keeps its repo, status, path and criterion link.
        (The old global-unique behaviour silently moved B's ref to A, which
        handed A's gate B's test results.)"""
        b_ref = TestRef(
            lazyaf_test_id="same.marker",
            repo_id=other_repo_row.id,
            file_path="tests/b.py",
            status="active",
        )
        db_session.add(b_ref)
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [{"lazyaf_test_id": "same.marker", "file_path": "tests/a.py"}],
            },
        )
        assert response.json() == {"created": 1, "updated": 0, "orphaned": 0}

        await db_session.refresh(b_ref)
        assert b_ref.repo_id == other_repo_row.id
        assert b_ref.file_path == "tests/b.py"
        assert b_ref.status == "active"

        a_ref = await _get_ref(db_session, "same.marker", repo_id=repo_row.id)
        assert a_ref.file_path == "tests/a.py"
        assert a_ref.status == "active"
        assert a_ref.id != b_ref.id

    async def test_absent_from_list_never_orphans_another_repos_same_id(
        self, client, db_session, repo_row, other_repo_row
    ):
        """The orphan flip is repo-scoped too: reconciling A with an empty
        list must not orphan B's identically-named ref."""
        b_ref = TestRef(
            lazyaf_test_id="shared.name", repo_id=other_repo_row.id, status="active"
        )
        a_ref = TestRef(
            lazyaf_test_id="shared.name", repo_id=repo_row.id, status="active"
        )
        db_session.add_all([b_ref, a_ref])
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile", json={"repo_id": repo_row.id, "refs": []}
        )
        assert response.json() == {"created": 0, "updated": 0, "orphaned": 1}

        await db_session.refresh(a_ref)
        await db_session.refresh(b_ref)
        assert a_ref.status == "orphan"
        assert b_ref.status == "active"

    async def test_repeated_id_in_one_request_is_upserted_once(
        self, client, db_session, repo_row
    ):
        """A caller that lists the same id twice gets one ref, not a unique
        violation."""
        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [
                    {"lazyaf_test_id": "twice.listed", "file_path": "tests/one.py"},
                    {"lazyaf_test_id": "twice.listed", "file_path": "tests/two.py"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json() == {"created": 1, "updated": 1, "orphaned": 0}

        refs = (
            (
                await db_session.execute(
                    select(TestRef).where(TestRef.lazyaf_test_id == "twice.listed")
                )
            )
            .scalars()
            .all()
        )
        assert len(refs) == 1
        assert refs[0].file_path == "tests/two.py"

    async def test_file_paths_follow_the_repo_root_relative_convention(
        self, client, db_session, repo_row
    ):
        """Contract #3: backslashes normalize, and a differently-rooted path
        is refused rather than stored."""
        response = await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [
                    {"lazyaf_test_id": "win.sep", "file_path": "tdd\\unit\\test_a.py"},
                    {"lazyaf_test_id": "abs.path", "file_path": "/workspace/tdd/b.py"},
                ],
            },
        )
        assert response.json()["created"] == 2

        assert (await _get_ref(db_session, "win.sep")).file_path == "tdd/unit/test_a.py"
        assert (await _get_ref(db_session, "abs.path")).file_path is None

    async def test_bad_path_does_not_wipe_a_good_one(
        self, client, db_session, repo_row
    ):
        db_session.add(
            TestRef(
                lazyaf_test_id="keeps.path",
                repo_id=repo_row.id,
                file_path="tdd/unit/test_keeps.py",
                status="active",
            )
        )
        await db_session.commit()

        await client.post(
            "/api/test-refs/reconcile",
            json={
                "repo_id": repo_row.id,
                "refs": [
                    {"lazyaf_test_id": "keeps.path", "file_path": "C:/repo/tdd/x.py"}
                ],
            },
        )
        ref = await _get_ref(db_session, "keeps.path")
        await db_session.refresh(ref)
        assert ref.file_path == "tdd/unit/test_keeps.py"

    async def test_reconcile_is_idempotent(self, client, repo_row):
        body = {
            "repo_id": repo_row.id,
            "refs": [{"lazyaf_test_id": "idem.ref", "file_path": "t.py"}],
        }
        first = await client.post("/api/test-refs/reconcile", json=body)
        assert first.json() == {"created": 1, "updated": 0, "orphaned": 0}

        second = await client.post("/api/test-refs/reconcile", json=body)
        assert second.json() == {"created": 0, "updated": 1, "orphaned": 0}


class TestCriterionHistory:
    async def test_unknown_criterion_404(self, client):
        response = await client.get("/api/criteria/nope/history")
        assert response.status_code == 404

    async def test_empty_history(self, client, criterion):
        response = await client.get(f"/api/criteria/{criterion.id}/history")
        assert response.status_code == 200
        assert response.json() == []

    async def _seed_runs(self, db_session, repo_id, criterion_id, n=3):
        ref = TestRef(
            lazyaf_test_id=f"hist.{uuid4().hex[:8]}",
            repo_id=repo_id,
            criterion_id=criterion_id,
        )
        db_session.add(ref)
        await db_session.flush()
        base = datetime(2026, 8, 1, 12, 0, 0)
        runs = []
        for i in range(n):
            run = TestRun(
                test_ref_id=ref.id,
                pipeline_run_id=f"pr{i}",
                step_run_id=f"sr{i}",
                commit_sha=f"sha{i}",
                branch="main" if i % 2 == 0 else "feature/x",
                status="passed" if i % 2 == 0 else "failed",
                duration_ms=i * 10,
                created_at=base + timedelta(hours=i),
            )
            db_session.add(run)
            runs.append(run)
        await db_session.commit()
        return ref, runs

    async def test_history_newest_first_joined_via_ref(
        self, client, db_session, repo_row, criterion
    ):
        ref, runs = await self._seed_runs(db_session, repo_row.id, criterion.id)
        # A run for an unlinked ref must NOT appear in the series.
        noise = TestRef(lazyaf_test_id="noise.ref", repo_id=repo_row.id)
        db_session.add(noise)
        await db_session.flush()
        db_session.add(
            TestRun(
                test_ref_id=noise.id,
                pipeline_run_id="prX",
                commit_sha="shaX",
                status="passed",
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/criteria/{criterion.id}/history")
        assert response.status_code == 200
        entries = response.json()
        assert [e["commit_sha"] for e in entries] == ["sha2", "sha1", "sha0"]
        assert all(e["lazyaf_test_id"] == ref.lazyaf_test_id for e in entries)
        assert entries[0]["status"] == "passed"
        assert entries[1]["status"] == "failed"

    async def test_history_limit_param(self, client, db_session, repo_row, criterion):
        await self._seed_runs(db_session, repo_row.id, criterion.id, n=5)

        response = await client.get(f"/api/criteria/{criterion.id}/history?limit=2")
        entries = response.json()
        assert len(entries) == 2
        assert [e["commit_sha"] for e in entries] == ["sha4", "sha3"]

    async def test_history_branch_filter(self, client, db_session, repo_row, criterion):
        await self._seed_runs(db_session, repo_row.id, criterion.id, n=4)

        response = await client.get(
            f"/api/criteria/{criterion.id}/history?branch=feature/x"
        )
        entries = response.json()
        assert [e["commit_sha"] for e in entries] == ["sha3", "sha1"]
        assert all(e["branch"] == "feature/x" for e in entries)

    async def test_history_entry_shape(self, client, db_session, repo_row, criterion):
        """The series is joinable to commits/runs: every provenance field is
        present (PLAN done-criterion: 'returns data joinable to commits')."""
        await self._seed_runs(db_session, repo_row.id, criterion.id, n=1)

        entry = (await client.get(f"/api/criteria/{criterion.id}/history")).json()[0]
        assert set(entry) == {
            "id",
            "test_ref_id",
            "lazyaf_test_id",
            "pipeline_run_id",
            "step_run_id",
            "commit_sha",
            "branch",
            "status",
            "duration_ms",
            "model",
            "prompt_template_id",
            "created_at",
        }
