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


# -----------------------------------------------------------------------------
# The YAML door: array in the file, graph in the row (12.8 §4.4)
# -----------------------------------------------------------------------------

class TestPipelineYamlToGraph:
    """`.lazyaf/pipelines/*.yaml` stays an array; the row it makes is a graph.

    The conversion happens ONCE, here, at the boundary. The alternative -
    persisting the array and letting the executor fork on which field is
    populated - is the fork this whole phase exists to delete.
    """

    def test_a_linear_yaml_becomes_a_linear_graph(self):
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph

        pipeline = PipelineYaml(
            name="ci",
            steps=[
                {"name": "Lint", "type": "script", "config": {"command": "a"}},
                {"name": "Test", "type": "script", "config": {"command": "b"}},
            ],
        )
        graph = pipeline_yaml_to_graph(pipeline)

        assert list(graph.steps) == ["step_0", "step_1"]
        assert graph.entry_points == ["step_0"]
        assert [(e.from_step, e.to_step, e.condition.value) for e in graph.edges] == [
            ("step_0", "step_1", "success")
        ]

    def test_an_unknown_step_type_refuses_naming_the_step(self):
        """`type: banana` is a pydantic ValidationError, not an ArrayConversionError.

        `PipelineStepYaml.type` is a bare `str` while `PipelineStepConfig.type`
        is the `StepType` enum, so the two refusals arrive as different
        exception types. Left unhandled that is a 500 on the push path AND a
        rolled-back sync that discards every other pipeline the push touched
        (`upsert_materialized_pipeline` runs outside the per-file
        swallow-and-continue). One refusal type, naming the step.
        """
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph
        from app.schemas.pipeline import ArrayConversionError

        pipeline = PipelineYaml(
            name="banana",
            steps=[{"name": "S", "type": "banana", "config": {"command": "x"}}],
        )
        with pytest.raises(ArrayConversionError) as excinfo:
            pipeline_yaml_to_graph(pipeline)
        assert "step #0" in str(excinfo.value)
        assert "'S'" in str(excinfo.value)
        assert "type" in str(excinfo.value)

    def test_a_stepless_yaml_refuses_rather_than_materializing_nothing(self):
        """QA4-08's root cause: an empty definition is not a valid pipeline.

        It used to run, do nothing, and report PASSED - a green tick for a
        pipeline that never existed.
        """
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph
        from app.schemas.pipeline import ArrayConversionError

        with pytest.raises(ArrayConversionError):
            pipeline_yaml_to_graph(PipelineYaml(name="nosteps", steps=[]))

    def test_a_mid_file_stop_that_orphans_the_tail_refuses(self):
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph
        from app.schemas.pipeline import ArrayConversionError

        pipeline = PipelineYaml(
            name="orphan",
            steps=[
                {"name": "First", "type": "script", "on_success": "stop",
                 "on_failure": "stop"},
                {"name": "Never", "type": "script"},
            ],
        )
        with pytest.raises(ArrayConversionError) as excinfo:
            pipeline_yaml_to_graph(pipeline)
        assert "unreachable" in str(excinfo.value)


class TestDogfoodPipelineConvertsFaithfully:
    """The repo's own `.lazyaf/pipelines/test-suite.yaml` is the acceptance.

    It is materialized on EVERY push to this repo, so it is the strongest
    available proof that the boundary converter is faithful - and the node ids
    it produces are the context-directory names, the debug breakpoint keys and
    what `verify_executor` correlates StepRuns by. Nothing tested that an
    authored id survived any conversion before 12.8.
    """

    @pytest.fixture(scope="class")
    def dogfood_yaml(self):
        path = (
            Path(__file__).parent.parent.parent.parent
            / ".lazyaf" / "pipelines" / "test-suite.yaml"
        )
        assert path.is_file(), f"the dogfood pipeline is missing: {path}"
        return PipelineYaml(**yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_every_authored_id_is_a_node_key(self, dogfood_yaml):
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph

        graph = pipeline_yaml_to_graph(dogfood_yaml)
        authored = [step.id for step in dogfood_yaml.steps]
        assert all(authored), "every dogfood step declares an id; keep it that way"
        assert list(graph.steps) == authored
        assert graph.entry_points == [authored[0]]

    def test_consecutive_next_becomes_one_success_edge_in_file_order(
        self, dogfood_yaml
    ):
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph

        graph = pipeline_yaml_to_graph(dogfood_yaml)
        authored = [step.id for step in dogfood_yaml.steps]

        # Every step is `on_success: next` / `on_failure: stop`, so the graph
        # is exactly the chain: one SUCCESS edge per consecutive pair, no
        # FAILURE edges, and nothing hanging off the last step.
        assert [
            (e.from_step, e.to_step, e.condition.value) for e in graph.edges
        ] == [
            (authored[i], authored[i + 1], "success")
            for i in range(len(authored) - 1)
        ]

    def test_the_converted_graph_has_no_definition_defects(self, dogfood_yaml):
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph
        from app.services.pipeline_executor import graph_definition_errors

        graph = pipeline_yaml_to_graph(dogfood_yaml)
        assert graph_definition_errors(graph.model_dump(mode="json")) == []

    def test_step_config_survives_verbatim(self, dogfood_yaml):
        """`requires:` / `needs:` / `control:` live in config and must not move.

        `verify_executor` reads `step["config"]["requires"]` to derive which
        lane each step was supposed to run on; a conversion that reshaped
        config would make the dogfood gate assert against the wrong thing.
        """
        from app.schemas.lazyaf_yaml import pipeline_yaml_to_graph

        graph = pipeline_yaml_to_graph(dogfood_yaml)
        for step in dogfood_yaml.steps:
            assert graph.steps[step.id].config == step.config
            assert graph.steps[step.id].timeout == step.timeout
            assert graph.steps[step.id].continue_in_context == step.continue_in_context


class TestUpsertRecordsARefusalInsteadOfRaising:
    """A refused conversion becomes `definition_error`, not an exception.

    `sync_repo_pipelines` calls the upsert OUTSIDE its per-file
    `except Exception: continue` and inside an `except Exception: rollback;
    raise`, so one unconvertible file raising here would discard every other
    pipeline that push synced.
    """

    @staticmethod
    async def _repo(db_session):
        from app.models import Repo

        repo = Repo(name="sync-repo", is_ingested=True, default_branch="main")
        db_session.add(repo)
        await db_session.commit()
        return repo

    async def test_a_refusal_lands_on_the_row_and_does_not_raise(self, db_session):
        from app.services.trigger_service import upsert_materialized_pipeline

        repo = await self._repo(db_session)
        pipeline = await upsert_materialized_pipeline(
            db_session,
            repo.id,
            PipelineYaml(
                name="banana",
                steps=[{"name": "S", "type": "banana", "config": {}}],
            ),
        )
        await db_session.commit()

        assert pipeline.definition_error, "the refusal must be visible on the row"
        assert "banana" in pipeline.definition_error
        assert pipeline.steps_graph is None

    async def test_a_later_good_sync_clears_the_error(self, db_session):
        from app.services.trigger_service import upsert_materialized_pipeline

        repo = await self._repo(db_session)
        await upsert_materialized_pipeline(
            db_session, repo.id,
            PipelineYaml(name="ci", steps=[{"name": "S", "type": "banana"}]),
        )
        await db_session.commit()

        pipeline = await upsert_materialized_pipeline(
            db_session, repo.id,
            PipelineYaml(name="ci", steps=[{"name": "S", "type": "script"}]),
        )
        await db_session.commit()

        assert pipeline.definition_error is None
        assert pipeline.steps_graph is not None

    async def test_a_refusal_leaves_the_previous_graph_in_place(self, db_session):
        """The row keeps its last-good graph - and the run guards refuse it.

        Overwriting it with NULL would lose the definition an operator needs
        to see to understand what changed; running it would be the Y5 dark
        channel. Keeping it visible AND unrunnable is the honest middle.
        """
        from app.services.trigger_service import upsert_materialized_pipeline

        repo = await self._repo(db_session)
        good = await upsert_materialized_pipeline(
            db_session, repo.id,
            PipelineYaml(name="ci", steps=[{"name": "S", "type": "script"}]),
        )
        await db_session.commit()
        last_good_graph = good.steps_graph

        broken = await upsert_materialized_pipeline(
            db_session, repo.id,
            PipelineYaml(name="ci", steps=[{"name": "S", "type": "banana"}]),
        )
        await db_session.commit()

        assert broken.steps_graph == last_good_graph
        assert broken.definition_error
