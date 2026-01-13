"""
E2E Tests for User Story 1: Create and Execute a Card

Tests the full workflow:
1. Create a card with mock executor
2. Start work on the card
3. Mock runner executes and makes changes
4. Card reaches "in_review" status
5. Diff is available showing changes

These tests require the mock runner to be running.
For API-only tests without the runner, see tdd/integration/api/test_cards_api.py
"""

import pytest
import asyncio

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


class TestCardCreateAndQueue:
    """Tests for card creation and job queuing (no runner required)."""

    async def test_create_card_with_mock_runner_type(self, api_client, test_repo):
        """Card can be created with mock runner type."""
        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Test Feature",
                "description": "A test feature implementation",
                "runner_type": "mock",
            },
        )
        assert response.status_code == 201
        card = response.json()
        assert card["runner_type"] == "mock"
        assert card["status"] == "todo"

    async def test_create_card_with_mock_config(self, api_client, test_repo, mock_config):
        """Card can be created with embedded mock configuration."""
        config = mock_config["simple_change"](
            file_path="src/feature.py",
            content="def feature():\n    return 'hello'\n",
        )

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Feature with Mock Config",
                "description": "Card with embedded mock config",
                "runner_type": "mock",
                "step_config": {"mock_config": config},
            },
        )
        assert response.status_code == 201
        card = response.json()
        assert card["step_config"]["mock_config"] == config

    async def test_start_card_queues_job(self, api_client, test_repo):
        """Starting a card queues a job for the mock runner."""
        # Create card
        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Queue Test",
                "description": "Test that starting queues a job",
                "runner_type": "mock",
            },
        )
        assert create_response.status_code == 201
        card = create_response.json()

        # Start card
        start_response = await api_client.post(f"/api/cards/{card['id']}/start")
        assert start_response.status_code == 200

        # Verify card is now in_progress with job_id
        get_response = await api_client.get(f"/api/cards/{card['id']}")
        assert get_response.status_code == 200
        updated_card = get_response.json()
        assert updated_card["status"] == "in_progress"
        assert updated_card["job_id"] is not None

    async def test_card_runner_type_persists(self, api_client, test_repo):
        """Card's runner_type is preserved after starting."""
        # Create and start card
        create_response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Runner Type Test",
                "description": "Verify runner type persists",
                "runner_type": "mock",
            },
        )
        card = create_response.json()
        await api_client.post(f"/api/cards/{card['id']}/start")

        # Verify card still has mock runner type
        get_response = await api_client.get(f"/api/cards/{card['id']}")
        updated_card = get_response.json()
        assert updated_card["runner_type"] == "mock"
        assert updated_card["status"] == "in_progress"
        assert updated_card["job_id"] is not None


class TestCardExecuteWithMockRunner:
    """Tests that require the mock runner to be running.

    To run these tests:
    1. Start the backend: docker-compose up backend
    2. Start the mock runner: MOCK_RUNNERS=1 docker-compose --profile testing up runner-mock
    3. Run tests: pytest tdd/e2e/test_card_execute.py -v -k "MockRunner"
    """

    @pytest.mark.slow
    async def test_card_reaches_in_review_status(
        self, api_client, test_repo, mock_config, websocket_client
    ):
        """Card reaches in_review status after mock executor completes."""
        # Create card with simple mock config
        config = mock_config["simple_change"]()

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "E2E Execute Test",
                "description": "Test full execution flow",
                "runner_type": "mock",
                "step_config": {"mock_config": config},
            },
        )
        card = response.json()

        # Start card
        await api_client.post(f"/api/cards/{card['id']}/start")

        # Wait for card to reach in_review (mock runner must be running)
        timeout = 60  # seconds
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            get_response = await api_client.get(f"/api/cards/{card['id']}")
            current_card = get_response.json()

            if current_card["status"] == "in_review":
                # Success!
                assert current_card["branch_name"] is not None
                return

            if current_card["status"] == "failed":
                pytest.fail(f"Card failed unexpectedly: {current_card}")

            await asyncio.sleep(0.3)

        pytest.fail(f"Card did not reach in_review within {timeout}s. Status: {current_card['status']}")

    @pytest.mark.slow
    async def test_websocket_receives_card_updates(
        self, api_client, test_repo, mock_config, websocket_client
    ):
        """WebSocket receives card status updates during execution."""
        config = mock_config["simple_change"]()

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "WebSocket Test",
                "description": "Test WebSocket updates",
                "runner_type": "mock",
                "step_config": {"mock_config": config},
            },
        )
        card = response.json()

        # Clear any existing events
        await websocket_client.clear_events()

        # Start card
        await api_client.post(f"/api/cards/{card['id']}/start")

        # Wait for card_updated event
        event = await websocket_client.wait_for_event(
            "card_updated",
            timeout=30,
            predicate=lambda e: e.get("data", {}).get("card_id") == card["id"],
        )

        if event is None:
            # Mock runner might not be running
            pytest.skip("No WebSocket event received - mock runner may not be running")

        assert event["data"]["card_id"] == card["id"]

    @pytest.mark.slow
    async def test_diff_available_after_completion(
        self, api_client, test_repo, mock_config
    ):
        """Diff is available after card reaches in_review."""
        config = mock_config["simple_change"](
            file_path="src/new_feature.py",
            content="# Generated by mock\ndef hello():\n    return 'world'\n",
        )

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Diff Test",
                "description": "Test diff availability",
                "runner_type": "mock",
                "step_config": {"mock_config": config},
            },
        )
        card = response.json()

        # Start and wait for completion
        await api_client.post(f"/api/cards/{card['id']}/start")

        # Wait for in_review
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            get_response = await api_client.get(f"/api/cards/{card['id']}")
            current_card = get_response.json()

            if current_card["status"] == "in_review":
                break

            if current_card["status"] == "failed":
                pytest.fail(f"Card failed: {current_card}")

            await asyncio.sleep(0.3)
        else:
            pytest.skip("Card did not reach in_review - mock runner may not be running")

        # Get diff
        diff_response = await api_client.get(f"/api/cards/{card['id']}/diff")
        assert diff_response.status_code == 200
        diff_data = diff_response.json()
        assert "diff" in diff_data
        assert "src/new_feature.py" in diff_data["diff"]


class TestCardExecuteFailureModes:
    """Tests for failure scenarios."""

    @pytest.mark.slow
    async def test_card_fails_on_executor_error(self, api_client, test_repo, mock_config):
        """Card reaches failed status when mock executor returns error."""
        config = mock_config["error"](error_message="Simulated failure for testing")

        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Failure Test",
                "description": "Test error handling",
                "runner_type": "mock",
                "step_config": {"mock_config": config},
            },
        )
        card = response.json()

        # Start card
        await api_client.post(f"/api/cards/{card['id']}/start")

        # Wait for failed status
        timeout = 60
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            get_response = await api_client.get(f"/api/cards/{card['id']}")
            current_card = get_response.json()

            if current_card["status"] == "failed":
                # Success - card failed as expected
                return

            if current_card["status"] in ("done", "in_review"):
                pytest.fail(f"Card should have failed but got status: {current_card['status']}")

            await asyncio.sleep(0.3)

        pytest.skip("Card did not reach failed status - mock runner may not be running")

    async def test_cannot_start_already_in_progress_card(self, api_client, test_repo):
        """Cannot start a card that's already in progress."""
        # Create and start card
        response = await api_client.post(
            f"/api/repos/{test_repo['id']}/cards",
            json={
                "title": "Double Start Test",
                "description": "Test preventing double start",
                "runner_type": "mock",
            },
        )
        card = response.json()

        # First start - should succeed
        start1 = await api_client.post(f"/api/cards/{card['id']}/start")
        assert start1.status_code == 200

        # Second start - should fail
        start2 = await api_client.post(f"/api/cards/{card['id']}/start")
        assert start2.status_code in (400, 409)  # Bad request or Conflict
