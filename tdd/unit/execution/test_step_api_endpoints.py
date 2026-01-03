"""
TDD Tests for Step API Endpoints (Phase 12.3).

These tests DEFINE the backend API contract for control layer communication.
The control layer inside containers uses these endpoints to:
- Report status changes (running, completed, failed)
- Stream logs
- Send heartbeats

Write these tests FIRST, then implement to make them pass.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.assertions import (
    assert_status_code,
    assert_json_contains,
    assert_error_response,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def step_execution_with_token(db_session, client):
    """Create a step execution and generate a valid token for it.

    Returns:
        tuple: (step_id, token) where step_id is the execution key
    """
    # Import here to avoid circular imports during collection
    from app.services.execution.step_token import generate_step_token

    step_id = "test-run-123:0:1"  # ExecutionKey format: run_id:step_index:attempt
    token = generate_step_token(step_id)

    return step_id, token


@pytest_asyncio.fixture
async def another_step_with_token(db_session, client):
    """Create a different step execution with its own token."""
    from app.services.execution.step_token import generate_step_token

    step_id = "test-run-456:0:1"
    token = generate_step_token(step_id)

    return step_id, token


# -----------------------------------------------------------------------------
# Status Endpoint Tests
# -----------------------------------------------------------------------------

class TestStatusEndpoint:
    """Tests for POST /api/steps/{step_id}/status endpoint."""

    async def test_post_status_running(self, client, step_execution_with_token):
        """Reports 'running' status successfully."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        assert_json_contains(response, status="ok")

    async def test_post_status_completed_with_exit_code(self, client, step_execution_with_token):
        """Reports 'completed' status with exit code 0."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={"status": "completed", "exit_code": 0},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        assert_json_contains(response, status="ok")

    async def test_post_status_failed_with_exit_code_and_error(self, client, step_execution_with_token):
        """Reports 'failed' status with non-zero exit code and error message."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={
                "status": "failed",
                "exit_code": 1,
                "error": "Command failed with exit code 1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        assert_json_contains(response, status="ok")

    async def test_post_status_requires_auth(self, client, step_execution_with_token):
        """Status endpoint requires Authorization header."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={"status": "running"},
            # No Authorization header
        )

        # Should return 401 or 403 for missing auth
        assert response.status_code in (401, 403)

    async def test_post_status_rejects_invalid_token(self, client, step_execution_with_token):
        """Status endpoint rejects invalid token."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={"status": "running"},
            headers={"Authorization": "Bearer invalid-token-abc123"},
        )

        assert response.status_code == 401

    async def test_post_status_token_must_match_step(
        self, client, step_execution_with_token, another_step_with_token
    ):
        """Token for step A cannot be used for step B."""
        step_a_id, _ = step_execution_with_token
        _, token_b = another_step_with_token

        # Try to use token_b for step_a
        response = await client.post(
            f"/api/steps/{step_a_id}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {token_b}"},
        )

        assert response.status_code == 403

    async def test_post_status_validates_status_value(self, client, step_execution_with_token):
        """Status must be a valid value."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/status",
            json={"status": "invalid_status"},
            headers={"Authorization": f"Bearer {token}"},
        )

        # Should return 422 for validation error
        assert response.status_code == 422


# -----------------------------------------------------------------------------
# Logs Endpoint Tests
# -----------------------------------------------------------------------------

class TestLogsEndpoint:
    """Tests for POST /api/steps/{step_id}/logs endpoint."""

    async def test_post_logs_single_line(self, client, step_execution_with_token):
        """Appends a single log line."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/logs",
            json={"lines": ["Hello, world!"]},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "ok"
        assert "total_lines" in result

    async def test_post_logs_multiple_lines(self, client, step_execution_with_token):
        """Appends multiple log lines in a batch."""
        step_id, token = step_execution_with_token

        lines = [
            "Step 1: Installing dependencies...",
            "Step 2: Running tests...",
            "Step 3: Building artifacts...",
        ]

        response = await client.post(
            f"/api/steps/{step_id}/logs",
            json={"lines": lines},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        result = response.json()
        assert result["total_lines"] >= 3

    async def test_post_logs_empty_batch(self, client, step_execution_with_token):
        """Empty log batch is accepted (no-op)."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/logs",
            json={"lines": []},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)

    async def test_post_logs_requires_auth(self, client, step_execution_with_token):
        """Logs endpoint requires Authorization header."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/logs",
            json={"lines": ["test"]},
            # No Authorization header
        )

        assert response.status_code in (401, 403)

    async def test_post_logs_rejects_invalid_token(self, client, step_execution_with_token):
        """Logs endpoint rejects invalid token."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/logs",
            json={"lines": ["test"]},
            headers={"Authorization": "Bearer invalid-token"},
        )

        assert response.status_code == 401


# -----------------------------------------------------------------------------
# Heartbeat Endpoint Tests
# -----------------------------------------------------------------------------

class TestHeartbeatEndpoint:
    """Tests for POST /api/steps/{step_id}/heartbeat endpoint."""

    async def test_post_heartbeat_success(self, client, step_execution_with_token):
        """Heartbeat updates last_heartbeat timestamp."""
        step_id, token = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert_status_code(response, 200)
        assert_json_contains(response, status="ok")

    async def test_post_heartbeat_requires_auth(self, client, step_execution_with_token):
        """Heartbeat endpoint requires Authorization header."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/heartbeat",
            json={},
            # No Authorization header
        )

        assert response.status_code in (401, 403)

    async def test_post_heartbeat_rejects_invalid_token(self, client, step_execution_with_token):
        """Heartbeat endpoint rejects invalid token."""
        step_id, _ = step_execution_with_token

        response = await client.post(
            f"/api/steps/{step_id}/heartbeat",
            json={},
            headers={"Authorization": "Bearer invalid-token"},
        )

        assert response.status_code == 401


# -----------------------------------------------------------------------------
# Token Service Tests
# -----------------------------------------------------------------------------

class TestStepTokenService:
    """Tests for the step token generation and validation service."""

    def test_generate_token_returns_string(self):
        """generate_step_token returns a non-empty string."""
        from app.services.execution.step_token import generate_step_token

        token = generate_step_token("test-step-id")

        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_unique_per_call(self):
        """Each call generates a unique token."""
        from app.services.execution.step_token import generate_step_token

        token1 = generate_step_token("step-1")
        token2 = generate_step_token("step-2")

        assert token1 != token2

    def test_token_can_be_validated(self):
        """Generated token can be validated successfully."""
        from app.services.execution.step_token import (
            generate_step_token,
            validate_step_token,
        )

        step_id = "test-step-123"
        token = generate_step_token(step_id)

        result = validate_step_token(token)

        assert result is not None
        assert result["step_id"] == step_id

    def test_invalid_token_fails_validation(self):
        """Invalid token returns None or raises exception."""
        from app.services.execution.step_token import validate_step_token

        result = validate_step_token("invalid-token-xyz")

        assert result is None

    def test_token_contains_step_id(self):
        """Token validation returns step_id."""
        from app.services.execution.step_token import (
            generate_step_token,
            validate_step_token,
        )

        step_id = "run-abc:0:1"
        token = generate_step_token(step_id)

        result = validate_step_token(token)

        assert result["step_id"] == step_id

    def test_revoke_token_invalidates_it(self):
        """Revoked token fails validation."""
        from app.services.execution.step_token import (
            generate_step_token,
            validate_step_token,
            revoke_step_token,
        )

        step_id = "test-step-to-revoke"
        token = generate_step_token(step_id)

        # Token should be valid before revocation
        assert validate_step_token(token) is not None

        # Revoke
        revoke_step_token(step_id)

        # Token should be invalid after revocation
        assert validate_step_token(token) is None
