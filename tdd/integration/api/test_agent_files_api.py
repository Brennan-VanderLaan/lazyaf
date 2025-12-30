"""
Integration tests for Agent Files API endpoints.

These tests verify the full request/response cycle through the FastAPI
application with a real (in-memory) database.
"""
import sys
from pathlib import Path

import pytest

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


def agent_file_create_payload(name="test_agent.py", content="# Test agent", description=None):
    """Factory for creating agent file payloads."""
    payload = {
        "name": name,
        "content": content,
    }
    if description is not None:
        payload["description"] = description
    return payload


def agent_file_update_payload(**kwargs):
    """Factory for updating agent file payloads."""
    return {k: v for k, v in kwargs.items() if v is not None}


class TestListAgentFiles:
    """Tests for GET /api/agent-files endpoint."""

    async def test_list_agent_files_empty(self, client):
        """Returns empty list when no agent files exist."""
        response = await client.get("/api/agent-files")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_agent_files_with_data(self, client):
        """Returns all agent files when they exist."""
        # Create two agent files
        await client.post("/api/agent-files", json=agent_file_create_payload(name="agent1.py"))
        await client.post("/api/agent-files", json=agent_file_create_payload(name="agent2.py"))

        response = await client.get("/api/agent-files")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_agent_files_returns_fields(self, client):
        """Returns agent files with all expected fields."""
        await client.post("/api/agent-files", json=agent_file_create_payload(
            name="test.py",
            content="print('hello')",
            description="Test agent"
        ))

        response = await client.get("/api/agent-files")
        assert_status_code(response, 200)
        data = response.json()
        assert len(data) == 1

        agent_file = data[0]
        assert "id" in agent_file
        assert "name" in agent_file
        assert "content" in agent_file
        assert "description" in agent_file
        assert "created_at" in agent_file
        assert "updated_at" in agent_file
        assert agent_file["name"] == "test.py"
        assert agent_file["content"] == "print('hello')"
        assert agent_file["description"] == "Test agent"


class TestCreateAgentFile:
    """Tests for POST /api/agent-files endpoint."""

    async def test_create_agent_file(self, client):
        """Creates a new agent file."""
        payload = agent_file_create_payload(name="agent.py", content="# Agent code")
        response = await client.post("/api/agent-files", json=payload)
        assert_created_response(response)
        assert_json_contains(response, name="agent.py", content="# Agent code")

    async def test_create_agent_file_with_description(self, client):
        """Creates agent file with description."""
        payload = agent_file_create_payload(
            name="agent.py",
            content="# Agent",
            description="My custom agent"
        )
        response = await client.post("/api/agent-files", json=payload)
        assert_created_response(response)
        assert_json_contains(response, description="My custom agent")

    async def test_create_agent_file_duplicate_name(self, client):
        """Returns 400 when creating agent file with duplicate name."""
        payload = agent_file_create_payload(name="agent.py")
        await client.post("/api/agent-files", json=payload)

        response = await client.post("/api/agent-files", json=payload)
        assert_status_code(response, 400)
        assert "already exists" in response.json()["detail"]


class TestGetAgentFile:
    """Tests for GET /api/agent-files/{id} endpoint."""

    async def test_get_agent_file(self, client):
        """Returns agent file by ID."""
        create_response = await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="test.py")
        )
        agent_file_id = create_response.json()["id"]

        response = await client.get(f"/api/agent-files/{agent_file_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, id=agent_file_id, name="test.py")

    async def test_get_agent_file_not_found(self, client):
        """Returns 404 when agent file doesn't exist."""
        response = await client.get("/api/agent-files/nonexistent-id")
        assert_not_found(response)


class TestGetAgentFileByName:
    """Tests for GET /api/agent-files/by-name/{name} endpoint."""

    async def test_get_agent_file_by_name(self, client):
        """Returns agent file by name."""
        await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="my_agent.py", content="# Code")
        )

        response = await client.get("/api/agent-files/by-name/my_agent.py")
        assert_status_code(response, 200)
        assert_json_contains(response, name="my_agent.py", content="# Code")

    async def test_get_agent_file_by_name_not_found(self, client):
        """Returns 404 when agent file name doesn't exist."""
        response = await client.get("/api/agent-files/by-name/nonexistent.py")
        assert_not_found(response)


class TestUpdateAgentFile:
    """Tests for PATCH /api/agent-files/{id} endpoint."""

    async def test_update_agent_file_content(self, client):
        """Updates agent file content."""
        create_response = await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="agent.py", content="# Old")
        )
        agent_file_id = create_response.json()["id"]

        update_payload = agent_file_update_payload(content="# New")
        response = await client.patch(f"/api/agent-files/{agent_file_id}", json=update_payload)
        assert_updated_response(response)
        assert_json_contains(response, content="# New")

    async def test_update_agent_file_name(self, client):
        """Updates agent file name."""
        create_response = await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="old_name.py")
        )
        agent_file_id = create_response.json()["id"]

        update_payload = agent_file_update_payload(name="new_name.py")
        response = await client.patch(f"/api/agent-files/{agent_file_id}", json=update_payload)
        assert_updated_response(response)
        assert_json_contains(response, name="new_name.py")

    async def test_update_agent_file_name_conflict(self, client):
        """Returns 400 when updating name conflicts with existing."""
        await client.post("/api/agent-files", json=agent_file_create_payload(name="agent1.py"))
        create_response = await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="agent2.py")
        )
        agent_file_id = create_response.json()["id"]

        update_payload = agent_file_update_payload(name="agent1.py")
        response = await client.patch(f"/api/agent-files/{agent_file_id}", json=update_payload)
        assert_status_code(response, 400)
        assert "already exists" in response.json()["detail"]

    async def test_update_agent_file_not_found(self, client):
        """Returns 404 when agent file doesn't exist."""
        response = await client.patch(
            "/api/agent-files/nonexistent-id",
            json=agent_file_update_payload(content="# New")
        )
        assert_not_found(response)


class TestDeleteAgentFile:
    """Tests for DELETE /api/agent-files/{id} endpoint."""

    async def test_delete_agent_file(self, client):
        """Deletes an agent file."""
        create_response = await client.post(
            "/api/agent-files",
            json=agent_file_create_payload(name="to_delete.py")
        )
        agent_file_id = create_response.json()["id"]

        response = await client.delete(f"/api/agent-files/{agent_file_id}")
        assert_deleted_response(response)

        # Verify it's deleted
        get_response = await client.get(f"/api/agent-files/{agent_file_id}")
        assert_not_found(get_response)

    async def test_delete_agent_file_not_found(self, client):
        """Returns 404 when trying to delete non-existent agent file."""
        response = await client.delete("/api/agent-files/nonexistent-id")
        assert_not_found(response)


class TestBatchGetAgentFiles:
    """Tests for POST /api/agent-files/batch endpoint."""

    async def test_batch_get_agent_files(self, client):
        """Returns multiple agent files by their IDs."""
        # Create three agent files
        r1 = await client.post("/api/agent-files", json=agent_file_create_payload(name="agent1.py"))
        r2 = await client.post("/api/agent-files", json=agent_file_create_payload(name="agent2.py"))
        r3 = await client.post("/api/agent-files", json=agent_file_create_payload(name="agent3.py"))

        id1 = r1.json()["id"]
        id2 = r2.json()["id"]
        id3 = r3.json()["id"]

        # Request agents in specific order
        response = await client.post("/api/agent-files/batch", json=[id2, id1, id3])
        assert_status_code(response, 200)
        data = response.json()
        assert len(data) == 3

        # Verify order is maintained
        assert data[0]["id"] == id2
        assert data[1]["id"] == id1
        assert data[2]["id"] == id3

    async def test_batch_get_agent_files_empty(self, client):
        """Returns empty list when no IDs provided."""
        response = await client.post("/api/agent-files/batch", json=[])
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_batch_get_agent_files_missing_ids(self, client):
        """Returns only found agent files when some IDs don't exist."""
        r1 = await client.post("/api/agent-files", json=agent_file_create_payload(name="agent1.py"))
        id1 = r1.json()["id"]

        # Request with one valid and one invalid ID
        response = await client.post("/api/agent-files/batch", json=[id1, "nonexistent-id"])
        assert_status_code(response, 200)
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == id1
