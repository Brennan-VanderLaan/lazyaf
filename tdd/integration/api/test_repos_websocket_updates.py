"""
Integration tests for Repo WebSocket real-time updates.

These tests verify that repo operations broadcast WebSocket messages
for real-time UI updates when repos are created, updated, or deleted.
"""
import sys
from pathlib import Path
import asyncio
import json

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload

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


class TestRepoCreationWebSocketBroadcast:
    """Tests for WebSocket broadcasts on repo creation."""

    async def test_create_repo_broadcasts_websocket_message(self, client, mock_ws):
        """Creating a repo broadcasts a repo_created WebSocket message."""
        mock_ws.clear_messages()

        # Create a repo
        response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="New Repo", remote_url="https://github.com/test/repo.git"),
        )
        assert response.status_code == 201
        repo = response.json()

        # Give websocket a moment to process
        await asyncio.sleep(0.1)

        # Verify WebSocket message was sent
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        # Find the repo_created message
        repo_messages = [m for m in messages if m["type"] == "repo_created"]
        assert len(repo_messages) >= 1

        message = repo_messages[-1]
        payload = message["payload"]

        # Verify message structure
        assert message["type"] == "repo_created"
        assert payload["id"] == repo["id"]
        assert payload["name"] == "New Repo"
        assert payload["remote_url"] == "https://github.com/test/repo.git"
        assert payload["is_ingested"] is False

    async def test_ingest_repo_broadcasts_websocket_message(self, client, mock_ws):
        """Ingesting a repo broadcasts a repo_created WebSocket message."""
        mock_ws.clear_messages()

        # Ingest a repo
        response = await client.post(
            "/api/repos/ingest",
            json=repo_create_payload(name="Ingested Repo", remote_url="https://github.com/test/repo.git"),
        )
        assert response.status_code == 201
        result = response.json()

        # Give websocket a moment to process
        await asyncio.sleep(0.1)

        # Verify WebSocket message was sent
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        message = messages[-1]
        payload = message["payload"]

        # Verify message structure
        assert message["type"] == "repo_created"
        assert payload["id"] == result["id"]
        assert payload["name"] == "Ingested Repo"
        assert payload["is_ingested"] is True

    async def test_create_repo_broadcasts_complete_data(self, client, mock_ws):
        """Repo creation broadcasts include all required fields including timestamps."""
        mock_ws.clear_messages()

        # Create a repo
        response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="Complete Repo", default_branch="develop"),
        )
        repo = response.json()

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify all required fields are present
        assert "id" in payload
        assert "name" in payload
        assert "remote_url" in payload
        assert "default_branch" in payload
        assert "is_ingested" in payload
        assert "internal_git_url" in payload

        # Verify timestamps are present and valid
        assert "created_at" in payload, "created_at timestamp missing from WebSocket broadcast"
        assert payload["created_at"] is not None

        # Verify data matches API response
        assert payload["name"] == repo["name"]
        assert payload["default_branch"] == "develop"


class TestRepoUpdateWebSocketBroadcast:
    """Tests for WebSocket broadcasts on repo updates."""

    async def test_update_repo_broadcasts_websocket_message(self, client, repo, mock_ws):
        """Updating a repo broadcasts a repo_updated WebSocket message."""
        mock_ws.clear_messages()

        # Update the repo
        response = await client.patch(
            f"/api/repos/{repo['id']}",
            json={"name": "Updated Name", "default_branch": "develop"},
        )
        assert response.status_code == 200
        updated_repo = response.json()

        await asyncio.sleep(0.1)

        # Verify WebSocket message
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        message = messages[-1]
        assert message["type"] == "repo_updated"
        assert message["payload"]["id"] == repo["id"]
        assert message["payload"]["name"] == "Updated Name"
        assert message["payload"]["default_branch"] == "develop"

    async def test_update_repo_broadcasts_with_timestamps(self, client, repo, mock_ws):
        """Repo updates broadcast complete data including timestamps."""
        mock_ws.clear_messages()

        # Update repo
        response = await client.patch(
            f"/api/repos/{repo['id']}",
            json={"remote_url": "https://github.com/updated/repo.git"},
        )
        updated_repo = response.json()

        await asyncio.sleep(0.1)

        message = mock_ws.get_last_message()
        payload = message["payload"]

        # Verify remote_url changed
        assert payload["remote_url"] == "https://github.com/updated/repo.git"

        # Verify timestamps are present
        assert "created_at" in payload
        assert payload["created_at"] == updated_repo["created_at"]


class TestRepoDeletionWebSocketBroadcast:
    """Tests for WebSocket broadcasts on repo deletion."""

    async def test_delete_repo_broadcasts_websocket_message(self, client, repo, mock_ws):
        """Deleting a repo broadcasts a repo_deleted WebSocket message."""
        repo_id = repo["id"]
        mock_ws.clear_messages()

        # Delete the repo
        response = await client.delete(f"/api/repos/{repo_id}")
        assert response.status_code == 204

        await asyncio.sleep(0.1)

        # Verify WebSocket message
        messages = mock_ws.get_messages()
        assert len(messages) >= 1

        message = messages[-1]
        assert message["type"] == "repo_deleted"
        assert message["payload"]["id"] == repo_id


class TestMultipleWebSocketConnectionsForRepos:
    """Tests for broadcasting repo operations to multiple WebSocket clients."""

    async def test_broadcasts_to_all_connected_clients(self, client):
        """Repo operations broadcast to all connected WebSocket clients."""
        # Connect multiple mock clients
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        ws3 = MockWebSocket()

        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.connect(ws3)

        try:
            # Create a repo
            response = await client.post(
                "/api/repos",
                json=repo_create_payload(name="Broadcast Test"),
            )
            assert response.status_code == 201

            await asyncio.sleep(0.1)

            # Verify all clients received the message
            for ws in [ws1, ws2, ws3]:
                messages = ws.get_messages()
                assert len(messages) >= 1
                assert messages[-1]["type"] == "repo_created"
                assert messages[-1]["payload"]["name"] == "Broadcast Test"

        finally:
            # Cleanup
            manager.disconnect(ws1)
            manager.disconnect(ws2)
            manager.disconnect(ws3)
