"""
Unit tests for Trigger Deduplication.

These tests define the contract for preventing duplicate pipeline triggers:
- Same trigger within window is ignored
- Same trigger after window is allowed
- Trigger key format
- Configurable dedup window per trigger type

Write these tests BEFORE implementing trigger deduplication.
"""
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timedelta

import pytest

# Tests enabled - trigger deduplication implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Trigger Key Format
# -----------------------------------------------------------------------------

class TestTriggerKeyFormat:
    """Tests that verify trigger key format."""

    def test_trigger_key_format_basic(self):
        """Trigger key follows format: {type}:{repo_id}:{ref}."""
        from app.services.workspace.trigger_dedup import generate_trigger_key

        key = generate_trigger_key(
            trigger_type="card_complete",
            repo_id="repo-123",
            ref="card-456"
        )
        assert key == "card_complete:repo-123:card-456"

    def test_trigger_key_format_push(self):
        """Push trigger includes branch ref."""
        from app.services.workspace.trigger_dedup import generate_trigger_key

        key = generate_trigger_key(
            trigger_type="push",
            repo_id="repo-123",
            ref="main"
        )
        assert key == "push:repo-123:main"

    def test_trigger_key_format_manual(self):
        """Manual trigger includes run identifier."""
        from app.services.workspace.trigger_dedup import generate_trigger_key

        key = generate_trigger_key(
            trigger_type="manual",
            repo_id="repo-123",
            ref="user-request-789"
        )
        assert key == "manual:repo-123:user-request-789"

    def test_parse_trigger_key(self):
        """Can parse trigger key back to components."""
        from app.services.workspace.trigger_dedup import (
            generate_trigger_key, parse_trigger_key
        )

        key = generate_trigger_key(
            trigger_type="card_complete",
            repo_id="repo-123",
            ref="card-456"
        )

        parsed = parse_trigger_key(key)
        assert parsed["type"] == "card_complete"
        assert parsed["repo_id"] == "repo-123"
        assert parsed["ref"] == "card-456"


# -----------------------------------------------------------------------------
# Contract: Deduplication Within Window
# -----------------------------------------------------------------------------

class TestDeduplicationWithinWindow:
    """Tests that verify duplicate triggers are ignored within window."""

    async def test_same_trigger_within_window_ignored(self):
        """Same trigger key within dedup window returns False."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()

        # First trigger allowed
        key = "card_complete:repo-123:card-456"
        allowed1 = await dedup.should_trigger(key, window_seconds=60)
        assert allowed1 is True

        # Same trigger immediately after - should be ignored
        allowed2 = await dedup.should_trigger(key, window_seconds=60)
        assert allowed2 is False

    async def test_different_triggers_both_allowed(self):
        """Different trigger keys are both allowed."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()

        key1 = "card_complete:repo-123:card-456"
        key2 = "card_complete:repo-123:card-789"

        allowed1 = await dedup.should_trigger(key1, window_seconds=60)
        allowed2 = await dedup.should_trigger(key2, window_seconds=60)

        assert allowed1 is True
        assert allowed2 is True

    async def test_same_type_different_repo_allowed(self):
        """Same trigger type for different repos are both allowed."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()

        key1 = "push:repo-A:main"
        key2 = "push:repo-B:main"

        allowed1 = await dedup.should_trigger(key1, window_seconds=60)
        allowed2 = await dedup.should_trigger(key2, window_seconds=60)

        assert allowed1 is True
        assert allowed2 is True


# -----------------------------------------------------------------------------
# Contract: Deduplication After Window
# -----------------------------------------------------------------------------

class TestDeduplicationAfterWindow:
    """Tests that verify triggers are allowed after window expires."""

    async def test_same_trigger_after_window_allowed(self):
        """Same trigger key after window expiration is allowed."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator
        import time

        dedup = TriggerDeduplicator()
        key = "card_complete:repo-123:card-456"

        # First trigger with very short window
        allowed1 = await dedup.should_trigger(key, window_seconds=0.1)
        assert allowed1 is True

        # Wait for window to expire
        time.sleep(0.15)

        # Same trigger should now be allowed
        allowed2 = await dedup.should_trigger(key, window_seconds=0.1)
        assert allowed2 is True

    async def test_window_extends_on_retrigger(self):
        """Window does NOT extend when duplicate is ignored."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator
        import time

        dedup = TriggerDeduplicator()
        key = "card_complete:repo-123:card-456"

        # First trigger
        await dedup.should_trigger(key, window_seconds=0.2)

        # Wait half the window
        time.sleep(0.1)

        # Duplicate (ignored) - should NOT extend window
        await dedup.should_trigger(key, window_seconds=0.2)

        # Wait the rest of original window
        time.sleep(0.15)

        # Should be allowed now (original window expired)
        allowed = await dedup.should_trigger(key, window_seconds=0.2)
        assert allowed is True


# -----------------------------------------------------------------------------
# Contract: Configurable Window
# -----------------------------------------------------------------------------

class TestConfigurableWindow:
    """Tests that verify configurable dedup window per trigger type."""

    async def test_custom_window_per_trigger(self):
        """Different triggers can have different windows."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator
        import time

        dedup = TriggerDeduplicator()

        # Short window for card_complete
        key1 = "card_complete:repo:card"
        await dedup.should_trigger(key1, window_seconds=0.1)

        # Long window for push
        key2 = "push:repo:main"
        await dedup.should_trigger(key2, window_seconds=10.0)

        time.sleep(0.15)

        # Card trigger window expired
        allowed1 = await dedup.should_trigger(key1, window_seconds=0.1)
        assert allowed1 is True

        # Push trigger window still active
        allowed2 = await dedup.should_trigger(key2, window_seconds=10.0)
        assert allowed2 is False

    async def test_zero_window_allows_all(self):
        """Zero-second window allows all triggers (no dedup)."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo:card"

        # All triggers should be allowed with zero window
        allowed1 = await dedup.should_trigger(key, window_seconds=0)
        allowed2 = await dedup.should_trigger(key, window_seconds=0)
        allowed3 = await dedup.should_trigger(key, window_seconds=0)

        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is True


# -----------------------------------------------------------------------------
# Contract: Record Trigger
# -----------------------------------------------------------------------------

class TestRecordTrigger:
    """Tests that verify trigger recording for audit/debugging."""

    async def test_record_trigger_stores_timestamp(self):
        """Triggered events are recorded with timestamps."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo-123:card-456"

        before = datetime.utcnow()
        await dedup.should_trigger(key, window_seconds=60)
        after = datetime.utcnow()

        record = await dedup.get_last_trigger(key)
        assert record is not None
        assert before <= record["triggered_at"] <= after

    async def test_record_trigger_stores_pipeline_run_id(self):
        """Triggered events record which pipeline run was started."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo-123:card-456"
        pipeline_run_id = str(uuid4())

        await dedup.record_trigger(key, pipeline_run_id=pipeline_run_id)

        record = await dedup.get_last_trigger(key)
        assert record["pipeline_run_id"] == pipeline_run_id

    async def test_get_recent_triggers(self):
        """Can list recent triggers for debugging."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()

        await dedup.should_trigger("card_complete:repo:card1", window_seconds=60)
        await dedup.should_trigger("push:repo:main", window_seconds=60)
        await dedup.should_trigger("manual:repo:user1", window_seconds=60)

        recent = await dedup.get_recent_triggers(limit=10)
        assert len(recent) == 3


# -----------------------------------------------------------------------------
# Contract: Cleanup
# -----------------------------------------------------------------------------

class TestTriggerCleanup:
    """Tests that verify cleanup of old trigger records."""

    async def test_cleanup_removes_old_records(self):
        """Old trigger records are cleaned up."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo:card"

        # Add a trigger
        await dedup.should_trigger(key, window_seconds=0.1)

        # Verify it's recorded
        record = await dedup.get_last_trigger(key)
        assert record is not None

        # Cleanup old records (older than 0 seconds = all)
        cleaned = await dedup.cleanup(max_age_seconds=0)
        assert cleaned >= 1

        # Record should be gone
        record = await dedup.get_last_trigger(key)
        assert record is None

    async def test_cleanup_preserves_recent_records(self):
        """Recent trigger records are preserved during cleanup."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo:card"

        # Add a trigger
        await dedup.should_trigger(key, window_seconds=60)

        # Cleanup with long max age (should preserve recent)
        await dedup.cleanup(max_age_seconds=3600)

        # Record should still exist
        record = await dedup.get_last_trigger(key)
        assert record is not None


# -----------------------------------------------------------------------------
# Contract: Force Allow
# -----------------------------------------------------------------------------

class TestForceAllow:
    """Tests that verify force-allow bypasses deduplication."""

    async def test_force_bypasses_dedup(self):
        """Force flag bypasses deduplication check."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo:card"

        # First trigger
        await dedup.should_trigger(key, window_seconds=60)

        # Force-allowed duplicate
        allowed = await dedup.should_trigger(key, window_seconds=60, force=True)
        assert allowed is True

    async def test_force_still_records_trigger(self):
        """Force-allowed triggers are still recorded."""
        from app.services.workspace.trigger_dedup import TriggerDeduplicator

        dedup = TriggerDeduplicator()
        key = "card_complete:repo:card"

        await dedup.should_trigger(key, window_seconds=60)

        before = datetime.utcnow()
        await dedup.should_trigger(key, window_seconds=60, force=True)
        after = datetime.utcnow()

        record = await dedup.get_last_trigger(key)
        # Record should be updated to forced trigger time
        assert before <= record["triggered_at"] <= after
