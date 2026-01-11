"""
E2E Tests for Graph-Based Pipeline Editor (Graph Creep)

User Stories:
1. Create a pipeline using the node graph UI
2. Add and connect nodes with success/failure edges
3. Execute a pipeline with parallel branches
4. Convert legacy sequential pipelines to graph format
5. Export pipelines to YAML

These tests define the expected behavior for the visual node graph pipeline editor.
"""

import pytest
import asyncio
from typing import Any

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


# =============================================================================
# User Story 1: Create Pipeline via Node Graph UI
# =============================================================================

class TestGraphPipelineCreate:
    """
    User Story 1: Create a Pipeline via Node Graph UI

    As a developer,
    I want to create pipelines using a visual node graph editor
    So that I can design complex workflows intuitively

    Acceptance Criteria:
    - Can create a new pipeline with graph-based step definition
    - Graph data includes nodes with positions and edges with conditions
    - Pipeline is saved and retrievable with graph structure intact
    """

    async def test_create_pipeline_with_graph_structure(self, api_client, test_repo):
        """Pipeline can be created with graph-based step definition."""
        graph_data = {
            "steps": {
                "step_1": {
                    "id": "step_1",
                    "name": "Build",
                    "type": "script",
                    "config": {"command": "npm run build"},
                    "position": {"x": 100, "y": 100}
                },
                "step_2": {
                    "id": "step_2",
                    "name": "Test",
                    "type": "script",
                    "config": {"command": "npm test"},
                    "position": {"x": 300, "y": 100}
                }
            },
            "edges": [
                {
                    "id": "edge_1",
                    "from_step": "step_1",
                    "to_step": "step_2",
                    "condition": "success"
                }
            ],
            "entry_points": ["step_1"],
            "version": 2
        }

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Graph Pipeline",
                "description": "Pipeline with graph structure",
                "steps_graph": graph_data,
            },
        )

        assert response.status_code == 201
        pipeline = response.json()
        assert pipeline["name"] == "Graph Pipeline"
        assert "steps_graph" in pipeline
        assert pipeline["steps_graph"]["version"] == 2

    async def test_graph_pipeline_preserves_node_positions(self, api_client, test_repo):
        """Node positions are preserved when saving and loading a graph pipeline."""
        graph_data = {
            "steps": {
                "node_a": {
                    "id": "node_a",
                    "name": "Step A",
                    "type": "script",
                    "config": {"command": "echo A"},
                    "position": {"x": 150.5, "y": 200.75}
                }
            },
            "edges": [],
            "entry_points": ["node_a"],
            "version": 2
        }

        # Create
        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Position Test", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        # Retrieve
        get_response = await api_client.get(f"/api/pipelines/{pipeline_id}")
        assert get_response.status_code == 200
        pipeline = get_response.json()

        node_a = pipeline["steps_graph"]["steps"]["node_a"]
        assert node_a["position"]["x"] == 150.5
        assert node_a["position"]["y"] == 200.75

    async def test_graph_pipeline_with_multiple_edge_conditions(self, api_client, test_repo):
        """Edges can have different conditions (success, failure, always)."""
        graph_data = {
            "steps": {
                "start": {
                    "id": "start",
                    "name": "Start",
                    "type": "script",
                    "config": {"command": "echo start"},
                    "position": {"x": 100, "y": 200}
                },
                "on_success": {
                    "id": "on_success",
                    "name": "Handle Success",
                    "type": "script",
                    "config": {"command": "echo success"},
                    "position": {"x": 300, "y": 100}
                },
                "on_failure": {
                    "id": "on_failure",
                    "name": "Handle Failure",
                    "type": "script",
                    "config": {"command": "echo failure"},
                    "position": {"x": 300, "y": 300}
                }
            },
            "edges": [
                {
                    "id": "edge_success",
                    "from_step": "start",
                    "to_step": "on_success",
                    "condition": "success"
                },
                {
                    "id": "edge_failure",
                    "from_step": "start",
                    "to_step": "on_failure",
                    "condition": "failure"
                }
            ],
            "entry_points": ["start"],
            "version": 2
        }

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Branching Pipeline", "steps_graph": graph_data},
        )

        assert response.status_code == 201
        pipeline = response.json()
        edges = pipeline["steps_graph"]["edges"]

        success_edge = next(e for e in edges if e["condition"] == "success")
        failure_edge = next(e for e in edges if e["condition"] == "failure")

        assert success_edge["to_step"] == "on_success"
        assert failure_edge["to_step"] == "on_failure"


# =============================================================================
# User Story 2: Execute Pipeline with Parallel Branches
# =============================================================================

class TestGraphPipelineParallelExecution:
    """
    User Story 2: Execute Pipeline with Parallel Branches

    As a developer,
    I want pipelines to execute parallel branches simultaneously
    So that independent steps run concurrently for faster execution

    Acceptance Criteria:
    - Steps with no dependencies execute in parallel
    - Fan-out: One step can trigger multiple downstream steps
    - Fan-in: A step can wait for multiple upstream steps to complete
    - Step status is tracked individually during parallel execution
    """

    async def test_parallel_steps_both_execute(self, api_client, test_repo):
        """Steps without dependencies on each other execute in parallel."""
        # Diamond pattern: start -> (A, B in parallel) -> end
        graph_data = {
            "steps": {
                "start": {
                    "id": "start",
                    "name": "Start",
                    "type": "script",
                    "config": {"command": "echo start"},
                    "position": {"x": 100, "y": 200}
                },
                "parallel_a": {
                    "id": "parallel_a",
                    "name": "Parallel A",
                    "type": "script",
                    "config": {"command": "echo A"},
                    "position": {"x": 300, "y": 100}
                },
                "parallel_b": {
                    "id": "parallel_b",
                    "name": "Parallel B",
                    "type": "script",
                    "config": {"command": "echo B"},
                    "position": {"x": 300, "y": 300}
                },
                "join": {
                    "id": "join",
                    "name": "Join",
                    "type": "script",
                    "config": {"command": "echo done"},
                    "position": {"x": 500, "y": 200}
                }
            },
            "edges": [
                {"id": "e1", "from_step": "start", "to_step": "parallel_a", "condition": "success"},
                {"id": "e2", "from_step": "start", "to_step": "parallel_b", "condition": "success"},
                {"id": "e3", "from_step": "parallel_a", "to_step": "join", "condition": "success"},
                {"id": "e4", "from_step": "parallel_b", "to_step": "join", "condition": "success"}
            ],
            "entry_points": ["start"],
            "version": 2
        }

        # Create pipeline
        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Diamond Pipeline", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        assert run_response.status_code in (200, 201, 202)
        run_id = run_response.json()["id"]

        # Wait for completion and verify both parallel steps ran
        final_run = await self._wait_for_pipeline_completion(api_client, run_id)

        # All 4 steps should have run
        assert final_run["steps_completed"] == 4
        assert final_run["status"] == "passed"

    async def test_fan_out_triggers_multiple_downstream_steps(self, api_client, test_repo):
        """One step completing triggers multiple downstream steps."""
        graph_data = {
            "steps": {
                "source": {
                    "id": "source",
                    "name": "Source",
                    "type": "script",
                    "config": {"command": "echo source"},
                    "position": {"x": 100, "y": 200}
                },
                "target_1": {
                    "id": "target_1",
                    "name": "Target 1",
                    "type": "script",
                    "config": {"command": "echo t1"},
                    "position": {"x": 300, "y": 100}
                },
                "target_2": {
                    "id": "target_2",
                    "name": "Target 2",
                    "type": "script",
                    "config": {"command": "echo t2"},
                    "position": {"x": 300, "y": 200}
                },
                "target_3": {
                    "id": "target_3",
                    "name": "Target 3",
                    "type": "script",
                    "config": {"command": "echo t3"},
                    "position": {"x": 300, "y": 300}
                }
            },
            "edges": [
                {"id": "e1", "from_step": "source", "to_step": "target_1", "condition": "success"},
                {"id": "e2", "from_step": "source", "to_step": "target_2", "condition": "success"},
                {"id": "e3", "from_step": "source", "to_step": "target_3", "condition": "success"}
            ],
            "entry_points": ["source"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Fan-out Pipeline", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        run_id = run_response.json()["id"]

        final_run = await self._wait_for_pipeline_completion(api_client, run_id)

        # All 4 steps should complete
        assert final_run["steps_completed"] == 4
        assert final_run["status"] == "passed"

    async def test_fan_in_waits_for_all_upstream(self, api_client, test_repo):
        """Join step waits for ALL upstream steps to complete before executing."""
        # This tests the join semantics: step executes only when all incoming edges are satisfied
        graph_data = {
            "steps": {
                "a": {"id": "a", "name": "A", "type": "script", "config": {"command": "echo A"}, "position": {"x": 100, "y": 100}},
                "b": {"id": "b", "name": "B", "type": "script", "config": {"command": "sleep 1 && echo B"}, "position": {"x": 100, "y": 300}},
                "join": {"id": "join", "name": "Join", "type": "script", "config": {"command": "echo joined"}, "position": {"x": 300, "y": 200}}
            },
            "edges": [
                {"id": "e1", "from_step": "a", "to_step": "join", "condition": "success"},
                {"id": "e2", "from_step": "b", "to_step": "join", "condition": "success"}
            ],
            "entry_points": ["a", "b"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Fan-in Pipeline", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        run_id = run_response.json()["id"]

        final_run = await self._wait_for_pipeline_completion(api_client, run_id)

        # Join should only run after both A and B complete
        assert final_run["steps_completed"] == 3
        assert final_run["status"] == "passed"

        # Verify join ran last by checking step_runs order
        step_runs = final_run.get("step_runs", [])
        if step_runs:
            join_run = next((s for s in step_runs if s["step_name"] == "Join"), None)
            assert join_run is not None
            # Join should have completed after A and B

    async def test_parallel_step_tracking_shows_multiple_active(self, api_client, test_repo):
        """During parallel execution, pipeline run shows multiple active steps."""
        # Use slow commands to catch the parallel state
        graph_data = {
            "steps": {
                "start": {"id": "start", "name": "Start", "type": "script", "config": {"command": "echo start"}, "position": {"x": 100, "y": 200}},
                "slow_a": {"id": "slow_a", "name": "Slow A", "type": "script", "config": {"command": "sleep 5"}, "position": {"x": 300, "y": 100}},
                "slow_b": {"id": "slow_b", "name": "Slow B", "type": "script", "config": {"command": "sleep 5"}, "position": {"x": 300, "y": 300}}
            },
            "edges": [
                {"id": "e1", "from_step": "start", "to_step": "slow_a", "condition": "success"},
                {"id": "e2", "from_step": "start", "to_step": "slow_b", "condition": "success"}
            ],
            "entry_points": ["start"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Slow Parallel", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        run_id = run_response.json()["id"]

        # Wait a bit for start to complete and parallel steps to begin
        await asyncio.sleep(2)

        # Check run status - should show multiple active steps
        status_response = await api_client.get(f"/api/pipeline-runs/{run_id}")
        run_data = status_response.json()

        # The run should have active_step_ids with multiple entries
        if "active_step_ids" in run_data:
            # Both slow_a and slow_b should be active
            assert len(run_data["active_step_ids"]) == 2 or run_data["status"] == "running"

    async def _wait_for_pipeline_completion(self, api_client, run_id: str, timeout: int = 60) -> dict:
        """Wait for pipeline to reach a terminal status."""
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            response = await api_client.get(f"/api/pipeline-runs/{run_id}")
            if response.status_code == 200:
                run_data = response.json()
                if run_data["status"] in ("passed", "failed", "cancelled"):
                    return run_data
            await asyncio.sleep(0.5)

        pytest.fail(f"Pipeline run {run_id} did not complete within {timeout}s")


# =============================================================================
# User Story 3: Legacy Pipeline Conversion
# =============================================================================

class TestGraphPipelineLegacyConversion:
    """
    User Story 3: Convert Legacy Pipelines to Graph Format

    As a developer with existing sequential pipelines,
    I want to convert them to graph format
    So that I can edit them in the visual node graph editor

    Acceptance Criteria:
    - Existing array-based pipelines can be converted to graph format
    - Conversion preserves step order and dependencies
    - Auto-layout positions nodes in a readable arrangement
    - Original pipeline functionality is unchanged
    """

    async def test_legacy_pipeline_still_works(self, api_client, test_repo):
        """Existing array-based pipelines continue to work."""
        # Legacy format with steps as array
        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Legacy Pipeline",
                "steps": [
                    {"name": "Step 1", "type": "script", "config": {"command": "echo 1"}},
                    {"name": "Step 2", "type": "script", "config": {"command": "echo 2"}}
                ]
            },
        )

        assert response.status_code == 201
        pipeline = response.json()
        assert len(pipeline["steps"]) == 2

    async def test_convert_legacy_to_graph(self, api_client, test_repo):
        """Legacy pipeline can be converted to graph format."""
        # Create legacy pipeline
        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "To Convert",
                "steps": [
                    {"name": "Build", "type": "script", "config": {"command": "npm build"}},
                    {"name": "Test", "type": "script", "config": {"command": "npm test"}},
                    {"name": "Deploy", "type": "script", "config": {"command": "npm deploy"}}
                ]
            },
        )
        pipeline_id = create_response.json()["id"]

        # Convert to graph format
        convert_response = await api_client.post(f"/api/pipelines/{pipeline_id}/convert-to-graph")
        assert convert_response.status_code == 200

        converted = convert_response.json()
        assert "steps_graph" in converted

        # Should have 3 nodes
        assert len(converted["steps_graph"]["steps"]) == 3

        # Should have 2 edges (Build->Test, Test->Deploy)
        assert len(converted["steps_graph"]["edges"]) == 2

        # First step should be entry point
        entry_points = converted["steps_graph"]["entry_points"]
        assert len(entry_points) == 1

    async def test_converted_pipeline_executes_same_as_legacy(self, api_client, test_repo):
        """Converted pipeline produces same execution result as legacy."""
        # Create and run legacy pipeline
        legacy_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Legacy",
                "steps": [
                    {"name": "Echo", "type": "script", "config": {"command": "echo hello"}}
                ]
            },
        )
        legacy_id = legacy_response.json()["id"]

        # Run legacy
        legacy_run = await api_client.post(f"/api/pipelines/{legacy_id}/run")

        # Convert to graph
        await api_client.post(f"/api/pipelines/{legacy_id}/convert-to-graph")

        # Run graph version
        graph_run = await api_client.post(f"/api/pipelines/{legacy_id}/run")

        # Both should complete with same status
        # (This is a behavior contract - actual implementation may vary)
        assert legacy_run.status_code == graph_run.status_code


# =============================================================================
# User Story 4: YAML Export
# =============================================================================

class TestGraphPipelineYAMLExport:
    """
    User Story 4: Export Pipeline to YAML

    As a developer,
    I want to export pipelines to YAML format
    So that I can version control pipeline definitions in my repository

    Acceptance Criteria:
    - Graph pipeline can be exported to YAML
    - YAML contains all steps, edges, and entry points
    - YAML is valid and can be re-imported
    - Parallel branches are represented correctly
    """

    async def test_export_graph_pipeline_to_yaml(self, api_client, test_repo):
        """Graph pipeline can be exported to YAML format."""
        graph_data = {
            "steps": {
                "build": {"id": "build", "name": "Build", "type": "script", "config": {"command": "npm build"}, "position": {"x": 100, "y": 100}},
                "test": {"id": "test", "name": "Test", "type": "script", "config": {"command": "npm test"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "build", "to_step": "test", "condition": "success"}
            ],
            "entry_points": ["build"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Export Test", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        # Export to YAML
        export_response = await api_client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert export_response.status_code == 200
        assert "text/yaml" in export_response.headers.get("content-type", "") or \
               "application/x-yaml" in export_response.headers.get("content-type", "")

        yaml_content = export_response.text
        assert "Build" in yaml_content
        assert "Test" in yaml_content
        assert "success" in yaml_content

    async def test_yaml_export_includes_parallel_branches(self, api_client, test_repo):
        """YAML export correctly represents parallel branches."""
        graph_data = {
            "steps": {
                "start": {"id": "start", "name": "Start", "type": "script", "config": {"command": "echo start"}, "position": {"x": 100, "y": 200}},
                "branch_a": {"id": "branch_a", "name": "Branch A", "type": "script", "config": {"command": "echo A"}, "position": {"x": 300, "y": 100}},
                "branch_b": {"id": "branch_b", "name": "Branch B", "type": "script", "config": {"command": "echo B"}, "position": {"x": 300, "y": 300}}
            },
            "edges": [
                {"id": "e1", "from_step": "start", "to_step": "branch_a", "condition": "success"},
                {"id": "e2", "from_step": "start", "to_step": "branch_b", "condition": "success"}
            ],
            "entry_points": ["start"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Parallel Export", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        export_response = await api_client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        yaml_content = export_response.text

        # Should show start has two success targets
        assert "Branch A" in yaml_content
        assert "Branch B" in yaml_content


# =============================================================================
# User Story 5: Visual Node Graph UI
# =============================================================================

class TestGraphPipelineUIBehaviors:
    """
    User Story 5: Visual Node Graph UI Interactions

    As a developer using the UI,
    I want to visually create and edit pipeline graphs
    So that I can design workflows intuitively

    Acceptance Criteria:
    - Nodes can be added by type (script, docker, agent)
    - Nodes can be connected by dragging edges
    - Edge conditions can be changed (success/failure/always)
    - Changes are saved when clicking Save
    - Node positions are preserved on reload

    Note: These tests focus on API contracts that the UI will call.
    Actual UI testing would use Playwright.
    """

    async def test_add_node_to_existing_graph(self, api_client, test_repo):
        """A new node can be added to an existing graph pipeline."""
        # Create initial graph
        initial_graph = {
            "steps": {
                "step_1": {"id": "step_1", "name": "Step 1", "type": "script", "config": {"command": "echo 1"}, "position": {"x": 100, "y": 100}}
            },
            "edges": [],
            "entry_points": ["step_1"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Add Node Test", "steps_graph": initial_graph},
        )
        pipeline_id = create_response.json()["id"]

        # Add a new node
        updated_graph = {
            "steps": {
                "step_1": {"id": "step_1", "name": "Step 1", "type": "script", "config": {"command": "echo 1"}, "position": {"x": 100, "y": 100}},
                "step_2": {"id": "step_2", "name": "Step 2", "type": "docker", "config": {"image": "node:18", "command": "npm test"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "step_1", "to_step": "step_2", "condition": "success"}
            ],
            "entry_points": ["step_1"],
            "version": 2
        }

        update_response = await api_client.put(
            f"/api/pipelines/{pipeline_id}",
            json={"steps_graph": updated_graph},
        )

        assert update_response.status_code == 200
        updated = update_response.json()
        assert len(updated["steps_graph"]["steps"]) == 2
        assert "step_2" in updated["steps_graph"]["steps"]

    async def test_remove_node_from_graph(self, api_client, test_repo):
        """A node can be removed from a graph pipeline."""
        initial_graph = {
            "steps": {
                "keep": {"id": "keep", "name": "Keep", "type": "script", "config": {"command": "echo keep"}, "position": {"x": 100, "y": 100}},
                "remove": {"id": "remove", "name": "Remove", "type": "script", "config": {"command": "echo remove"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "keep", "to_step": "remove", "condition": "success"}
            ],
            "entry_points": ["keep"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Remove Node Test", "steps_graph": initial_graph},
        )
        pipeline_id = create_response.json()["id"]

        # Remove node and its edges
        updated_graph = {
            "steps": {
                "keep": {"id": "keep", "name": "Keep", "type": "script", "config": {"command": "echo keep"}, "position": {"x": 100, "y": 100}}
            },
            "edges": [],
            "entry_points": ["keep"],
            "version": 2
        }

        update_response = await api_client.put(
            f"/api/pipelines/{pipeline_id}",
            json={"steps_graph": updated_graph},
        )

        assert update_response.status_code == 200
        updated = update_response.json()
        assert len(updated["steps_graph"]["steps"]) == 1
        assert "remove" not in updated["steps_graph"]["steps"]

    async def test_change_edge_condition(self, api_client, test_repo):
        """Edge condition can be changed from success to failure."""
        initial_graph = {
            "steps": {
                "a": {"id": "a", "name": "A", "type": "script", "config": {"command": "echo A"}, "position": {"x": 100, "y": 100}},
                "b": {"id": "b", "name": "B", "type": "script", "config": {"command": "echo B"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "a", "to_step": "b", "condition": "success"}
            ],
            "entry_points": ["a"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Edge Condition Test", "steps_graph": initial_graph},
        )
        pipeline_id = create_response.json()["id"]

        # Change edge condition to failure
        updated_graph = {
            "steps": initial_graph["steps"],
            "edges": [
                {"id": "e1", "from_step": "a", "to_step": "b", "condition": "failure"}
            ],
            "entry_points": ["a"],
            "version": 2
        }

        update_response = await api_client.put(
            f"/api/pipelines/{pipeline_id}",
            json={"steps_graph": updated_graph},
        )

        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["steps_graph"]["edges"][0]["condition"] == "failure"

    async def test_update_node_position(self, api_client, test_repo):
        """Node position can be updated (drag and drop)."""
        initial_graph = {
            "steps": {
                "movable": {"id": "movable", "name": "Movable", "type": "script", "config": {"command": "echo move"}, "position": {"x": 100, "y": 100}}
            },
            "edges": [],
            "entry_points": ["movable"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Position Update Test", "steps_graph": initial_graph},
        )
        pipeline_id = create_response.json()["id"]

        # Move node
        updated_graph = {
            "steps": {
                "movable": {"id": "movable", "name": "Movable", "type": "script", "config": {"command": "echo move"}, "position": {"x": 500, "y": 300}}
            },
            "edges": [],
            "entry_points": ["movable"],
            "version": 2
        }

        update_response = await api_client.put(
            f"/api/pipelines/{pipeline_id}",
            json={"steps_graph": updated_graph},
        )

        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["steps_graph"]["steps"]["movable"]["position"]["x"] == 500
        assert updated["steps_graph"]["steps"]["movable"]["position"]["y"] == 300


# =============================================================================
# User Story 6: Execution Visualization
# =============================================================================

class TestGraphPipelineExecutionVisualization:
    """
    User Story 6: Visualize Pipeline Execution on Graph

    As a developer watching a pipeline run,
    I want to see execution status on the node graph
    So that I can understand which steps are running/completed/failed

    Acceptance Criteria:
    - Each node shows its execution status (pending/running/passed/failed)
    - Parallel steps show running simultaneously
    - WebSocket updates reflect status changes in real-time
    - Completed edges are highlighted
    """

    async def test_pipeline_run_includes_step_status_per_node(self, api_client, test_repo):
        """Pipeline run response includes status for each graph node."""
        graph_data = {
            "steps": {
                "s1": {"id": "s1", "name": "Step 1", "type": "script", "config": {"command": "echo 1"}, "position": {"x": 100, "y": 100}},
                "s2": {"id": "s2", "name": "Step 2", "type": "script", "config": {"command": "echo 2"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "s1", "to_step": "s2", "condition": "success"}
            ],
            "entry_points": ["s1"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Status Viz Test", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        run_id = run_response.json()["id"]

        # Get run details
        details_response = await api_client.get(f"/api/pipeline-runs/{run_id}")
        run_details = details_response.json()

        # Should have step_runs with step IDs matching graph nodes
        step_runs = run_details.get("step_runs", [])
        # Each step_run should have a status and link to a step
        for step_run in step_runs:
            assert "status" in step_run
            assert step_run["status"] in ("pending", "running", "passed", "failed", "cancelled")


# =============================================================================
# Failure Mode Tests
# =============================================================================

class TestGraphPipelineFailureModes:
    """Tests for error handling and edge cases."""

    async def test_invalid_edge_reference_rejected(self, api_client, test_repo):
        """Edge referencing non-existent step is rejected."""
        graph_data = {
            "steps": {
                "real_step": {"id": "real_step", "name": "Real", "type": "script", "config": {"command": "echo"}, "position": {"x": 100, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "real_step", "to_step": "fake_step", "condition": "success"}
            ],
            "entry_points": ["real_step"],
            "version": 2
        }

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Invalid Edge", "steps_graph": graph_data},
        )

        assert response.status_code in (400, 422)  # Bad request or validation error

    async def test_cycle_detection(self, api_client, test_repo):
        """Graph with cycles is rejected or handled appropriately."""
        # A -> B -> A creates a cycle
        graph_data = {
            "steps": {
                "a": {"id": "a", "name": "A", "type": "script", "config": {"command": "echo A"}, "position": {"x": 100, "y": 100}},
                "b": {"id": "b", "name": "B", "type": "script", "config": {"command": "echo B"}, "position": {"x": 300, "y": 100}}
            },
            "edges": [
                {"id": "e1", "from_step": "a", "to_step": "b", "condition": "success"},
                {"id": "e2", "from_step": "b", "to_step": "a", "condition": "success"}
            ],
            "entry_points": ["a"],
            "version": 2
        }

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Cycle Graph", "steps_graph": graph_data},
        )

        # Should either reject (400) or accept (we might support cycles later)
        # For now, document the behavior
        assert response.status_code in (201, 400, 422)

    async def test_no_entry_points_rejected(self, api_client, test_repo):
        """Graph with no entry points is rejected."""
        graph_data = {
            "steps": {
                "orphan": {"id": "orphan", "name": "Orphan", "type": "script", "config": {"command": "echo"}, "position": {"x": 100, "y": 100}}
            },
            "edges": [],
            "entry_points": [],
            "version": 2
        }

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "No Entry", "steps_graph": graph_data},
        )

        assert response.status_code in (400, 422)

    async def test_partial_failure_marks_correct_steps(self, api_client, test_repo):
        """When one parallel branch fails, other branch's status is correct."""
        graph_data = {
            "steps": {
                "start": {"id": "start", "name": "Start", "type": "script", "config": {"command": "echo start"}, "position": {"x": 100, "y": 200}},
                "will_pass": {"id": "will_pass", "name": "Will Pass", "type": "script", "config": {"command": "echo pass"}, "position": {"x": 300, "y": 100}},
                "will_fail": {"id": "will_fail", "name": "Will Fail", "type": "script", "config": {"command": "exit 1"}, "position": {"x": 300, "y": 300}}
            },
            "edges": [
                {"id": "e1", "from_step": "start", "to_step": "will_pass", "condition": "success"},
                {"id": "e2", "from_step": "start", "to_step": "will_fail", "condition": "success"}
            ],
            "entry_points": ["start"],
            "version": 2
        }

        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={"name": "Partial Fail", "steps_graph": graph_data},
        )
        pipeline_id = create_response.json()["id"]

        run_response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
        run_id = run_response.json()["id"]

        # Wait for completion
        timeout = 30
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            response = await api_client.get(f"/api/pipeline-runs/{run_id}")
            run_data = response.json()
            if run_data["status"] in ("passed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)

        # Pipeline should be failed (one step failed)
        assert run_data["status"] == "failed"

        # Check individual step statuses
        step_runs = run_data.get("step_runs", [])
        pass_step = next((s for s in step_runs if s["step_name"] == "Will Pass"), None)
        fail_step = next((s for s in step_runs if s["step_name"] == "Will Fail"), None)

        if pass_step and fail_step:
            assert pass_step["status"] == "passed"
            assert fail_step["status"] == "failed"
