"""
Unit Tests for Graph-Based Pipeline Schemas (Phase 1: Graph Creep)

Tests for:
- EdgeCondition enum
- PipelineNodePosition model
- PipelineEdge model
- PipelineStepV2 model
- PipelineGraphModel validation and utilities
- array_to_graph conversion
"""

import pytest
from pydantic import ValidationError

import sys
from pathlib import Path
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas.pipeline import (
    EdgeCondition,
    PipelineNodePosition,
    PipelineEdge,
    PipelineStepV2,
    PipelineGraphModel,
    PipelineStepConfig,
    array_to_graph,
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
        """on_success: stop creates no edge to next step."""
        steps = [
            PipelineStepConfig(
                name="Terminal",
                type=StepType.SCRIPT,
                config={},
                on_success="stop",
            ),
            PipelineStepConfig(name="Never", type=StepType.SCRIPT, config={}),
        ]
        graph = array_to_graph(steps)

        # No success edge from step_0 to step_1
        success_edges = [e for e in graph.edges if e.condition == EdgeCondition.SUCCESS and e.from_step == "step_0"]
        assert len(success_edges) == 0

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
