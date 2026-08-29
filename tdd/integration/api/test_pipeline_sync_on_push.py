"""
Integration tests for pipeline-definition sync-on-push + trigger dedup (Phase 0a).

Verifies via the ASGI client and a real internal bare repo that:
- a push containing a new .lazyaf/pipelines yaml materializes a platform
  pipeline WITH triggers, and that same push triggers a run
- a push that modifies the yaml means the run triggered by that push uses
  the NEW definition
- removing the yaml clears triggers on the materialized row (row kept)
- a yaml that EXISTS but is empty/unparseable keeps definition AND triggers
- pushes to non-default branches do not re-sync the definition
- two rapid identical push events produce exactly ONE PipelineRun
- a failed run start releases the dedup key so a retry push can fire
- a push that does not touch .lazyaf/pipelines short-circuits the sync
- the manual run-by-name endpoint still works on the default branch and
  refuses (400) to materialize from any other branch

Pushes are simulated the way the platform's own git server does it: commits
land in the internal bare repo (push_from_local), then the internal
push-event endpoint fires - the same trigger_service.on_push path the
git-receive-pack handler calls.
"""
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import assert_status_code


PIPELINE_YAML_V1 = """name: repo-ci
description: Repo CI pipeline
triggers:
  - type: push
    config:
      branches: ["{branch}"]
steps:
  - name: Test
    type: script
    config:
      command: echo "v1 tests"
"""

PIPELINE_YAML_V2 = """name: repo-ci
description: Repo CI pipeline v2
triggers:
  - type: push
    config:
      branches: ["{branch}"]
steps:
  - name: Lint
    type: script
    config:
      command: echo "v2 lint"
  - name: Test
    type: script
    config:
      command: echo "v2 tests"
"""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _reset_dedup():
    """Dedup state is process-local; keep tests isolated from each other."""
    from app.services.trigger_service import reset_trigger_dedup

    reset_trigger_dedup()
    yield
    reset_trigger_dedup()


@pytest_asyncio.fixture(autouse=True)
async def _sync_sessions_use_test_db(async_engine, monkeypatch):
    """Point the app's sessionmaker at this test's engine.

    sync_repo_pipelines opens its OWN session from app.database's
    sessionmaker (so a failed sync cannot poison the request session); in
    tests that sessionmaker must produce sessions on the same in-memory
    database the API client uses.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import app.database as app_database

    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(app_database, "async_session", factory)


@pytest_asyncio.fixture
async def pushed_ci_repo(client, clean_git_repos, clean_job_queue):
    """Ingest a repo whose first commit carries a triggered CI yaml.

    Yields (repo dict, local repo path, default branch). The local working
    copy stays alive so tests can make follow-up commits and push them.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "ci-repo"
        repo_path.mkdir()

        _git(repo_path, "init")
        _git(repo_path, "config", "user.email", "test@test.com")
        _git(repo_path, "config", "user.name", "Test")

        (repo_path / "README.md").write_text("# CI Repo")
        branch = _git(repo_path, "branch", "--show-current")

        pipelines_dir = repo_path / ".lazyaf" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "repo-ci.yaml").write_text(
            PIPELINE_YAML_V1.format(branch=branch)
        )

        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "add CI definition")

        response = await client.post(
            "/api/repos/ingest",
            json={"path": str(repo_path), "name": "ci-repo"},
        )
        assert response.status_code == 201, f"Failed to ingest repo: {response.text}"
        repo_id = response.json()["id"]

        repo_response = await client.get(f"/api/repos/{repo_id}")
        repo = repo_response.json()

        yield repo, repo_path, repo["default_branch"]


async def _push_and_fire_event(client, clean_git_repos, repo, repo_path, branch):
    """Push local commits into the bare repo and fire the push event."""
    old_sha = clean_git_repos.get_branch_commit(repo["id"], branch) or ""
    result = clean_git_repos.push_from_local(repo["id"], str(repo_path))
    assert result["success"], result
    new_sha = _git(repo_path, "rev-parse", "HEAD")

    response = await client.post(
        f"/git/{repo['id']}.git/_internal/push-event",
        json={"branch": branch, "new_sha": new_sha, "old_sha": old_sha},
    )
    assert_status_code(response, 200)
    return response.json(), new_sha


async def _get_materialized_pipeline(client, repo_id, name="[repo] repo-ci"):
    """Fetch the materialized platform pipeline row via the API, or None."""
    response = await client.get(f"/api/repos/{repo_id}/pipelines")
    assert_status_code(response, 200)
    for pipeline in response.json():
        if pipeline["name"] == name:
            return pipeline
    return None


class TestSyncOnPushCreatesPipeline:
    """(a) Push with a new pipelines yaml materializes row + triggers."""

    async def test_push_event_materializes_pipeline_with_triggers(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo
        head_sha = _git(repo_path, "rev-parse", "HEAD")

        # No materialized pipeline exists before any push event fires
        assert await _get_materialized_pipeline(client, repo["id"]) is None

        response = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        assert_status_code(response, 200)
        result = response.json()

        pipeline = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline is not None
        assert pipeline["description"] == "Repo CI pipeline"
        assert len(pipeline["steps"]) == 1
        assert pipeline["triggers"] == [
            {
                "type": "push",
                "config": {"branches": [branch]},
                "enabled": True,
                "on_pass": "nothing",
                "on_fail": "nothing",
            }
        ]

        # The SAME push that introduced the yaml triggered its run
        assert result["triggered_runs"] == 1
        runs_response = await client.get(f"/api/pipelines/{pipeline['id']}/runs")
        assert_status_code(runs_response, 200)
        runs = runs_response.json()
        assert len(runs) == 1
        assert runs[0]["trigger_type"] == "push"
        assert runs[0]["trigger_context"]["commit_sha"] == head_sha

    async def test_push_to_other_branch_does_not_sync(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        """CI definition follows the trunk: feature-branch pushes don't sync."""
        repo, repo_path, branch = pushed_ci_repo

        # Change the yaml on a feature branch and push it
        _git(repo_path, "checkout", "-b", "feature/tweak-ci")
        yaml_path = repo_path / ".lazyaf" / "pipelines" / "repo-ci.yaml"
        yaml_path.write_text(PIPELINE_YAML_V2.format(branch=branch))
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "tweak CI on feature branch")

        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, "feature/tweak-ci"
        )

        # The feature-branch definition was NOT materialized
        assert await _get_materialized_pipeline(client, repo["id"]) is None
        assert result["triggered_runs"] == 0


class TestSyncOnPushRefreshesPipeline:
    """(b) The run triggered by a modifying push uses the NEW definition."""

    async def test_modifying_push_runs_new_definition(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo

        # First push event materializes v1
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        pipeline_v1 = await _get_materialized_pipeline(client, repo["id"])
        assert len(pipeline_v1["steps"]) == 1

        # Modify the yaml and push
        yaml_path = repo_path / ".lazyaf" / "pipelines" / "repo-ci.yaml"
        yaml_path.write_text(PIPELINE_YAML_V2.format(branch=branch))
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "expand CI to two steps")

        result, new_sha = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )
        assert result["triggered_runs"] == 1

        # Same row, refreshed definition
        pipeline_v2 = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline_v2["id"] == pipeline_v1["id"]
        assert pipeline_v2["description"] == "Repo CI pipeline v2"
        assert [s["name"] for s in pipeline_v2["steps"]] == ["Lint", "Test"]

        # The run started by that same push uses the NEW two-step definition
        run_response = await client.get(
            f"/api/pipeline-runs/{result['run_ids'][0]}"
        )
        assert_status_code(run_response, 200)
        run = run_response.json()
        assert run["steps_total"] == 2
        assert run["trigger_context"]["commit_sha"] == new_sha


class TestSyncOnPushClearsRemovedYaml:
    """(c) yaml removed from the repo -> triggers cleared, row kept."""

    async def test_removed_yaml_clears_triggers_keeps_row(
        self, client, clean_git_repos, pushed_ci_repo, db_session
    ):
        from app.models import Pipeline

        repo, repo_path, branch = pushed_ci_repo

        head_sha = _git(repo_path, "rev-parse", "HEAD")
        await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        pipeline = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline["triggers"] != []

        # Remove the yaml and push
        _git(repo_path, "rm", "-r", ".lazyaf")
        _git(repo_path, "commit", "-m", "drop CI definition")
        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )

        # No run triggered by the removing push
        assert result["triggered_runs"] == 0

        # Row survives (run history hangs off it) but triggers are cleared
        pipeline_after = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline_after is not None
        assert pipeline_after["id"] == pipeline["id"]
        assert pipeline_after["triggers"] == []
        assert pipeline_after["steps"] == pipeline["steps"]

        # And it stays in the DB
        db_result = await db_session.execute(
            select(Pipeline).where(Pipeline.id == pipeline["id"])
        )
        assert db_result.scalar_one_or_none() is not None

    async def test_invalid_yaml_is_skipped_but_valid_files_sync(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        """A broken CI file must not break the push or the other files."""
        repo, repo_path, branch = pushed_ci_repo

        pipelines_dir = repo_path / ".lazyaf" / "pipelines"
        (pipelines_dir / "broken.yaml").write_text(
            "name: broken\ntriggers: not-a-list\nsteps: []\n"
        )
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "add broken CI file")

        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )

        # Valid file synced and triggered; broken one was skipped entirely
        assert result["triggered_runs"] == 1
        assert await _get_materialized_pipeline(client, repo["id"]) is not None
        assert await _get_materialized_pipeline(
            client, repo["id"], name="[repo] broken"
        ) is None


class TestBrokenYamlKeepsTriggers:
    """A yaml that EXISTS but is broken keeps definition AND triggers.

    Only files truly absent from the pushed tree may clear triggers (the
    removal case above); a parse failure must never wipe them.
    """

    async def _materialize_v1(self, client, repo, repo_path, branch):
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        response = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        assert_status_code(response, 200)
        pipeline = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline["triggers"] != []
        return pipeline

    async def test_unparseable_yaml_leaves_triggers_intact(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo
        pipeline = await self._materialize_v1(client, repo, repo_path, branch)

        yaml_path = repo_path / ".lazyaf" / "pipelines" / "repo-ci.yaml"
        yaml_path.write_text("{ this is not: valid yaml")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "break the CI yaml")
        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )

        pipeline_after = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline_after["triggers"] == pipeline["triggers"]
        assert pipeline_after["steps"] == pipeline["steps"]
        # The still-armed trigger fired for this very push
        assert result["triggered_runs"] == 1

    async def test_empty_yaml_leaves_triggers_intact(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo
        pipeline = await self._materialize_v1(client, repo, repo_path, branch)

        yaml_path = repo_path / ".lazyaf" / "pipelines" / "repo-ci.yaml"
        yaml_path.write_text("")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "truncate the CI yaml")
        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )

        pipeline_after = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline_after["triggers"] == pipeline["triggers"]
        assert pipeline_after["steps"] == pipeline["steps"]
        assert result["triggered_runs"] == 1


class TestSyncShortCircuit:
    """A push that does not touch .lazyaf/pipelines skips definition sync."""

    async def test_untouched_pipelines_dir_does_no_upserts(
        self, client, clean_git_repos, pushed_ci_repo, monkeypatch
    ):
        import app.services.trigger_service as trigger_service_module

        repo, repo_path, branch = pushed_ci_repo
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        before = await _get_materialized_pipeline(client, repo["id"])
        assert before is not None

        # Spy on the shared upsert the sync path uses
        calls = []
        real_upsert = trigger_service_module.upsert_materialized_pipeline

        async def spying_upsert(*args, **kwargs):
            calls.append((args, kwargs))
            return await real_upsert(*args, **kwargs)

        monkeypatch.setattr(
            trigger_service_module, "upsert_materialized_pipeline", spying_upsert
        )

        # Push a commit that leaves .lazyaf/pipelines untouched
        (repo_path / "notes.txt").write_text("no ci change")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "unrelated change")
        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )

        # Sync short-circuited: no upserts; the trigger still matched
        assert calls == []
        assert result["triggered_runs"] == 1
        after = await _get_materialized_pipeline(client, repo["id"])
        for field in ("id", "description", "steps", "triggers"):
            assert after[field] == before[field]


class TestPushTriggerDedup:
    """(d) Two rapid identical push events -> exactly one PipelineRun."""

    async def test_duplicate_push_events_produce_one_run(
        self, client, clean_git_repos, pushed_ci_repo, db_session
    ):
        from app.models.pipeline import PipelineRun

        repo, repo_path, branch = pushed_ci_repo
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        event = {"branch": branch, "new_sha": head_sha, "old_sha": ""}

        response1 = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event", json=event
        )
        response2 = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event", json=event
        )
        assert_status_code(response1, 200)
        assert_status_code(response2, 200)

        assert response1.json()["triggered_runs"] == 1
        assert response2.json()["triggered_runs"] == 0

        pipeline = await _get_materialized_pipeline(client, repo["id"])
        db_result = await db_session.execute(
            select(PipelineRun).where(PipelineRun.pipeline_id == pipeline["id"])
        )
        assert len(list(db_result.scalars().all())) == 1

    async def test_new_commit_is_not_deduplicated(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        """Dedup keys on the sha: a real second push still runs."""
        repo, repo_path, branch = pushed_ci_repo

        head_sha = _git(repo_path, "rev-parse", "HEAD")
        await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )

        (repo_path / "file.txt").write_text("change")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-m", "a real change")
        result, _ = await _push_and_fire_event(
            client, clean_git_repos, repo, repo_path, branch
        )
        assert result["triggered_runs"] == 1

    async def test_reset_hook_allows_retrigger(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        """The test-mode reset hook clears dedup state for identical events."""
        from app.services.trigger_service import reset_trigger_dedup

        repo, repo_path, branch = pushed_ci_repo
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        event = {"branch": branch, "new_sha": head_sha, "old_sha": ""}

        response1 = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event", json=event
        )
        assert response1.json()["triggered_runs"] == 1

        reset_trigger_dedup()

        response2 = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event", json=event
        )
        assert response2.json()["triggered_runs"] == 1


class TestDedupKeyLifecycle:
    """A failed start releases the dedup key so a retry push can fire."""

    async def test_failed_start_then_retry_within_window_creates_run(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        from app.services.pipeline_executor import pipeline_executor

        repo, repo_path, branch = pushed_ci_repo
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        event = {"branch": branch, "new_sha": head_sha, "old_sha": ""}

        original_start = pipeline_executor.start_pipeline

        async def failing_start(*args, **kwargs):
            raise RuntimeError("runner offline")

        # NOT the monkeypatch fixture: the executor must come back BEFORE the
        # retry event inside this test, and monkeypatch.undo() would also
        # undo unrelated fixture patches.
        pipeline_executor.start_pipeline = failing_start
        try:
            response1 = await client.post(
                f"/git/{repo['id']}.git/_internal/push-event", json=event
            )
        finally:
            pipeline_executor.start_pipeline = original_start

        assert_status_code(response1, 200)
        assert response1.json()["triggered_runs"] == 0

        # Retry inside the dedup window: the key was released, so this fires
        response2 = await client.post(
            f"/git/{repo['id']}.git/_internal/push-event", json=event
        )
        assert_status_code(response2, 200)
        assert response2.json()["triggered_runs"] == 1


class TestManualRunByNameStillWorks:
    """(e) The manual run-by-name endpoint is unchanged for callers."""

    async def test_manual_run_still_works_and_syncs_triggers(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo

        response = await client.post(
            f"/api/repos/{repo['id']}/lazyaf/pipelines/repo-ci/run",
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "running"

        run_response = await client.get(f"/api/pipeline-runs/{result['run_id']}")
        assert_status_code(run_response, 200)
        assert run_response.json()["trigger_type"] == "manual"

        # Materialization through the manual path now carries triggers too
        pipeline = await _get_materialized_pipeline(client, repo["id"])
        assert pipeline["id"] == result["pipeline_id"]
        assert pipeline["triggers"] != []

    async def test_repo_pipeline_listing_exposes_triggers(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        """GET .../lazyaf/pipelines surfaces the yaml's triggers."""
        repo, _, branch = pushed_ci_repo

        response = await client.get(f"/api/repos/{repo['id']}/lazyaf/pipelines")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert len(pipelines) == 1
        assert pipelines[0]["triggers"][0]["type"] == "push"
        assert pipelines[0]["triggers"][0]["config"] == {"branches": [branch]}


class TestManualRunBranchScoping:
    """Manual run-by-name materializes only from the trunk."""

    async def test_non_default_branch_returns_400_and_row_untouched(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, repo_path, branch = pushed_ci_repo

        # Materialize v1 from the trunk first
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        await client.post(
            f"/git/{repo['id']}.git/_internal/push-event",
            json={"branch": branch, "new_sha": head_sha, "old_sha": ""},
        )
        before = await _get_materialized_pipeline(client, repo["id"])
        assert before is not None

        response = await client.post(
            f"/api/repos/{repo['id']}/lazyaf/pipelines/repo-ci/run",
            params={"branch": "feature/other-ci"},
        )
        assert_status_code(response, 400)
        detail = response.json()["detail"]
        # The error names both the requested branch and the trunk
        assert "feature/other-ci" in detail
        assert branch in detail

        after = await _get_materialized_pipeline(client, repo["id"])
        for field in ("id", "description", "steps", "triggers"):
            assert after[field] == before[field]

    async def test_explicit_default_branch_still_runs(
        self, client, clean_git_repos, pushed_ci_repo
    ):
        repo, _, branch = pushed_ci_repo

        response = await client.post(
            f"/api/repos/{repo['id']}/lazyaf/pipelines/repo-ci/run",
            params={"branch": branch},
        )
        assert_status_code(response, 200)
        assert response.json()["status"] == "running"
