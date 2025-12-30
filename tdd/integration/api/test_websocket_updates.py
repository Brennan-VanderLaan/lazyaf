"""
Integration tests for WebSocket real-time updates.

These tests verify that card operations broadcast WebSocket messages
with complete data including timestamps for real-time UI updates.
"""
import sys
from pathlib import Path
import asyncio
import json
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import WebSocket

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload, card_create_payload

from app.services.websocket import manager


class MockWebSocket:
    """Mock WebSocket for testing broadcasts."""

    def __init__(self):
        self.messages = []
        self.closed = False

    async def accept(self):
        """Mock accept."""
        pass

    async def send_text(self, data: str):
        """Capture sent messages."""
        if not self.closed:
            self.messages.append(json.loads(data))

    def get_messages(self):
        """Return all captured messages."""
        return self.messages

    def get_last_message(self):
        """Return last captured message."""
        return self.messages[-1] if self.messages else None

    def clear_messages(self):
        """Clear captured messages."""
        self.messages.clear()


@pytest_asyncio.fixture
async def mock_ws():
    """Provide a mock WebSocket connection."""
    ws = MockWebSocket()
    await manager.connect(ws)
    yield ws
    manager.disconnect(ws)


class TestCardCreationWebSocketBroadcast:
    """Tests for WebSocket broadcasts on card creation."""

    async def test_create_card_broadcasts_websocket_message(self, client, repo, mock_ws):
        """Creating a card broadcasts a card_updated WebSocket message."""
        # Clear any previous messages
        mock_ws.clear_messages()

        # Create a card
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="New Feature", description="Test feature"),
        )
        assert response.status_code == 201
        card = response.json()

        # Give websocket a moment to process
        await asyncio.sleep(0.1)

        # Verify WebSocket message was sent
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        # Find the card_updated message
        card_messages = [m for m in messages if m["type"] == "card_updated"]
        assert len(card_messages) >= 1

        message = card_messages[-1]
        payload = message["payload"]

        # Verify message structure
        assert message["type"] == "card_updated"
        assert payload["id"] == card["id"]
        assert payload["repo_id"] == repo["id"]
        assert payload["title"] == "New Feature"
        assert payload["description"] == "Test feature"
        assert payload["status"] == "todo"

    async def test_create_card_broadcasts_complete_card_data(self, client, repo, mock_ws):
        """Card creation broadcasts include all required fields including timestamps."""
        mock_ws.clear_messages()

        # Create a card
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Complete Card"),
        )
        card = response.json()

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify all required fields are present
        assert "id" in payload
        assert "repo_id" in payload
        assert "title" in payload
        assert "description" in payload
        assert "status" in payload
        assert "branch_name" in payload
        assert "pr_url" in payload
        assert "job_id" in payload

        # Verify timestamps are present and valid
        assert "created_at" in payload, "created_at timestamp missing from WebSocket broadcast"
        assert "updated_at" in payload, "updated_at timestamp missing from WebSocket broadcast"
        assert payload["created_at"] is not None
        assert payload["updated_at"] is not None

        # Verify timestamps match the API response
        assert payload["created_at"] == card["created_at"]
        assert payload["updated_at"] == card["updated_at"]


class TestCardUpdateWebSocketBroadcast:
    """Tests for WebSocket broadcasts on card updates."""

    async def test_update_card_broadcasts_websocket_message(self, client, repo, mock_ws):
        """Updating a card broadcasts a card_updated WebSocket message."""
        # Create a card first
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Original"),
        )
        card_id = create_response.json()["id"]

        # Clear messages from creation
        mock_ws.clear_messages()

        # Update the card
        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"title": "Updated Title"},
        )
        assert response.status_code == 200

        await asyncio.sleep(0.1)

        # Verify WebSocket message
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        message = messages[-1]
        assert message["type"] == "card_updated"
        assert message["payload"]["id"] == card_id
        assert message["payload"]["title"] == "Updated Title"

    async def test_update_card_status_broadcasts_with_timestamps(self, client, repo, mock_ws):
        """Status updates broadcast complete data including timestamps."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Status Test"),
        )
        card_id = create_response.json()["id"]

        mock_ws.clear_messages()

        # Update card status
        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_progress"},
        )
        updated_card = response.json()

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify status changed
        assert payload["status"] == "in_progress"

        # Verify timestamps are present
        assert "created_at" in payload
        assert "updated_at" in payload
        assert payload["created_at"] == updated_card["created_at"]
        assert payload["updated_at"] == updated_card["updated_at"]


class TestCardDeletionWebSocketBroadcast:
    """Tests for WebSocket broadcasts on card deletion."""

    async def test_delete_card_broadcasts_websocket_message(self, client, repo, mock_ws):
        """Deleting a card broadcasts a card_deleted WebSocket message."""
        # Create a card first
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="To Delete"),
        )
        card_id = create_response.json()["id"]

        # Clear messages from creation
        mock_ws.clear_messages()

        # Delete the card
        response = await client.delete(f"/api/cards/{card_id}")
        assert response.status_code == 204

        await asyncio.sleep(0.1)

        # Verify WebSocket message
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        message = messages[-1]
        assert message["type"] == "card_deleted"
        assert message["payload"]["id"] == card_id


class TestCardActionWebSocketBroadcasts:
    """Tests for WebSocket broadcasts on card lifecycle actions."""

    async def test_start_card_broadcasts_with_timestamps(self, client, ingested_repo, clean_job_queue, mock_ws):
        """Starting a card broadcasts update with timestamps."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Start Test"),
        )
        card_id = create_response.json()["id"]

        mock_ws.clear_messages()

        # Start the card
        response = await client.post(f"/api/cards/{card_id}/start")
        assert response.status_code == 200
        started_card = response.json()

        await asyncio.sleep(0.1)

        # Find the card_updated message (there may be job_status messages too)
        messages = mock_ws.get_messages()
        card_messages = [m for m in messages if m["type"] == "card_updated"]
        assert len(card_messages) >= 1

        message = card_messages[-1]
        payload = message["payload"]

        # Verify card is in_progress with job and branch
        assert payload["id"] == card_id
        assert payload["status"] == "in_progress"
        assert payload["job_id"] is not None
        assert payload["branch_name"] is not None

        # Verify timestamps
        assert "created_at" in payload
        assert "updated_at" in payload
        assert payload["created_at"] == started_card["created_at"]
        assert payload["updated_at"] == started_card["updated_at"]

    async def test_approve_card_broadcasts_with_timestamps(self, client, repo, mock_ws):
        """Approving a card broadcasts update with timestamps."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Approve Test"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        mock_ws.clear_messages()

        # Approve the card
        response = await client.post(
            f"/api/cards/{card_id}/approve",
            json={"target_branch": None},
        )
        assert response.status_code == 200
        result = response.json()
        approved_card = result["card"]

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify card is done
        assert payload["status"] == "done"

        # Verify timestamps
        assert "created_at" in payload
        assert "updated_at" in payload
        assert payload["created_at"] == approved_card["created_at"]
        assert payload["updated_at"] == approved_card["updated_at"]

    async def test_reject_card_broadcasts_with_timestamps(self, client, repo, mock_ws):
        """Rejecting a card broadcasts update with timestamps."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Reject Test"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        mock_ws.clear_messages()

        # Reject the card
        response = await client.post(f"/api/cards/{card_id}/reject")
        assert response.status_code == 200
        rejected_card = response.json()

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify card reset to todo
        assert payload["status"] == "todo"
        assert payload["branch_name"] is None
        assert payload["pr_url"] is None

        # Verify timestamps
        assert "created_at" in payload
        assert "updated_at" in payload
        assert payload["created_at"] == rejected_card["created_at"]
        assert payload["updated_at"] == rejected_card["updated_at"]

    async def test_retry_card_broadcasts_with_timestamps(self, client, ingested_repo, clean_job_queue, mock_ws):
        """Retrying a card broadcasts update with timestamps."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Retry Test"),
        )
        card_id = create_response.json()["id"]

        # Set to failed status
        await client.patch(f"/api/cards/{card_id}", json={"status": "failed"})

        mock_ws.clear_messages()

        # Retry the card
        response = await client.post(f"/api/cards/{card_id}/retry")
        assert response.status_code == 200
        retried_card = response.json()

        await asyncio.sleep(0.1)

        # Find the card_updated message
        messages = mock_ws.get_messages()
        card_messages = [m for m in messages if m["type"] == "card_updated"]
        assert len(card_messages) >= 1

        message = card_messages[-1]
        payload = message["payload"]

        # Verify card is in_progress with new job
        assert payload["status"] == "in_progress"
        assert payload["job_id"] is not None

        # Verify timestamps
        assert "created_at" in payload
        assert "updated_at" in payload
        assert payload["created_at"] == retried_card["created_at"]
        assert payload["updated_at"] == retried_card["updated_at"]


class TestMultipleWebSocketConnections:
    """Tests for broadcasting to multiple WebSocket clients."""

    async def test_broadcasts_to_all_connected_clients(self, client, repo):
        """Card operations broadcast to all connected WebSocket clients."""
        # Connect multiple mock clients
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        ws3 = MockWebSocket()

        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.connect(ws3)

        try:
            # Create a card
            response = await client.post(
                f"/api/repos/{repo['id']}/cards",
                json=card_create_payload(title="Broadcast Test"),
            )
            assert response.status_code == 201

            await asyncio.sleep(0.1)

            # Verify all clients received the message
            for ws in [ws1, ws2, ws3]:
                messages = ws.get_messages()
                assert len(messages) >= 1
                assert messages[-1]["type"] == "card_updated"
                assert messages[-1]["payload"]["title"] == "Broadcast Test"

        finally:
            # Cleanup
            manager.disconnect(ws1)
            manager.disconnect(ws2)
            manager.disconnect(ws3)
