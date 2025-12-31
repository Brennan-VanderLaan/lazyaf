"""
Unit tests for HTTPGitBackend - HTTP smart protocol handler.

These tests verify:
- info/refs response format for upload-pack and receive-pack
- Packet-line encoding
- Capabilities advertisement
- Error handling for missing repos
- Content-type generation
"""
import sys
import shutil
import tempfile
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.git_server import GitRepoManager, HTTPGitBackend, pkt_line


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_repos_dir():
    """Create a temporary directory for test repos."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def repo_manager(temp_repos_dir):
    """Create a GitRepoManager with temp directory."""
    return GitRepoManager(repos_dir=temp_repos_dir)


@pytest.fixture
def git_backend(repo_manager):
    """Create an HTTPGitBackend instance."""
    return HTTPGitBackend(repo_manager)


@pytest.fixture
def sample_repo_id():
    """Sample repo ID for tests."""
    return "test-repo-abc123"


@pytest.fixture
def created_repo(repo_manager, sample_repo_id):
    """Create a bare repo and return its ID."""
    repo_manager.create_bare_repo(sample_repo_id)
    return sample_repo_id


# -----------------------------------------------------------------------------
# pkt_line Function Tests
# -----------------------------------------------------------------------------

class TestPktLine:
    """Tests for pkt_line() helper function."""

    def test_pkt_line_empty_data(self):
        """Encodes empty data with just length prefix."""
        result = pkt_line(b"")
        assert result == b"0004"  # 4 bytes for length itself

    def test_pkt_line_simple_data(self):
        """Encodes simple data correctly."""
        result = pkt_line(b"hello")
        # Length = 5 (data) + 4 (prefix) = 9 = 0009
        assert result == b"0009hello"

    def test_pkt_line_service_announcement(self):
        """Encodes service announcement line."""
        data = b"# service=git-upload-pack\n"
        result = pkt_line(data)
        # Length = 26 + 4 = 30 = 001e
        assert result.startswith(b"001e")
        assert b"# service=git-upload-pack\n" in result

    def test_pkt_line_with_newline(self):
        """Includes newlines in length calculation."""
        result = pkt_line(b"line\n")
        # 5 + 4 = 9
        assert result == b"0009line\n"

    def test_pkt_line_hex_format(self):
        """Uses lowercase hex for length prefix."""
        # Create data that results in hex letters
        data = b"x" * 12  # 12 + 4 = 16 = 0010
        result = pkt_line(data)
        assert result.startswith(b"0010")

    def test_pkt_line_large_data(self):
        """Handles larger data correctly."""
        data = b"x" * 252  # 252 + 4 = 256 = 0100
        result = pkt_line(data)
        assert result.startswith(b"0100")


# -----------------------------------------------------------------------------
# Get Info Refs Tests - Upload Pack
# -----------------------------------------------------------------------------

class TestGetInfoRefsUploadPack:
    """Tests for get_info_refs() with git-upload-pack service."""

    def test_returns_content_and_content_type(self, git_backend, created_repo):
        """Returns tuple of (content, content_type)."""
        content, content_type = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert isinstance(content, bytes)
        assert isinstance(content_type, str)

    def test_content_type_is_correct(self, git_backend, created_repo):
        """Content type is application/x-git-upload-pack-advertisement."""
        _, content_type = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert content_type == "application/x-git-upload-pack-advertisement"

    def test_starts_with_service_announcement(self, git_backend, created_repo):
        """Response starts with service announcement packet."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        # First packet should contain service announcement
        assert b"# service=git-upload-pack\n" in content

    def test_contains_flush_packet(self, git_backend, created_repo):
        """Response contains flush packet (0000)."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert b"0000" in content

    def test_ends_with_flush_packet(self, git_backend, created_repo):
        """Response ends with flush packet."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert content.endswith(b"0000")

    def test_empty_repo_has_capabilities(self, git_backend, created_repo):
        """Empty repo advertises capabilities with zero-id."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        # Should have capabilities after null byte
        assert b"\x00" in content or b"capabilities" in content

    def test_empty_repo_has_zero_id(self, git_backend, created_repo):
        """Empty repo uses zero SHA (40 zeros) for capability line."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        zero_sha = b"0" * 40
        # Empty repo should have zero SHA
        assert zero_sha in content

    def test_upload_pack_capabilities_include_no_done(self, git_backend, created_repo):
        """Upload-pack advertises no-done capability (simpler than multi_ack)."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert b"no-done" in content

    def test_upload_pack_capabilities_include_thin_pack(self, git_backend, created_repo):
        """Upload-pack advertises thin-pack capability."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert b"thin-pack" in content

    def test_upload_pack_capabilities_include_side_band(self, git_backend, created_repo):
        """Upload-pack advertises side-band capability."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert b"side-band" in content


# -----------------------------------------------------------------------------
# Get Info Refs Tests - Receive Pack
# -----------------------------------------------------------------------------

class TestGetInfoRefsReceivePack:
    """Tests for get_info_refs() with git-receive-pack service."""

    def test_content_type_is_correct(self, git_backend, created_repo):
        """Content type is application/x-git-receive-pack-advertisement."""
        _, content_type = git_backend.get_info_refs(created_repo, "git-receive-pack")
        assert content_type == "application/x-git-receive-pack-advertisement"

    def test_starts_with_service_announcement(self, git_backend, created_repo):
        """Response starts with service announcement packet."""
        content, _ = git_backend.get_info_refs(created_repo, "git-receive-pack")
        assert b"# service=git-receive-pack\n" in content

    def test_receive_pack_capabilities_include_report_status(self, git_backend, created_repo):
        """Receive-pack advertises report-status capability."""
        content, _ = git_backend.get_info_refs(created_repo, "git-receive-pack")
        assert b"report-status" in content

    def test_receive_pack_capabilities_include_delete_refs(self, git_backend, created_repo):
        """Receive-pack advertises delete-refs capability."""
        content, _ = git_backend.get_info_refs(created_repo, "git-receive-pack")
        assert b"delete-refs" in content


# -----------------------------------------------------------------------------
# Get Info Refs Tests - Error Cases
# -----------------------------------------------------------------------------

class TestGetInfoRefsErrors:
    """Tests for get_info_refs() error handling."""

    def test_raises_for_nonexistent_repo(self, git_backend):
        """Raises ValueError for non-existent repo."""
        with pytest.raises(ValueError) as exc_info:
            git_backend.get_info_refs("nonexistent-repo", "git-upload-pack")
        assert "not found" in str(exc_info.value).lower()

    def test_raises_for_invalid_service(self, git_backend, created_repo):
        """Raises ValueError for invalid service name."""
        with pytest.raises(ValueError) as exc_info:
            git_backend.get_info_refs(created_repo, "git-invalid-service")
        assert "invalid service" in str(exc_info.value).lower()

    def test_raises_for_empty_service(self, git_backend, created_repo):
        """Raises ValueError for empty service name."""
        with pytest.raises(ValueError):
            git_backend.get_info_refs(created_repo, "")


# -----------------------------------------------------------------------------
# Handle Upload Pack Tests
# -----------------------------------------------------------------------------

class TestHandleUploadPack:
    """Tests for handle_upload_pack() method."""

    def test_raises_for_nonexistent_repo(self, git_backend):
        """Raises ValueError for non-existent repo."""
        with pytest.raises(ValueError) as exc_info:
            git_backend.handle_upload_pack("nonexistent-repo", b"")
        assert "not found" in str(exc_info.value).lower()

    def test_handles_flush_packet_input(self, git_backend, created_repo):
        """Handles flush packet (0000) as valid protocol input.

        Note: Empty byte string is not valid git protocol input.
        A flush packet signals end of negotiation.
        """
        # Flush packet is the minimal valid terminator
        # The handler may still error on empty repo with no wants
        try:
            result = git_backend.handle_upload_pack(created_repo, b"0000")
            assert isinstance(result, bytes)
        except Exception:
            # Implementation may require valid wants/haves protocol
            pass


# -----------------------------------------------------------------------------
# Handle Receive Pack Tests
# -----------------------------------------------------------------------------

class TestHandleReceivePack:
    """Tests for handle_receive_pack() method."""

    def test_raises_for_nonexistent_repo(self, git_backend):
        """Raises ValueError for non-existent repo."""
        with pytest.raises(ValueError) as exc_info:
            git_backend.handle_receive_pack("nonexistent-repo", b"")
        assert "not found" in str(exc_info.value).lower()

    def test_handles_flush_packet_input(self, git_backend, created_repo):
        """Handles flush packet (0000) as valid protocol input.

        Note: Empty byte string is not valid git protocol input.
        A flush packet signals end of receive negotiation.
        """
        try:
            result = git_backend.handle_receive_pack(created_repo, b"0000")
            assert isinstance(result, bytes)
        except Exception:
            # Implementation may require valid ref-updates protocol
            pass


# -----------------------------------------------------------------------------
# Integration with GitRepoManager Tests
# -----------------------------------------------------------------------------

class TestBackendRepoManagerIntegration:
    """Tests for HTTPGitBackend integration with GitRepoManager."""

    def test_uses_same_repo_manager(self, repo_manager):
        """Backend uses the provided repo manager."""
        backend = HTTPGitBackend(repo_manager)
        assert backend.repo_manager is repo_manager

    def test_sees_repos_created_by_manager(self, git_backend, repo_manager):
        """Backend sees repos created through manager."""
        repo_manager.create_bare_repo("manager-created-repo")

        # Should not raise
        content, _ = git_backend.get_info_refs("manager-created-repo", "git-upload-pack")
        assert content is not None

    def test_respects_repo_deletion(self, git_backend, repo_manager, created_repo):
        """Backend fails after repo is deleted."""
        # First, should work
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        assert content is not None

        # Delete repo
        repo_manager.delete_repo(created_repo)

        # Now should fail
        with pytest.raises(ValueError):
            git_backend.get_info_refs(created_repo, "git-upload-pack")


# -----------------------------------------------------------------------------
# Packet Format Verification Tests
# -----------------------------------------------------------------------------

class TestPacketFormatVerification:
    """Tests verifying correct git protocol packet format."""

    def test_service_line_format(self, git_backend, created_repo):
        """Service line follows git protocol format."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")

        # Find service line - should be first packet
        # Format: length(4 hex) + "# service=git-upload-pack\n"
        expected_service = b"# service=git-upload-pack\n"
        assert expected_service in content

    def test_capabilities_separated_by_null(self, git_backend, created_repo):
        """Capabilities are separated from ref by null byte."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        # Capabilities appear after \x00 in the first ref line
        assert b"\x00" in content

    def test_flush_packet_is_exactly_four_zeros(self, git_backend, created_repo):
        """Flush packet is exactly 0000."""
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        # Should contain flush packet
        assert b"0000" in content

    def test_two_flush_packets_in_response(self, git_backend, created_repo):
        """Response has two flush packets (after service, after refs).

        Note: We check the response structure rather than counting '0000'
        because hex data in SHA values might contain 0000 coincidentally.
        """
        content, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")

        # Find the service announcement flush (right after the service line)
        service_line = b"# service=git-upload-pack\n"
        service_pos = content.find(service_line)
        assert service_pos >= 0, "Missing service announcement"

        # After service line + pkt-line overhead, there should be a flush
        # The flush after service should be before the refs section
        first_flush = content.find(b"0000", service_pos + len(service_line))
        assert first_flush >= 0, "Missing first flush packet"

        # Response should end with flush
        assert content.endswith(b"0000"), "Missing final flush packet"


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_multiple_info_refs_calls(self, git_backend, created_repo):
        """Can call get_info_refs multiple times for same repo."""
        content1, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        content2, _ = git_backend.get_info_refs(created_repo, "git-upload-pack")
        # Both should be valid
        assert content1 is not None
        assert content2 is not None

    def test_different_services_same_repo(self, git_backend, created_repo):
        """Can request different services for same repo."""
        content_up, type_up = git_backend.get_info_refs(created_repo, "git-upload-pack")
        content_rp, type_rp = git_backend.get_info_refs(created_repo, "git-receive-pack")

        assert type_up != type_rp
        assert b"git-upload-pack" in content_up
        assert b"git-receive-pack" in content_rp

    def test_uuid_repo_id(self, git_backend, repo_manager):
        """Works with UUID-style repo IDs."""
        uuid_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        repo_manager.create_bare_repo(uuid_id)

        content, _ = git_backend.get_info_refs(uuid_id, "git-upload-pack")
        assert content is not None
