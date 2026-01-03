"""
Trigger Deduplication for Phase 12.2.

Prevents duplicate pipeline triggers within a configurable time window.
- Same trigger_key within window -> ignore duplicate
- Same trigger_key after window -> allow new trigger

Trigger key format: {type}:{repo_id}:{ref}
Examples:
- push:repo-123:refs/heads/main
- card_complete:repo-123:card-456
- manual:repo-123:run-001
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any


class DuplicateTriggerError(Exception):
    """Raised when a duplicate trigger is detected."""
    pass


@dataclass
class TriggerKey:
    """
    Unique identifier for a trigger event.

    Format: {type}:{repo_id}:{ref}
    """
    trigger_type: str
    repo_id: str
    ref: str  # branch ref, card id, or run id depending on type

    def __str__(self) -> str:
        return f"{self.trigger_type}:{self.repo_id}:{self.ref}"

    def __hash__(self) -> int:
        return hash(str(self))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TriggerKey):
            return False
        return str(self) == str(other)

    @classmethod
    def for_push(
        cls,
        repo_id: str,
        ref: str,
        sha: Optional[str] = None,
    ) -> TriggerKey:
        """Create trigger key for push event."""
        ref_part = f"{ref}:{sha}" if sha else ref
        return cls(
            trigger_type="push",
            repo_id=repo_id,
            ref=ref_part,
        )

    @classmethod
    def for_card_complete(cls, repo_id: str, card_id: str) -> TriggerKey:
        """Create trigger key for card completion event."""
        return cls(
            trigger_type="card_complete",
            repo_id=repo_id,
            ref=card_id,
        )

    @classmethod
    def for_manual(cls, repo_id: str, run_id: str) -> TriggerKey:
        """Create trigger key for manual trigger."""
        return cls(
            trigger_type="manual",
            repo_id=repo_id,
            ref=run_id,
        )

    @classmethod
    def from_string(cls, key_str: str) -> TriggerKey:
        """Parse trigger key from string."""
        parts = key_str.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid trigger key format: {key_str}")
        return cls(
            trigger_type=parts[0],
            repo_id=parts[1],
            ref=parts[2],
        )


@dataclass
class TriggerCheckResult:
    """Result of checking a trigger for duplicates."""
    is_allowed: bool
    is_duplicate: bool
    original_run_id: Optional[str] = None
    run_id: Optional[str] = None  # Set if this trigger was allowed
    checked_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TriggerRecord:
    """Record of a processed trigger."""
    key: TriggerKey
    pipeline_run_id: Optional[str]
    recorded_at: datetime


class TriggerDeduplicator:
    """
    Manages trigger deduplication with configurable time window.

    Uses in-memory storage by default. For production, use with
    database session for persistence across restarts.
    """

    def __init__(
        self,
        window: timedelta = timedelta(hours=1),
        session: Optional[Any] = None,
    ):
        """
        Initialize deduplicator.

        Args:
            window: Time window for considering duplicates (default 1 hour)
            session: Optional database session for persistence
        """
        self.window = window
        self.session = session
        self._records: Dict[str, TriggerRecord] = {}

    @property
    def entry_count(self) -> int:
        """Number of entries in the deduplication cache."""
        return len(self._records)

    def check(self, key: TriggerKey) -> TriggerCheckResult:
        """
        Check if a trigger should be allowed.

        Args:
            key: Trigger key to check

        Returns:
            TriggerCheckResult indicating if trigger is allowed
        """
        now = datetime.utcnow()
        key_str = str(key)

        # Zero window means no deduplication
        if self.window.total_seconds() == 0:
            return TriggerCheckResult(
                is_allowed=True,
                is_duplicate=False,
            )

        # Check for existing record
        record = self._records.get(key_str)
        if record:
            # Check if within window
            age = now - record.recorded_at
            if age < self.window:
                return TriggerCheckResult(
                    is_allowed=False,
                    is_duplicate=True,
                    original_run_id=record.pipeline_run_id,
                )

        # No duplicate found
        return TriggerCheckResult(
            is_allowed=True,
            is_duplicate=False,
        )

    def record(
        self,
        key: TriggerKey,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """
        Record that a trigger was processed.

        Args:
            key: Trigger key that was processed
            pipeline_run_id: ID of the pipeline run created
        """
        now = datetime.utcnow()
        key_str = str(key)

        self._records[key_str] = TriggerRecord(
            key=key,
            pipeline_run_id=pipeline_run_id,
            recorded_at=now,
        )

    def check_and_record(
        self,
        key: TriggerKey,
        pipeline_run_id: Optional[str] = None,
    ) -> TriggerCheckResult:
        """
        Atomically check and record a trigger.

        Args:
            key: Trigger key to check
            pipeline_run_id: ID of the pipeline run (if allowed)

        Returns:
            TriggerCheckResult indicating if trigger was allowed
        """
        result = self.check(key)
        if result.is_allowed:
            self.record(key, pipeline_run_id)
            result.run_id = pipeline_run_id
        return result

    def cleanup_expired(self) -> int:
        """
        Remove expired entries from the cache.

        Returns:
            Number of entries removed
        """
        now = datetime.utcnow()
        expired_keys = [
            key_str
            for key_str, record in self._records.items()
            if (now - record.recorded_at) >= self.window
        ]

        for key_str in expired_keys:
            del self._records[key_str]

        return len(expired_keys)

    # Async versions for database-backed deduplication

    async def check_async(self, key: TriggerKey) -> TriggerCheckResult:
        """Async version of check() for database-backed deduplication."""
        # For now, delegate to sync version
        # TODO: Implement proper async database query
        return self.check(key)

    async def record_async(
        self,
        key: TriggerKey,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """Async version of record() for database-backed deduplication."""
        # For now, delegate to sync version
        # TODO: Implement proper async database insert
        self.record(key, pipeline_run_id)

    async def check_and_record_async(
        self,
        key: TriggerKey,
        pipeline_run_id: Optional[str] = None,
    ) -> TriggerCheckResult:
        """Async version of check_and_record() for database-backed deduplication."""
        # For now, delegate to sync version
        # TODO: Implement proper async transaction
        return self.check_and_record(key, pipeline_run_id)


# Module-level helper for time mocking in tests
# Tests can patch this to control "now"
def _get_now() -> datetime:
    return datetime.utcnow()
