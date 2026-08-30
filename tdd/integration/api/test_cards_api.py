"""
Integration tests for Cards API endpoints.

These tests verify the full request/response cycle for card management,
including status transitions and card lifecycle operations.
"""
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from app.models import Job, PipelineRun, StepRun, TestRef, TestRun

from shared.factories import repo_create_payload, repo_ingest_payload, card_create_payload, card_update_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for card tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="CardTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def ingested_repo(client, clean_git_repos):
    """Create an ingested repo for card lifecycle tests that require starting jobs."""
    response = await client.post(
        "/api/repos/ingest",
        json=repo_ingest_payload(name="IngestedCardTestRepo"),
    )
    return response.json()


class TestListCards:
    """Tests for GET /api/repos/{repo_id}/cards endpoint."""

    async def test_list_cards_empty(self, client, repo):
        """Returns empty list when repo has no cards."""
        response = await client.get(f"/api/repos/{repo['id']}/cards")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_cards_with_data(self, client, repo):
        """Returns all cards for a repo."""
        # Create cards
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Card 1"),
        )
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Card 2"),
        )

        response = await client.get(f"/api/repos/{repo['id']}/cards")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_cards_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/cards")
        assert_not_found(response, "Repo")

    async def test_list_cards_only_for_repo(self, client):
        """Returns only cards belonging to specified repo."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Repo2"))
        repo1_id = resp1.json()["id"]
        repo2_id = resp2.json()["id"]

        # Create cards in each repo
        await client.post(
            f"/api/repos/{repo1_id}/cards",
            json=card_create_payload(title="Repo1 Card"),
        )
        await client.post(
            f"/api/repos/{repo2_id}/cards",
            json=card_create_payload(title="Repo2 Card"),
        )

        # Verify isolation
        response = await client.get(f"/api/repos/{repo1_id}/cards")
        cards = response.json()
        assert len(cards) == 1
        assert cards[0]["title"] == "Repo1 Card"


class TestCreateCard:
    """Tests for POST /api/repos/{repo_id}/cards endpoint."""

    async def test_create_card_minimal(self, client, repo):
        """Creates card with minimal required fields."""
        payload = card_create_payload(title="Minimal Card")

        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=payload,
        )
        result = assert_created_response(response, {"title": "Minimal Card"})
        assert result["repo_id"] == repo["id"]
        assert result["status"] == "todo"

    async def test_create_card_full(self, client, repo):
        """Creates card with all fields."""
        payload = {
            "title": "Full Card",
            "description": "Detailed description of the feature",
        }

        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=payload,
        )
        result = response.json()
        assert result["title"] == "Full Card"
        assert result["description"] == "Detailed description of the feature"

    async def test_create_card_defaults(self, client, repo):
        """Creates card with expected default values."""
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        result = response.json()
        assert result["status"] == "todo"
        assert result["branch_name"] is None
        assert result["pr_url"] is None
        assert result["job_id"] is None

    async def test_create_card_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/api/repos/nonexistent-repo/cards",
            json=card_create_payload(),
        )
        assert_not_found(response, "Repo")

    async def test_create_card_missing_title_fails(self, client, repo):
        """Fails without required title field."""
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={"description": "No title"},
        )
        assert_status_code(response, 422)


class TestGetCard:
    """Tests for GET /api/cards/{card_id} endpoint."""

    async def test_get_card_exists(self, client, repo):
        """Returns card when it exists."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="GetTest Card"),
        )
        card_id = create_response.json()["id"]

        response = await client.get(f"/api/cards/{card_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": card_id, "title": "GetTest Card"})

    async def test_get_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.get("/api/cards/nonexistent-card-id")
        assert_not_found(response, "Card")

    async def test_get_card_returns_all_fields(self, client, repo):
        """Returns card with complete field set."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={"title": "Complete Card", "description": "Full description"},
        )
        card_id = create_response.json()["id"]

        response = await client.get(f"/api/cards/{card_id}")
        result = response.json()
        assert "id" in result
        assert "repo_id" in result
        assert "title" in result
        assert "description" in result
        assert "status" in result
        assert "created_at" in result
        assert "updated_at" in result


class TestUpdateCard:
    """Tests for PATCH /api/cards/{card_id} endpoint."""

    async def test_update_card_title(self, client, repo):
        """Updates card title only."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Original Title"),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"title": "Updated Title"},
        )
        result = assert_updated_response(response, {"title": "Updated Title"})
        assert result["id"] == card_id

    async def test_update_card_status(self, client, repo):
        """Updates card status."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_progress"},
        )
        assert response.json()["status"] == "in_progress"

    async def test_update_card_description(self, client, repo):
        """Updates card description."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(description="Original"),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"description": "Updated description"},
        )
        assert response.json()["description"] == "Updated description"

    async def test_update_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.patch(
            "/api/cards/nonexistent-id",
            json={"title": "New Title"},
        )
        assert_not_found(response, "Card")


class TestDeleteCard:
    """Tests for DELETE /api/cards/{card_id} endpoint."""

    async def test_delete_card_exists(self, client, repo):
        """Deletes card when it exists."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        card_id = create_response.json()["id"]

        response = await client.delete(f"/api/cards/{card_id}")
        assert_deleted_response(response)

        # Verify card is gone
        get_response = await client.get(f"/api/cards/{card_id}")
        assert_not_found(get_response, "Card")

    async def test_delete_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.delete("/api/cards/nonexistent-id")
        assert_not_found(response, "Card")


class TestCardLifecycleActions:
    """Tests for card lifecycle endpoints: start, approve, reject."""

    async def test_start_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/start moves card to in_progress."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Start"),
        )
        card_id = create_response.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/start")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"
        assert result["job_id"] is not None
        assert result["branch_name"] is not None

    async def test_start_card_not_found(self, client):
        """Returns 404 when starting non-existent card."""
        response = await client.post("/api/cards/nonexistent/start")
        assert_not_found(response, "Card")

    async def test_start_card_already_started(self, client, ingested_repo, clean_job_queue):
        """Returns 400 when starting card that is not in todo status."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Already Started"),
        )
        card_id = create_response.json()["id"]

        # Start once
        await client.post(f"/api/cards/{card_id}/start")

        # Try to start again
        response = await client.post(f"/api/cards/{card_id}/start")
        assert_status_code(response, 400)
        assert "todo" in response.json()["detail"].lower()

    async def test_approve_card(self, client, repo):
        """POST /api/cards/{id}/approve moves card to done and returns merge result."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Feature to Approve"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review first
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        response = await client.post(
            f"/api/cards/{card_id}/approve",
            json={"target_branch": None},
        )
        assert_status_code(response, 200)
        result = response.json()
        # Response includes card and merge_result
        assert result["card"]["status"] == "done"
        # merge_result may be None if repo is not ingested
        assert "merge_result" in result

    async def test_approve_card_not_found(self, client):
        """Returns 404 when approving non-existent card."""
        response = await client.post("/api/cards/nonexistent/approve")
        assert_not_found(response, "Card")

    async def test_reject_card(self, client, repo):
        """POST /api/cards/{id}/reject resets card to todo."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Feature to Reject"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review with branch and PR
        await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_review"},
        )

        response = await client.post(f"/api/cards/{card_id}/reject")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "todo"
        assert result["branch_name"] is None
        assert result["pr_url"] is None

    async def test_reject_card_not_found(self, client):
        """Returns 404 when rejecting non-existent card."""
        response = await client.post("/api/cards/nonexistent/reject")
        assert_not_found(response, "Card")

    async def test_retry_failed_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/retry retries a failed card."""
        # Create card and move to failed status
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Retry"),
        )
        card_id = create_response.json()["id"]

        # Set card to failed status
        await client.patch(f"/api/cards/{card_id}", json={"status": "failed"})

        # Retry the card
        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"
        assert result["job_id"] is not None
        assert result["branch_name"] is not None

    async def test_retry_in_review_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/retry can retry a card in review."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Re-Review"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review status
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        # Retry the card
        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"

    async def test_retry_todo_card_fails(self, client, ingested_repo):
        """Cannot retry a card in todo status."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Todo Card"),
        )
        card_id = create_response.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 400)
        assert "failed" in response.json()["detail"].lower() or "in_review" in response.json()["detail"].lower()

    async def test_retry_card_not_found(self, client):
        """Returns 404 when retrying non-existent card."""
        response = await client.post("/api/cards/nonexistent/retry")
        assert_not_found(response, "Card")


class TestScriptDockerCardsRejected:
    """12.4 fallout: script/docker cards have no execution path.

    Phase 12.4 deleted script/docker execution from every runner entrypoint,
    and cards run ONLY on the runner queue (LocalExecutor is driven per
    PipelineRun/StepRun, which a card does not have). Starting one used to
    enqueue a job that a runner picked up and instantly rejected - the card
    flipped in_progress and then failed with a message about a routing bug.

    The contract now: reject at the API with a 400 that names the reason, and
    leave the card exactly where it was.
    """

    @pytest.mark.parametrize(
        "step_type,step_config",
        [
            ("script", {"command": "pytest -q"}),
            ("docker", {"image": "python:3.12", "command": "pytest -q"}),
        ],
    )
    async def test_start_rejects_script_and_docker_cards(
        self, client, ingested_repo, clean_job_queue, step_type, step_config
    ):
        payload = card_create_payload(title=f"A {step_type} card")
        payload["step_type"] = step_type
        payload["step_config"] = step_config
        card_id = (
            await client.post(
                f"/api/repos/{ingested_repo['id']}/cards", json=payload
            )
        ).json()["id"]

        response = await client.post(f"/api/cards/{card_id}/start")

        assert_status_code(response, 400)
        detail = response.json()["detail"]
        assert step_type in detail
        assert "12.4" in detail
        # Points at the supported alternative rather than just saying "no".
        assert "pipeline" in detail.lower()

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_rejected_card_stays_in_todo(
        self, client, ingested_repo, clean_job_queue, step_type
    ):
        """No silent in_progress -> failed loop: the card never moves."""
        payload = card_create_payload(title=f"Untouched {step_type} card")
        payload["step_type"] = step_type
        payload["step_config"] = {"command": "echo hi", "image": "alpine:3"}
        card_id = (
            await client.post(
                f"/api/repos/{ingested_repo['id']}/cards", json=payload
            )
        ).json()["id"]

        await client.post(f"/api/cards/{card_id}/start")

        card = (await client.get(f"/api/cards/{card_id}")).json()
        assert card["status"] == "todo"
        assert card["job_id"] is None
        assert card["branch_name"] is None

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_retry_rejects_script_and_docker_cards(
        self, client, ingested_repo, clean_job_queue, step_type
    ):
        """Retry closes the same loop - a failed script card cannot be
        re-enqueued into the same rejection."""
        payload = card_create_payload(title=f"Failed {step_type} card")
        payload["step_type"] = step_type
        payload["step_config"] = {"command": "echo hi", "image": "alpine:3"}
        card_id = (
            await client.post(
                f"/api/repos/{ingested_repo['id']}/cards", json=payload
            )
        ).json()["id"]
        await client.patch(f"/api/cards/{card_id}", json={"status": "failed"})

        response = await client.post(f"/api/cards/{card_id}/retry")

        assert_status_code(response, 400)
        assert step_type in response.json()["detail"]

        card = (await client.get(f"/api/cards/{card_id}")).json()
        assert card["status"] == "failed"

    async def test_agent_cards_still_start(
        self, client, ingested_repo, clean_job_queue
    ):
        """The guard is narrow: agent cards are unaffected."""
        payload = card_create_payload(title="An agent card")
        payload["step_type"] = "agent"
        card_id = (
            await client.post(
                f"/api/repos/{ingested_repo['id']}/cards", json=payload
            )
        ).json()["id"]

        response = await client.post(f"/api/cards/{card_id}/start")

        assert_status_code(response, 200)
        assert response.json()["status"] == "in_progress"

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_script_docker_cards_are_still_creatable(
        self, client, repo, step_type
    ):
        """Deprecated, not removed: existing cards keep round-tripping so a
        user can read them and convert them - only STARTING is refused."""
        payload = card_create_payload(title=f"Legacy {step_type} card")
        payload["step_type"] = step_type
        payload["step_config"] = {"command": "make test", "image": "alpine:3"}

        response = await client.post(
            f"/api/repos/{repo['id']}/cards", json=payload
        )

        assert_status_code(response, 201)
        assert response.json()["step_type"] == step_type


# =============================================================================
# Phase 12.5: the ad-hoc card-work RUN behind a card
#
# Since 12.5 starting a card creates an ephemeral single-agent-step
# PipelineRun and the Job row is its twin (app/services/agent_run.py). Every
# test below is about the RUN - the thing that holds a container and spends
# provider budget - rather than about the Job row that reports on it, because
# each of these defects shipped past a test that only checked the report.
# =============================================================================


class ParkedStubExecutor:
    """Control-mode LocalExecutor stand-in that parks mid-step.

    Agent steps REQUIRE a control-layer image (``_prepare_control_mode``
    refuses anything else), so unlike the T1 stub in tdd/conftest.py this one
    declares the capability - which means the backend really mints a
    StepExecution row and a step JWT, and the test gets to play the container
    against it (``act_as_container`` below). It then parks until the test
    releases it, which is what makes cancellation observable.
    """

    def __init__(self):
        self.calls = []
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_keys = []
        # Real containers do not always die the instant they are killed.
        # Setting this False keeps the step task parked after the cancel, so
        # assertions about what CANCEL wrote are not racing a straggler that
        # is busy overwriting it.
        self.release_on_cancel = True

    async def image_supports_control_layer(self, image):
        return True

    async def find_missing_images(self, images):
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "preparing"}
        yield {"type": "status", "status": "running"}
        self.dispatched.set()
        await self.release.wait()
        yield {
            "type": "result",
            "status": "completed",
            "exit_code": 0,
            "error": None,
            "log_tail": [],
        }

    async def cancel_step(self, execution_key):
        self.cancelled_keys.append(execution_key)
        if self.release_on_cancel:
            self.release.set()
        return True

    async def cancel_all(self):
        self.release.set()

    def reset(self):
        self.calls.clear()

    @property
    def context(self):
        assert self.calls, "the step was never dispatched to the executor"
        return self.calls[-1][1]


class MissingImageStubExecutor(ParkedStubExecutor):
    """Preflight fails: start_pipeline completes the run before returning."""

    async def find_missing_images(self, images):
        return sorted(images)

    async def execute_step(self, step_config, execution_context):
        raise AssertionError("preflight failed - nothing should dispatch")
        yield  # pragma: no cover - keeps this an async generator


def _install_local_executor(stub):
    from app.services.pipeline_executor import pipeline_executor

    previous = pipeline_executor._local_executor
    pipeline_executor._local_executor = stub
    try:
        yield stub
    finally:
        stub.release.set()
        pipeline_executor._local_executor = previous


@pytest.fixture
def parked_executor():
    yield from _install_local_executor(ParkedStubExecutor())


@pytest.fixture
def missing_image_executor():
    yield from _install_local_executor(MissingImageStubExecutor())


@pytest.fixture
def trigger_spy(monkeypatch):
    """Record every card_complete gate call without firing real pipelines."""
    from app.services.trigger_service import trigger_service

    calls = []

    async def spy(db, card, old_status, new_status):
        calls.append((card.id, old_status, new_status))

    monkeypatch.setattr(trigger_service, "on_card_status_change", spy)
    return calls


async def settle(cycles=40):
    """Let the dispatched step task run to wherever it is going."""
    for _ in range(cycles):
        await asyncio.sleep(0.01)


AGENT_LOG_LINE = "[agent] rewriting the module\n"


async def _fresh(db_session):
    """Drop this session's snapshot so the next read sees other sessions.

    The test client and the step task hold different sessions on the same
    engine; the client's is ``expire_on_commit=False`` and, once it has read
    anything, sits inside a transaction whose SQLite snapshot predates the
    step task's commits. Rolling back ends that transaction and expires the
    identity map, so the next GET is a real read of committed state rather
    than a replay of what this session saw before the run finished.
    """
    await db_session.rollback()
    db_session.expire_all()


async def read_card(client, db_session, card_id):
    """GET a card, seeing what the STEP TASK committed."""
    await _fresh(db_session)
    response = await client.get(f"/api/cards/{card_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def read_job(client, db_session, job_id):
    """GET a job, seeing what the step task committed (see read_card)."""
    await _fresh(db_session)
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def await_card_status(client, db_session, card_id, expected, timeout=10.0):
    """Poll until the card reaches `expected`, then return it.

    The run completes on a background step task, so the card's terminal
    status arrives whenever that task gets there. Polling keeps the
    assertion about the OUTCOME instead of about how many event-loop turns
    the settle helper happened to buy.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    card = await read_card(client, db_session, card_id)
    while card["status"] != expected and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
        card = await read_card(client, db_session, card_id)
    assert card["status"] == expected, (
        f"card stayed {card['status']!r}; expected {expected!r} within {timeout}s"
    )
    return card


async def await_card_gate(trigger_spy, expected_status, timeout=5.0):
    """Wait for the card_complete gate to fire for `expected_status`.

    The card's status is COMMITTED before the gate is awaited, so a test that
    polls the status and then reads the spy is racing the last two lines of
    the completion handler.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while (
        not any(call[2] == expected_status for call in trigger_spy)
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.02)
    assert [call[2] for call in trigger_spy] == [expected_status], (
        f"card_complete gate calls were {trigger_spy!r}; expected exactly one "
        f"for {expected_status!r}"
    )


async def act_as_container(client, executor, log_line=AGENT_LOG_LINE):
    """Do what the control runtime in the container would do.

    Reports "running" and streams one log line, both through the REAL
    /api/steps routes with the REAL step JWT the backend minted at dispatch.
    Without the status report, the executor's finish-time reconciliation
    fails the step for never having reported - correctly, since an image with
    no working /control runtime must never read green.
    """
    context = executor.context
    headers = {"Authorization": f"Bearer {context['step_auth_token']}"}
    step_id = context["step_execution_id"]

    response = await client.post(
        f"/api/steps/{step_id}/status", headers=headers, json={"status": "running"}
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"/api/steps/{step_id}/logs",
        headers=headers,
        json={"lines": [{"content": log_line}]},
    )
    assert response.status_code == 200, response.text
    return step_id


async def await_dispatch(executor, timeout=10.0):
    """Wait for the step to reach the executor, loudly.

    A bare ``Event.wait()`` on a step that never dispatches hangs the whole
    suite with no message; this fails the one test that broke.
    """
    try:
        await asyncio.wait_for(executor.dispatched.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"the card's agent step never reached the executor within "
            f"{timeout}s (calls={executor.calls})"
        )


async def start_agent_card(client, repo_id, title="Agent work"):
    response = await client.post(
        f"/api/repos/{repo_id}/cards",
        json={
            "title": title,
            "description": "Do the thing in the title.",
            "runner_type": "mock",
            "step_type": "agent",
        },
    )
    assert response.status_code == 201, response.text
    card = response.json()
    started = await client.post(f"/api/cards/{card['id']}/start")
    assert started.status_code == 200, started.text
    return started.json()


async def adhoc_run_for(db_session, card_id):
    result = await db_session.execute(
        select(PipelineRun).where(PipelineRun.trigger_ref == card_id)
    )
    return result.scalars().one()


async def step_run_for(db_session, run_id):
    result = await db_session.execute(
        select(StepRun).where(StepRun.pipeline_run_id == run_id)
    )
    return result.scalars().first()


async def seed_test_results(
    db_session, repo_id, run_id, step_run_id, *, passed=(), failed=(), skipped=()
):
    """Write the TestRun rows the 12.2.6 tie-back would have written.

    This is the persisted evidence the card test gate reads - whether it was
    produced by the agent step itself or by a post-agent verification step of
    the same run, the rows look identical.
    """
    for status, test_ids in (
        ("passed", passed),
        ("failed", failed),
        ("skipped", skipped),
    ):
        for test_id in test_ids:
            ref = TestRef(id=str(uuid4()), lazyaf_test_id=test_id, repo_id=repo_id)
            db_session.add(ref)
            db_session.add(
                TestRun(
                    id=str(uuid4()),
                    test_ref_id=ref.id,
                    pipeline_run_id=run_id,
                    step_run_id=step_run_id,
                    commit_sha="",
                    status=status,
                )
            )
    await db_session.commit()


class TestCancelJobCancelsTheRun:
    """POST /api/jobs/{id}/cancel used to be a TODO with a status write.

    It flipped the Job row to failed and left the agent container running - a
    cancel that cancels nothing, with the UI reporting it stopped. Nobody
    watches a job that says it is over, so the container ran on to its
    1800-second deadline spending real provider budget.
    """

    async def test_cancel_reaches_the_executor_and_cancels_the_run(
        self, client, ingested_repo, db_session, parked_executor
    ):
        # Keep the "container" alive past the kill so these assertions are
        # about what CANCEL wrote, not about whether the straggler step task
        # got there first.
        parked_executor.release_on_cancel = False

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        run = await adhoc_run_for(db_session, card["id"])

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 200, response.text

        assert parked_executor.cancelled_keys, (
            "cancelling a card job never reached the executor - the agent "
            "container is still running"
        )
        await db_session.refresh(run)
        assert run.status == "cancelled"

        step_run = await step_run_for(db_session, run.id)
        await db_session.refresh(step_run)
        assert step_run.status == "cancelled"

    async def test_cancel_lands_the_card_failed_with_a_terminal_job(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
        assert response.json()["error"] == "Cancelled by user"

        card_after = await client.get(f"/api/cards/{card['id']}")
        assert card_after.json()["status"] == "failed", (
            "a cancelled card must not sit in in_progress forever - nothing "
            "is left to land it, and retry only accepts failed/in_review"
        )

    async def test_a_cancelled_run_never_walks_the_card_into_in_review(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """The full sequence: cancel, then let the step task finish anyway."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        await client.post(f"/api/jobs/{card['job_id']}/cancel")
        parked_executor.release.set()  # the container "exits 0" after the kill
        await settle()

        card_after = await read_card(client, db_session, card["id"])
        assert card_after["status"] == "failed"

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed"
        assert job_after["error"] == "Cancelled by user"

        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a cancelled run fired the card_complete gate - that is the "
            "self-triggering loop with an extra step in front of it"
        )

    async def test_on_run_complete_refuses_to_succeed_a_cancelled_run(
        self, client, ingested_repo, db_session, trigger_spy
    ):
        """Defence in depth, on rows alone - no executor, no race.

        The run column is asserted directly here because in the live path a
        straggler step task can OVERWRITE ``cancelled`` with its own verdict
        before this hook runs (its session's snapshot predates the cancel
        commit). That is why the card is guarded by its own state as well;
        this test pins the other half.
        """
        from app.models import Card, Pipeline, RunStatus
        from app.services import agent_run

        card_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json={"title": "Cancelled work", "runner_type": "mock",
                  "step_type": "agent"},
        )
        card_id = card_response.json()["id"]
        card = await db_session.get(Card, card_id)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=ingested_repo["id"],
            name=agent_run.adhoc_pipeline_name("card_work", card_id),
            steps="[]",
            triggers="[]",
        )
        job = Job(id=str(uuid4()), card_id=card_id, status="running")
        db_session.add_all([pipeline, job])
        card.status = "in_progress"
        card.job_id = job.id
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.CANCELLED.value,
            trigger_type=agent_run.TRIGGER_CARD_WORK,
            trigger_ref=card_id,
        )
        db_session.add(run)
        await db_session.commit()

        await agent_run.on_run_complete(db_session, run, success=True)

        await db_session.refresh(card)
        assert card.status == "failed", (
            "a CANCELLED run reported success and the card was promoted"
        )
        await db_session.refresh(job)
        assert job.status == "failed"
        assert trigger_spy == []

    async def test_a_pipeline_step_job_does_not_take_its_pipeline_down(
        self, client, ingested_repo, db_session, monkeypatch
    ):
        """Only an AD-HOC card-work run is owned by one job.

        A job that is a step of a real multi-step pipeline does not own that
        pipeline; cancelling one card's job must not cancel the run every
        other step of it is still using.
        """
        from app.models import Card, Pipeline, RunStatus
        from app.services.pipeline_executor import pipeline_executor

        cancelled = []

        async def spy(db, run):
            cancelled.append(run.id)
            return run

        monkeypatch.setattr(pipeline_executor, "cancel_run", spy)

        card_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json={"title": "Step card", "runner_type": "mock",
                  "step_type": "agent"},
        )
        card_id = card_response.json()["id"]
        card = await db_session.get(Card, card_id)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=ingested_repo["id"],
            name="a-real-pipeline",
            steps="[]",
            triggers="[]",
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            trigger_type="manual",
        )
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=0,
            step_name="Step",
            status=RunStatus.RUNNING.value,
        )
        job = Job(
            id=str(uuid4()),
            card_id=card_id,
            status="running",
            step_run_id=step_run.id,
        )
        card.status = "in_progress"
        card.job_id = job.id
        db_session.add_all([pipeline, run, step_run, job])
        await db_session.commit()

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert response.status_code == 200, response.text
        assert cancelled == [], (
            "cancelling one step card's job cancelled the whole pipeline run"
        )

        await db_session.refresh(run)
        assert run.status == "running"

    async def test_a_cancel_that_cannot_cancel_surfaces(
        self, client, ingested_repo, db_session, parked_executor, monkeypatch
    ):
        from app.services.pipeline_executor import pipeline_executor

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        async def boom(db, run):
            raise RuntimeError("docker daemon went away")

        monkeypatch.setattr(pipeline_executor, "cancel_run", boom)

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 503, response.text

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "running", (
            "a failed cancel must leave the job live so it can be retried - "
            "reporting 'failed' hides a container that is still running"
        )


class TestSynchronousStartFailure:
    """``start_pipeline`` does not always return a RUNNING run.

    Image preflight fails INLINE - _complete_pipeline -> on_run_complete
    lands the card and the Job before start_adhoc_agent_run has returned - so
    a caller that writes "running" afterwards resurrects a run that already
    failed and strands the card in in_progress with nothing left to land it.
    """

    async def test_card_ends_failed_with_a_terminal_job(
        self, client, ingested_repo, db_session, missing_image_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])

        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] == "failed"

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed", (
            "the job was flipped back to running over its own terminal status"
        )
        assert job_after["completed_at"] is not None

        run = await adhoc_run_for(db_session, card["id"])
        assert run.status == "failed"


class TestCardOutcomeRespectsTests:
    """A card whose suite is red must not be offered for merge.

    The legacy runner path had this gate (routers/jobs.py: "if tests were run
    and failed, mark card as failed instead of in_review"). Losing it on the
    12.5 path does two things at once: it offers red work for review, and -
    because reaching in_review is what fires the card_complete triggers - it
    hands a red branch to the verification pipeline as if it were done.
    """

    async def _finish_run(self, db_session, card, executor, **results):
        run = await adhoc_run_for(db_session, card["id"])
        step_run = await step_run_for(db_session, run.id)
        await seed_test_results(
            db_session,
            card["repo_id"],
            run.id,
            step_run.id if step_run else None,
            **results,
        )
        executor.release.set()
        await settle()
        return run

    async def test_red_suite_holds_the_card_out_of_review(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await self._finish_run(
            db_session,
            card,
            parked_executor,
            passed=("suite::ok",),
            failed=("suite::regression",),
        )

        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] != "in_review", (
            "the agent step succeeded but the repo suite is red - the card "
            "must not reach in_review"
        )
        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a red card fired the card_complete gate - the verification "
            "pipeline was handed a red branch as if it were done"
        )

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed"
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is False
        assert job_after["test_pass_count"] == 1
        assert job_after["test_fail_count"] == 1

    async def test_a_red_verification_step_of_the_same_run_holds_the_card(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """The gate is RUN-scoped, not step-scoped.

        The executor's own gate demotes a STEP that reported failing tests
        (``_finish_local_step_locked`` reads TestRun by step_run_id), which
        covers the agent step's own suite run. A post-agent verification step
        reports under its OWN step run, so its red results are invisible to
        the finishing agent step's gate: the run passes and the card would be
        promoted. The card's gate reads every TestRun of the RUN, so it holds
        the card whichever step of the run went red.
        """
        from app.models import RunStatus

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)

        run = await adhoc_run_for(db_session, card["id"])
        verification = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=1,
            step_name="Verify",
            status=RunStatus.PASSED.value,
            executor="local",
        )
        db_session.add(verification)
        await db_session.commit()
        await seed_test_results(
            db_session,
            card["repo_id"],
            run.id,
            verification.id,
            passed=("suite::ok",),
            failed=("suite::verification-regression",),
        )

        parked_executor.release.set()
        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] != "in_review"
        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a card whose verification step went red fired the card_complete "
            "gate"
        )

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is False
        assert job_after["test_fail_count"] == 1

    async def test_green_suite_lands_in_review_with_the_counts(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await self._finish_run(
            db_session,
            card,
            parked_executor,
            passed=("suite::a", "suite::b"),
            skipped=("suite::c",),
        )

        await await_card_status(client, db_session, card["id"], "in_review")
        await await_card_gate(trigger_spy, "in_review")

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "completed"
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is True
        assert job_after["test_pass_count"] == 2
        assert job_after["test_skip_count"] == 1
        assert job_after["test_fail_count"] == 0

    async def test_no_manifest_is_neither_a_pass_nor_a_fail(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """No evidence gates nothing, but never claims a green suite."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        parked_executor.release.set()
        await settle()

        await await_card_status(client, db_session, card["id"], "in_review")

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["tests_run"] is False
        assert job_after["tests_passed"] is None


class TestCardJobLogsAreNotDark:
    """The card modal reads JOB logs; the 12.5 path writes StepRun logs.

    Until 12.6 rebuilds that panel, a card work run that leaves Job.logs
    empty means the user sees a blank pane for the whole run and for its
    entire history afterwards.
    """

    async def test_a_completed_run_leaves_non_empty_job_logs(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        parked_executor.release.set()
        await await_card_status(client, db_session, card["id"], "in_review")

        response = await client.get(f"/api/jobs/{card['job_id']}/logs")
        assert response.status_code == 200, response.text
        logs = response.json()["logs"]
        assert logs, "a completed card work run left the card log pane blank"
        assert "rewriting the module" in logs

        job = await db_session.get(Job, card["job_id"])
        await db_session.refresh(job)
        assert job.logs, "Job.logs was never mirrored off the run"

    async def test_logs_are_served_live_while_the_run_is_still_going(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await settle(cycles=10)  # let the log flush land

        response = await client.get(f"/api/jobs/{card['job_id']}/logs")
        assert response.status_code == 200, response.text
        assert "rewriting the module" in response.json()["logs"], (
            "the card modal polls this endpoint every 3s while a job runs; "
            "before completion Job.logs is empty, so it has to fall back to "
            "the StepRun the job is linked to"
        )
