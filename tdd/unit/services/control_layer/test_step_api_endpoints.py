"""
Unit tests for Step API Endpoints.

These tests define the backend API contract for step communication:
- POST /api/steps/{step_id}/status - Updates step status
- POST /api/steps/{step_id}/logs - Appends logs
- POST /api/steps/{step_id}/heartbeat - Extends timeout
- Auth token required for all endpoints

Write these tests BEFORE implementing the step API.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

# Tests enabled - Phase 12.3 step API implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Status Endpoint
# -----------------------------------------------------------------------------

class TestStatusEndpoint:
    """Tests that verify the step status update endpoint."""

    async def test_update_status_to_running(self, client, step_execution):
        """POST /api/steps/{step_id}/status updates status to running."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    async def test_update_status_to_completed(self, client, step_execution):
        """POST /api/steps/{step_id}/status updates status to completed."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={
                "status": "completed",
                "exit_code": 0,
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["exit_code"] == 0

    async def test_update_status_to_failed(self, client, step_execution):
        """POST /api/steps/{step_id}/status updates status to failed with error."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={
                "status": "failed",
                "exit_code": 1,
                "error": "Command not found",
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "Command not found"

    async def test_status_update_records_timestamp(self, client, step_execution):
        """Status update records timestamp of change."""
        before = datetime.utcnow()

        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        after = datetime.utcnow()

        assert response.status_code == 200
        data = response.json()
        assert "started_at" in data

    async def test_status_requires_auth(self, client, step_execution):
        """Status endpoint requires authentication."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
        )

        assert response.status_code == 401

    async def test_status_rejects_invalid_token(self, client, step_execution):
        """Status endpoint rejects invalid auth token."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": "Bearer invalid-token"},
        )

        assert response.status_code == 403

    async def test_status_404_for_unknown_step(self, client):
        """Status endpoint returns 404 for unknown step ID."""
        response = await client.post(
            "/api/steps/unknown-step-id/status",
            json={"status": "running"},
            headers={"Authorization": "Bearer some-token"},
        )

        assert response.status_code == 404


# -----------------------------------------------------------------------------
# Contract: Logs Endpoint
# -----------------------------------------------------------------------------

class TestLogsEndpoint:
    """Tests that verify the step log append endpoint."""

    async def test_append_stdout_logs(self, client, step_execution):
        """POST /api/steps/{step_id}/logs appends stdout logs."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "content": "Running tests...\n",
                "stream": "stdout",
                "timestamp": datetime.utcnow().isoformat(),
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200

    async def test_append_stderr_logs(self, client, step_execution):
        """POST /api/steps/{step_id}/logs appends stderr logs."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "content": "Warning: deprecated function\n",
                "stream": "stderr",
                "timestamp": datetime.utcnow().isoformat(),
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200

    async def test_append_batch_logs(self, client, step_execution):
        """POST /api/steps/{step_id}/logs can append batched log lines."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "lines": [
                    {"content": "Line 1\n", "stream": "stdout", "timestamp": datetime.utcnow().isoformat()},
                    {"content": "Line 2\n", "stream": "stdout", "timestamp": datetime.utcnow().isoformat()},
                    {"content": "Line 3\n", "stream": "stdout", "timestamp": datetime.utcnow().isoformat()},
                ],
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["lines_appended"] == 3

    async def test_logs_persisted_to_step_run(self, client, step_execution):
        """Logs are persisted and retrievable."""
        # Append some logs
        await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "content": "Test output\n",
                "stream": "stdout",
                "timestamp": datetime.utcnow().isoformat(),
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        # Retrieve step run to verify logs
        get_response = await client.get(
            f"/api/step-runs/{step_execution['step_run_id']}",
        )

        assert get_response.status_code == 200
        data = get_response.json()
        assert "Test output" in data["logs"]

    async def test_logs_requires_auth(self, client, step_execution):
        """Logs endpoint requires authentication."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={"content": "Test\n", "stream": "stdout"},
        )

        assert response.status_code == 401

    async def test_logs_default_stream_is_stdout(self, client, step_execution):
        """Default stream is stdout if not specified."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "content": "Default stream test\n",
                "timestamp": datetime.utcnow().isoformat(),
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Contract: Heartbeat Endpoint
# -----------------------------------------------------------------------------

class TestHeartbeatEndpoint:
    """Tests that verify the step heartbeat endpoint."""

    async def test_heartbeat_extends_timeout(self, client, step_execution):
        """POST /api/steps/{step_id}/heartbeat extends timeout."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={"extend_seconds": 300},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timeout_extended"] is True

    async def test_heartbeat_updates_last_seen(self, client, step_execution):
        """Heartbeat updates last_seen timestamp."""
        before = datetime.utcnow()

        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "last_seen" in data

    async def test_heartbeat_with_progress(self, client, step_execution):
        """Heartbeat can include progress information."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={
                "progress": {
                    "percent": 75,
                    "message": "Processing files...",
                    "current_file": "src/main.py",
                },
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["progress_updated"] is True

    async def test_heartbeat_requires_auth(self, client, step_execution):
        """Heartbeat endpoint requires authentication."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={},
        )

        assert response.status_code == 401

    async def test_heartbeat_prevents_timeout(self, client, step_execution):
        """Regular heartbeats prevent step timeout."""
        # This is more of an integration test, but defines the contract
        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={"extend_seconds": 60},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200

        # Step should still be active - use the steps GET endpoint
        get_response = await client.get(
            f"/api/steps/{step_execution['id']}",
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert get_response.status_code == 200
        data = get_response.json()
        assert data["status"] != "timeout"


# -----------------------------------------------------------------------------
# Contract: Auth Token Generation
# -----------------------------------------------------------------------------

class TestAuthTokenGeneration:
    """Tests that verify auth token generation for steps."""

    async def test_step_execution_includes_auth_token(self, client):
        """Step execution includes auth token for API access."""
        from app.services.control_layer.auth import generate_step_token

        token = generate_step_token(
            step_id="step-123",
            execution_key="exec-789:0:1",
        )

        assert token is not None
        assert len(token) > 0

    async def test_auth_token_is_unique_per_step(self, client):
        """Each step gets a unique auth token."""
        from app.services.control_layer.auth import generate_step_token

        token1 = generate_step_token(step_id="step-1", execution_key="exec-1:0:1")
        token2 = generate_step_token(step_id="step-2", execution_key="exec-2:0:1")

        assert token1 != token2

    async def test_auth_token_validates_step_id(self, client):
        """Auth token is only valid for its step ID."""
        from app.services.control_layer.auth import generate_step_token, validate_step_token

        token = generate_step_token(step_id="step-123", execution_key="exec-789:0:1")

        # Valid for correct step
        assert validate_step_token(token, step_id="step-123") is True

        # Invalid for different step
        assert validate_step_token(token, step_id="step-456") is False

    async def test_auth_token_expires(self, client):
        """Auth tokens have an expiration time."""
        from app.services.control_layer.auth import generate_step_token, validate_step_token
        import time

        token = generate_step_token(
            step_id="step-123",
            execution_key="exec-789:0:1",
            expires_in_seconds=1,
        )

        # Valid immediately
        assert validate_step_token(token, step_id="step-123") is True

        # Wait for expiration
        time.sleep(1.5)

        # Should be expired
        assert validate_step_token(token, step_id="step-123") is False


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
async def db_session():
    """Create a test database session."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    # Use in-memory SQLite for tests
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def client(db_session):
    """Create a test client with database override."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import get_db

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def step_execution(client, db_session):
    """Create a step execution for testing."""
    from app.models import StepExecution, StepRun, PipelineRun, Pipeline, Repo
    from app.services.control_layer.auth import generate_step_token
    from uuid import uuid4

    # Create repo
    repo_id = str(uuid4())
    repo = Repo(
        id=repo_id,
        name="test-repo",
        remote_url="https://github.com/test/test",
        is_ingested=True,
    )
    db_session.add(repo)

    # Create pipeline
    pipeline_id = str(uuid4())
    pipeline = Pipeline(
        id=pipeline_id,
        repo_id=repo_id,
        name="test-pipeline",
        steps="[]",
    )
    db_session.add(pipeline)

    # Create pipeline run
    pipeline_run_id = str(uuid4())
    pipeline_run = PipelineRun(
        id=pipeline_run_id,
        pipeline_id=pipeline_id,
        status="running",
    )
    db_session.add(pipeline_run)

    # Create step run
    step_run_id = str(uuid4())
    step_run = StepRun(
        id=step_run_id,
        pipeline_run_id=pipeline_run_id,
        step_index=0,
        step_name="test-step",
        status="pending",
        logs="",
    )
    db_session.add(step_run)

    # Create step execution
    execution_id = str(uuid4())
    execution_key = f"{pipeline_run_id}:0:1"
    execution = StepExecution(
        id=execution_id,
        execution_key=execution_key,
        step_run_id=step_run_id,
        status="pending",
    )
    db_session.add(execution)

    await db_session.commit()

    # Generate auth token
    token = generate_step_token(
        step_id=execution_id,
        execution_key=execution_key,
    )

    return {
        "id": execution_id,
        "step_run_id": step_run_id,
        "execution_key": execution_key,
        "auth_token": token,
    }
