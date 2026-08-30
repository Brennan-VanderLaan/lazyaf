"""
Integration tests for POST /api/steps/{step_id}/test-results (Phase 12.2.6).

Pinned contracts exercised here:
- #1 manifest shape: {"version": 1, "results": [...]} with status
  passed|failed|skipped (anything else is a 422)
- #3 endpoint semantics: Bearer step-token auth (same as /logs), server
  derives pipeline_run/commit/branch from the StepExecution -> StepRun ->
  PipelineRun chain, unknown lazyaf_test_ids auto-create ORPHAN TestRefs,
  terminal StepExecutions answer 409
- Ingestion idempotency: re-POSTing a step's manifest updates instead of
  duplicating (key: step_run_id + test_ref)
- Repo scoping (contract #1): TestRef identity is (repo_id, lazyaf_test_id),
  so the same marker string in two repos stays two independent refs
- Duplicate ids in ONE manifest are aggregated worst-status-wins, never
  last-wins (a green rerun may not bury a red case)
- file_path (contract #3) is repo-root-relative; anything else is refused
  rather than written over a good path
- Ref writes are content-addressed: unchanged refs are not re-stamped
"""
import json
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
    Pipeline,
    PipelineRun,
    Repo,
    StepExecution,
    StepRun,
    TestRef,
    TestRun,
    UserStory,
)
from app.services.control_layer.auth import generate_step_token

COMMIT = "deadbeefcafe0001"
BRANCH = "main"


async def _make_step_ctx(db_session, repo_name: str) -> dict:
    """Full chain: repo -> pipeline -> run (with trigger_context) -> step run
    -> step execution, plus a valid step token."""
    repo = Repo(id=str(uuid4()), name=repo_name, is_ingested=True)
    db_session.add(repo)

    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci", steps="[]")
    db_session.add(pipeline)

    pipeline_run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status="running",
        trigger_type="push",
        trigger_context=json.dumps({"branch": BRANCH, "commit_sha": COMMIT}),
    )
    db_session.add(pipeline_run)

    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=pipeline_run.id,
        step_index=0,
        step_name="pytest",
        status="running",
        logs="",
    )
    db_session.add(step_run)

    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{pipeline_run.id}:0:1",
        step_run_id=step_run.id,
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    token = generate_step_token(step_id=execution.id, execution_key=execution.execution_key)
    return {
        "repo_id": repo.id,
        "pipeline_run_id": pipeline_run.id,
        "step_run_id": step_run.id,
        "execution_id": execution.id,
        "execution": execution,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
async def step_ctx(db_session):
    return await _make_step_ctx(db_session, "tieback-repo")


@pytest.fixture
async def other_step_ctx(db_session):
    """A second repo's chain — same shape, different repo_id."""
    return await _make_step_ctx(db_session, "tieback-repo-2")


def manifest(*results):
    return {"version": 1, "results": list(results)}


def result_entry(test_id, status="passed", duration_ms=10, file_path="tests/test_x.py"):
    return {
        "lazyaf_test_id": test_id,
        "status": status,
        "duration_ms": duration_ms,
        "file_path": file_path,
    }


async def _post(client, ctx, body):
    return await client.post(
        f"/api/steps/{ctx['execution_id']}/test-results",
        json=body,
        headers=ctx["headers"],
    )


class TestAuth:
    async def test_requires_auth_header(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/test-results",
            json=manifest(),
        )
        assert response.status_code == 401

    async def test_rejects_invalid_token(self, client, step_ctx):
        response = await client.post(
            f"/api/steps/{step_ctx['execution_id']}/test-results",
            json=manifest(),
            headers={"Authorization": "Bearer bogus"},
        )
        assert response.status_code == 403

    async def test_404_for_unknown_step(self, client):
        response = await client.post(
            "/api/steps/nope/test-results",
            json=manifest(),
            headers={"Authorization": "Bearer whatever"},
        )
        assert response.status_code == 404

    async def test_409_on_terminal_execution(self, client, db_session, step_ctx):
        """Zombie-token hardening applies to test-results like every other
        step write endpoint."""
        step_ctx["execution"].status = "completed"
        await db_session.commit()

        response = await _post(client, step_ctx, manifest(result_entry("a.b")))
        assert response.status_code == 409


class TestManifestValidation:
    async def test_invalid_status_rejected(self, client, step_ctx):
        response = await _post(
            client, step_ctx, manifest(result_entry("a.b", status="errored"))
        )
        assert response.status_code == 422

    async def test_unknown_version_rejected(self, client, step_ctx):
        response = await _post(client, step_ctx, {"version": 2, "results": []})
        assert response.status_code == 422

    async def test_missing_results_rejected(self, client, step_ctx):
        response = await _post(client, step_ctx, {"version": 1})
        assert response.status_code == 422

    async def test_empty_manifest_accepted(self, client, step_ctx):
        response = await _post(client, step_ctx, manifest())
        assert response.status_code == 200
        assert response.json() == {
            "results_received": 0,
            "test_runs_created": 0,
            "test_runs_updated": 0,
            "orphan_refs_created": 0,
        }


class TestIngestion:
    async def test_ingest_creates_test_runs_with_run_context(
        self, client, db_session, step_ctx
    ):
        """One TestRun per result, joined to the run chain: pipeline_run_id,
        step_run_id, commit and branch all derived server-side."""
        ref = TestRef(
            lazyaf_test_id="us1.push_triggers", repo_id=step_ctx["repo_id"]
        )
        db_session.add(ref)
        await db_session.commit()

        response = await _post(
            client,
            step_ctx,
            manifest(result_entry("us1.push_triggers", status="failed", duration_ms=77)),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["test_runs_created"] == 1
        assert data["orphan_refs_created"] == 0

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == ref.id)
            )
        ).scalar_one()
        assert run.pipeline_run_id == step_ctx["pipeline_run_id"]
        assert run.step_run_id == step_ctx["step_run_id"]
        assert run.commit_sha == COMMIT
        assert run.branch == BRANCH
        assert run.status == "failed"
        assert run.duration_ms == 77

    async def test_unknown_test_id_auto_creates_orphan_ref(
        self, client, db_session, step_ctx
    ):
        """Contract #3: unknown lazyaf_test_ids auto-create ORPHAN TestRefs
        attributed to the run's repo, with the manifest's file_path."""
        response = await _post(
            client,
            step_ctx,
            manifest(result_entry("never.seen", file_path="tests/test_new.py")),
        )
        assert response.status_code == 200
        assert response.json()["orphan_refs_created"] == 1

        ref = (
            await db_session.execute(
                select(TestRef).where(TestRef.lazyaf_test_id == "never.seen")
            )
        ).scalar_one()
        assert ref.status == "orphan"
        assert ref.repo_id == step_ctx["repo_id"]
        assert ref.file_path == "tests/test_new.py"
        # ... and the result itself is not dropped:
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == ref.id)
            )
        ).scalar_one()
        assert run.status == "passed"

    async def test_reingest_is_idempotent_per_step(self, client, db_session, step_ctx):
        """Re-POSTing the same step's manifest does not duplicate TestRuns
        (key: step_run_id + test_ref); last write wins on status."""
        first = await _post(
            client, step_ctx, manifest(result_entry("a.b"), result_entry("c.d"))
        )
        assert first.json()["test_runs_created"] == 2

        second = await _post(
            client,
            step_ctx,
            manifest(result_entry("a.b", status="failed"), result_entry("c.d")),
        )
        assert second.status_code == 200
        assert second.json()["test_runs_created"] == 0
        assert second.json()["test_runs_updated"] == 2

        runs = (
            (
                await db_session.execute(
                    select(TestRun).where(TestRun.step_run_id == step_ctx["step_run_id"])
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 2
        by_ref = {}
        for run in runs:
            ref = await db_session.get(TestRef, run.test_ref_id)
            by_ref[ref.lazyaf_test_id] = run
        assert by_ref["a.b"].status == "failed"  # updated in place
        assert by_ref["c.d"].status == "passed"

    async def test_unchanged_ref_is_not_restamped(self, client, db_session, step_ctx):
        """Ingestion writes runs, not ref churn: a ref whose stored content
        still matches the manifest is left completely alone (no UPDATE, no
        updated_at bump). updated_at means "this registration changed", and
        reconcile owns the declared-set freshness signal."""
        stale = datetime.utcnow() - timedelta(days=3)
        ref = TestRef(
            lazyaf_test_id="old.but.alive",
            repo_id=step_ctx["repo_id"],
            file_path="tests/test_x.py",
            created_at=stale,
            updated_at=stale,
        )
        db_session.add(ref)
        await db_session.commit()

        response = await _post(client, step_ctx, manifest(result_entry("old.but.alive")))
        assert response.status_code == 200

        await db_session.refresh(ref)
        assert ref.updated_at == stale
        # ingestion never flips status either — that is reconcile's job
        assert ref.status == "active"

    async def test_moved_test_file_updates_the_ref(self, client, db_session, step_ctx):
        """Content DID change: the ref follows the test to its new path (and
        that write is what bumps updated_at)."""
        stale = datetime.utcnow() - timedelta(days=3)
        ref = TestRef(
            lazyaf_test_id="it.moved",
            repo_id=step_ctx["repo_id"],
            file_path="tests/old_home.py",
            created_at=stale,
            updated_at=stale,
        )
        db_session.add(ref)
        await db_session.commit()

        response = await _post(
            client,
            step_ctx,
            manifest(result_entry("it.moved", file_path="tests/new_home.py")),
        )
        assert response.status_code == 200

        await db_session.refresh(ref)
        assert ref.file_path == "tests/new_home.py"
        assert ref.updated_at > stale

    async def test_linked_criterion_run_reaches_history(
        self, client, db_session, step_ctx
    ):
        """Round trip (R3): manifest POST -> TestRun row -> criterion history
        series via the API."""
        feature = Feature(title="F", repo_ids="[]")
        db_session.add(feature)
        await db_session.flush()
        story = UserStory(feature_id=feature.id, title="S")
        db_session.add(story)
        await db_session.flush()
        criterion = AcceptanceCriterion(user_story_id=story.id, text="verified")
        db_session.add(criterion)
        await db_session.flush()
        ref = TestRef(
            lazyaf_test_id="crit.check",
            repo_id=step_ctx["repo_id"],
            criterion_id=criterion.id,
        )
        db_session.add(ref)
        await db_session.commit()

        response = await _post(client, step_ctx, manifest(result_entry("crit.check")))
        assert response.status_code == 200

        history = await client.get(f"/api/criteria/{criterion.id}/history")
        assert history.status_code == 200
        entries = history.json()
        assert len(entries) == 1
        assert entries[0]["lazyaf_test_id"] == "crit.check"
        assert entries[0]["status"] == "passed"
        assert entries[0]["commit_sha"] == COMMIT
        assert entries[0]["pipeline_run_id"] == step_ctx["pipeline_run_id"]


class TestDuplicateIdAggregation:
    """R4 (fake green): one manifest, one id, several entries."""

    async def test_fail_plus_pass_aggregates_to_failed(
        self, client, db_session, step_ctx
    ):
        """The parametrized-marker case: several cases share one marker id
        and one of them failed. Last-wins would report green; aggregation
        reports the failure."""
        response = await _post(
            client,
            step_ctx,
            manifest(
                result_entry("param.case", status="failed", duration_ms=5),
                result_entry("param.case", status="passed", duration_ms=7),
            ),
        )
        assert response.status_code == 200
        assert response.json()["results_received"] == 2
        # one id -> one ref, one run
        assert response.json()["orphan_refs_created"] == 1
        assert response.json()["test_runs_created"] == 1

        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.step_run_id == step_ctx["step_run_id"])
            )
        ).scalar_one()
        assert run.status == "failed"
        # durations of the collapsed entries add up
        assert run.duration_ms == 12

    async def test_order_does_not_decide_the_status(self, client, db_session, step_ctx):
        """Same two entries the other way round: still failed."""
        await _post(
            client,
            step_ctx,
            manifest(
                result_entry("param.case", status="passed"),
                result_entry("param.case", status="failed"),
            ),
        )
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.step_run_id == step_ctx["step_run_id"])
            )
        ).scalar_one()
        assert run.status == "failed"

    async def test_pass_beats_skipped(self, client, db_session, step_ctx):
        """skipped only wins when EVERY entry skipped - a marker with one
        real green case is green."""
        await _post(
            client,
            step_ctx,
            manifest(
                result_entry("mixed.case", status="skipped"),
                result_entry("mixed.case", status="passed"),
            ),
        )
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.step_run_id == step_ctx["step_run_id"])
            )
        ).scalar_one()
        assert run.status == "passed"

    async def test_all_skipped_stays_skipped(self, client, db_session, step_ctx):
        await _post(
            client,
            step_ctx,
            manifest(
                result_entry("all.skipped", status="skipped"),
                result_entry("all.skipped", status="skipped"),
            ),
        )
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.step_run_id == step_ctx["step_run_id"])
            )
        ).scalar_one()
        assert run.status == "skipped"

    async def test_duplicate_ids_do_not_double_create_refs(
        self, client, db_session, step_ctx
    ):
        """Three entries, two ids: two refs, two runs (and no unique
        violation against the composite identity)."""
        response = await _post(
            client,
            step_ctx,
            manifest(
                result_entry("dup.one"),
                result_entry("dup.two"),
                result_entry("dup.one", status="failed"),
            ),
        )
        assert response.status_code == 200
        assert response.json()["orphan_refs_created"] == 2
        assert response.json()["test_runs_created"] == 2

        refs = (
            (
                await db_session.execute(
                    select(TestRef).where(TestRef.repo_id == step_ctx["repo_id"])
                )
            )
            .scalars()
            .all()
        )
        assert sorted(r.lazyaf_test_id for r in refs) == ["dup.one", "dup.two"]


class TestRepoScoping:
    """Contract #1: a TestRef is identified by (repo_id, lazyaf_test_id)."""

    async def test_same_marker_id_in_two_repos_stays_two_refs(
        self, client, db_session, step_ctx, other_step_ctx
    ):
        """Repo A's run must not attach to repo B's ref, and vice versa."""
        shared_id = "shared.marker.id"

        first = await _post(client, step_ctx, manifest(result_entry(shared_id)))
        second = await _post(
            client, other_step_ctx, manifest(result_entry(shared_id, status="failed"))
        )
        assert first.json()["orphan_refs_created"] == 1
        # repo B does NOT find repo A's ref: it creates its own
        assert second.json()["orphan_refs_created"] == 1

        refs = (
            (
                await db_session.execute(
                    select(TestRef).where(TestRef.lazyaf_test_id == shared_id)
                )
            )
            .scalars()
            .all()
        )
        assert {r.repo_id for r in refs} == {
            step_ctx["repo_id"],
            other_step_ctx["repo_id"],
        }

        by_repo = {r.repo_id: r for r in refs}
        run_a = (
            await db_session.execute(
                select(TestRun).where(
                    TestRun.test_ref_id == by_repo[step_ctx["repo_id"]].id
                )
            )
        ).scalar_one()
        run_b = (
            await db_session.execute(
                select(TestRun).where(
                    TestRun.test_ref_id == by_repo[other_step_ctx["repo_id"]].id
                )
            )
        ).scalar_one()
        assert run_a.status == "passed"
        assert run_b.status == "failed"

    async def test_ingestion_never_rehomes_another_repos_ref(
        self, client, db_session, step_ctx, other_step_ctx
    ):
        """A registered ACTIVE ref in repo B is untouched when repo A ingests
        the same marker id - B keeps its repo, status and file_path."""
        registered = TestRef(
            lazyaf_test_id="registered.elsewhere",
            repo_id=other_step_ctx["repo_id"],
            file_path="tests/b.py",
            status="active",
        )
        db_session.add(registered)
        await db_session.commit()

        response = await _post(
            client, step_ctx, manifest(result_entry("registered.elsewhere"))
        )
        assert response.json()["orphan_refs_created"] == 1

        await db_session.refresh(registered)
        assert registered.repo_id == other_step_ctx["repo_id"]
        assert registered.status == "active"
        assert registered.file_path == "tests/b.py"
        runs_for_b = (
            (
                await db_session.execute(
                    select(TestRun).where(TestRun.test_ref_id == registered.id)
                )
            )
            .scalars()
            .all()
        )
        assert runs_for_b == []


class TestFilePathConvention:
    """Contract #3: repo-root-relative paths, everywhere."""

    async def test_absolute_path_does_not_overwrite_a_seeded_path(
        self, client, db_session, step_ctx
    ):
        """A differently-rooted path (a runner shipping absolute paths) is
        refused - the seeded repo-root-relative path survives."""
        ref = TestRef(
            lazyaf_test_id="seeded.path",
            repo_id=step_ctx["repo_id"],
            file_path="tdd/integration/api/test_seeded.py",
            status="active",
        )
        db_session.add(ref)
        await db_session.commit()

        response = await _post(
            client,
            step_ctx,
            manifest(
                result_entry(
                    "seeded.path",
                    file_path="/workspace/tdd/integration/api/test_seeded.py",
                )
            ),
        )
        assert response.status_code == 200

        await db_session.refresh(ref)
        assert ref.file_path == "tdd/integration/api/test_seeded.py"

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/abs/tests/test_x.py",
            "C:/repo/tests/test_x.py",
            "../outside/tests/test_x.py",
        ],
    )
    async def test_non_repo_relative_paths_are_refused_on_create(
        self, client, db_session, step_ctx, bad_path
    ):
        """An auto-created ref would rather carry no path than a wrong one."""
        response = await _post(
            client, step_ctx, manifest(result_entry("bad.path", file_path=bad_path))
        )
        assert response.status_code == 200

        ref = (
            await db_session.execute(
                select(TestRef).where(TestRef.lazyaf_test_id == "bad.path")
            )
        ).scalar_one()
        assert ref.file_path is None

    async def test_windows_separators_are_normalized(
        self, client, db_session, step_ctx
    ):
        """A Windows runner's backslashes are the same repo-root-relative
        path, not a different one."""
        await _post(
            client,
            step_ctx,
            manifest(
                result_entry(
                    "win.path", file_path=".\\tdd\\integration\\test_win.py"
                )
            ),
        )
        ref = (
            await db_session.execute(
                select(TestRef).where(TestRef.lazyaf_test_id == "win.path")
            )
        ).scalar_one()
        assert ref.file_path == "tdd/integration/test_win.py"


class TestConcurrentIngestion:
    async def test_racing_ref_insert_does_not_lose_the_manifest(
        self, client, db_session, step_ctx, monkeypatch
    ):
        """Two runners shipping manifests for the same new lazyaf_test_id at
        once: the loser of the (repo_id, lazyaf_test_id) insert race must
        recover (rollback + re-select, the idempotency.py idiom) instead of
        500ing an entire manifest away."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.ext.asyncio import AsyncSession

        original_flush = AsyncSession.flush
        state = {"raised": False}

        async def racy_flush(self, *args, **kwargs):
            pending_refs = [o for o in self.sync_session.new if isinstance(o, TestRef)]
            if pending_refs and not state["raised"]:
                state["raised"] = True
                raise IntegrityError(
                    "INSERT INTO test_refs",
                    {},
                    Exception(
                        "UNIQUE constraint failed: "
                        "test_refs.repo_id, test_refs.lazyaf_test_id"
                    ),
                )
            return await original_flush(self, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "flush", racy_flush)

        response = await _post(client, step_ctx, manifest(result_entry("raced.id")))

        assert state["raised"] is True, "the race was never simulated"
        assert response.status_code == 200
        assert response.json()["test_runs_created"] == 1

        monkeypatch.undo()
        ref = (
            await db_session.execute(
                select(TestRef).where(TestRef.lazyaf_test_id == "raced.id")
            )
        ).scalar_one()
        run = (
            await db_session.execute(
                select(TestRun).where(TestRun.test_ref_id == ref.id)
            )
        ).scalar_one()
        assert run.status == "passed"
