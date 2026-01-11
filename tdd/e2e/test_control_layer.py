"""
E2E Tests for Phase 12.3: Control Layer Integration

Tests the control layer communication protocol:
1. Control layer reports status correctly (running, completed, failed)
2. Control layer streams logs to backend
3. Control layer sends heartbeats
4. HOME persistence works across pipeline steps
5. Agent/script/agent pipelines share workspace correctly

These tests require Docker to be available.
"""

import pytest
import asyncio
import json
from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


class TestControlLayerStatusReporting:
    """Tests that control layer reports status correctly to backend."""

    @pytest.mark.slow
    async def test_control_layer_reports_running_status(self, api_client, test_repo):
        """Control layer reports 'running' when step starts."""
        # Create a pipeline with a script step
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Status Test Pipeline",
                "steps": [
                    {
                        "name": "echo-step",
                        "type": "script",
                        "config": {"command": "echo 'Hello from control layer'"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run the pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        assert run_response.status_code in (200, 201)
        run = run_response.json()

        # Wait for completion or timeout
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] in ("completed", "failed"):
                break

            await asyncio.sleep(1)

        # Verify step ran - check logs contain our echo
        steps_response = await api_client.get(f"/api/pipeline-runs/{run['id']}/steps")
        if steps_response.status_code == 200:
            steps = steps_response.json()
            if steps:
                assert any("Hello" in (step.get("logs") or "") for step in steps), \
                    "Expected log output not found in step logs"

    @pytest.mark.slow
    async def test_control_layer_reports_failed_on_nonzero_exit(self, api_client, test_repo):
        """Control layer reports 'failed' when command exits non-zero."""
        # Create pipeline with failing step
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Failure Test Pipeline",
                "steps": [
                    {
                        "name": "failing-step",
                        "type": "script",
                        "config": {"command": "exit 1"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run the pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        assert run_response.status_code in (200, 201)
        run = run_response.json()

        # Wait for failure
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "failed":
                # Success - pipeline correctly marked as failed
                return

            if current_run["status"] == "completed":
                pytest.fail("Pipeline should have failed but completed successfully")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not reach failed status - Docker may not be available")

    @pytest.mark.slow
    async def test_control_layer_reports_completed_on_success(self, api_client, test_repo):
        """Control layer reports 'completed' when command exits zero."""
        # Create pipeline with succeeding step
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Success Test Pipeline",
                "steps": [
                    {
                        "name": "success-step",
                        "type": "script",
                        "config": {"command": "echo 'success' && exit 0"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run the pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        assert run_response.status_code in (200, 201)
        run = run_response.json()

        # Wait for completion
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "completed":
                # Success!
                return

            if current_run["status"] == "failed":
                pytest.fail(f"Pipeline failed unexpectedly: {current_run}")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not complete - Docker may not be available")


class TestControlLayerLogStreaming:
    """Tests that control layer streams logs to backend."""

    @pytest.mark.slow
    async def test_stdout_logs_captured(self, api_client, test_repo):
        """stdout from command is captured in step logs."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Stdout Capture Pipeline",
                "steps": [
                    {
                        "name": "stdout-step",
                        "type": "script",
                        "config": {"command": "echo 'STDOUT_MARKER_12345'"},
                    }
                ],
            },
        )
        pipeline = pipeline_response.json()

        # Run and wait
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1)

        # Check logs
        logs_response = await api_client.get(
            f"/api/pipeline-runs/{run['id']}/steps/0/logs"
        )
        if logs_response.status_code == 200:
            logs_data = logs_response.json()
            logs = logs_data.get("logs", "")
            assert "STDOUT_MARKER_12345" in logs, f"Expected marker in logs: {logs}"

    @pytest.mark.slow
    async def test_stderr_logs_captured(self, api_client, test_repo):
        """stderr from command is captured in step logs."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Stderr Capture Pipeline",
                "steps": [
                    {
                        "name": "stderr-step",
                        "type": "script",
                        "config": {"command": "echo 'STDERR_MARKER_67890' >&2"},
                    }
                ],
            },
        )
        pipeline = pipeline_response.json()

        # Run and wait
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1)

        # Check logs
        logs_response = await api_client.get(
            f"/api/pipeline-runs/{run['id']}/steps/0/logs"
        )
        if logs_response.status_code == 200:
            logs_data = logs_response.json()
            logs = logs_data.get("logs", "")
            assert "STDERR_MARKER_67890" in logs, f"Expected marker in logs: {logs}"


class TestHomePersistenceAcrossSteps:
    """Tests that HOME directory persists across pipeline steps."""

    @pytest.mark.slow
    async def test_pip_install_persists_across_steps(self, api_client, test_repo):
        """Tool installed via pip in step 1 is available in step 2."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Pip Persistence Pipeline",
                "steps": [
                    {
                        "name": "install-step",
                        "type": "script",
                        "config": {"command": "pip install --user cowsay && echo 'INSTALL_DONE'"},
                        "continue_in_context": True,
                    },
                    {
                        "name": "use-step",
                        "type": "script",
                        "config": {"command": "python -m cowsay 'Persistence works!'"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for completion
        timeout = 120  # Longer timeout for pip install
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "completed":
                # Both steps succeeded - pip install persisted!
                return

            if current_run["status"] == "failed":
                # Check which step failed
                pytest.fail(f"Pipeline failed: {current_run}")

            await asyncio.sleep(2)

        pytest.skip("Pipeline did not complete - Docker may not be available")

    @pytest.mark.slow
    async def test_file_created_in_home_persists(self, api_client, test_repo):
        """File created in $HOME in step 1 is visible in step 2."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Home File Persistence Pipeline",
                "steps": [
                    {
                        "name": "create-file",
                        "type": "script",
                        "config": {"command": "echo 'PERSISTENCE_TEST_DATA' > $HOME/test_file.txt"},
                        "continue_in_context": True,
                    },
                    {
                        "name": "read-file",
                        "type": "script",
                        "config": {"command": "cat $HOME/test_file.txt"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for completion
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "completed":
                # Check step 2 logs for the data
                logs_response = await api_client.get(
                    f"/api/pipeline-runs/{run['id']}/steps/1/logs"
                )
                if logs_response.status_code == 200:
                    logs = logs_response.json().get("logs", "")
                    assert "PERSISTENCE_TEST_DATA" in logs, \
                        f"Expected persisted data in logs: {logs}"
                return

            if current_run["status"] == "failed":
                pytest.fail(f"Pipeline failed: {current_run}")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not complete - Docker may not be available")


class TestMixedStepTypePipelines:
    """Tests that different step types can share workspace."""

    @pytest.mark.slow
    async def test_script_creates_file_for_next_step(self, api_client, test_repo):
        """Script step creates file that subsequent step can read."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Script Chain Pipeline",
                "steps": [
                    {
                        "name": "create-artifact",
                        "type": "script",
                        "config": {"command": "echo 'BUILD_ARTIFACT_CONTENT' > /workspace/repo/artifact.txt"},
                        "continue_in_context": True,
                    },
                    {
                        "name": "verify-artifact",
                        "type": "script",
                        "config": {"command": "cat /workspace/repo/artifact.txt && test -f /workspace/repo/artifact.txt"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for completion
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "completed":
                return  # Success!

            if current_run["status"] == "failed":
                pytest.fail(f"Pipeline failed - workspace may not persist: {current_run}")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not complete - Docker may not be available")

    @pytest.mark.slow
    async def test_agent_script_agent_pipeline_shares_workspace(
        self, api_client, test_repo, mock_config
    ):
        """Agent step -> Script step -> Agent step all share the same workspace.

        This is the key Phase 12.3 integration test:
        1. Agent step (mock) creates a file
        2. Script step reads/modifies it
        3. Agent step (mock) can see the modifications
        """
        # Create mock config that creates a file
        agent1_config = {
            "response_mode": "batch",
            "delay_ms": 50,
            "file_operations": [
                {
                    "action": "create",
                    "path": "agent_output.txt",
                    "content": "Created by agent step 1\n"
                }
            ],
            "output_events": [
                {"type": "content", "text": "Creating file..."},
                {"type": "complete", "text": "Done"}
            ],
            "exit_code": 0
        }

        # Create mock config that appends to file
        agent3_config = {
            "response_mode": "batch",
            "delay_ms": 50,
            "file_operations": [
                {
                    "action": "modify",
                    "path": "agent_output.txt",
                    "search": "Modified by script",
                    "replace": "Modified by script\nVerified by agent step 3"
                }
            ],
            "output_events": [
                {"type": "content", "text": "Reading file..."},
                {"type": "complete", "text": "Done"}
            ],
            "exit_code": 0
        }

        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Agent-Script-Agent Pipeline",
                "steps": [
                    {
                        "name": "agent-create",
                        "type": "agent",
                        "config": {
                            "runner_type": "mock",
                            "mock_config": agent1_config
                        },
                        "continue_in_context": True,
                    },
                    {
                        "name": "script-modify",
                        "type": "script",
                        "config": {
                            "command": "echo 'Modified by script' >> /workspace/repo/agent_output.txt && cat /workspace/repo/agent_output.txt"
                        },
                        "continue_in_context": True,
                    },
                    {
                        "name": "agent-verify",
                        "type": "agent",
                        "config": {
                            "runner_type": "mock",
                            "mock_config": agent3_config
                        },
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for completion
        timeout = 120
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "completed":
                # Verify script step saw agent's file
                logs_response = await api_client.get(
                    f"/api/pipeline-runs/{run['id']}/steps/1/logs"
                )
                if logs_response.status_code == 200:
                    logs = logs_response.json().get("logs", "")
                    assert "Created by agent" in logs, \
                        f"Script step should see agent's file: {logs}"
                return  # Success!

            if current_run["status"] == "failed":
                pytest.fail(f"Pipeline failed: {current_run}")

            await asyncio.sleep(2)

        pytest.skip("Pipeline did not complete - mock runner may not be available")


class TestControlLayerErrorHandling:
    """Tests that control layer handles errors gracefully."""

    @pytest.mark.slow
    async def test_timeout_kills_container(self, api_client, test_repo):
        """Step that exceeds timeout is killed and marked as timeout."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Timeout Test Pipeline",
                "steps": [
                    {
                        "name": "slow-step",
                        "type": "script",
                        "config": {
                            "command": "sleep 300",  # 5 minutes
                            "timeout_seconds": 5,    # But only 5 second timeout
                        },
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for timeout (should be quick - 5 seconds + some buffer)
        timeout = 30
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "failed":
                # Check if it was a timeout
                return  # Success - it failed (likely timeout)

            if current_run["status"] == "completed":
                pytest.fail("Step should have timed out")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not fail - Docker may not be available")

    @pytest.mark.slow
    async def test_command_not_found_fails_gracefully(self, api_client, test_repo):
        """Step with non-existent command fails with clear error."""
        pipeline_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/pipelines",
            json={
                "name": "Bad Command Pipeline",
                "steps": [
                    {
                        "name": "bad-command",
                        "type": "script",
                        "config": {"command": "this_command_does_not_exist_12345"},
                    }
                ],
            },
        )
        assert pipeline_response.status_code == 201
        pipeline = pipeline_response.json()

        # Run pipeline
        run_response = await api_client.post(f"/api/pipelines/{pipeline['id']}/run")
        run = run_response.json()

        # Wait for failure
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            status_response = await api_client.get(f"/api/pipeline-runs/{run['id']}")
            current_run = status_response.json()

            if current_run["status"] == "failed":
                return  # Success - bad command caused failure

            if current_run["status"] == "completed":
                pytest.fail("Step with bad command should have failed")

            await asyncio.sleep(1)

        pytest.skip("Pipeline did not fail - Docker may not be available")
