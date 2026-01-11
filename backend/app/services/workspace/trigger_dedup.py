"""
Trigger Deduplication - Phase 12.2

Prevents duplicate pipeline triggers within a configurable time window:
- Same trigger within window is ignored
- Same trigger after window expires is allowed
- Configurable dedup window per trigger type
- Force flag to bypass deduplication
"""
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional


def generate_trigger_key(
    trigger_type: str,
    repo_id: str,
    ref: str,
) -> str:
    """
    Generate a trigger deduplication key.

    Format: {type}:{repo_id}:{ref}

    Args:
        trigger_type: Type of trigger (card_complete, push, manual, etc.)
        repo_id: Repository identifier
        ref: Reference (card ID, branch name, user request ID, etc.)

    Returns:
        Formatted trigger key
    """
    return f"{trigger_type}:{repo_id}:{ref}"


def parse_trigger_key(key: str) -> Dict[str, str]:
    """
    Parse a trigger key back to its components.

    Args:
        key: Trigger key in format {type}:{repo_id}:{ref}

    Returns:
        Dict with 'type', 'repo_id', 'ref' keys
    """
    parts = key.split(":", 2)
    if len(parts) == 3:
        return {
            "type": parts[0],
            "repo_id": parts[1],
            "ref": parts[2],
        }
    return {
        "type": "",
        "repo_id": "",
        "ref": "",
    }


class TriggerDeduplicator:
    """
    Manages trigger deduplication to prevent duplicate pipeline runs.

    Tracks:
    - Last trigger time per key
    - Pipeline run ID associated with each trigger
    - Supports configurable dedup windows
    """

    def __init__(self):
        # key -> {triggered_at: datetime, pipeline_run_id: str | None}
        self._triggers: Dict[str, Dict[str, Any]] = {}

    async def should_trigger(
        self,
        key: str,
        window_seconds: float,
        force: bool = False,
    ) -> bool:
        """
        Check if a trigger should be allowed.

        Args:
            key: Trigger deduplication key
            window_seconds: Deduplication window in seconds
            force: If True, bypass deduplication check

        Returns:
            True if trigger is allowed, False if deduplicated
        """
        now = datetime.utcnow()

        # Zero window = no deduplication
        if window_seconds <= 0:
            self._triggers[key] = {
                "triggered_at": now,
                "pipeline_run_id": None,
            }
            return True

        # Force bypasses dedup
        if force:
            self._triggers[key] = {
                "triggered_at": now,
                "pipeline_run_id": None,
            }
            return True

        # Check existing trigger
        existing = self._triggers.get(key)
        if existing:
            window = timedelta(seconds=window_seconds)
            elapsed = now - existing["triggered_at"]
            if elapsed < window:
                # Still within window - deduplicate
                return False

        # Allow trigger and record it
        self._triggers[key] = {
            "triggered_at": now,
            "pipeline_run_id": None,
        }
        return True

    async def record_trigger(
        self,
        key: str,
        pipeline_run_id: str,
    ) -> None:
        """
        Record a trigger with its associated pipeline run.

        Args:
            key: Trigger deduplication key
            pipeline_run_id: ID of the pipeline run started
        """
        now = datetime.utcnow()
        self._triggers[key] = {
            "triggered_at": now,
            "pipeline_run_id": pipeline_run_id,
        }

    async def get_last_trigger(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Get the last trigger record for a key.

        Args:
            key: Trigger deduplication key

        Returns:
            Dict with 'triggered_at' and 'pipeline_run_id', or None
        """
        return self._triggers.get(key)

    async def get_recent_triggers(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get recent triggers for debugging.

        Args:
            limit: Maximum number of triggers to return

        Returns:
            List of trigger records, sorted by most recent first
        """
        # Sort by triggered_at descending
        sorted_items = sorted(
            [
                {"key": key, **record}
                for key, record in self._triggers.items()
            ],
            key=lambda x: x["triggered_at"],
            reverse=True,
        )
        return sorted_items[:limit]

    async def cleanup(self, max_age_seconds: float) -> int:
        """
        Remove trigger records older than max_age.

        Args:
            max_age_seconds: Maximum age in seconds for records to keep

        Returns:
            Number of records removed
        """
        now = datetime.utcnow()
        max_age = timedelta(seconds=max_age_seconds)

        keys_to_remove = []
        for key, record in self._triggers.items():
            age = now - record["triggered_at"]
            if age >= max_age:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._triggers[key]

        return len(keys_to_remove)
