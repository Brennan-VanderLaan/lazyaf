"""
Tests for Trigger Deduplication (Phase 12.2).

These tests DEFINE the trigger deduplication contract.
Write tests first, then implement to make them pass.

Deduplication Rules:
- Same trigger_key within time window -> ignore duplicate
- Same trigger_key after window expires -> allow new trigger
- Format: {type}:{repo_id}:{ref} (e.g., "push:repo-123:refs/heads/main")

Time Window:
- Default: 1 hour
- Configurable per pipeline
- Prevents rapid-fire duplicate triggers
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.trigger_dedup import (
        TriggerKey,
        TriggerDeduplicator,
        DuplicateTriggerError,
    )
    TRIGGER_DEDUP_MODULE_AVAILABLE = True
except ImportError:
    TRIGGER_DEDUP_MODULE_AVAILABLE = False
    # Define placeholders for test collection
    TriggerKey = None
    TriggerDeduplicator = None
    DuplicateTriggerError = Exception


pytestmark = pytest.mark.skipif(
    not TRIGGER_DEDUP_MODULE_AVAILABLE,
    reason="trigger deduplication module not yet implemented"
)


class TestTriggerKeyFormat:
    """Tests for trigger key format."""

    def test_trigger_key_format_push(self):
        """Push trigger key format: push:{repo_id}:{ref}"""
        key = TriggerKey.for_push(
            repo_id="repo-abc-123",
            ref="refs/heads/main",
        )
        assert str(key) == "push:repo-abc-123:refs/heads/main"

    def test_trigger_key_format_card_complete(self):
        """Card complete trigger key format: card_complete:{repo_id}:{card_id}"""
        key = TriggerKey.for_card_complete(
            repo_id="repo-abc-123",
            card_id="card-xyz-789",
        )
        assert str(key) == "card_complete:repo-abc-123:card-xyz-789"

    def test_trigger_key_format_manual(self):
        """Manual trigger key format: manual:{repo_id}:{run_id}"""
        key = TriggerKey.for_manual(
            repo_id="repo-abc-123",
            run_id="run-manual-001",
        )
        assert str(key) == "manual:repo-abc-123:run-manual-001"

    def test_trigger_key_from_string(self):
        """TriggerKey can be parsed from string."""
        key = TriggerKey.from_string("push:repo-123:refs/heads/dev")
        assert key.trigger_type == "push"
        assert key.repo_id == "repo-123"
        assert key.ref == "refs/heads/dev"

    def test_trigger_key_equality(self):
        """TriggerKeys with same components are equal."""
        key1 = TriggerKey.for_push("repo-123", "refs/heads/main")
        key2 = TriggerKey.for_push("repo-123", "refs/heads/main")
        assert key1 == key2

    def test_trigger_key_hash(self):
        """TriggerKey is hashable for use in sets/dicts."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")
        assert hash(key) == hash(str(key))

    def test_trigger_key_includes_sha_when_provided(self):
        """Push trigger key can include commit SHA for uniqueness."""
        key = TriggerKey.for_push(
            repo_id="repo-123",
            ref="refs/heads/main",
            sha="abc123def456",
        )
        assert "abc123def456" in str(key)


class TestSameTriggerKeyWithinWindowIgnored:
    """Tests for duplicate detection within time window."""

    @pytest.fixture
    def dedup(self):
        """Create a trigger deduplicator with 1 hour window."""
        return TriggerDeduplicator(window=timedelta(hours=1))

    def test_first_trigger_allowed(self, dedup):
        """First trigger with a key is allowed."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")
        result = dedup.check(key)
        assert result.is_allowed is True
        assert result.is_duplicate is False

    def test_same_key_within_window_ignored(self, dedup):
        """Same trigger key within window is ignored."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # First trigger allowed
        result1 = dedup.check(key)
        assert result1.is_allowed is True

        # Mark it as processed
        dedup.record(key)

        # Same key immediately after is duplicate
        result2 = dedup.check(key)
        assert result2.is_allowed is False
        assert result2.is_duplicate is True

    def test_duplicate_includes_original_info(self, dedup):
        """Duplicate result includes info about original trigger."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        dedup.check(key)
        dedup.record(key, pipeline_run_id="run-original-001")

        result = dedup.check(key)
        assert result.is_duplicate is True
        assert result.original_run_id == "run-original-001"

    def test_different_keys_independent(self, dedup):
        """Different trigger keys don't affect each other."""
        key1 = TriggerKey.for_push("repo-123", "refs/heads/main")
        key2 = TriggerKey.for_push("repo-123", "refs/heads/dev")

        # First key
        dedup.check(key1)
        dedup.record(key1)

        # Second key should still be allowed
        result = dedup.check(key2)
        assert result.is_allowed is True

    def test_different_repos_independent(self, dedup):
        """Same ref in different repos are independent."""
        key1 = TriggerKey.for_push("repo-aaa", "refs/heads/main")
        key2 = TriggerKey.for_push("repo-bbb", "refs/heads/main")

        dedup.check(key1)
        dedup.record(key1)

        result = dedup.check(key2)
        assert result.is_allowed is True


class TestSameTriggerKeyAfterWindowAllowed:
    """Tests for trigger allowed after window expires."""

    def test_same_key_after_window_allowed(self):
        """Same trigger key after window expires is allowed."""
        dedup = TriggerDeduplicator(window=timedelta(hours=1))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # Record trigger - simulating T=0
        dedup.check(key)
        dedup.record(key)

        # Immediately should be duplicate
        result = dedup.check(key)
        assert result.is_duplicate is True

        # Manually expire the record by manipulating recorded_at
        record = dedup._records[str(key)]
        record.recorded_at = datetime.utcnow() - timedelta(hours=2)

        # Now should be allowed (window expired)
        result = dedup.check(key)
        assert result.is_allowed is True
        assert result.is_duplicate is False

    def test_custom_window_respected(self):
        """Custom window duration is respected."""
        dedup = TriggerDeduplicator(window=timedelta(minutes=5))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # Record trigger
        dedup.check(key)
        dedup.record(key)

        # Should be duplicate (within window)
        result = dedup.check(key)
        assert result.is_duplicate is True

        # Expire the record (6 minutes ago)
        record = dedup._records[str(key)]
        record.recorded_at = datetime.utcnow() - timedelta(minutes=6)

        # Now should be allowed
        result = dedup.check(key)
        assert result.is_allowed is True

    def test_window_is_sliding(self):
        """Window is relative to most recent trigger, not first."""
        dedup = TriggerDeduplicator(window=timedelta(hours=1))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # First trigger
        dedup.check(key)
        dedup.record(key)

        # Expire the record
        record = dedup._records[str(key)]
        record.recorded_at = datetime.utcnow() - timedelta(hours=2)

        # Should be allowed (window expired)
        result = dedup.check(key)
        assert result.is_allowed is True

        # Record again
        dedup.record(key)

        # Now should be duplicate (new window started)
        result = dedup.check(key)
        assert result.is_duplicate is True


class TestDatabasePersistence:
    """Tests for database-backed deduplication."""

    @pytest.fixture
    def db_dedup(self, test_db_session):
        """Create a database-backed deduplicator."""
        return TriggerDeduplicator(
            window=timedelta(hours=1),
            session=test_db_session,
        )

    @pytest.mark.asyncio
    async def test_persistence_across_instances(self, test_db_session):
        """Deduplication state persists across instances."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # First instance records trigger
        dedup1 = TriggerDeduplicator(
            window=timedelta(hours=1),
            session=test_db_session,
        )
        await dedup1.check_async(key)
        await dedup1.record_async(key, pipeline_run_id="run-001")

        # Second instance should see it as duplicate
        dedup2 = TriggerDeduplicator(
            window=timedelta(hours=1),
            session=test_db_session,
        )
        result = await dedup2.check_async(key)
        assert result.is_duplicate is True

    @pytest.mark.asyncio
    async def test_concurrent_check_and_record(self, test_db_session):
        """Concurrent check and record are atomic."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")
        dedup = TriggerDeduplicator(
            window=timedelta(hours=1),
            session=test_db_session,
        )

        # Atomic check-and-record
        result = await dedup.check_and_record_async(
            key,
            pipeline_run_id="run-001",
        )

        if result.is_allowed:
            # We got the trigger
            assert result.run_id == "run-001"
        else:
            # Someone else got it
            assert result.original_run_id is not None


class TestTriggerTypes:
    """Tests for different trigger types."""

    @pytest.fixture
    def dedup(self):
        """Create a trigger deduplicator."""
        return TriggerDeduplicator(window=timedelta(hours=1))

    def test_push_trigger_deduplicated(self, dedup):
        """Push triggers are deduplicated."""
        key = TriggerKey.for_push("repo-123", "refs/heads/main")
        dedup.check(key)
        dedup.record(key)

        result = dedup.check(key)
        assert result.is_duplicate is True

    def test_card_complete_trigger_deduplicated(self, dedup):
        """Card complete triggers are deduplicated."""
        key = TriggerKey.for_card_complete("repo-123", "card-456")
        dedup.check(key)
        dedup.record(key)

        result = dedup.check(key)
        assert result.is_duplicate is True

    def test_manual_triggers_unique_by_run_id(self, dedup):
        """Manual triggers are unique per run_id."""
        # Each manual trigger has unique run_id, so never duplicate
        key1 = TriggerKey.for_manual("repo-123", "run-manual-001")
        key2 = TriggerKey.for_manual("repo-123", "run-manual-002")

        dedup.check(key1)
        dedup.record(key1)

        result = dedup.check(key2)
        assert result.is_allowed is True


class TestCleanup:
    """Tests for expired entry cleanup."""

    def test_cleanup_expired_entries(self):
        """Expired entries are cleaned up."""
        dedup = TriggerDeduplicator(window=timedelta(hours=1))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # Record trigger
        dedup.check(key)
        dedup.record(key)
        assert dedup.entry_count == 1

        # Expire the record
        record = dedup._records[str(key)]
        record.recorded_at = datetime.utcnow() - timedelta(hours=2)

        # Cleanup should remove it
        dedup.cleanup_expired()
        assert dedup.entry_count == 0

    def test_cleanup_preserves_active_entries(self):
        """Cleanup preserves non-expired entries."""
        dedup = TriggerDeduplicator(window=timedelta(hours=1))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        # Record trigger (recent)
        dedup.check(key)
        dedup.record(key)

        # Cleanup should not remove it (still within window)
        dedup.cleanup_expired()
        assert dedup.entry_count == 1


class TestEdgeCases:
    """Tests for edge cases."""

    def test_zero_window_always_allows(self):
        """Zero-length window means no deduplication."""
        dedup = TriggerDeduplicator(window=timedelta(seconds=0))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        dedup.check(key)
        dedup.record(key)

        # Immediately checking again should be allowed
        result = dedup.check(key)
        assert result.is_allowed is True

    def test_very_long_window(self):
        """Very long window works correctly."""
        dedup = TriggerDeduplicator(window=timedelta(days=30))
        key = TriggerKey.for_push("repo-123", "refs/heads/main")

        dedup.check(key)
        dedup.record(key)

        # Still within 30-day window, should be duplicate
        result = dedup.check(key)
        assert result.is_duplicate is True

        # Simulate 29 days ago (still within window)
        record = dedup._records[str(key)]
        record.recorded_at = datetime.utcnow() - timedelta(days=29)
        result = dedup.check(key)
        assert result.is_duplicate is True

        # Simulate 31 days ago (outside window)
        record.recorded_at = datetime.utcnow() - timedelta(days=31)
        result = dedup.check(key)
        assert result.is_allowed is True

    def test_trigger_key_special_characters(self):
        """Trigger keys handle special characters in refs."""
        key = TriggerKey.for_push(
            "repo-123",
            "refs/heads/feature/my-feature",
        )
        assert "feature/my-feature" in str(key)

        # Round-trip through string
        parsed = TriggerKey.from_string(str(key))
        assert parsed.ref == "refs/heads/feature/my-feature"


# Note: Time-based tests use direct manipulation of recorded_at
# rather than mocking datetime, which is simpler and more reliable


# Pytest fixture for test database (placeholder)
@pytest.fixture
def test_db_session():
    """Placeholder for database session fixture."""
    pytest.skip("Database fixture not yet implemented")
