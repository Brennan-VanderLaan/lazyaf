"""
Runtime demo: End-to-end Pipeline Creation and Execution.

This demo tests a complete pipeline workflow scenario including:
1. Creating a repository
2. Defining a multi-step CI pipeline
3. Running the pipeline
4. Monitoring execution progress
5. Cancelling a pipeline run

This serves as executable documentation for the Pipelines feature.
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import (
    repo_ingest_payload,
    pipeline_create_payload,
    pipeline_step_payload,
)


@pytest.mark.demo
class TestPipelineWorkflowDemo:
    """
    End-to-end demo of the Pipelines feature.

    This demo walks through a complete CI/CD pipeline workflow,
    demonstrating all the key capabilities of the Pipelines system.
    """

    @pytest_asyncio.fixture
    async def demo_repo(self, client, clean_git_repos):
        """Create a demo repository for the workflow."""
        response = await client.post(
            "/api/repos/ingest",
            json=repo_ingest_payload(name="demo-project"),
        )
        assert response.status_code == 201
        repo = response.json()
        print(f"\n[Setup] Created repo: {repo['name']} (ID: {repo['id'][:8]}...)")
        return repo

    async def test_complete_pipeline_workflow(self, client, demo_repo, clean_job_queue):
        """
        Demo: Complete pipeline creation and execution workflow.

        This test demonstrates:
        1. Creating a multi-step CI pipeline
        2. Running the pipeline
        3. Monitoring run status
        4. Viewing step details
        5. Cancelling a run
        """
        print("\n" + "=" * 60)
        print("DEMO: Complete Pipeline Workflow")
        print("=" * 60)

        # ---------------------------------------------------------------------
        # Step 1: Create a CI Pipeline with Multiple Steps
        # ---------------------------------------------------------------------
        print("\n[1] Creating CI Pipeline with 3 steps...")

        steps = [
            pipeline_step_payload(
                name="Install Dependencies",
                step_type="script",
                config={"command": "npm install"},
                on_success="next",
                on_failure="stop",
            ),
            pipeline_step_payload(
                name="Run Linter",
                step_type="script",
                config={"command": "npm run lint"},
                on_success="next",
                on_failure="next",  # Continue even if lint fails
            ),
            pipeline_step_payload(
                name="Run Tests",
                step_type="script",
                config={"command": "npm test"},
                on_success="stop",
                on_failure="stop",
                timeout=600,  # 10 minute timeout for tests
            ),
        ]

        pipeline_response = await client.post(
            f"/api/repos/{demo_repo['id']}/pipelines",
            json=pipeline_create_payload(
                name="CI Pipeline",
                description="Continuous Integration: Install, Lint, Test",
                steps=steps,
            ),
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        print(f"    Pipeline created: {pipeline['name']}")
        print(f"    Pipeline ID: {pipeline['id'][:8]}...")
        print(f"    Steps: {len(pipeline['steps'])}")
        for i, step in enumerate(pipeline['steps']):
            print(f"      [{i}] {step['name']} ({step['type']})")
            print(f"          on_success: {step['on_success']}, on_failure: {step['on_failure']}")

        # ---------------------------------------------------------------------
        # Step 2: List Pipelines for the Repository
        # ---------------------------------------------------------------------
        print("\n[2] Listing pipelines for repository...")

        list_response = await client.get(f"/api/repos/{demo_repo['id']}/pipelines")
        assert list_response.status_code == 200
        pipelines = list_response.json()

        print(f"    Found {len(pipelines)} pipeline(s)")
        for p in pipelines:
            print(f"      - {p['name']} (template: {p['is_template']})")

        # ---------------------------------------------------------------------
        # Step 3: Run the Pipeline
        # ---------------------------------------------------------------------
        print("\n[3] Starting pipeline run...")

        run_response = await client.post(
            f"/api/pipelines/{pipeline['id']}/run",
            json={"trigger_type": "manual"},
        )
        assert run_response.status_code == 200
        run = run_response.json()

        print(f"    Run started: {run['id'][:8]}...")
        print(f"    Status: {run['status']}")
        print(f"    Trigger: {run['trigger_type']}")
        print(f"    Steps: {run['steps_completed']}/{run['steps_total']}")

        # ---------------------------------------------------------------------
        # Step 4: Get Run Details with Step Runs
        # ---------------------------------------------------------------------
        print("\n[4] Getting pipeline run details...")

        details_response = await client.get(f"/api/pipeline-runs/{run['id']}")
        assert details_response.status_code == 200
        details = details_response.json()

        print(f"    Run ID: {details['id'][:8]}...")
        print(f"    Status: {details['status']}")
        print(f"    Current Step: {details['current_step']}")
        print(f"    Step Runs: {len(details['step_runs'])}")
        for sr in details['step_runs']:
            print(f"      [{sr['step_index']}] {sr['step_name']}: {sr['status']}")

        # ---------------------------------------------------------------------
        # Step 5: Get Step Logs
        # ---------------------------------------------------------------------
        print("\n[5] Getting step logs for step 0...")

        logs_response = await client.get(f"/api/pipeline-runs/{run['id']}/steps/0/logs")
        assert logs_response.status_code == 200
        logs = logs_response.json()

        print(f"    Step: {logs['step_name']}")
        print(f"    Status: {logs['status']}")
        print(f"    Logs: {logs['logs'][:100] if logs['logs'] else '(empty)'}")

        # ---------------------------------------------------------------------
        # Step 6: List All Pipeline Runs
        # ---------------------------------------------------------------------
        print("\n[6] Listing all pipeline runs...")

        all_runs_response = await client.get("/api/pipeline-runs")
        assert all_runs_response.status_code == 200
        all_runs = all_runs_response.json()

        print(f"    Found {len(all_runs)} run(s)")
        for r in all_runs[:3]:  # Show first 3
            print(f"      - {r['id'][:8]}... ({r['status']})")

        # ---------------------------------------------------------------------
        # Step 7: Cancel the Pipeline Run
        # ---------------------------------------------------------------------
        print("\n[7] Cancelling pipeline run...")

        cancel_response = await client.post(f"/api/pipeline-runs/{run['id']}/cancel")
        assert cancel_response.status_code == 200
        cancelled = cancel_response.json()

        print(f"    Run cancelled: {cancelled['id'][:8]}...")
        print(f"    Status: {cancelled['status']}")
        print(f"    Completed at: {cancelled['completed_at']}")

        # ---------------------------------------------------------------------
        # Step 8: Update Pipeline (add a new step)
        # ---------------------------------------------------------------------
        print("\n[8] Updating pipeline to add deployment step...")

        updated_steps = steps + [
            pipeline_step_payload(
                name="Deploy to Staging",
                step_type="docker",
                config={"image": "aws-cli:latest", "command": "aws deploy"},
                on_success="stop",
                on_failure="stop",
            ),
        ]

        update_response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"steps": updated_steps},
        )
        assert update_response.status_code == 200
        updated = update_response.json()

        print(f"    Pipeline updated: {updated['name']}")
        print(f"    New step count: {len(updated['steps'])}")

        # ---------------------------------------------------------------------
        # Step 9: Cleanup - Delete Pipeline
        # ---------------------------------------------------------------------
        print("\n[9] Cleaning up - deleting pipeline...")

        delete_response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert delete_response.status_code == 204

        print("    Pipeline deleted successfully")

        # ---------------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------------
        print("\n" + "=" * 60)
        print("DEMO COMPLETE")
        print("=" * 60)
        print("\nDemonstrated capabilities:")
        print("  - Pipeline CRUD operations")
        print("  - Multi-step pipeline definition")
        print("  - Pipeline execution and monitoring")
        print("  - Step logs retrieval")
        print("  - Pipeline cancellation")
        print("  - Pipeline modification")


@pytest.mark.demo
class TestPipelineWithBranchingDemo:
    """
    Demo of advanced pipeline branching features.

    Shows how to configure pipelines with custom
    on_success and on_failure actions.
    """

    @pytest_asyncio.fixture
    async def demo_repo(self, client, clean_git_repos):
        """Create a demo repository."""
        response = await client.post(
            "/api/repos/ingest",
            json=repo_ingest_payload(name="branching-demo"),
        )
        return response.json()

    async def test_pipeline_with_branching_actions(self, client, demo_repo, clean_job_queue):
        """
        Demo: Pipeline with conditional branching.

        Shows how to configure:
        - on_success: "next" to continue, "stop" to finish, "merge:{branch}" to auto-merge
        - on_failure: "next" to continue despite failure, "stop" to halt, "trigger:{card_id}" to fix
        """
        print("\n" + "=" * 60)
        print("DEMO: Pipeline Branching Actions")
        print("=" * 60)

        # Create a pipeline with branching logic
        steps = [
            pipeline_step_payload(
                name="Lint (non-blocking)",
                step_type="script",
                config={"command": "npm run lint"},
                on_success="next",
                on_failure="next",  # Continue even if lint fails
            ),
            pipeline_step_payload(
                name="Unit Tests",
                step_type="script",
                config={"command": "npm test:unit"},
                on_success="next",
                on_failure="stop",  # Stop if unit tests fail
            ),
            pipeline_step_payload(
                name="Integration Tests",
                step_type="script",
                config={"command": "npm test:integration"},
                on_success="merge:main",  # Auto-merge on success
                on_failure="stop",
            ),
        ]

        pipeline_response = await client.post(
            f"/api/repos/{demo_repo['id']}/pipelines",
            json=pipeline_create_payload(
                name="Branching Pipeline",
                description="Demo of conditional branching",
                steps=steps,
            ),
        )
        pipeline = pipeline_response.json()

        print(f"\nPipeline: {pipeline['name']}")
        print("\nBranching logic:")
        for step in pipeline['steps']:
            print(f"  {step['name']}:")
            print(f"    on_success -> {step['on_success']}")
            print(f"    on_failure -> {step['on_failure']}")

        print("\nExpected behavior:")
        print("  1. Lint runs, continues regardless of result")
        print("  2. Unit tests run, stops on failure")
        print("  3. Integration tests run, auto-merges to main on success")

        # Cleanup
        await client.delete(f"/api/pipelines/{pipeline['id']}")
        print("\n[Cleanup] Pipeline deleted")


@pytest.mark.demo
class TestPipelineTemplatesDemo:
    """
    Demo of pipeline templates feature.

    Shows how to create and use pipeline templates
    that can be reused across projects.
    """

    @pytest_asyncio.fixture
    async def demo_repo(self, client, clean_git_repos):
        """Create a demo repository."""
        response = await client.post(
            "/api/repos/ingest",
            json=repo_ingest_payload(name="templates-demo"),
        )
        return response.json()

    async def test_pipeline_templates(self, client, demo_repo):
        """
        Demo: Creating and using pipeline templates.
        """
        print("\n" + "=" * 60)
        print("DEMO: Pipeline Templates")
        print("=" * 60)

        # Create a template pipeline
        template_steps = [
            pipeline_step_payload(
                name="Setup",
                step_type="script",
                config={"command": "npm ci"},
            ),
            pipeline_step_payload(
                name="Build",
                step_type="script",
                config={"command": "npm run build"},
            ),
            pipeline_step_payload(
                name="Test",
                step_type="script",
                config={"command": "npm test"},
            ),
        ]

        template_response = await client.post(
            f"/api/repos/{demo_repo['id']}/pipelines",
            json=pipeline_create_payload(
                name="Node.js CI Template",
                description="Standard CI pipeline for Node.js projects",
                steps=template_steps,
                is_template=True,
            ),
        )
        template = template_response.json()

        print(f"\nTemplate created: {template['name']}")
        print(f"  is_template: {template['is_template']}")
        print(f"  steps: {len(template['steps'])}")

        # List to show template is marked differently
        list_response = await client.get(f"/api/repos/{demo_repo['id']}/pipelines")
        pipelines = list_response.json()

        print("\nPipelines in repo:")
        for p in pipelines:
            template_marker = "[TEMPLATE]" if p['is_template'] else ""
            print(f"  - {p['name']} {template_marker}")

        # Cleanup
        await client.delete(f"/api/pipelines/{template['id']}")
        print("\n[Cleanup] Template deleted")
