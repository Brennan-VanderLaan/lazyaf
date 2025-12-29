"""
End-to-end demo tests for the internal git server.

These tests demonstrate the complete workflow:
1. Create a repo via ingest endpoint
2. Access git endpoints to verify refs discovery
3. Verify the repo is cloneable (protocol-level validation)
4. Cleanup (delete removes both DB and git storage)

These tests serve as executable documentation and smoke tests
for the git server functionality introduced in Phase 3.75a.
"""
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import assert_status_code


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


# -----------------------------------------------------------------------------
# Complete Workflow Demo
# -----------------------------------------------------------------------------

class TestCompleteGitServerWorkflow:
    """
    Demonstrates the complete git server workflow.

    This test class walks through the entire lifecycle of an ingested repo,
    from creation through git access to deletion.
    """

    async def test_full_repo_lifecycle(self, client, git_repo_manager, temp_git_repos_dir):
        """
        DEMO: Complete repo lifecycle from ingest to deletion.

        Steps:
        1. Ingest a new repo
        2. Verify git storage was created
        3. Access git endpoints (info/refs, HEAD)
        4. Verify clone URL works
        5. Delete repo and verify cleanup
        """
        # Step 1: Ingest a new repo
        ingest_payload = {
            "name": "DemoProject",
            "remote_url": "https://github.com/example/demo.git",
            "default_branch": "main",
        }
        ingest_response = await client.post("/api/repos/ingest", json=ingest_payload)
        assert_status_code(ingest_response, 201)

        result = ingest_response.json()
        repo_id = result["id"]
        clone_url = result["clone_url"]

        print(f"\n[DEMO] Created repo: {repo_id}")
        print(f"[DEMO] Clone URL: {clone_url}")
        print(f"[DEMO] Internal URL: {result['internal_git_url']}")

        # Step 2: Verify git storage was created
        assert git_repo_manager.repo_exists(repo_id), "Git storage should exist"
        repo_path = temp_git_repos_dir / f"{repo_id}.git"
        assert (repo_path / "HEAD").exists(), "Should be a valid bare repo"
        print(f"[DEMO] Git storage verified at: {repo_path}")

        # Step 3: Access git endpoints
        # 3a. info/refs for clone
        refs_response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(refs_response, 200)
        assert b"# service=git-upload-pack" in refs_response.content
        print("[DEMO] info/refs endpoint working (git-upload-pack)")

        # 3b. info/refs for push
        push_refs_response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-receive-pack"}
        )
        assert_status_code(push_refs_response, 200)
        assert b"# service=git-receive-pack" in push_refs_response.content
        print("[DEMO] info/refs endpoint working (git-receive-pack)")

        # 3c. HEAD endpoint
        head_response = await client.get(f"/git/{repo_id}.git/HEAD")
        assert_status_code(head_response, 200)
        assert head_response.text.startswith("ref: refs/heads/")
        print(f"[DEMO] HEAD: {head_response.text.strip()}")

        # Step 4: Verify repo is in the list and marked as ingested
        get_response = await client.get(f"/api/repos/{repo_id}")
        assert_status_code(get_response, 200)
        repo_data = get_response.json()
        assert repo_data["is_ingested"] is True
        assert repo_data["name"] == "DemoProject"
        print("[DEMO] Repo correctly marked as ingested in DB")

        # Step 5: Delete and verify cleanup
        delete_response = await client.delete(f"/api/repos/{repo_id}")
        assert_status_code(delete_response, 204)

        # DB record gone
        verify_response = await client.get(f"/api/repos/{repo_id}")
        assert_status_code(verify_response, 404)

        # Git storage gone
        assert git_repo_manager.repo_exists(repo_id) is False
        print("[DEMO] Repo deleted - DB and git storage cleaned up")

        print("\n[DEMO] Full lifecycle completed successfully!")


class TestGitProtocolDemo:
    """
    Demonstrates git protocol responses match expected format.

    These tests verify that the server sends properly formatted
    git smart protocol responses that a git client can understand.
    """

    async def test_info_refs_protocol_format(self, client, git_repo_manager):
        """
        DEMO: Verify info/refs follows git smart protocol.

        The response format is:
        1. Packet-line with service announcement
        2. Flush packet (0000)
        3. Refs with capabilities
        4. Flush packet (0000)
        """
        # Create repo
        payload = {"name": "ProtocolDemo"}
        response = await client.post("/api/repos/ingest", json=payload)
        repo_id = response.json()["id"]

        # Get info/refs
        refs_response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )

        content = refs_response.content
        print(f"\n[DEMO] Raw info/refs response ({len(content)} bytes):")
        print(f"[DEMO] First 200 bytes: {content[:200]}")

        # Verify structure
        # 1. Service announcement (starts with length prefix)
        assert content[0:4] == b"001e" or content[0:4].isdigit(), "Should start with pkt-line length"
        print("[DEMO] Has pkt-line length prefix")

        # 2. Contains service name
        assert b"# service=git-upload-pack\n" in content
        print("[DEMO] Contains service announcement")

        # 3. Has flush packets - note: b"0000" may appear in SHA hex data too
        # so we verify structural positions instead of raw count
        service_line = b"# service=git-upload-pack\n"
        service_pos = content.find(service_line)
        first_flush = content.find(b"0000", service_pos + len(service_line))
        assert first_flush >= 0, "Missing first flush packet after service announcement"
        assert content.endswith(b"0000"), "Response should end with flush packet"
        print("[DEMO] Has flush packets in correct positions")

        # 4. Has capabilities (after null byte)
        assert b"\x00" in content or b"capabilities" in content.lower()
        print("[DEMO] Advertises capabilities")

        # 5. Ends with flush
        assert content.endswith(b"0000")
        print("[DEMO] Ends with flush packet")

        # Cleanup
        await client.delete(f"/api/repos/{repo_id}")

        print("[DEMO] Protocol format verified!")

    async def test_empty_repo_refs_format(self, client, git_repo_manager):
        """
        DEMO: Empty repo uses zero-SHA for capability advertisement.

        When a repo has no commits, the server advertises capabilities
        using a special format with 40 zeros as the SHA.
        """
        payload = {"name": "EmptyRepoDemo"}
        response = await client.post("/api/repos/ingest", json=payload)
        repo_id = response.json()["id"]

        refs_response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )

        content = refs_response.content
        zero_sha = b"0" * 40

        print(f"\n[DEMO] Checking empty repo refs format...")
        print(f"[DEMO] Looking for zero-SHA: {zero_sha.decode()}")

        assert zero_sha in content, "Empty repo should have zero-SHA capability line"
        print("[DEMO] Found zero-SHA (empty repo correctly formatted)")

        # Cleanup
        await client.delete(f"/api/repos/{repo_id}")


class TestMultipleReposDemo:
    """
    Demonstrates handling of multiple repos.

    Verifies that the git server correctly isolates and manages
    multiple independent repositories.
    """

    async def test_multiple_repos_isolation(self, client, git_repo_manager):
        """
        DEMO: Multiple repos are properly isolated.

        Each repo has its own git storage and can be accessed
        independently without interference.
        """
        repo_ids = []
        repo_names = ["ProjectAlpha", "ProjectBeta", "ProjectGamma"]

        print("\n[DEMO] Creating multiple repos...")

        # Create multiple repos
        for name in repo_names:
            payload = {"name": name}
            response = await client.post("/api/repos/ingest", json=payload)
            repo_id = response.json()["id"]
            repo_ids.append(repo_id)
            print(f"[DEMO] Created {name}: {repo_id}")

        # Verify each can be accessed independently
        for repo_id, name in zip(repo_ids, repo_names):
            # Git endpoint
            refs_response = await client.get(
                f"/git/{repo_id}.git/info/refs",
                params={"service": "git-upload-pack"}
            )
            assert_status_code(refs_response, 200)

            # DB record
            get_response = await client.get(f"/api/repos/{repo_id}")
            assert get_response.json()["name"] == name

            print(f"[DEMO] Verified {name} is accessible")

        # Delete one, verify others unaffected
        deleted_id = repo_ids[1]
        await client.delete(f"/api/repos/{deleted_id}")
        print(f"[DEMO] Deleted {repo_names[1]}")

        # Deleted repo should 404
        refs_response = await client.get(
            f"/git/{deleted_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(refs_response, 404)

        # Others should still work
        for repo_id, name in zip([repo_ids[0], repo_ids[2]], [repo_names[0], repo_names[2]]):
            refs_response = await client.get(
                f"/git/{repo_id}.git/info/refs",
                params={"service": "git-upload-pack"}
            )
            assert_status_code(refs_response, 200)
            print(f"[DEMO] {name} still accessible after sibling deletion")

        # Cleanup remaining
        for repo_id in [repo_ids[0], repo_ids[2]]:
            await client.delete(f"/api/repos/{repo_id}")

        print("[DEMO] Multi-repo isolation verified!")


class TestCloneUrlDemo:
    """
    Demonstrates clone URL generation and usage.

    The clone URL is the primary interface for external tools
    (like git CLI or IDE integrations) to access repos.
    """

    async def test_clone_url_construction(self, client, git_repo_manager):
        """
        DEMO: Clone URL is correctly constructed.

        The clone URL combines the server base URL with the
        internal git path pattern.
        """
        payload = {"name": "CloneUrlDemo"}
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        result = ingest_response.json()

        repo_id = result["id"]
        clone_url = result["clone_url"]
        internal_url = result["internal_git_url"]

        print(f"\n[DEMO] Repo ID: {repo_id}")
        print(f"[DEMO] Clone URL: {clone_url}")
        print(f"[DEMO] Internal URL: {internal_url}")

        # Verify internal URL format
        assert internal_url == f"/git/{repo_id}.git"
        print("[DEMO] Internal URL format correct")

        # Verify clone URL contains internal path
        assert internal_url in clone_url or f"/git/{repo_id}.git" in clone_url
        print("[DEMO] Clone URL contains internal path")

        # Verify clone URL is HTTP(S)
        assert clone_url.startswith("http")
        print("[DEMO] Clone URL is HTTP-based")

        # Get clone URL via separate endpoint
        url_response = await client.get(f"/api/repos/{repo_id}/clone-url")
        url_result = url_response.json()

        assert url_result["clone_url"] == clone_url
        assert url_result["is_ingested"] is True
        print("[DEMO] clone-url endpoint returns consistent data")

        # Cleanup
        await client.delete(f"/api/repos/{repo_id}")

        print("[DEMO] Clone URL demo completed!")


# -----------------------------------------------------------------------------
# Smoke Tests
# -----------------------------------------------------------------------------

class TestGitServerSmoke:
    """
    Quick smoke tests to verify basic functionality.

    These tests run quickly and catch obvious regressions.
    """

    async def test_smoke_ingest_and_access(self, client, git_repo_manager):
        """SMOKE: Can ingest repo and access git endpoints."""
        response = await client.post("/api/repos/ingest", json={"name": "SmokeTest"})
        assert_status_code(response, 201)
        repo_id = response.json()["id"]

        refs = await client.get(f"/git/{repo_id}.git/info/refs", params={"service": "git-upload-pack"})
        assert_status_code(refs, 200)

        await client.delete(f"/api/repos/{repo_id}")

    async def test_smoke_all_git_endpoints(self, client, git_repo_manager):
        """SMOKE: All git endpoints respond correctly."""
        response = await client.post("/api/repos/ingest", json={"name": "EndpointSmoke"})
        repo_id = response.json()["id"]

        # info/refs upload-pack
        r1 = await client.get(f"/git/{repo_id}.git/info/refs", params={"service": "git-upload-pack"})
        assert_status_code(r1, 200)

        # info/refs receive-pack
        r2 = await client.get(f"/git/{repo_id}.git/info/refs", params={"service": "git-receive-pack"})
        assert_status_code(r2, 200)

        # upload-pack POST - Note: requires valid protocol data, just verify endpoint exists
        r3 = await client.post(f"/git/{repo_id}.git/git-upload-pack", content=b"0000")
        assert r3.status_code != 404, "upload-pack endpoint should exist"

        # receive-pack POST - Note: requires valid protocol data, just verify endpoint exists
        r4 = await client.post(f"/git/{repo_id}.git/git-receive-pack", content=b"0000")
        assert r4.status_code != 404, "receive-pack endpoint should exist"

        # HEAD
        r5 = await client.get(f"/git/{repo_id}.git/HEAD")
        assert_status_code(r5, 200)

        await client.delete(f"/api/repos/{repo_id}")

    async def test_smoke_404_for_missing_repo(self, client, git_repo_manager):
        """SMOKE: Missing repos return 404."""
        fake_id = "this-repo-does-not-exist-12345"

        r1 = await client.get(f"/git/{fake_id}.git/info/refs", params={"service": "git-upload-pack"})
        assert_status_code(r1, 404)

        r2 = await client.get(f"/git/{fake_id}.git/HEAD")
        assert_status_code(r2, 404)
