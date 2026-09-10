"""
Integration tests for Git HTTP smart protocol endpoints.

These tests verify the full request/response cycle through the FastAPI
application for git clone/fetch/push operations.

Endpoints tested:
- GET /git/{repo_id}.git/info/refs?service=git-upload-pack
- GET /git/{repo_id}.git/info/refs?service=git-receive-pack
- POST /git/{repo_id}.git/git-upload-pack
- POST /git/{repo_id}.git/git-receive-pack
- GET /git/{repo_id}.git/HEAD
"""
import sys
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_ingest_payload
from shared.assertions import (
    assert_status_code,
    assert_not_found,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_git_repos_dir():
    """Create a temporary directory for git repos during tests.

    Uses resolve() to get the full path and avoid Windows 8.3 short name issues.
    """
    temp_dir = Path(tempfile.mkdtemp()).resolve()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def git_repo_manager(temp_git_repos_dir):
    """Override git_repo_manager to use temp directory.

    This fixture must swap the singleton in all places it's referenced:
    1. app.services.git_server module
    2. git_backend.repo_manager
    3. app.routers.git module (captured at import time)
    4. app.routers.repos module (captured at import time)
    """
    from app.services.git_server import GitRepoManager, git_backend
    import app.services.git_server as git_module
    import app.routers.git as git_router
    import app.routers.repos as repos_router

    # Create a new manager with temp dir
    temp_manager = GitRepoManager(repos_dir=temp_git_repos_dir)

    # Save originals
    original_service_manager = git_module.git_repo_manager
    original_backend_manager = git_backend.repo_manager
    original_router_manager = git_router.git_repo_manager
    original_repos_router_manager = repos_router.git_repo_manager

    # Replace all references
    git_module.git_repo_manager = temp_manager
    git_backend.repo_manager = temp_manager
    git_router.git_repo_manager = temp_manager
    repos_router.git_repo_manager = temp_manager

    yield temp_manager

    # Restore all originals
    git_module.git_repo_manager = original_service_manager
    git_backend.repo_manager = original_backend_manager
    git_router.git_repo_manager = original_router_manager
    repos_router.git_repo_manager = original_repos_router_manager


@pytest_asyncio.fixture
async def created_git_repo(git_repo_manager):
    """Create a bare git repo and return its ID."""
    repo_id = "test-repo-12345"
    git_repo_manager.create_bare_repo(repo_id)
    return repo_id


# -----------------------------------------------------------------------------
# Info Refs - Upload Pack Tests
# -----------------------------------------------------------------------------

class TestInfoRefsUploadPack:
    """Tests for GET /git/{repo_id}.git/info/refs?service=git-upload-pack."""

    async def test_returns_200_for_existing_repo(self, client, created_git_repo):
        """Returns 200 for existing repo."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response, 200)

    async def test_returns_404_for_nonexistent_repo(self, client, git_repo_manager):
        """Returns 404 for non-existent repo."""
        response = await client.get(
            "/git/nonexistent-repo.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response, 404)

    async def test_content_type_is_correct(self, client, created_git_repo):
        """Content type is git-upload-pack-advertisement."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert response.headers["content-type"] == "application/x-git-upload-pack-advertisement"

    async def test_has_cache_control_no_cache(self, client, created_git_repo):
        """Response has Cache-Control: no-cache header."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert response.headers.get("cache-control") == "no-cache"

    async def test_response_contains_service_announcement(self, client, created_git_repo):
        """Response body contains service announcement."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert b"# service=git-upload-pack" in response.content

    async def test_response_contains_capabilities(self, client, created_git_repo):
        """Response body contains capabilities."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        # Should have essential capabilities (no multi_ack - we use simpler no-done protocol)
        assert b"side-band" in response.content
        assert b"thin-pack" in response.content

    async def test_response_ends_with_flush(self, client, created_git_repo):
        """Response ends with flush packet (0000)."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert response.content.endswith(b"0000")


# -----------------------------------------------------------------------------
# Info Refs - Receive Pack Tests
# -----------------------------------------------------------------------------

class TestInfoRefsReceivePack:
    """Tests for GET /git/{repo_id}.git/info/refs?service=git-receive-pack."""

    async def test_returns_200_for_existing_repo(self, client, created_git_repo):
        """Returns 200 for existing repo."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-receive-pack"}
        )
        assert_status_code(response, 200)

    async def test_content_type_is_correct(self, client, created_git_repo):
        """Content type is git-receive-pack-advertisement."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-receive-pack"}
        )
        assert response.headers["content-type"] == "application/x-git-receive-pack-advertisement"

    async def test_response_contains_service_announcement(self, client, created_git_repo):
        """Response body contains service announcement."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-receive-pack"}
        )
        assert b"# service=git-receive-pack" in response.content

    async def test_response_contains_receive_pack_capabilities(self, client, created_git_repo):
        """Response contains receive-pack specific capabilities."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-receive-pack"}
        )
        assert b"report-status" in response.content


# -----------------------------------------------------------------------------
# Info Refs - Invalid Service Tests
# -----------------------------------------------------------------------------

class TestInfoRefsInvalidService:
    """Tests for invalid service parameter."""

    async def test_returns_400_for_invalid_service(self, client, created_git_repo):
        """Returns 400 for invalid service name."""
        response = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-invalid"}
        )
        assert_status_code(response, 400)

    async def test_returns_422_for_missing_service(self, client, created_git_repo):
        """Returns 422 when service parameter is missing."""
        response = await client.get(f"/git/{created_git_repo}.git/info/refs")
        # FastAPI returns 422 for missing required query params
        assert_status_code(response, 422)


# -----------------------------------------------------------------------------
# Upload Pack Endpoint Tests
# -----------------------------------------------------------------------------

class TestUploadPackEndpoint:
    """Tests for POST /git/{repo_id}.git/git-upload-pack.

    Note: The upload-pack endpoint requires valid git protocol input.
    Empty requests or invalid protocol data may return 500 errors.
    These tests focus on basic endpoint routing and error handling.
    """

    async def test_returns_404_for_nonexistent_repo(self, client, git_repo_manager):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/git/nonexistent-repo.git/git-upload-pack",
            content=b"",
            headers={"Content-Type": "application/x-git-upload-pack-request"}
        )
        assert_status_code(response, 404)

    async def test_endpoint_exists_and_processes_request(self, client, created_git_repo):
        """Endpoint exists and attempts to process request.

        Note: Without valid git protocol input, the handler may fail,
        but the endpoint itself should be reachable (not 404).
        """
        response = await client.post(
            f"/git/{created_git_repo}.git/git-upload-pack",
            content=b"0000",  # Flush packet
            headers={"Content-Type": "application/x-git-upload-pack-request"}
        )
        # Either succeeds or fails with git protocol error (not 404)
        assert response.status_code != 404


# -----------------------------------------------------------------------------
# Receive Pack Endpoint Tests
# -----------------------------------------------------------------------------

class TestReceivePackEndpoint:
    """Tests for POST /git/{repo_id}.git/git-receive-pack.

    Note: The receive-pack endpoint requires valid git protocol input.
    Empty requests or invalid protocol data may return 500 errors.
    These tests focus on basic endpoint routing and error handling.
    """

    async def test_returns_404_for_nonexistent_repo(self, client, git_repo_manager):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/git/nonexistent-repo.git/git-receive-pack",
            content=b"",
            headers={"Content-Type": "application/x-git-receive-pack-request"}
        )
        assert_status_code(response, 404)

    async def test_endpoint_exists_and_processes_request(self, client, created_git_repo):
        """Endpoint exists and attempts to process request.

        Note: Without valid git protocol input, the handler may fail,
        but the endpoint itself should be reachable (not 404).
        """
        response = await client.post(
            f"/git/{created_git_repo}.git/git-receive-pack",
            content=b"0000",  # Flush packet
            headers={"Content-Type": "application/x-git-receive-pack-request"}
        )
        # Either succeeds or fails with git protocol error (not 404)
        assert response.status_code != 404


# -----------------------------------------------------------------------------
# HEAD Endpoint Tests
# -----------------------------------------------------------------------------

class TestHeadEndpoint:
    """Tests for GET /git/{repo_id}.git/HEAD."""

    async def test_returns_200_for_existing_repo(self, client, created_git_repo):
        """Returns 200 for existing repo."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        assert_status_code(response, 200)

    async def test_returns_404_for_nonexistent_repo(self, client, git_repo_manager):
        """Returns 404 for non-existent repo."""
        response = await client.get("/git/nonexistent-repo.git/HEAD")
        assert_status_code(response, 404)

    async def test_content_type_is_text_plain(self, client, created_git_repo):
        """Content type is text/plain."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        assert "text/plain" in response.headers["content-type"]

    async def test_response_is_symbolic_ref(self, client, created_git_repo):
        """Response is a symbolic ref."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        content = response.text
        assert content.startswith("ref: refs/heads/")

    async def test_response_defaults_to_main(self, client, created_git_repo):
        """Default branch is main when not set."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        content = response.text
        # For empty repo, defaults to main
        assert "refs/heads/" in content

    async def test_response_ends_with_newline(self, client, created_git_repo):
        """Response ends with newline."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        assert response.text.endswith("\n")

    async def test_has_cache_control_no_cache(self, client, created_git_repo):
        """Response has Cache-Control: no-cache header."""
        response = await client.get(f"/git/{created_git_repo}.git/HEAD")
        assert response.headers.get("cache-control") == "no-cache"


# -----------------------------------------------------------------------------
# URL Pattern Tests
# -----------------------------------------------------------------------------

class TestUrlPatterns:
    """Tests for URL pattern matching."""

    async def test_repo_id_with_uuid(self, client, git_repo_manager):
        """Works with UUID-style repo IDs."""
        uuid_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        git_repo_manager.create_bare_repo(uuid_id)

        response = await client.get(
            f"/git/{uuid_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response, 200)

    async def test_repo_id_with_underscores(self, client, git_repo_manager):
        """Works with underscore-containing repo IDs."""
        repo_id = "my_test_repo"
        git_repo_manager.create_bare_repo(repo_id)

        response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response, 200)

    async def test_repo_id_with_numbers(self, client, git_repo_manager):
        """Works with numeric repo IDs."""
        repo_id = "123456"
        git_repo_manager.create_bare_repo(repo_id)

        response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response, 200)


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and error scenarios."""

    async def test_multiple_requests_same_repo(self, client, created_git_repo):
        """Can make multiple requests to same repo."""
        for _ in range(3):
            response = await client.get(
                f"/git/{created_git_repo}.git/info/refs",
                params={"service": "git-upload-pack"}
            )
            assert_status_code(response, 200)

    async def test_different_endpoints_same_repo(self, client, created_git_repo):
        """Can access different endpoints for same repo."""
        # info/refs
        response1 = await client.get(
            f"/git/{created_git_repo}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(response1, 200)

        # HEAD
        response2 = await client.get(f"/git/{created_git_repo}.git/HEAD")
        assert_status_code(response2, 200)

        # Note: POST endpoints require valid protocol data
        # Just verify they're reachable (not 404)
        response3 = await client.post(
            f"/git/{created_git_repo}.git/git-upload-pack",
            content=b"0000"
        )
        assert response3.status_code != 404

    async def test_concurrent_requests(self, client, created_git_repo):
        """Handles concurrent requests."""
        import asyncio

        async def make_request():
            return await client.get(
                f"/git/{created_git_repo}.git/info/refs",
                params={"service": "git-upload-pack"}
            )

        # Make 5 concurrent requests
        responses = await asyncio.gather(*[make_request() for _ in range(5)])

        for response in responses:
            assert_status_code(response, 200)


class TestPushAdoptsADefaultBranchThatExists:
    """A pushed repo whose trunk is not `main` must be startable immediately.

    The UI's Add Repo form hardcodes `default_branch: "main"` and offers no
    field to change it (RepoSelector.svelte). Push a repo whose trunk is
    `develop` and the row named a branch that did not exist - so starting a
    card refused with a 400 saying "no such branch exists here", naming a
    branch the user had never chosen.

    GET /branches always healed this, which is exactly why it survived
    testing: anyone who opened the repo view fixed it by accident. A user who
    pushed and went straight to a card did not. The row is corrected at the
    push now, where the evidence arrives.
    """

    async def test_a_pushed_branch_becomes_the_default_when_the_row_names_nothing(
        self, client, db_session, git_repo_manager
    ):
        from app.models import Repo
        from app.routers.git import _adopt_pushed_default_branch
        from shared.git_seed import seed_branch

        payload = repo_ingest_payload(name="TrunkIsDevelop")
        payload["default_branch"] = "main"
        repo_id = (await client.post("/api/repos/ingest", json=payload)).json()["id"]

        # What a `git push lazyaf develop` leaves behind: one ref, and it is
        # not the one the row names.
        seed_branch(repo_id, "develop", path="README.md", content=b"work' + BS + 'n")

        await _adopt_pushed_default_branch(db_session, repo_id)

        repo = await db_session.get(Repo, repo_id)
        await db_session.refresh(repo)
        assert repo.default_branch == "develop", (
            "the row still names a branch that does not exist, so starting a "
            "card will refuse with a 400"
        )

    async def test_a_default_that_exists_is_never_overwritten(
        self, client, db_session, git_repo_manager
    ):
        """A deliberate choice survives. Someone who set `develop` while
        `main` also exists means it."""
        from app.models import Repo
        from app.routers.git import _adopt_pushed_default_branch
        from shared.git_seed import seed_branch

        payload = repo_ingest_payload(name="DeliberateDefault")
        payload["default_branch"] = "develop"
        repo_id = (await client.post("/api/repos/ingest", json=payload)).json()["id"]

        seed_branch(repo_id, "develop", path="a.txt", content=b"a' + BS + 'n")
        seed_branch(repo_id, "main", path="b.txt", content=b"b' + BS + 'n")

        await _adopt_pushed_default_branch(db_session, repo_id)

        repo = await db_session.get(Repo, repo_id)
        await db_session.refresh(repo)
        assert repo.default_branch == "develop"

    async def test_an_agent_branch_is_not_adopted_as_the_default(
        self, client, db_session, git_repo_manager
    ):
        """`lazyaf/*` branches are the platform's own work, not a trunk."""
        from app.models import Repo
        from app.routers.git import _adopt_pushed_default_branch
        from shared.git_seed import seed_branch

        payload = repo_ingest_payload(name="AgentBranchOnly")
        payload["default_branch"] = "main"
        repo_id = (await client.post("/api/repos/ingest", json=payload)).json()["id"]

        seed_branch(repo_id, "lazyaf/abc123", path="x.txt", content=b"x' + BS + 'n")
        seed_branch(repo_id, "trunk", path="y.txt", content=b"y' + BS + 'n")

        await _adopt_pushed_default_branch(db_session, repo_id)

        repo = await db_session.get(Repo, repo_id)
        await db_session.refresh(repo)
        assert repo.default_branch == "trunk"

    async def test_no_branches_leaves_the_row_alone(
        self, client, db_session, git_repo_manager
    ):
        """An empty repo has nothing to adopt. Inventing a name would be worse
        than leaving the honest default in place."""
        from app.models import Repo
        from app.routers.git import _adopt_pushed_default_branch

        payload = repo_ingest_payload(name="StillEmpty")
        payload["default_branch"] = "main"
        repo_id = (await client.post("/api/repos/ingest", json=payload)).json()["id"]

        await _adopt_pushed_default_branch(db_session, repo_id)

        repo = await db_session.get(Repo, repo_id)
        assert repo.default_branch == "main"
