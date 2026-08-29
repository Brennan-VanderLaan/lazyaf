"""
Unit tests for Step API Endpoints.

These tests define the backend API contract for step communication:
- POST /api/steps/{step_id}/status - Updates step status
- POST /api/steps/{step_id}/logs - Appends logs
- POST /api/steps/{step_id}/heartbeat - Extends timeout
- Auth token required for all endpoints

Phase 12.3 bridge contract (wave2-123-wiring design, R3) + the adversarial
review hardening:
- /logs is the SOLE writer of StepRun.logs + the log WS frames in control
  mode: ONE step_log_batch frame per POST (lines rstripped of the trailing
  newline - the frontend consumes step_log_batch alongside step_log), one
  string-join append per POST, one commit
- /status broadcasts step_update for `running` ONLY and NEVER writes
  StepRun.status (terminal StepRun state belongs to _finish_local_step; the
  old mirror's "completed" vocabulary vs RunStatus.PASSED divergence is
  dead)
- terminal StepExecutions accept NO further writes (409) - zombie tokens
  from finished steps cannot smear logs/telemetry
- heartbeat extensions never SHRINK timeout_at
- broadcast assertions run through the REAL ConnectionManager with a
  capturing transport - never an AsyncMock (R6)

Dropped plumbing (12.3 cleanup, noted per design): LogLine.stream/timestamp
were accepted-and-discarded wire fields - now deleted from the schema (the
runtime may still send them; pydantic ignores unknown keys). The heartbeat
`progress` field and GET /api/steps/{id} had no reader anywhere - gone.
"""
import json
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

    async def test_logs_tolerate_dropped_wire_fields(self, client, step_execution):
        """stream/timestamp are DELETED from the schema (accepted-and-
        discarded plumbing, 12.3 cleanup) - a runtime still sending them
        gets a normal 200 with the content transported."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "lines": [
                    {"content": "tolerant\n", "stream": "stdout",
                     "timestamp": datetime.utcnow().isoformat()},
                ],
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert response.json()["lines_appended"] == 1


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

    async def test_heartbeat_ignores_dropped_progress_field(
        self, client, db_session, step_execution
    ):
        """`progress` was write-only plumbing with no reader anywhere - it is
        deleted (12.3 cleanup). A runtime still sending it gets a normal 200
        (pydantic ignores unknown keys) and nothing is stored."""
        from app.models import StepExecution

        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={
                "progress": {"percent": 75, "message": "Processing files..."},
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert "progress_updated" not in response.json()
        execution = await db_session.get(StepExecution, step_execution["id"])
        await db_session.refresh(execution)
        assert execution.progress is None

    async def test_heartbeat_requires_auth(self, client, step_execution):
        """Heartbeat endpoint requires authentication."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={},
        )

        assert response.status_code == 401

    async def test_heartbeat_prevents_timeout(self, client, db_session, step_execution):
        """Regular heartbeats extend timeout_at into the future."""
        from datetime import datetime

        from app.models import StepExecution

        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={"extend_seconds": 60},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert response.json()["timeout_extended"] is True
        execution = await db_session.get(StepExecution, step_execution["id"])
        await db_session.refresh(execution)
        assert execution.timeout_at is not None
        assert execution.timeout_at > datetime.utcnow()

    async def test_heartbeat_never_shrinks_timeout(
        self, client, db_session, step_execution
    ):
        """timeout_at = max(current, now + extend_seconds): a short/late
        heartbeat must never pull an already-earned deadline closer."""
        from app.models import StepExecution

        far_future = datetime.utcnow() + timedelta(hours=2)
        execution = await db_session.get(StepExecution, step_execution["id"])
        execution.timeout_at = far_future
        await db_session.commit()

        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={"extend_seconds": 1},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert response.json()["timeout_extended"] is False
        await db_session.refresh(execution)
        assert execution.timeout_at == far_future


# -----------------------------------------------------------------------------
# Contract: the 12.3 bridge - WS frames + StepRun ownership (R3/R6)
# -----------------------------------------------------------------------------

class TestLogsBroadcastBridge:
    """/logs bridges to step_log_batch frames: ONE frame per POST (12.3
    hardening - never a frame per line), addressed like the step_log frames
    the frontend already consumes."""

    async def test_batch_logs_broadcast_one_step_log_batch_frame(
        self, client, step_execution, ws_socket
    ):
        """The whole POSTed batch goes out as ONE step_log_batch frame,
        lines newline-rstripped, with the StepRun's pipeline_run_id/
        step_index - and no per-line step_log frames at all."""
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "lines": [
                    {"content": "bridge line 1\n"},
                    {"content": "bridge line 2\n"},
                ],
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert ws_socket.of_type("step_log") == []
        frames = ws_socket.of_type("step_log_batch")
        assert frames == [
            {
                "pipeline_run_id": step_execution["pipeline_run_id"],
                "step_index": 0,
                "lines": ["bridge line 1", "bridge line 2"],
            }
        ]

    async def test_single_content_broadcasts_one_batch_frame(
        self, client, step_execution, ws_socket
    ):
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={"content": "solo line\n"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        frames = ws_socket.of_type("step_log_batch")
        assert [f["lines"] for f in frames] == [["solo line"]]

    async def test_empty_post_broadcasts_nothing(
        self, client, step_execution, ws_socket
    ):
        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        assert response.json()["lines_appended"] == 0
        assert ws_socket.of_type("step_log_batch") == []

    async def test_logs_append_verbatim_no_newline_added(
        self, client, db_session, step_execution
    ):
        """StepRun.logs concatenates the posted content VERBATIM - the wire
        contract is that the control runtime sends lines WITH trailing
        newlines; the router must not add its own."""
        from app.models import StepRun

        await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={
                "lines": [
                    {"content": "a\n", "stream": "stdout"},
                    {"content": "b\n", "stream": "stdout"},
                ],
            },
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        step_run = await db_session.get(StepRun, step_execution["step_run_id"])
        await db_session.refresh(step_run)
        assert step_run.logs == "a\nb\n"


class TestStatusBridge:
    """/status owns StepExecution telemetry + the `running` step_update
    frame - and NOTHING of StepRun's terminal state."""

    async def test_running_broadcasts_step_update_frame(
        self, client, step_execution, ws_socket
    ):
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        assert response.status_code == 200
        frames = ws_socket.of_type("step_update")
        assert frames == [
            {
                "pipeline_run_id": step_execution["pipeline_run_id"],
                "step_index": 0,
                "status": "running",
            }
        ]

    async def test_running_sets_step_run_started_at_only(
        self, client, db_session, step_execution
    ):
        from app.models import StepRun

        await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )

        step_run = await db_session.get(StepRun, step_execution["step_run_id"])
        await db_session.refresh(step_run)
        assert step_run.started_at is not None
        # status is NOT mirrored - _finish_local_step owns it
        assert step_run.status == "pending"

    async def test_terminal_status_never_touches_step_run(
        self, client, db_session, step_execution, ws_socket
    ):
        """Terminal status updates StepExecution ONLY: StepRun keeps its
        status/completed_at/error (the executor's result event drives them
        through _finish_local_step in RunStatus vocabulary), and no
        step_update frame goes out for terminal statuses."""
        from app.models import StepExecution, StepRun

        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "failed", "exit_code": 1, "error": "boom"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )
        assert response.status_code == 200

        step_run = await db_session.get(StepRun, step_execution["step_run_id"])
        await db_session.refresh(step_run)
        assert step_run.status == "pending"  # untouched
        assert step_run.completed_at is None
        assert step_run.error is None

        execution = await db_session.get(StepExecution, step_execution["id"])
        await db_session.refresh(execution)
        assert execution.status == "failed"
        assert execution.error == "boom"
        assert execution.completed_at is not None

        assert ws_socket.of_type("step_update") == []


# -----------------------------------------------------------------------------
# Contract: terminal StepExecutions reject writes (zombie-token hardening)
# -----------------------------------------------------------------------------

class TestTerminalRejection:
    """Once a StepExecution is terminal - runtime-reported or reconciled by
    _finish_local_step - status/logs/heartbeat all answer 409. A leaked
    token from a finished step can no longer smear later attempts."""

    async def _make_terminal(self, client, step_execution, status="completed"):
        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": status, "exit_code": 0},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )
        assert response.status_code == 200

    async def test_status_write_rejected_after_terminal(
        self, client, step_execution
    ):
        await self._make_terminal(client, step_execution)

        response = await client.post(
            f"/api/steps/{step_execution['id']}/status",
            json={"status": "running"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )
        assert response.status_code == 409
        assert "terminal" in response.json()["detail"]

    async def test_logs_write_rejected_after_terminal(
        self, client, db_session, step_execution, ws_socket
    ):
        from app.models import StepRun

        await self._make_terminal(client, step_execution, status="failed")

        response = await client.post(
            f"/api/steps/{step_execution['id']}/logs",
            json={"content": "zombie line\n"},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )
        assert response.status_code == 409

        step_run = await db_session.get(StepRun, step_execution["step_run_id"])
        await db_session.refresh(step_run)
        assert "zombie line" not in (step_run.logs or "")
        assert ws_socket.of_type("step_log_batch") == []

    async def test_heartbeat_rejected_after_terminal(self, client, step_execution):
        await self._make_terminal(client, step_execution, status="timeout")

        response = await client.post(
            f"/api/steps/{step_execution['id']}/heartbeat",
            json={"extend_seconds": 600},
            headers={"Authorization": f"Bearer {step_execution['auth_token']}"},
        )
        assert response.status_code == 409


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


class CapturingSocket:
    """Capturing transport attached to the REAL ConnectionManager (R6)."""

    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, text: str):
        self.messages.append(json.loads(text))

    def of_type(self, message_type: str) -> list[dict]:
        return [m["payload"] for m in self.messages if m["type"] == message_type]


@pytest.fixture
async def ws_socket():
    """Attach a capturing transport to the real WS manager singleton."""
    from app.services.websocket import manager

    socket = CapturingSocket()
    manager.active_connections.append(socket)
    yield socket
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)


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
        "pipeline_run_id": pipeline_run_id,
        "execution_key": execution_key,
        "auth_token": token,
    }
