"""
Unit Tests for Graph-Based Pipeline Schemas (Phase 1: Graph Creep)

Tests for:
- EdgeCondition enum
- PipelineNodePosition model
- PipelineEdge model
- PipelineStepV2 model
- PipelineGraphModel validation and utilities
- array_to_graph conversion
- describe_terminal_action / StepActions / PipelineStepV2.actions (12.8, P1)
"""

import json

import pytest
from pydantic import ValidationError

import sys
from pathlib import Path
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas.pipeline import (
    TERMINAL_ACTION_PREFIXES,
    ArrayConversionError,
    EdgeCondition,
    PipelineNodePosition,
    PipelineEdge,
    PipelineStepV2,
    PipelineGraphModel,
    PipelineStepConfig,
    StepActions,
    array_to_graph,
    describe_terminal_action,
)
from app.models.card import StepType


class TestEdgeCondition:
    """Tests for EdgeCondition enum."""

    def test_success_condition(self):
        """SUCCESS condition has correct value."""
        assert EdgeCondition.SUCCESS.value == "success"

    def test_failure_condition(self):
        """FAILURE condition has correct value."""
        assert EdgeCondition.FAILURE.value == "failure"

    def test_always_condition(self):
        """ALWAYS condition has correct value."""
        assert EdgeCondition.ALWAYS.value == "always"

    def test_condition_from_string(self):
        """Condition can be created from string value."""
        assert EdgeCondition("success") == EdgeCondition.SUCCESS
        assert EdgeCondition("failure") == EdgeCondition.FAILURE
        assert EdgeCondition("always") == EdgeCondition.ALWAYS


class TestPipelineNodePosition:
    """Tests for PipelineNodePosition model."""

    def test_create_with_integers(self):
        """Position can be created with integer coordinates."""
        pos = PipelineNodePosition(x=100, y=200)
        assert pos.x == 100.0
        assert pos.y == 200.0

    def test_create_with_floats(self):
        """Position can be created with float coordinates."""
        pos = PipelineNodePosition(x=150.5, y=275.75)
        assert pos.x == 150.5
        assert pos.y == 275.75

    def test_negative_coordinates_allowed(self):
        """Negative coordinates are allowed."""
        pos = PipelineNodePosition(x=-100, y=-50)
        assert pos.x == -100.0
        assert pos.y == -50.0


class TestPipelineEdge:
    """Tests for PipelineEdge model."""

    def test_create_success_edge(self):
        """Success edge can be created."""
        edge = PipelineEdge(
            id="edge_1",
            from_step="step_a",
            to_step="step_b",
            condition=EdgeCondition.SUCCESS,
        )
        assert edge.id == "edge_1"
        assert edge.from_step == "step_a"
        assert edge.to_step == "step_b"
        assert edge.condition == EdgeCondition.SUCCESS

    def test_create_failure_edge(self):
        """Failure edge can be created."""
        edge = PipelineEdge(
            id="edge_fail",
            from_step="main",
            to_step="error_handler",
            condition=EdgeCondition.FAILURE,
        )
        assert edge.condition == EdgeCondition.FAILURE

    def test_default_condition_is_success(self):
        """Default condition is SUCCESS."""
        edge = PipelineEdge(
            id="e1",
            from_step="a",
            to_step="b",
        )
        assert edge.condition == EdgeCondition.SUCCESS

    def test_edge_from_string_condition(self):
        """Edge can be created with string condition."""
        edge = PipelineEdge(
            id="e1",
            from_step="a",
            to_step="b",
            condition="failure",
        )
        assert edge.condition == EdgeCondition.FAILURE


class TestPipelineStepV2:
    """Tests for PipelineStepV2 model."""

    def test_create_script_step(self):
        """Script step can be created."""
        step = PipelineStepV2(
            id="build_step",
            name="Build",
            type=StepType.SCRIPT,
            config={"command": "npm run build"},
        )
        assert step.id == "build_step"
        assert step.name == "Build"
        assert step.type == StepType.SCRIPT
        assert step.config["command"] == "npm run build"

    def test_create_docker_step(self):
        """Docker step can be created."""
        step = PipelineStepV2(
            id="test_step",
            name="Test",
            type=StepType.DOCKER,
            config={"image": "node:18", "command": "npm test"},
        )
        assert step.type == StepType.DOCKER
        assert step.config["image"] == "node:18"

    def test_create_agent_step(self):
        """Agent step can be created."""
        step = PipelineStepV2(
            id="agent_step",
            name="AI Task",
            type=StepType.AGENT,
            config={"runner_type": "claude", "title": "Fix bug"},
        )
        assert step.type == StepType.AGENT

    def test_step_with_position(self):
        """Step can have position for UI layout."""
        step = PipelineStepV2(
            id="positioned",
            name="With Position",
            type=StepType.SCRIPT,
            config={},
            position=PipelineNodePosition(x=200, y=150),
        )
        assert step.position is not None
        assert step.position.x == 200
        assert step.position.y == 150

    def test_step_without_position(self):
        """Step position is optional."""
        step = PipelineStepV2(
            id="no_pos",
            name="No Position",
            type=StepType.SCRIPT,
            config={},
        )
        assert step.position is None

    def test_default_timeout(self):
        """Default timeout is 300 seconds."""
        step = PipelineStepV2(
            id="s1",
            name="Step",
            type=StepType.SCRIPT,
            config={},
        )
        assert step.timeout == 300

    def test_custom_timeout(self):
        """Custom timeout can be specified."""
        step = PipelineStepV2(
            id="s1",
            name="Slow Step",
            type=StepType.SCRIPT,
            config={},
            timeout=3600,
        )
        assert step.timeout == 3600

    def test_continue_in_context_default_false(self):
        """continue_in_context defaults to False."""
        step = PipelineStepV2(
            id="s1",
            name="Step",
            type=StepType.SCRIPT,
            config={},
        )
        assert step.continue_in_context is False


class TestPipelineGraphModel:
    """Tests for PipelineGraphModel validation and utilities."""

    def test_create_simple_graph(self):
        """Simple two-node graph can be created."""
        graph = PipelineGraphModel(
            steps={
                "a": PipelineStepV2(id="a", name="A", type=StepType.SCRIPT, config={}),
                "b": PipelineStepV2(id="b", name="B", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="a", to_step="b", condition=EdgeCondition.SUCCESS),
            ],
            entry_points=["a"],
        )
        assert len(graph.steps) == 2
        assert len(graph.edges) == 1
        assert graph.entry_points == ["a"]

    def test_default_version_is_2(self):
        """Default version is 2."""
        graph = PipelineGraphModel(
            steps={"s": PipelineStepV2(id="s", name="S", type=StepType.SCRIPT, config={})},
            edges=[],
            entry_points=["s"],
        )
        assert graph.version == 2

    def test_invalid_edge_from_step_rejected(self):
        """Edge referencing non-existent from_step is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineGraphModel(
                steps={
                    "real": PipelineStepV2(id="real", name="Real", type=StepType.SCRIPT, config={}),
                },
                edges=[
                    PipelineEdge(id="e1", from_step="fake", to_step="real"),
                ],
                entry_points=["real"],
            )
        assert "non-existent from_step" in str(exc_info.value)

    def test_invalid_edge_to_step_rejected(self):
        """Edge referencing non-existent to_step is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineGraphModel(
                steps={
                    "real": PipelineStepV2(id="real", name="Real", type=StepType.SCRIPT, config={}),
                },
                edges=[
                    PipelineEdge(id="e1", from_step="real", to_step="fake"),
                ],
                entry_points=["real"],
            )
        assert "non-existent to_step" in str(exc_info.value)

    def test_empty_entry_points_rejected(self):
        """Graph with no entry points is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineGraphModel(
                steps={
                    "s": PipelineStepV2(id="s", name="S", type=StepType.SCRIPT, config={}),
                },
                edges=[],
                entry_points=[],
            )
        assert "at least one entry point" in str(exc_info.value)

    def test_invalid_entry_point_rejected(self):
        """Entry point referencing non-existent step is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineGraphModel(
                steps={
                    "real": PipelineStepV2(id="real", name="Real", type=StepType.SCRIPT, config={}),
                },
                edges=[],
                entry_points=["fake"],
            )
        assert "non-existent step" in str(exc_info.value)

    def test_get_successors_success_condition(self):
        """get_successors returns steps following under success condition."""
        graph = PipelineGraphModel(
            steps={
                "a": PipelineStepV2(id="a", name="A", type=StepType.SCRIPT, config={}),
                "b": PipelineStepV2(id="b", name="B", type=StepType.SCRIPT, config={}),
                "c": PipelineStepV2(id="c", name="C", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="a", to_step="b", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e2", from_step="a", to_step="c", condition=EdgeCondition.FAILURE),
            ],
            entry_points=["a"],
        )
        successors = graph.get_successors("a", EdgeCondition.SUCCESS)
        assert successors == ["b"]

    def test_get_successors_failure_condition(self):
        """get_successors returns steps following under failure condition."""
        graph = PipelineGraphModel(
            steps={
                "main": PipelineStepV2(id="main", name="Main", type=StepType.SCRIPT, config={}),
                "error": PipelineStepV2(id="error", name="Error", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="main", to_step="error", condition=EdgeCondition.FAILURE),
            ],
            entry_points=["main"],
        )
        successors = graph.get_successors("main", EdgeCondition.FAILURE)
        assert successors == ["error"]

    def test_get_successors_fan_out(self):
        """get_successors returns multiple steps for fan-out."""
        graph = PipelineGraphModel(
            steps={
                "start": PipelineStepV2(id="start", name="Start", type=StepType.SCRIPT, config={}),
                "a": PipelineStepV2(id="a", name="A", type=StepType.SCRIPT, config={}),
                "b": PipelineStepV2(id="b", name="B", type=StepType.SCRIPT, config={}),
                "c": PipelineStepV2(id="c", name="C", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="start", to_step="a", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e2", from_step="start", to_step="b", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e3", from_step="start", to_step="c", condition=EdgeCondition.SUCCESS),
            ],
            entry_points=["start"],
        )
        successors = graph.get_successors("start", EdgeCondition.SUCCESS)
        assert set(successors) == {"a", "b", "c"}

    def test_get_predecessors(self):
        """get_predecessors returns steps that must complete before given step."""
        graph = PipelineGraphModel(
            steps={
                "a": PipelineStepV2(id="a", name="A", type=StepType.SCRIPT, config={}),
                "b": PipelineStepV2(id="b", name="B", type=StepType.SCRIPT, config={}),
                "join": PipelineStepV2(id="join", name="Join", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="a", to_step="join", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e2", from_step="b", to_step="join", condition=EdgeCondition.SUCCESS),
            ],
            entry_points=["a", "b"],
        )
        predecessors = graph.get_predecessors("join")
        assert set(predecessors) == {"a", "b"}

    def test_get_predecessors_entry_point(self):
        """Entry point has no predecessors."""
        graph = PipelineGraphModel(
            steps={
                "start": PipelineStepV2(id="start", name="Start", type=StepType.SCRIPT, config={}),
            },
            edges=[],
            entry_points=["start"],
        )
        predecessors = graph.get_predecessors("start")
        assert predecessors == []

    def test_get_all_successors(self):
        """get_all_successors returns all following steps regardless of condition."""
        graph = PipelineGraphModel(
            steps={
                "main": PipelineStepV2(id="main", name="Main", type=StepType.SCRIPT, config={}),
                "ok": PipelineStepV2(id="ok", name="OK", type=StepType.SCRIPT, config={}),
                "err": PipelineStepV2(id="err", name="Err", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="main", to_step="ok", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e2", from_step="main", to_step="err", condition=EdgeCondition.FAILURE),
            ],
            entry_points=["main"],
        )
        all_successors = graph.get_all_successors("main")
        assert set(all_successors) == {"ok", "err"}

    def test_diamond_pattern(self):
        """Diamond pattern graph (fan-out then fan-in) is valid."""
        graph = PipelineGraphModel(
            steps={
                "start": PipelineStepV2(id="start", name="Start", type=StepType.SCRIPT, config={}),
                "left": PipelineStepV2(id="left", name="Left", type=StepType.SCRIPT, config={}),
                "right": PipelineStepV2(id="right", name="Right", type=StepType.SCRIPT, config={}),
                "end": PipelineStepV2(id="end", name="End", type=StepType.SCRIPT, config={}),
            },
            edges=[
                PipelineEdge(id="e1", from_step="start", to_step="left", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e2", from_step="start", to_step="right", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e3", from_step="left", to_step="end", condition=EdgeCondition.SUCCESS),
                PipelineEdge(id="e4", from_step="right", to_step="end", condition=EdgeCondition.SUCCESS),
            ],
            entry_points=["start"],
        )
        assert len(graph.steps) == 4
        assert len(graph.edges) == 4
        assert graph.get_predecessors("end") == ["left", "right"]

    def test_multiple_entry_points(self):
        """Graph can have multiple entry points for parallel start."""
        graph = PipelineGraphModel(
            steps={
                "a": PipelineStepV2(id="a", name="A", type=StepType.SCRIPT, config={}),
                "b": PipelineStepV2(id="b", name="B", type=StepType.SCRIPT, config={}),
            },
            edges=[],
            entry_points=["a", "b"],
        )
        assert graph.entry_points == ["a", "b"]


class TestArrayToGraphConversion:
    """Tests for array_to_graph conversion utility."""

    def test_convert_single_step(self):
        """Single step is converted to graph with one node."""
        steps = [
            PipelineStepConfig(
                name="Build",
                type=StepType.SCRIPT,
                config={"command": "npm build"},
            )
        ]
        graph = array_to_graph(steps)

        assert len(graph.steps) == 1
        assert "step_0" in graph.steps
        assert graph.steps["step_0"].name == "Build"
        assert graph.entry_points == ["step_0"]
        assert len(graph.edges) == 0

    def test_convert_two_steps(self):
        """Two sequential steps create one success edge."""
        steps = [
            PipelineStepConfig(name="Build", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="Test", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        assert len(graph.steps) == 2
        assert len(graph.edges) == 1

        edge = graph.edges[0]
        assert edge.from_step == "step_0"
        assert edge.to_step == "step_1"
        assert edge.condition == EdgeCondition.SUCCESS

    def test_convert_preserves_timeout(self):
        """Custom timeout is preserved."""
        steps = [
            PipelineStepConfig(
                name="Slow",
                type=StepType.SCRIPT,
                config={},
                timeout=3600,
            )
        ]
        graph = array_to_graph(steps)

        assert graph.steps["step_0"].timeout == 3600

    def test_convert_preserves_continue_in_context(self):
        """continue_in_context flag is preserved."""
        steps = [
            PipelineStepConfig(
                name="Step",
                type=StepType.SCRIPT,
                config={},
                continue_in_context=True,
            )
        ]
        graph = array_to_graph(steps)

        assert graph.steps["step_0"].continue_in_context is True

    def test_convert_auto_layout_vertical(self):
        """Steps are laid out vertically."""
        steps = [
            PipelineStepConfig(name="A", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="B", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="C", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        assert graph.steps["step_0"].position.y == 0
        assert graph.steps["step_1"].position.y == 150
        assert graph.steps["step_2"].position.y == 300

    def test_convert_on_failure_next(self):
        """on_failure: next creates failure edge."""
        steps = [
            PipelineStepConfig(
                name="Main",
                type=StepType.SCRIPT,
                config={},
                on_success="next",
                on_failure="next",
            ),
            PipelineStepConfig(name="Next", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        # Should have both success and failure edges
        assert len(graph.edges) == 2
        conditions = {e.condition for e in graph.edges}
        assert EdgeCondition.SUCCESS in conditions
        assert EdgeCondition.FAILURE in conditions

    def test_convert_on_success_stop_no_edge(self):
        """on_success: stop creates no edge to the next step.

        12.8 P2 CHANGED THIS TEST, deliberately and per plan §2. It used to
        put `stop` on a NON-final step and assert that the following step was
        silently orphaned. That is now a refusal (see
        `TestArrayToGraphRefusesAnOrphanedTail`): the orphan makes
        `graph_definition_errors` reject the graph and `_verify_graph_coverage`
        FAIL the run, so the old behaviour turned a green v1 pipeline into a
        red v2 one for the wrong reason.

        The half of the claim that survives is the one this test was really
        for: `stop` emits no edge. Asserted where it costs nothing - on the
        last step, where there is no tail to orphan.
        """
        steps = [
            PipelineStepConfig(
                name="Work", type=StepType.SCRIPT, config={}, on_success="next"
            ),
            PipelineStepConfig(
                name="Terminal",
                type=StepType.SCRIPT,
                config={},
                on_success="stop",
            ),
        ]
        graph = array_to_graph(steps)

        assert [e.from_step for e in graph.edges] == ["step_0"]
        assert graph.get_all_successors("step_1") == []

    def test_convert_empty_raises_error(self):
        """Converting empty array raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            array_to_graph([])
        assert "empty" in str(exc_info.value).lower()

    def test_convert_three_step_chain(self):
        """Three-step chain creates two edges."""
        steps = [
            PipelineStepConfig(name="A", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="B", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="C", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        assert len(graph.steps) == 3
        assert len(graph.edges) == 2
        assert graph.entry_points == ["step_0"]

        # Verify chain: A -> B -> C
        a_successors = graph.get_successors("step_0", EdgeCondition.SUCCESS)
        assert a_successors == ["step_1"]

        b_successors = graph.get_successors("step_1", EdgeCondition.SUCCESS)
        assert b_successors == ["step_2"]

    def test_converted_graph_is_valid(self):
        """Converted graph passes all validation."""
        steps = [
            PipelineStepConfig(name="Build", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="Test", type=StepType.SCRIPT, config={}, on_success="next"),
            PipelineStepConfig(name="Deploy", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        # Should not raise validation errors
        assert graph.version == 2
        assert len(graph.entry_points) > 0


# =============================================================================
# 12.8 P1 - the graph gains the terminal-action capability (purely additive)
# =============================================================================
#
# v1's `on_success` / `on_failure` carried FLOW and EFFECT in one string.
# Flow (`next` / `stop`) is an edge, or the absence of one, and already lives
# on the graph. What is left - `merge:{branch}` and `trigger:{card_id}` - is
# pure side effect, and `array_to_graph` silently dropped both. `StepActions`
# is where the effect half lands.
#
# `describe_terminal_action` is the SINGLE definition of that vocabulary (R3):
# these tests are the vocabulary's pin, and the executor's dispatcher calls
# the same function, so a form accepted here is dispatchable there and a form
# refused here is a named failure there - never a silent no-op (R1).


VALID_TERMINAL_ACTIONS = [
    "trigger:card-abc",
    "trigger:9f1c2b7e-0000-4000-8000-000000000000",
    "merge:main",
    "merge:release/1.4",
    "merge:feature/a b",  # a branch name is opaque to the vocabulary
]


class TestDescribeTerminalActionVocabulary:
    """The closed vocabulary, function-level. One definition, R3."""

    @pytest.mark.parametrize("action", VALID_TERMINAL_ACTIONS)
    def test_valid_forms_are_accepted(self, action):
        """A dispatchable node action describes no problem."""
        assert describe_terminal_action(action) is None

    def test_prefixes_are_exactly_trigger_and_merge(self):
        """The prefix tuple is the vocabulary, and it excludes flow."""
        assert TERMINAL_ACTION_PREFIXES == ("trigger:", "merge:")

    @pytest.mark.parametrize("action", ["next", "stop"])
    def test_flow_words_are_refused_as_flow_not_as_typos(self, action):
        """next/stop are FLOW: the message must say so and point at edges.

        Refusing them with a bare "unknown action" would send an author
        looking for a spelling mistake instead of telling them the concept
        moved to the edges.
        """
        problem = describe_terminal_action(action)
        assert problem is not None
        assert "FLOW" in problem
        assert "edge" in problem
        assert "trigger:{card_id}" in problem and "merge:{branch}" in problem

    def test_trigger_pipeline_is_refused_as_retired_with_the_replacement(self):
        """The retired form names its replacement, not just its absence."""
        problem = describe_terminal_action("trigger:pipeline:pipeline-123")
        assert problem is not None
        assert "retired" in problem
        assert "card_complete" in problem and "push" in problem

    def test_trigger_pipeline_retirement_beats_the_trigger_prefix(self):
        """Ordering pin: trigger:pipeline:x also matches the trigger: prefix.

        If the prefix loop ran first the string would be ACCEPTED as a card
        trigger and dispatch would go looking for a card whose id is
        "pipeline:x" - the exact silent-wrong-thing this vocabulary exists to
        stop.
        """
        problem = describe_terminal_action("trigger:pipeline:x")
        assert problem is not None
        assert "retired" in problem

    @pytest.mark.parametrize(
        "action", ["trigger:", "merge:", "trigger:   ", "merge:\t"]
    )
    def test_empty_target_is_refused(self, action):
        """A prefix with nothing after it names no card and no branch."""
        problem = describe_terminal_action(action)
        assert problem is not None
        assert "empty" in problem
        assert repr(action) in problem

    @pytest.mark.parametrize(
        "action", [123, None, True, ["merge:main"], {"merge": "main"}, 1.5]
    )
    def test_non_string_is_refused_naming_the_type(self, action):
        """The executor calls this over raw JSON, where anything can arrive."""
        problem = describe_terminal_action(action)
        assert problem is not None
        assert "must be a string" in problem
        assert type(action).__name__ in problem

    @pytest.mark.parametrize(
        "action", ["deploy", "trigger-card-abc", "merge", "", "continue", "TRIGGER:x"]
    )
    def test_unknown_action_is_refused_naming_itself(self, action):
        """A typo is named, with the vocabulary, so it can be fixed."""
        problem = describe_terminal_action(action)
        assert problem is not None
        assert repr(action) in problem
        assert "trigger:{card_id}" in problem and "merge:{branch}" in problem


class TestStepActions:
    """The model that carries the vocabulary onto a node."""

    def test_defaults_are_three_empty_lists(self):
        actions = StepActions()
        assert actions.success == []
        assert actions.failure == []
        assert actions.always == []

    def test_conditions_are_the_edge_condition_vocabulary(self):
        """One notion of "when" (R3): the same three words the edges use."""
        assert set(StepActions.model_fields) == {
            condition.value for condition in EdgeCondition
        }

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    @pytest.mark.parametrize("action", VALID_TERMINAL_ACTIONS)
    def test_every_valid_form_is_accepted_on_every_condition(
        self, condition, action
    ):
        actions = StepActions(**{condition: [action]})
        assert getattr(actions, condition) == [action]

    def test_a_condition_holds_several_actions(self):
        """The thing v1 could not say: merge AND spawn a fix card."""
        actions = StepActions(success=["merge:main", "trigger:card-abc"])
        assert actions.success == ["merge:main", "trigger:card-abc"]

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    @pytest.mark.parametrize("action", ["next", "stop"])
    def test_flow_is_refused_on_every_condition(self, condition, action):
        """The validator runs on all three fields, not just the first."""
        with pytest.raises(ValidationError) as exc:
            StepActions(**{condition: [action]})
        message = str(exc.value)
        assert "FLOW" in message and "edge" in message

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    def test_trigger_pipeline_is_refused_on_every_condition(self, condition):
        with pytest.raises(ValidationError) as exc:
            StepActions(**{condition: ["trigger:pipeline:p1"]})
        assert "retired" in str(exc.value)

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    def test_empty_target_is_refused_on_every_condition(self, condition):
        with pytest.raises(ValidationError) as exc:
            StepActions(**{condition: ["merge:"]})
        assert "empty" in str(exc.value)

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    def test_unknown_action_is_refused_on_every_condition(self, condition):
        with pytest.raises(ValidationError) as exc:
            StepActions(**{condition: ["deploy"]})
        assert "unknown node action" in str(exc.value)

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    def test_a_non_string_never_reaches_the_field(self, condition):
        """list[str] refuses the type before the vocabulary is consulted.

        Pinned so nobody "fixes" the missing vocabulary wording by widening
        the annotation to list[Any]: the refusal is the point, and
        describe_terminal_action still covers the non-string case for the
        executor, which reads raw JSON rather than this model.
        """
        with pytest.raises(ValidationError) as exc:
            StepActions(**{condition: [123]})
        assert exc.value.errors()[0]["type"] == "string_type"
        assert describe_terminal_action(123) is not None

    def test_one_bad_action_refuses_the_whole_list(self):
        """No partial acceptance - a half-applied effect list is dark."""
        with pytest.raises(ValidationError):
            StepActions(failure=["trigger:card-abc", "explode"])


class TestPipelineStepV2Actions:
    """The additive-default guarantee. This is what keeps P1 green."""

    def test_a_step_written_without_actions_gets_an_empty_one(self):
        step = PipelineStepV2(id="s1", name="Step", type=StepType.SCRIPT)
        assert isinstance(step.actions, StepActions)
        assert step.actions.success == []
        assert step.actions.failure == []
        assert step.actions.always == []

    def test_each_step_gets_its_own_actions_instance(self):
        """default_factory, not a shared mutable default.

        A shared instance would let one node's actions appear on every other
        node in the process - the loudest possible version of a silent wrong
        effect.
        """
        a = PipelineStepV2(id="a", name="A", type=StepType.SCRIPT)
        b = PipelineStepV2(id="b", name="B", type=StepType.SCRIPT)
        a.actions.success.append("merge:main")
        assert b.actions.success == []

    def test_a_pre_p1_node_dict_still_validates_unchanged(self):
        """A node stored before actions existed keeps its exact meaning.

        This dict is the full shape PipelineStepV2.model_dump() produced
        before this field landed. Nothing about it changes; it simply gains
        an empty effect list.
        """
        stored = {
            "id": "tier1",
            "name": "T1",
            "type": "script",
            "config": {"command": "make test"},
            "position": {"x": 100.0, "y": 150.0},
            "timeout": 1800,
            "continue_in_context": False,
        }
        step = PipelineStepV2(**stored)
        assert step.id == "tier1"
        assert step.timeout == 1800
        assert step.config == {"command": "make test"}
        assert step.actions.model_dump() == {
            "success": [], "failure": [], "always": []
        }

    def test_the_wire_shape_of_a_defaulted_node(self):
        """Contract 4.1: absent actions serialize as three empty lists."""
        step = PipelineStepV2(
            id="tier1",
            name="T1",
            type=StepType.SCRIPT,
            config={"command": "make test"},
            timeout=1800,
        )
        assert step.model_dump()["actions"] == {
            "success": [], "failure": [], "always": []
        }

    def test_actions_are_carried_on_the_node(self):
        step = PipelineStepV2(
            id="tier1",
            name="T1",
            type=StepType.SCRIPT,
            actions={"failure": ["trigger:card-abc"]},
        )
        assert step.actions.failure == ["trigger:card-abc"]
        assert step.actions.success == []
        assert step.model_dump()["actions"] == {
            "success": [], "failure": ["trigger:card-abc"], "always": []
        }

    def test_a_bad_action_refuses_the_whole_node(self):
        """The refusal reaches the boundary through the nesting."""
        with pytest.raises(ValidationError) as exc:
            PipelineStepV2(
                id="s1",
                name="Step",
                type=StepType.SCRIPT,
                actions={"success": ["trigger:pipeline:p1"]},
            )
        assert "retired" in str(exc.value)

    def test_actions_is_not_named_on_success(self):
        """Section 1.3, non-negotiable.

        routers/pipelines.export_pipeline_yaml ALREADY writes on_success on a
        graph step, meaning "the id of the node this success edge points at"
        (verified: it builds success_targets from the outgoing edges and
        assigns them to that key). A node field spelled the same way would
        put two vocabularies behind one key on day one - an R3 violation
        baked in.
        """
        assert "on_success" not in PipelineStepV2.model_fields
        assert "on_failure" not in PipelineStepV2.model_fields
        assert "actions" in PipelineStepV2.model_fields

    @pytest.mark.parametrize("condition", ["success", "failure", "always"])
    def test_the_consumer_idiom_from_the_contract_is_safe(self, condition):
        """Section 4.1: (step.get("actions") or {}).get(condition) or [].

        Every consumer reads it this way so a pre-P1 graph dict - which has
        no "actions" key at all - is safe.
        """
        pre_p1 = {"id": "s1", "name": "S", "type": "script"}
        assert ((pre_p1.get("actions") or {}).get(condition) or []) == []

        dumped = PipelineStepV2(**pre_p1).model_dump()
        assert ((dumped.get("actions") or {}).get(condition) or []) == []


class TestGraphActionsRoundTrip:
    """Actions survive the trip a stored pipeline actually makes."""

    def _graph_with_actions(self):
        return PipelineGraphModel(
            steps={
                "build": PipelineStepV2(
                    id="build",
                    name="Build",
                    type=StepType.SCRIPT,
                    config={"command": "make"},
                    actions={"failure": ["trigger:card-abc"]},
                ),
                "deploy": PipelineStepV2(
                    id="deploy",
                    name="Deploy",
                    type=StepType.SCRIPT,
                    actions={"success": ["merge:main"], "always": ["merge:audit"]},
                ),
            },
            edges=[
                PipelineEdge(
                    id="e1",
                    from_step="build",
                    to_step="deploy",
                    condition=EdgeCondition.SUCCESS,
                )
            ],
            entry_points=["build"],
        )

    def test_actions_survive_json_round_trip(self):
        """model_dump -> json.dumps -> json.loads -> model, the stored path."""
        graph = self._graph_with_actions()
        restored = PipelineGraphModel(**json.loads(json.dumps(graph.model_dump())))

        assert restored.steps["build"].actions.failure == ["trigger:card-abc"]
        assert restored.steps["build"].actions.success == []
        assert restored.steps["deploy"].actions.success == ["merge:main"]
        assert restored.steps["deploy"].actions.always == ["merge:audit"]
        assert restored.model_dump() == graph.model_dump()

    def test_a_stored_pre_p1_graph_round_trips_with_empty_actions(self):
        """A graph written before P1 keeps working, untouched.

        This is the whole additive claim, at the level the database stores.
        """
        stored = {
            "steps": {
                "step_0": {
                    "id": "step_0",
                    "name": "Build",
                    "type": "script",
                    "config": {"command": "make"},
                    "position": {"x": 100.0, "y": 0.0},
                    "timeout": 300,
                    "continue_in_context": False,
                },
                "step_1": {
                    "id": "step_1",
                    "name": "Test",
                    "type": "script",
                    "config": {},
                    "position": {"x": 100.0, "y": 150.0},
                    "timeout": 300,
                    "continue_in_context": False,
                },
            },
            "edges": [
                {
                    "id": "edge_0_success",
                    "from_step": "step_0",
                    "to_step": "step_1",
                    "condition": "success",
                }
            ],
            "entry_points": ["step_0"],
            "version": 2,
        }
        graph = PipelineGraphModel(**json.loads(json.dumps(stored)))

        assert list(graph.steps) == ["step_0", "step_1"]
        assert graph.get_successors("step_0", EdgeCondition.SUCCESS) == ["step_1"]
        for step in graph.steps.values():
            assert step.actions.model_dump() == {
                "success": [], "failure": [], "always": []
            }

    def test_a_bad_action_deep_in_a_graph_payload_is_refused(self):
        """A refusal is not lost between the graph and its nodes."""
        with pytest.raises(ValidationError) as exc:
            PipelineGraphModel(
                steps={
                    "s1": {
                        "id": "s1",
                        "name": "S",
                        "type": "script",
                        "actions": {"success": ["stop"]},
                    }
                },
                edges=[],
                entry_points=["s1"],
            )
        assert "FLOW" in str(exc.value)

    def test_array_to_graph_output_is_unchanged_by_p1(self):
        """P1 does not touch the converter; every node gets empty actions.

        The converter learning merge:/trigger: is P2. Pinning the current
        output means P2's change is visible as a diff here rather than
        arriving as an untested side effect.
        """
        graph = array_to_graph([
            PipelineStepConfig(name="Build", type=StepType.SCRIPT, on_success="next"),
            PipelineStepConfig(name="Test", type=StepType.SCRIPT),
        ])
        for step in graph.steps.values():
            assert step.actions.model_dump() == {
                "success": [], "failure": [], "always": []
            }


# =============================================================================
# 12.8 P2 - array_to_graph becomes the faithful, REFUSING boundary converter
# =============================================================================
#
# Before this phase the converter emitted an edge only for the literal string
# "next" and dropped `merge:` / `trigger:` on the floor - and its
# `if i < len(steps) - 1:` guard meant an action on the LAST step (the common
# "merge when this passes" shape) was never even examined. The conversion was
# 100% silent, the graph validated, the run went green, the branch was never
# merged. That is the R1 violation the whole retirement exists to remove.
#
# The split (§1.2): v1's on_success/on_failure carried FLOW and EFFECT in one
# string. Flow (`next` / `stop`) is an edge or the absence of one. Effect
# (`merge:{branch}` / `trigger:{card_id}`) is a node action. Anything that is
# neither refuses, naming the step, the offending value and the vocabulary.

from app.services.pipeline_executor import graph_definition_errors


def _step(
    name="Step",
    *,
    step_id=None,
    on_success="next",
    on_failure="stop",
    step_type=StepType.SCRIPT,
    **kwargs,
):
    """A v1 array step. Defaults are PipelineStepConfig's own defaults."""
    return PipelineStepConfig(
        id=step_id,
        name=name,
        type=step_type,
        on_success=on_success,
        on_failure=on_failure,
        **kwargs,
    )


#: (condition, the v1 field that feeds it) - every effect test runs both
#: directions, because v1's vocabulary was shared between on_success and
#: on_failure and a converter that only handled one would be silently lossy
#: on the other.
DIRECTIONS = [
    ("success", "on_success", EdgeCondition.SUCCESS),
    ("failure", "on_failure", EdgeCondition.FAILURE),
]

EFFECTS = ["merge:main", "trigger:card-abc"]


class TestArrayToGraphHonoursAuthorIds:
    """§1.6(b): author-supplied step ids are HONOURED, not renamed.

    Without this, converting `.lazyaf/pipelines/test-suite.yaml` renames
    `sync-deps`/`tier1`/`verify-executor` to `step_0..step_9` - which changes
    the context-directory names, the debug breakpoint keys, and the
    readability of the very graph the conversion is meant to prove.
    `PipelineStepYaml.id` has accepted an id since it was written; until P2
    `PipelineStepConfig` had nowhere to put it.
    """

    def test_an_authored_id_becomes_the_node_id(self):
        graph = array_to_graph([_step("Tier 1", step_id="tier1", on_success="stop")])

        assert list(graph.steps) == ["tier1"]
        assert graph.steps["tier1"].id == "tier1"
        assert graph.steps["tier1"].name == "Tier 1"

    def test_a_step_without_an_id_falls_back_to_its_index(self):
        graph = array_to_graph([_step("A", on_success="next"), _step("B")])

        assert list(graph.steps) == ["step_0", "step_1"]

    def test_authored_and_generated_ids_mix_in_one_array(self):
        graph = array_to_graph([
            _step("A", step_id="build", on_success="next"),
            _step("B", on_success="next"),
            _step("C", step_id="deploy"),
        ])

        assert list(graph.steps) == ["build", "step_1", "deploy"]

    def test_the_entry_point_is_the_first_step_s_resolved_id(self):
        graph = array_to_graph([
            _step("A", step_id="first", on_success="next"),
            _step("B", step_id="second"),
        ])

        assert graph.entry_points == ["first"]

    def test_edges_run_between_resolved_ids_not_indices(self):
        graph = array_to_graph([
            _step("A", step_id="first", on_success="next", on_failure="next"),
            _step("B", step_id="second"),
        ])

        assert {(e.from_step, e.to_step) for e in graph.edges} == {
            ("first", "second")
        }
        assert {e.id for e in graph.edges} == {"edge_0_success", "edge_0_failure"}

    def test_duplicate_authored_ids_refuse_naming_both_steps(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("First", step_id="tier1", on_success="next"),
                _step("Second", step_id="tier1"),
            ])

        message = str(exc.value)
        assert "duplicate step id 'tier1'" in message
        assert "#0" in message and "#1" in message
        assert "'First'" in message and "'Second'" in message

    def test_an_authored_id_colliding_with_a_generated_one_refuses(self):
        """`step_1` is not a reserved word to an author, but it is to us.

        A graph keys its steps by id, so this would not be an error at all -
        it would be one step silently overwriting the other in the dict.
        """
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Author picked this", step_id="step_1", on_success="next"),
                _step("Unnamed"),
            ])

        message = str(exc.value)
        assert "collides with the id generated for step #1" in message
        assert "'Author picked this'" in message

    def test_the_collision_refuses_in_either_order(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Unnamed", on_success="next"),
                _step("Author picked this", step_id="step_0"),
            ])

        assert "collides with the id generated for step #0" in str(exc.value)

    @pytest.mark.parametrize("bad_id", ["", "   ", "\t\n"])
    def test_an_empty_or_whitespace_id_refuses(self, bad_id):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([_step("Nameless", step_id=bad_id, on_success="stop")])

        message = str(exc.value)
        assert "declares an empty id" in message
        assert "'Nameless'" in message

    def test_duplicate_step_NAMES_convert_fine(self):
        """Names are display text; ids are identity. v1 never made names
        unique and nothing downstream keys on them, so refusing here would
        invent a rule the array format never had."""
        graph = array_to_graph([
            _step("Test", on_success="next"),
            _step("Test", on_success="next"),
            _step("Test"),
        ])

        assert list(graph.steps) == ["step_0", "step_1", "step_2"]
        assert [s.name for s in graph.steps.values()] == ["Test", "Test", "Test"]

    def test_two_steps_may_share_a_name_while_carrying_distinct_ids(self):
        graph = array_to_graph([
            _step("Probe", step_id="probe-a", on_success="next"),
            _step("Probe", step_id="probe-b"),
        ])

        assert list(graph.steps) == ["probe-a", "probe-b"]

    def test_the_dogfood_shape_converts_to_named_nodes_and_a_linear_chain(self):
        """The shape of `.lazyaf/pipelines/test-suite.yaml`: ten steps, each
        with an author id, `on_success: next` / `on_failure: stop`, and the
        LAST one still saying `on_success: next` with nothing after it.

        The real-file test is B1's (§5.1); this is the converter-level
        analogue, so a converter change that would break the acceptance
        pipeline goes red here first instead of on a push.
        """
        ids = [
            "sync-deps", "tier1", "tier2", "tier3", "mock-agent",
            "remote-probe", "seed-endpoints", "harness-probe",
            "harness-probe-notools", "verify-executor",
        ]
        graph = array_to_graph([
            _step(name, step_id=name, on_success="next", on_failure="stop")
            for name in ids
        ])

        assert list(graph.steps) == ids
        assert graph.entry_points == ["sync-deps"]
        assert len(graph.edges) == 9
        assert all(e.condition == EdgeCondition.SUCCESS for e in graph.edges)
        assert [(e.from_step, e.to_step) for e in graph.edges] == list(
            zip(ids, ids[1:])
        )
        assert graph_definition_errors(graph.model_dump(mode="json")) == []


class TestArrayToGraphTerminalActions:
    """§4.2: `merge:` / `trigger:` become node actions - AND an edge.

    v1's `_merge_branch` and `_trigger_card` both end with
    `await self._execute_step(..., current_step + 1)`. The effect was never a
    terminator; it was an effect the run continued past. So the faithful
    rendering of a non-final effect is the action PLUS the edge, and dropping
    either half is lossy in a different direction.
    """

    @pytest.mark.parametrize("action", EFFECTS)
    @pytest.mark.parametrize("condition,field,edge_condition", DIRECTIONS)
    def test_a_non_final_effect_becomes_an_action_and_an_edge(
        self, action, condition, field, edge_condition
    ):
        graph = array_to_graph([
            _step("First", **{field: action}),
            _step("Second"),
        ])

        assert getattr(graph.steps["step_0"].actions, condition) == [action]
        matching = [e for e in graph.edges if e.condition == edge_condition]
        assert [(e.id, e.from_step, e.to_step) for e in matching] == [
            (f"edge_0_{condition}", "step_0", "step_1")
        ]

    @pytest.mark.parametrize("action", EFFECTS)
    @pytest.mark.parametrize("condition,field,edge_condition", DIRECTIONS)
    def test_a_final_effect_becomes_an_action_and_NO_edge(
        self, action, condition, field, edge_condition
    ):
        """The case the old `if i < len(steps) - 1:` guard never examined.

        v1's `_execute_step` guards its continuation with
        `current_step + 1 < len(steps)`, so on the last step the effect fired
        and nothing followed. Action, no edge.
        """
        graph = array_to_graph([
            _step("Only", **{field: action}),
        ])

        assert getattr(graph.steps["step_0"].actions, condition) == [action]
        assert graph.edges == []

    @pytest.mark.parametrize("condition,field,edge_condition", DIRECTIONS)
    def test_the_other_condition_stays_empty(self, condition, field, edge_condition):
        other = "failure" if condition == "success" else "success"
        graph = array_to_graph([_step("Only", **{field: "merge:main"})])

        assert getattr(graph.steps["step_0"].actions, other) == []

    def test_always_is_never_populated_by_conversion(self):
        """v1 had no `always`. Inventing one would be a conversion writing
        something the author never said."""
        graph = array_to_graph([
            _step("A", on_success="merge:main", on_failure="trigger:card-x"),
        ])

        assert graph.steps["step_0"].actions.always == []

    def test_both_conditions_can_carry_an_effect(self):
        graph = array_to_graph([
            _step("A", on_success="merge:main", on_failure="trigger:card-x"),
            _step("B"),
        ])

        actions = graph.steps["step_0"].actions
        assert actions.success == ["merge:main"]
        assert actions.failure == ["trigger:card-x"]
        assert {(e.id, e.condition) for e in graph.edges} == {
            ("edge_0_success", EdgeCondition.SUCCESS),
            ("edge_0_failure", EdgeCondition.FAILURE),
        }

    def test_an_effect_on_success_and_next_on_failure(self):
        graph = array_to_graph([
            _step("A", on_success="merge:main", on_failure="next"),
            _step("B"),
        ])

        assert graph.steps["step_0"].actions.success == ["merge:main"]
        assert graph.steps["step_0"].actions.failure == []
        assert len(graph.edges) == 2

    def test_a_step_with_only_flow_gets_no_actions(self):
        graph = array_to_graph([
            _step("A", on_success="next", on_failure="stop"),
            _step("B"),
        ])

        for step in graph.steps.values():
            assert step.actions.model_dump() == {
                "success": [], "failure": [], "always": []
            }

    def test_the_action_is_one_the_executor_can_dispatch(self):
        """R3: the vocabulary has ONE definition. Whatever the converter puts
        in `actions` must be a form `describe_terminal_action` accepts, which
        is the same function the executor's dispatcher calls."""
        graph = array_to_graph([
            _step("A", on_success="merge:release/1.4", on_failure="trigger:card-9"),
        ])

        actions = graph.steps["step_0"].actions
        for action in actions.success + actions.failure + actions.always:
            assert describe_terminal_action(action) is None


class TestArrayToGraphRefusesUnrepresentableActions:
    """§4.2: `trigger:pipeline:*`, unknown actions and empty targets refuse,
    naming the step, the offending value and the vocabulary (R1)."""

    @pytest.mark.parametrize("condition,field,edge_condition", DIRECTIONS)
    def test_trigger_pipeline_refuses_as_retired(self, condition, field, edge_condition):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Chainer", step_id="chainer", **{field: "trigger:pipeline:p-1"}),
                _step("After"),
            ])

        message = str(exc.value)
        assert "step 'chainer' (#0, 'Chainer')" in message
        assert f"on_{condition}='trigger:pipeline:p-1'" in message
        assert "retired" in message
        assert "card_complete" in message  # the named replacement
        assert "'trigger:{card_id}' or 'merge:{branch}'" in message

    @pytest.mark.parametrize("condition,field,edge_condition", DIRECTIONS)
    def test_an_unknown_action_refuses_naming_itself(
        self, condition, field, edge_condition
    ):
        """`nextt` - one character. On the array path this used to be logged
        at WARNING and treated as `stop`."""
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Typo", **{field: "nextt"}),
                _step("After"),
            ])

        message = str(exc.value)
        assert "unknown node action 'nextt'" in message
        assert "step 'step_0' (#0, 'Typo')" in message
        assert "'trigger:{card_id}' or 'merge:{branch}'" in message

    @pytest.mark.parametrize("action", ["merge:", "trigger:", "merge:   "])
    def test_an_empty_target_refuses(self, action):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([_step("Empty target", on_success=action)])

        assert "with an empty target" in str(exc.value)

    def test_every_bad_action_is_reported_not_just_the_first(self):
        """§4.2: the error 'carries every reason'. A user fixing one typo per
        push is a user we made a lap of the CI for each time."""
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("A", on_success="nextt", on_failure="stahp"),
                _step("B", on_success="trigger:pipeline:p-1", on_failure="stop"),
            ])

        assert len(exc.value.reasons) == 3
        joined = str(exc.value)
        assert "'nextt'" in joined
        assert "'stahp'" in joined
        assert "'trigger:pipeline:p-1'" in joined

    def test_a_refused_conversion_returns_nothing_at_all(self):
        """No partial graph escapes. A caller cannot accidentally persist the
        half that converted."""
        result = "untouched"
        with pytest.raises(ArrayConversionError):
            result = array_to_graph([_step("A", on_success="nonsense")])
        assert result == "untouched"

    def test_the_refusal_is_a_ValueError(self):
        """So a caller validating inside pydantic gets a 422, not a 500 - and
        so the pre-12.8 `pytest.raises(ValueError)` callers still hold."""
        assert issubclass(ArrayConversionError, ValueError)

    def test_reasons_is_the_list_and_str_is_the_join(self):
        error = ArrayConversionError(["first thing", "second thing"])

        assert error.reasons == ["first thing", "second thing"]
        assert str(error) == "first thing; second thing"

    def test_an_empty_array_refuses_with_the_conversion_error(self):
        """§1.6(c). `graph.entry_points` may not be empty and
        `PipelineGraphModel` rejects it, so an 'empty graph' is
        unrepresentable by construction - there is no lossy alternative to
        refusing."""
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([])

        assert "empty" in str(exc.value).lower()
        assert "entry point" in str(exc.value)

    @pytest.mark.parametrize("action", [None, 3, ["next"], True])
    def test_a_non_string_action_never_reaches_the_converter(self, action):
        """`PipelineStepConfig.on_success` is a bare `str`, so pydantic
        refuses before conversion. Pinned so nobody 'fixes' the converter by
        widening it and re-opening the hole."""
        with pytest.raises(ValidationError):
            PipelineStepConfig(name="A", type=StepType.SCRIPT, on_success=action)


class TestArrayToGraphRefusesAnOrphanedTail:
    """§1.6(a), as corrected by the plan's own adversarial review (item 8).

    THE REASONING, stated because the brief asks for it. A mid-array
    `on_success: "stop"` used to emit no edge and leave the following node
    unreachable. Since M14 that is not a quiet cosmetic defect:
    `graph_definition_errors` flags the orphan at definition time and
    `_verify_graph_coverage` FAILS the run at execution time. So the old
    conversion turns a v1 pipeline that ran GREEN into a v2 pipeline that
    runs RED - and red for a reason that has nothing to do with the user's
    code. The two alternatives are both worse: silently truncating the
    orphaned tail deletes steps the author wrote, and emitting the edge
    anyway invents a continuation `stop` explicitly denied.

    So the faithful conversion of a mid-array `stop` is: THERE ISN'T ONE.
    The array said "run these ten steps" and "never get past step three" in
    the same breath; that is a contradiction the array format could hold
    silently and the graph cannot hold at all. Refusing at the boundary, in
    the author's own terms, is the only R1 answer.

    But the predicate is NOT 'stop on a non-final step'. That refuses arrays
    that convert perfectly: a step that stops on success and continues on
    failure still reaches its successor. The predicate is 'the resulting
    graph has an unreachable node', which is exactly what
    `graph_definition_errors` computes - so that is what decides, and this
    class pins both halves.
    """

    def test_a_mid_array_stop_on_both_outcomes_refuses(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Terminal", on_success="stop", on_failure="stop"),
                _step("Never"),
            ])

        assert "unreachable" in str(exc.value)

    def test_the_refusal_names_the_step_whose_action_orphaned_the_tail(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Gate", step_id="gate", on_success="stop", on_failure="stop"),
                _step("Orphan", step_id="orphan"),
            ])

        message = str(exc.value)
        assert "step 'gate' (#0, 'Gate')" in message
        assert "on_success='stop'" in message
        assert "on_failure='stop'" in message
        assert "1 step(s) after it unreachable" in message

    def test_the_refusal_carries_the_executor_s_own_defect_line(self):
        """R3: the decision is `graph_definition_errors`', and its message
        travels with ours rather than being paraphrased. If the executor's
        wording or its rules change, this test sees it."""
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Gate", step_id="gate", on_success="stop", on_failure="stop"),
                _step("Orphan", step_id="orphan"),
            ])

        assert (
            "step 'orphan' is unreachable: no entry point names it and no "
            "edge leads to it"
        ) in exc.value.reasons

    def test_a_stop_two_steps_from_the_end_counts_both_orphans(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("A", on_success="next"),
                _step("B", on_success="stop", on_failure="stop"),
                _step("C", on_success="next"),
                _step("D"),
            ])

        assert "2 step(s) after it unreachable" in str(exc.value)

    def test_a_step_that_continues_via_an_EFFECT_edge_is_not_blamed(self):
        """The blame follows the edges the converter actually emitted, not
        the literal string `next`. `merge:` continues too (v1 ran
        `current_step + 1` after merging), so the step downstream of the real
        orphan must not be named as a second culprit - a refusal that accuses
        two steps when one is at fault sends the author to edit working code.
        """
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Gate", step_id="gate", on_success="stop", on_failure="stop"),
                _step("Merger", step_id="merger", on_success="merge:main"),
                _step("Tail", step_id="tail"),
            ])

        blamed = [
            reason for reason in exc.value.reasons
            if "continues on neither outcome" in reason
        ]
        assert len(blamed) == 1
        assert "'gate'" in blamed[0]
        assert "merger" not in blamed[0]

    def test_a_step_that_continues_only_on_failure_is_not_blamed(self):
        with pytest.raises(ArrayConversionError) as exc:
            array_to_graph([
                _step("Gate", step_id="gate", on_success="stop", on_failure="stop"),
                _step("Guard", step_id="guard", on_success="stop", on_failure="next"),
                _step("Tail", step_id="tail"),
            ])

        blamed = [
            reason for reason in exc.value.reasons
            if "continues on neither outcome" in reason
        ]
        assert [("'gate'" in r) for r in blamed] == [True]

    def test_stop_on_success_with_next_on_failure_STILL_CONVERTS(self):
        """Adversarial review item 8: the naive 'stop on a non-final step'
        rule would refuse this, and it is perfectly convertible - the
        successor is reached over the FAILURE edge."""
        graph = array_to_graph([
            _step("Guard", on_success="stop", on_failure="next"),
            _step("Recover"),
        ])

        assert [(e.from_step, e.to_step, e.condition) for e in graph.edges] == [
            ("step_0", "step_1", EdgeCondition.FAILURE)
        ]
        assert graph_definition_errors(graph.model_dump(mode="json")) == []

    def test_stop_on_success_with_an_EFFECT_on_failure_still_converts(self):
        """The effect continues too, so the tail is reached."""
        graph = array_to_graph([
            _step("Guard", on_success="stop", on_failure="trigger:card-fix"),
            _step("Recover"),
        ])

        assert graph.steps["step_0"].actions.failure == ["trigger:card-fix"]
        assert [e.condition for e in graph.edges] == [EdgeCondition.FAILURE]

    def test_stop_on_the_last_step_is_not_a_refusal(self):
        graph = array_to_graph([
            _step("A", on_success="next"),
            _step("B", on_success="stop", on_failure="stop"),
        ])

        assert len(graph.steps) == 2
        assert len(graph.edges) == 1

    def test_a_single_step_array_that_stops_converts(self):
        graph = array_to_graph([_step("Only", on_success="stop", on_failure="stop")])

        assert list(graph.steps) == ["step_0"]
        assert graph.edges == []
        assert graph.entry_points == ["step_0"]

    @pytest.mark.parametrize("terminal_action", ["next", "stop"])
    def test_flow_on_the_TERMINAL_step_is_neither_edge_nor_refusal(
        self, terminal_action
    ):
        """Adversarial review item 9. `.lazyaf/pipelines/test-suite.yaml`'s
        tenth step is `on_success: next` with nothing after it; v1's
        continuation guard swallowed it. Reading 'faithful, refusing'
        literally here would make the acceptance pipeline itself
        unconvertible."""
        graph = array_to_graph([
            _step("A", on_success="next"),
            _step("Last", on_success=terminal_action, on_failure=terminal_action),
        ])

        assert graph.get_all_successors("step_1") == []
        assert graph.steps["step_1"].actions.model_dump() == {
            "success": [], "failure": [], "always": []
        }

    @pytest.mark.parametrize(
        "on_success,on_failure,convertible",
        [
            ("next", "stop", True),
            ("next", "next", True),
            ("stop", "next", True),
            ("stop", "stop", False),
            ("merge:main", "stop", True),
            ("stop", "merge:main", True),
            ("trigger:card-x", "stop", True),
            ("merge:main", "trigger:card-x", True),
        ],
    )
    def test_the_converter_and_the_executor_agree_on_every_flow_combination(
        self, on_success, on_failure, convertible
    ):
        """The R3 guard. The converter refuses exactly when the executor's
        own definition-time check would reject the result - never more (which
        would refuse working pipelines) and never less (which would ship a
        graph that fails at run time for a reason the boundary already knew).
        """
        steps = [
            _step("First", on_success=on_success, on_failure=on_failure),
            _step("Second"),
        ]

        if convertible:
            graph = array_to_graph(steps)
            assert graph_definition_errors(graph.model_dump(mode="json")) == []
        else:
            with pytest.raises(ArrayConversionError):
                array_to_graph(steps)


class TestArrayToGraphFidelity:
    """Every v1 construct survives the trip, or the trip refuses."""

    def test_continue_in_context_survives(self):
        """§3.6 keeps `continue_in_context` on all three schemas,
        accepted-and-ignored. Dropping it in conversion would be the same
        silent loss in a new place."""
        graph = array_to_graph([
            _step("Setup", on_success="next", continue_in_context=True),
            _step("Use", continue_in_context=False),
        ])

        assert graph.steps["step_0"].continue_in_context is True
        assert graph.steps["step_1"].continue_in_context is False

    def test_continue_in_context_survives_alongside_an_action(self):
        graph = array_to_graph([
            _step(
                "Build",
                step_id="build",
                on_success="merge:main",
                continue_in_context=True,
            ),
        ])

        assert graph.steps["build"].continue_in_context is True
        assert graph.steps["build"].actions.success == ["merge:main"]

    def test_timeout_and_config_survive(self):
        graph = array_to_graph([
            _step(
                "Slow",
                step_id="slow",
                on_success="stop",
                timeout=1800,
                config={"command": "pytest -q", "control": False},
            ),
        ])

        assert graph.steps["slow"].timeout == 1800
        assert graph.steps["slow"].config == {
            "command": "pytest -q", "control": False
        }

    @pytest.mark.parametrize(
        "step_type", [StepType.SCRIPT, StepType.DOCKER, StepType.AGENT]
    )
    def test_every_step_type_survives(self, step_type):
        graph = array_to_graph([
            _step("Typed", step_id="typed", on_success="stop", step_type=step_type),
        ])

        assert graph.steps["typed"].type == step_type

    def test_auto_layout_is_unchanged_by_author_ids(self):
        graph = array_to_graph([
            _step("A", step_id="a", on_success="next"),
            _step("B", step_id="b", on_success="next"),
            _step("C", step_id="c"),
        ])

        assert [
            (s.position.x, s.position.y) for s in graph.steps.values()
        ] == [(100, 0), (100, 150), (100, 300)]

    def test_a_converted_graph_never_carries_a_definition_error(self):
        """The property that makes the converter safe to put on the write
        path at P3: whatever it RETURNS is runnable."""
        graph = array_to_graph([
            _step("A", step_id="a", on_success="next", on_failure="next"),
            _step("B", step_id="b", on_success="merge:main", on_failure="next"),
            _step("C", step_id="c", on_success="trigger:card-x", on_failure="stop"),
        ])

        assert graph_definition_errors(graph.model_dump(mode="json")) == []

    def test_a_converted_graph_survives_the_json_round_trip_it_is_stored_as(self):
        graph = array_to_graph([
            _step("A", step_id="a", on_success="merge:main", on_failure="next"),
            _step("B", step_id="b", on_failure="trigger:card-x"),
        ])

        restored = PipelineGraphModel(**json.loads(json.dumps(graph.model_dump())))

        assert restored.model_dump() == graph.model_dump()
        assert restored.steps["a"].actions.success == ["merge:main"]
        assert restored.steps["b"].actions.failure == ["trigger:card-x"]

    def test_version_is_2_and_the_graph_validates(self):
        graph = array_to_graph([_step("Only", on_success="stop")])

        assert graph.version == 2
        assert graph.entry_points == ["step_0"]
