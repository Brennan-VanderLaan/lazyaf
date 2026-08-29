"""
Unit tests for repo-defined pipeline sync building blocks (Phase 0a).

Covers:
- PipelineYaml `triggers:` parsing (same shape as platform Pipeline.triggers)
- Rejection of invalid trigger shapes
- Trigger dedup wiring in trigger_service (reset hook)
"""
import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas.lazyaf_yaml import PipelineYaml
from app.schemas.pipeline import TriggerConfig


# -----------------------------------------------------------------------------
# PipelineYaml triggers parsing
# -----------------------------------------------------------------------------

class TestPipelineYamlTriggers:
    """Tests for the `triggers:` block in .lazyaf/pipelines/*.yaml files."""

    def test_triggers_default_to_empty_list(self):
        """A yaml without triggers parses with triggers == []."""
        pipeline = PipelineYaml(name="ci", steps=[])
        assert pipeline.triggers == []

    def test_push_trigger_with_branches(self):
        """Push trigger with branches config parses to TriggerConfig."""
        data = yaml.safe_load(
            """
name: ci
triggers:
  - type: push
    config:
      branches: ["main", "release/*"]
steps: []
"""
        )
        pipeline = PipelineYaml(**data)
        assert len(pipeline.triggers) == 1
        trigger = pipeline.triggers[0]
        assert isinstance(trigger, TriggerConfig)
        assert trigger.type == "push"
        assert trigger.config == {"branches": ["main", "release/*"]}
        assert trigger.enabled is True
        assert trigger.on_pass == "nothing"
        assert trigger.on_fail == "nothing"

    def test_card_complete_trigger_with_actions(self):
        """card_complete trigger with status/on_pass/on_fail parses."""
        data = yaml.safe_load(
            """
name: gate
triggers:
  - type: card_complete
    config:
      status: in_review
    on_pass: merge
    on_fail: reject
steps: []
"""
        )
        pipeline = PipelineYaml(**data)
        trigger = pipeline.triggers[0]
        assert trigger.type == "card_complete"
        assert trigger.config == {"status": "in_review"}
        assert trigger.on_pass == "merge"
        assert trigger.on_fail == "reject"

    def test_multiple_triggers(self):
        """A yaml can bind several triggers."""
        pipeline = PipelineYaml(
            name="ci",
            triggers=[
                {"type": "push", "config": {"branches": ["main"]}},
                {"type": "card_complete", "config": {"status": "done"}},
            ],
        )
        assert [t.type for t in pipeline.triggers] == ["push", "card_complete"]

    def test_disabled_trigger_round_trips(self):
        """enabled: false survives parsing."""
        pipeline = PipelineYaml(
            name="ci",
            triggers=[{"type": "push", "enabled": False}],
        )
        assert pipeline.triggers[0].enabled is False

    def test_trigger_dump_matches_platform_triggers_json_shape(self):
        """model_dump produces exactly the dict shape parse_triggers returns.

        This is the wire contract: the sync stores yaml triggers verbatim as
        the platform Pipeline.triggers JSON, and trigger matching reads them
        back with parse_triggers.
        """
        from app.services.trigger_service import parse_triggers

        pipeline = PipelineYaml(
            name="ci",
            triggers=[{"type": "push", "config": {"branches": ["main"]}}],
        )
        triggers_json = json.dumps([t.model_dump() for t in pipeline.triggers])
        parsed = parse_triggers(triggers_json)

        assert parsed == [
            {
                "type": "push",
                "config": {"branches": ["main"]},
                "enabled": True,
                "on_pass": "nothing",
                "on_fail": "nothing",
            }
        ]


class TestPipelineYamlTriggersInvalidShapes:
    """Invalid trigger shapes are rejected at parse time."""

    @pytest.mark.parametrize(
        "triggers",
        [
            pytest.param("push", id="plain-string"),
            pytest.param({"type": "push"}, id="mapping-not-list"),
            pytest.param([{"config": {"branches": ["main"]}}], id="missing-type"),
            pytest.param([{"type": "push", "config": ["main"]}], id="config-not-mapping"),
            pytest.param([{"type": "push", "enabled": "definitely"}], id="non-boolish-enabled"),
            pytest.param(["push"], id="scalar-list-item"),
        ],
    )
    def test_invalid_trigger_shape_is_rejected(self, triggers):
        with pytest.raises(ValidationError):
            PipelineYaml(name="ci", triggers=triggers)


# -----------------------------------------------------------------------------
# Trigger dedup wiring
# -----------------------------------------------------------------------------

class TestTriggerDedupWiring:
    """trigger_service exposes a process-local deduplicator + reset hook."""

    def setup_method(self):
        from app.services.trigger_service import reset_trigger_dedup

        reset_trigger_dedup()

    def teardown_method(self):
        from app.services.trigger_service import reset_trigger_dedup

        reset_trigger_dedup()

    async def test_reset_hook_clears_recorded_triggers(self):
        """reset_trigger_dedup clears state so the same key fires again."""
        from app.services.trigger_service import (
            PUSH_DEDUP_WINDOW_SECONDS,
            reset_trigger_dedup,
            trigger_deduplicator,
        )

        key = "push:repo-1:pipe-1:main:abc123"
        assert await trigger_deduplicator.should_trigger(key, PUSH_DEDUP_WINDOW_SECONDS)
        assert not await trigger_deduplicator.should_trigger(key, PUSH_DEDUP_WINDOW_SECONDS)

        reset_trigger_dedup()

        assert await trigger_deduplicator.should_trigger(key, PUSH_DEDUP_WINDOW_SECONDS)
