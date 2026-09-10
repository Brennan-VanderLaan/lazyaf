"""
Integration tests for Repos API endpoints.

These tests verify the full request/response cycle through the FastAPI
application with a real (in-memory) database.
"""
import sys
from pathlib import Path

import pytest

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload, repo_update_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


class TestListRepos:
    """Tests for GET /api/repos endpoint."""

    async def test_list_repos_empty(self, client):
        """Returns empty list when no repos exist."""
        response = await client.get("/api/repos")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_repos_with_data(self, client):
        """Returns all repos when they exist."""
        # Create two repos
        await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        await client.post("/api/repos", json=repo_create_payload(name="Repo2"))

        response = await client.get("/api/repos")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_repos_returns_repo_fields(self, client):
        """Returns repos with all expected fields."""
        payload = repo_create_payload(
            name="TestRepo",
            remote_url="https://github.com/org/test.git",
        )
        await client.post("/api/repos", json=payload)

        response = await client.get("/api/repos")
        repos = response.json()
        assert len(repos) == 1
        repo = repos[0]
        assert "id" in repo
        assert repo["name"] == "TestRepo"
        assert repo["remote_url"] == "https://github.com/org/test.git"
        assert "is_ingested" in repo
        assert "internal_git_url" in repo
        assert "created_at" in repo


class TestCreateRepo:
    """Tests for POST /api/repos endpoint."""

    async def test_create_repo_minimal(self, client):
        """Creates repo with minimal required fields."""
        payload = repo_create_payload(name="MinimalRepo")

        response = await client.post("/api/repos", json=payload)
        result = assert_created_response(response, {"name": "MinimalRepo"})
        assert result["default_branch"] == "main"
        assert result["is_ingested"] is False
        assert "internal_git_url" in result

    async def test_create_repo_full(self, client):
        """Creates repo with all fields specified."""
        payload = {
            "name": "FullRepo",
            "remote_url": "https://github.com/org/full.git",
            "default_branch": "dev",
        }

        response = await client.post("/api/repos", json=payload)
        result = assert_created_response(response, {"name": "FullRepo"})
        assert result["remote_url"] == "https://github.com/org/full.git"
        assert result["default_branch"] == "dev"
        assert "internal_git_url" in result

    async def test_create_repo_generates_uuid(self, client):
        """Creates repo with auto-generated UUID."""
        payload = repo_create_payload()

        response = await client.post("/api/repos", json=payload)
        result = response.json()
        assert "id" in result
        assert len(result["id"]) == 36  # UUID format

    async def test_create_repo_missing_name_fails(self, client):
        """Fails to create repo without name."""
        payload = {"remote_url": "https://github.com/org/test.git"}

        response = await client.post("/api/repos", json=payload)
        assert_status_code(response, 422)  # Validation error

    async def test_create_repo_name_only_succeeds(self, client):
        """Creates repo with only name (all other fields have defaults)."""
        payload = {"name": "NameOnlyRepo"}

        response = await client.post("/api/repos", json=payload)
        assert_status_code(response, 201)
        result = response.json()
        assert result["name"] == "NameOnlyRepo"
        assert result["default_branch"] == "main"


class TestGetRepo:
    """Tests for GET /api/repos/{repo_id} endpoint."""

    async def test_get_repo_exists(self, client):
        """Returns repo when it exists."""
        create_payload = repo_create_payload(name="GetTestRepo")
        create_response = await client.post("/api/repos", json=create_payload)
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": repo_id, "name": "GetTestRepo"})

    async def test_get_repo_not_found(self, client):
        """Returns 404 when repo does not exist."""
        response = await client.get("/api/repos/nonexistent-id-12345")
        assert_not_found(response, "Repo")

    async def test_get_repo_returns_all_fields(self, client):
        """Returns repo with complete field set."""
        create_payload = {
            "name": "CompleteRepo",
            "remote_url": "https://github.com/org/complete.git",
            "default_branch": "develop",
        }
        create_response = await client.post("/api/repos", json=create_payload)
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}")
        result = response.json()
        assert result["name"] == "CompleteRepo"
        assert result["remote_url"] == "https://github.com/org/complete.git"
        assert result["default_branch"] == "develop"
        assert "is_ingested" in result
        assert "internal_git_url" in result
        assert "created_at" in result


class TestUpdateRepo:
    """Tests for PATCH /api/repos/{repo_id} endpoint."""

    async def test_update_repo_name(self, client):
        """Updates repo name only."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="OriginalName"),
        )
        repo_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/repos/{repo_id}",
            json=repo_update_payload(name="UpdatedName"),
        )
        result = assert_updated_response(response, {"name": "UpdatedName"})
        assert result["id"] == repo_id

    async def test_update_repo_multiple_fields(self, client):
        """Updates multiple repo fields at once."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(),
        )
        repo_id = create_response.json()["id"]

        update_data = {
            "name": "NewName",
            "remote_url": "https://github.com/org/updated.git",
            "default_branch": "develop",
        }
        response = await client.patch(f"/api/repos/{repo_id}", json=update_data)
        result = response.json()
        assert result["name"] == "NewName"
        assert result["remote_url"] == "https://github.com/org/updated.git"
        assert result["default_branch"] == "develop"

    async def test_update_repo_not_found(self, client):
        """Returns 404 when updating non-existent repo."""
        response = await client.patch(
            "/api/repos/nonexistent-id",
            json=repo_update_payload(name="NewName"),
        )
        assert_not_found(response, "Repo")

    async def test_update_repo_empty_body(self, client):
        """Accepts empty update body (no changes)."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="UnchangedRepo"),
        )
        repo_id = create_response.json()["id"]

        response = await client.patch(f"/api/repos/{repo_id}", json={})
        assert_status_code(response, 200)
        assert response.json()["name"] == "UnchangedRepo"


class TestDeleteRepo:
    """Tests for DELETE /api/repos/{repo_id} endpoint."""

    async def test_delete_repo_exists(self, client):
        """Deletes repo when it exists."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(),
        )
        repo_id = create_response.json()["id"]

        response = await client.delete(f"/api/repos/{repo_id}")
        assert_deleted_response(response)

        # Verify repo is gone
        get_response = await client.get(f"/api/repos/{repo_id}")
        assert_not_found(get_response, "Repo")

    async def test_delete_repo_not_found(self, client):
        """Returns 404 when deleting non-existent repo."""
        response = await client.delete("/api/repos/nonexistent-id")
        assert_not_found(response, "Repo")

    async def test_delete_repo_removes_from_list(self, client):
        """Deleted repo no longer appears in list."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Keep"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Delete"))

        # Delete one
        await client.delete(f"/api/repos/{resp2.json()['id']}")

        # Verify only one remains
        list_response = await client.get("/api/repos")
        repos = list_response.json()
        assert len(repos) == 1
        assert repos[0]["name"] == "Keep"


class TestDeleteRepoWithLiveWork:
    """DELETE must refuse while the repo has work in flight (QA2-08).

    Deleting a repo removes its git storage and cascade-deletes its Cards and
    Pipelines. Mid-run that strands a Job at 'running' forever pointing at a
    card that no longer exists, and erases a live PipelineRun out from under
    the executor.
    """

    async def _repo(self, client, name="LiveWorkRepo"):
        response = await client.post("/api/repos", json=repo_create_payload(name=name))
        return response.json()["id"]

    async def test_delete_refused_while_a_pipeline_run_is_live(
        self, client, db_session
    ):
        from app.models import Pipeline, PipelineRun

        repo_id = await self._repo(client)
        pipeline = Pipeline(repo_id=repo_id, name="Live Pipeline")
        db_session.add(pipeline)
        await db_session.flush()
        run = PipelineRun(pipeline_id=pipeline.id, status="running")
        db_session.add(run)
        await db_session.commit()

        response = await client.delete(f"/api/repos/{repo_id}")
        assert_status_code(response, 409)

        detail = response.json()["detail"]
        assert run.id in detail, f"refusal does not name the live run: {detail}"
        assert "cancel" in detail.lower(), f"refusal gives no way forward: {detail}"

        # Repo and run both survive.
        assert_status_code(await client.get(f"/api/repos/{repo_id}"), 200)
        assert_status_code(await client.get(f"/api/pipeline-runs/{run.id}"), 200)

    async def test_delete_refused_while_a_job_is_running(self, client, db_session):
        from app.models import Card, Job

        repo_id = await self._repo(client, name="LiveJobRepo")
        card = Card(repo_id=repo_id, title="Live card", status="in_progress")
        db_session.add(card)
        await db_session.flush()
        job = Job(card_id=card.id, status="running")
        db_session.add(job)
        await db_session.commit()

        response = await client.delete(f"/api/repos/{repo_id}")
        assert_status_code(response, 409)

        detail = response.json()["detail"]
        assert job.id in detail, f"refusal does not name the live job: {detail}"
        assert "cancel" in detail.lower(), f"refusal gives no way forward: {detail}"

        # The job is still 'running' against a card and repo that still exist -
        # nothing has been stranded.
        assert_status_code(await client.get(f"/api/repos/{repo_id}"), 200)

    async def test_finished_work_does_not_block_delete(self, client, db_session):
        from app.models import Card, Job, Pipeline, PipelineRun

        repo_id = await self._repo(client, name="SettledRepo")
        pipeline = Pipeline(repo_id=repo_id, name="Done Pipeline")
        card = Card(repo_id=repo_id, title="Done card", status="done")
        db_session.add_all([pipeline, card])
        await db_session.flush()
        db_session.add_all([
            PipelineRun(pipeline_id=pipeline.id, status="passed"),
            Job(card_id=card.id, status="completed"),
        ])
        await db_session.commit()

        response = await client.delete(f"/api/repos/{repo_id}")
        assert_deleted_response(response)


# -----------------------------------------------------------------------------
# LANE 3: "I need to be able to see which branches are available in lazyaf".
#
# Branches live in the internal repo's REFS, not in the Repo row, so every ref
# write used to reach the UI as silence: the sidebar went on saying "No
# branches yet. Push your repo to get started." after the push that answered
# it, and only F5 fixed it. These pin both halves of the fix - the listing
# itself, and the `repo_refs_changed` frame that keeps an open page honest.
# -----------------------------------------------------------------------------

@pytest.fixture
def captured_frames(monkeypatch):
    """Record every WS frame the manager broadcasts during one test."""
    from app.services.websocket import manager

    frames: list[tuple] = []
    original = manager.broadcast

    async def _spy(message_type, payload):
        frames.append((message_type, payload))
        return await original(message_type, payload)

    monkeypatch.setattr(manager, "broadcast", _spy)
    return frames


def refs_frames(frames) -> list[dict]:
    """Just the repo_refs_changed payloads, in order."""
    return [
        payload for message_type, payload in frames
        if message_type == "repo_refs_changed"
    ]


def make_branch(repo_id: str, branch: str, parent: str | None = None) -> str:
    """Put a real branch on the internal git server. Returns its tip sha."""
    from dulwich.objects import Blob, Commit, Tree

    from app.services.git_server import git_repo_manager

    repo = git_repo_manager.get_repo(repo_id)
    assert repo is not None, f"repo {repo_id} is not on the internal git server"

    blob = Blob.from_string(f"content of {branch}\n".encode())
    tree = Tree()
    tree.add(b"work.txt", 0o100644, blob.id)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"LazyAF QA <qa@lazyaf.test>"
    commit.commit_time = commit.author_time = 1756000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = f"work on {branch}".encode()
    if parent:
        commit.parents = [parent.encode("ascii")]

    repo.object_store.add_object(blob)
    repo.object_store.add_object(tree)
    repo.object_store.add_object(commit)
    repo.refs[f"refs/heads/{branch}".encode()] = commit.id
    return commit.id.decode("ascii")


class TestBranchListing:
    """GET /api/repos/{id}/branches - what the sidebar renders."""

    async def test_ingested_repo_with_no_pushes_lists_no_branches(
        self, client, clean_git_repos
    ):
        """The state a brand new repo sits in, and it is NOT an error.

        Ingesting without a path creates the bare repo and marks it ingested;
        nothing has been pushed yet. This has to answer 200 with an empty
        list - a 4xx/5xx here is what let the UI render a failed listing and
        an empty repo identically, and then tell the user to push a repo they
        had already pushed.
        """
        created = await client.post(
            "/api/repos/ingest",
            json={"name": "no-pushes-yet", "default_branch": "main"},
        )
        assert_status_code(created, 201)
        repo_id = created.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/branches")
        assert_status_code(response, 200)
        body = response.json()
        assert body["branches"] == []
        assert body["total"] == 0

    async def test_listing_names_default_and_agent_branches_with_their_tips(
        self, client, ingested_repo, clean_git_repos
    ):
        """Every fact the branch list renders comes from this one response."""
        repo_id = ingested_repo["id"]
        default_branch = ingested_repo["default_branch"]

        default_tip = clean_git_repos.get_branch_commit(repo_id, default_branch)
        agent_tip = make_branch(repo_id, "lazyaf/ab12cd34", parent=default_tip)

        response = await client.get(f"/api/repos/{repo_id}/branches")
        assert_status_code(response, 200)
        by_name = {b["name"]: b for b in response.json()["branches"]}

        assert set(by_name) == {default_branch, "lazyaf/ab12cd34"}
        assert by_name[default_branch]["is_default"] is True
        assert by_name[default_branch]["is_lazyaf"] is False
        # The `lazyaf/` prefix is how a human tells agent work apart from
        # their own - it is the whole reason the flag exists.
        assert by_name["lazyaf/ab12cd34"]["is_lazyaf"] is True
        assert by_name["lazyaf/ab12cd34"]["is_default"] is False
        # The tip commits, or a row can only say a branch exists, never where
        # it is.
        assert by_name[default_branch]["commit"] == default_tip
        assert by_name["lazyaf/ab12cd34"]["commit"] == agent_tip

    async def test_unknown_repo_is_a_404_not_an_empty_list(self, client):
        """An empty list is a fact about a repo. A missing repo is not that."""
        assert_not_found(await client.get("/api/repos/does-not-exist/branches"))


class TestRefsChangedBroadcast:
    """Every path that writes a ref announces the new listing (LANE 3).

    Without this an open page shows the branches it happened to fetch once,
    forever. The reported symptom was a push that changed nothing on screen.
    """

    async def test_the_frame_carries_exactly_what_the_endpoint_returns(
        self, client, ingested_repo, clean_git_repos, captured_frames
    ):
        """One source of truth: frame and fetch are built by one function.

        A page hydrated by GET and a page updated by the frame must not be
        able to disagree about which branches exist, so this compares them
        field for field rather than spot-checking a name.
        """
        repo_id = ingested_repo["id"]
        default_tip = clean_git_repos.get_branch_commit(
            repo_id, ingested_repo["default_branch"]
        )
        make_branch(repo_id, "lazyaf/frame-check", parent=default_tip)

        # Any ref-writing endpoint will do; sync is the cheapest.
        assert_status_code(await client.post(f"/api/repos/{repo_id}/sync"), 200)

        frames = refs_frames(captured_frames)
        assert frames, "a ref write broadcast no repo_refs_changed frame"
        frame = frames[-1]

        fetched = (await client.get(f"/api/repos/{repo_id}/branches")).json()
        assert frame["repo_id"] == repo_id
        assert {k: v for k, v in frame.items() if k != "repo_id"} == fetched

    async def test_deleting_a_branch_broadcasts_the_shorter_list(
        self, client, ingested_repo, clean_git_repos, captured_frames
    ):
        repo_id = ingested_repo["id"]
        default_tip = clean_git_repos.get_branch_commit(
            repo_id, ingested_repo["default_branch"]
        )
        make_branch(repo_id, "lazyaf/going-away", parent=default_tip)

        response = await client.delete(
            f"/api/repos/{repo_id}/branches/lazyaf/going-away"
        )
        assert_status_code(response, 200)

        frames = refs_frames(captured_frames)
        assert frames, (
            "deleting a branch broadcast nothing - open pages keep offering it"
        )
        names = [b["name"] for b in frames[-1]["branches"]]
        assert "lazyaf/going-away" not in names

    async def test_a_refused_delete_broadcasts_nothing(
        self, client, ingested_repo, captured_frames
    ):
        """A refusal changed no ref. Announcing one would be a lie."""
        repo_id = ingested_repo["id"]

        response = await client.delete(
            f"/api/repos/{repo_id}/branches/lazyaf/never-existed"
        )
        assert response.status_code == 400, response.text
        assert refs_frames(captured_frames) == []

    async def test_the_push_endpoint_announces_the_branch_it_just_created(
        self, client, ingested_repo, clean_git_repos, captured_frames
    ):
        """The owner's exact path: a push, then the list he is looking at.

        The git server calls this internal endpoint after the refs are on
        disk, so the branch created here must be IN the frame it emits - not
        in some later one.
        """
        repo_id = ingested_repo["id"]
        default_tip = clean_git_repos.get_branch_commit(
            repo_id, ingested_repo["default_branch"]
        )
        pushed_sha = make_branch(repo_id, "lazyaf/just-pushed", parent=default_tip)

        response = await client.post(
            f"/git/{repo_id}.git/_internal/push-event",
            json={
                "branch": "lazyaf/just-pushed",
                "new_sha": pushed_sha,
                "old_sha": "",
            },
        )
        assert_status_code(response, 200)

        frames = refs_frames(captured_frames)
        assert frames, (
            "a push broadcast no repo_refs_changed - this is the reported "
            "defect: the panel keeps saying 'No branches yet. Push your repo "
            "to get started.' after the push that answered it"
        )
        pushed = [
            b for b in frames[-1]["branches"] if b["name"] == "lazyaf/just-pushed"
        ]
        assert pushed, f"frame does not carry the pushed branch: {frames[-1]}"
        assert pushed[0]["commit"] == pushed_sha
        assert pushed[0]["is_lazyaf"] is True

    async def test_a_repo_with_no_git_storage_does_not_break_the_caller(
        self, client, db_session, captured_frames
    ):
        """The broadcast must never turn a completed write into a failure.

        A repo row that was never ingested has no listing to send. That is a
        real absence rather than a swallowed error, and the push path that
        called it carries on.
        """
        from app.routers.repos import broadcast_repo_refs_changed

        created = await client.post(
            "/api/repos", json=repo_create_payload(name="NoGitStorage")
        )
        repo_id = created.json()["id"]

        await broadcast_repo_refs_changed(db_session, repo_id)

        assert refs_frames(captured_frames) == []
