"""
Integration tests for the playground's HISTORY and DURABLE-READ endpoints.

The playground was reported as having "no history" and "outputs weren't
saved". Neither was a persistence gap. Every playground run already leaves a
complete durable record behind it (12.5): a PipelineRun with
``trigger_type='playground'`` and ``trigger_ref=<session_id>``, whose single
StepRun holds the transcript, hanging off a hidden
``__lazyaf_adhoc__:playground:<id>`` Pipeline that holds the prompt. The
in-memory ``playground_service._sessions`` dict is a live-streaming cache with
a 30-minute TTL, and it was the ONLY thing the read endpoints consulted - so a
result that existed in the database answered 404, and nothing listed past runs
at all.

These tests pin the read path that closes that, and the one honest edge it
has: a diff CANNOT be rebuilt from the run record, because
``agent_run._dispose_playground_branch`` deletes the ``playground/<id>`` ref
once the diff has been computed. The result therefore carries ``source``, and
``source="run"`` is the contract that lets the UI say "the diff was not
retained" instead of the indistinguishable "no changes were made" (R1).

They also pin the edge validation the start endpoint gained: a blank prompt
and an unknown runner type are refused loudly, at the edge, instead of inside
a container that already cost money to start.

Everything below drives the REAL router and the REAL service against the real
(in-memory) database. The only double is the container.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

# The shared factories the `repo` fixture reaches for live under tdd/, which is
# not importable from here by default (same two lines as test_repos_api.py).
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "tdd"))

from app.models import PipelineRun, StepRun
from app.services import agent_run
from app.services.playground_service import (
    playground_service,
    session_status_for_run,
)

pytestmark = pytest.mark.asyncio


# -----------------------------------------------------------------------------
# Test doubles / helpers
# -----------------------------------------------------------------------------


class ControlModeStubExecutor:
    """Docker-free LocalExecutor stand-in that supports control mode.

    Same shape as the one in test_playground_control_mode.py: it parks inside
    ``execute_step`` until the test releases it, so the test can act as the
    container.
    """

    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.exit_code = 0

    async def image_supports_control_layer(self, image: str) -> bool:
        return True

    async def find_missing_images(self, images) -> list[str]:
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "preparing"}
        yield {"type": "status", "status": "running"}
        self.dispatched.set()
        await self.release.wait()
        yield {
            "type": "result",
            "status": "completed" if self.exit_code == 0 else "failed",
            "exit_code": self.exit_code,
            "error": None,
            "log_tail": [],
        }

    async def cancel_step(self, execution_key):
        self.release.set()
        return True

    async def cancel_all(self):
        self.release.set()


@pytest.fixture
def control_executor():
    from app.services.pipeline_executor import pipeline_executor

    stub = ControlModeStubExecutor()
    previous = pipeline_executor._local_executor
    pipeline_executor._local_executor = stub
    try:
        yield stub
    finally:
        stub.release.set()
        pipeline_executor._local_executor = previous


async def wait_for(predicate, timeout=5.0, message="condition never became true"):
    """Poll a predicate on the running loop. Loud on timeout (R4)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


async def start_playground(client, repo, **overrides):
    payload = {"runner_type": "mock", "branch": repo["default_branch"]}
    payload.update(overrides)
    response = await client.post(
        f"/api/repos/{repo['id']}/playground/test", json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


async def finish_run(db_session, session_id, executor, logs: str, status: str):
    """Land the run behind a session the way a real container's exit does.

    The transcript is written onto the StepRun exactly where control-mode log
    ingestion writes it, so the durable read below is reading the real column
    and not a fixture-shaped one.
    """
    executor.release.set()
    run = await load_run(db_session, session_id)
    step = (
        await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run.id)
        )
    ).scalars().first()
    step.logs = logs
    step.status = status
    run.status = status
    await db_session.commit()


async def load_run(db_session, session_id) -> PipelineRun:
    run = (
        await db_session.execute(
            select(PipelineRun)
            .where(PipelineRun.trigger_type == agent_run.TRIGGER_PLAYGROUND)
            .where(PipelineRun.trigger_ref == session_id)
        )
    ).scalars().first()
    assert run is not None, f"no playground run for session {session_id}"
    return run


def forget_session(session_id):
    """Drop the in-memory session, exactly as the 30-minute TTL sweep does.

    Every durable-read assertion below goes through this first: reading the
    cache proves nothing about what survives it.
    """
    playground_service._sessions.pop(session_id, None)


# -----------------------------------------------------------------------------
# Edge validation on start
# -----------------------------------------------------------------------------


class TestStartValidation:
    async def test_blank_prompt_is_refused(self, client, ingested_repo, control_executor):
        """A whitespace-only prompt must not start an agent container."""
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={
                "runner_type": "mock",
                "branch": ingested_repo["default_branch"],
                "task_override": "   ",
            },
        )
        assert response.status_code == 422, response.text
        assert "blank" in response.text

    async def test_unknown_runner_type_is_refused_naming_the_known_ones(
        self, client, ingested_repo, control_executor
    ):
        """R3: one vocabulary, and it is agent_run.AGENT_BY_RUNNER_TYPE.

        The refusal names the known values, so a frontend that has drifted
        gets told what the vocabulary actually is instead of silently
        resolving to claude-code.
        """
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={
                "runner_type": "gpt-9",
                "branch": ingested_repo["default_branch"],
                "task_override": "hello",
            },
        )
        assert response.status_code == 422, response.text
        for known in agent_run.AGENT_BY_RUNNER_TYPE:
            assert known in response.text

    async def test_absurdly_long_prompt_is_refused(
        self, client, ingested_repo, control_executor
    ):
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={
                "runner_type": "mock",
                "branch": ingested_repo["default_branch"],
                "task_override": "x" * 200_000,
            },
        )
        assert response.status_code == 422, response.text


# -----------------------------------------------------------------------------
# History
# -----------------------------------------------------------------------------


class TestSessionHistory:
    async def test_empty_repo_has_an_empty_history(self, client, ingested_repo):
        response = await client.get(
            f"/api/repos/{ingested_repo['id']}/playground/sessions"
        )
        assert response.status_code == 200, response.text
        assert response.json() == []

    async def test_unknown_repo_is_a_404_not_an_empty_list(self, client):
        """An empty list for a repo that does not exist is a dark answer."""
        response = await client.get("/api/repos/no-such-repo/playground/sessions")
        assert response.status_code == 404, response.text

    async def test_a_run_appears_in_history_with_its_prompt(
        self, client, ingested_repo, db_session, control_executor
    ):
        session_id = await start_playground(
            client, ingested_repo, task_override="explain this repo"
        )
        await wait_for(
            control_executor.dispatched.is_set, message="step never dispatched"
        )

        response = await client.get(
            f"/api/repos/{ingested_repo['id']}/playground/sessions"
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == session_id
        # The prompt is the whole point: without it a history row is a
        # timestamp nobody can identify.
        assert row["prompt"] == "explain this repo"
        assert row["agent"] == "mock"
        assert row["base_branch"] == ingested_repo["default_branch"]
        assert row["work_branch"] == f"playground/{session_id[:8]}"
        assert row["live"] is True

    async def test_history_survives_the_session_being_swept(
        self, client, ingested_repo, db_session, control_executor
    ):
        """The 30-minute TTL must not erase the run from history."""
        session_id = await start_playground(
            client, ingested_repo, task_override="still here?"
        )
        await wait_for(control_executor.dispatched.is_set)
        await finish_run(db_session, session_id, control_executor, "done\n", "passed")
        forget_session(session_id)

        rows = (
            await client.get(
                f"/api/repos/{ingested_repo['id']}/playground/sessions"
            )
        ).json()
        assert [r["session_id"] for r in rows] == [session_id]
        assert rows[0]["prompt"] == "still here?"
        assert rows[0]["status"] == "completed"
        assert rows[0]["live"] is False

    async def test_a_second_run_does_not_destroy_the_first(
        self, client, ingested_repo, db_session, control_executor
    ):
        first = await start_playground(client, ingested_repo, task_override="run one")
        await wait_for(control_executor.dispatched.is_set)
        await finish_run(db_session, first, control_executor, "one\n", "passed")

        control_executor.dispatched.clear()
        control_executor.release.clear()
        second = await start_playground(client, ingested_repo, task_override="run two")
        await wait_for(control_executor.dispatched.is_set)

        rows = (
            await client.get(
                f"/api/repos/{ingested_repo['id']}/playground/sessions"
            )
        ).json()
        assert {r["session_id"] for r in rows} == {first, second}
        # Newest first: the list is read top-down.
        assert rows[0]["session_id"] == second

    async def test_history_is_scoped_to_the_repo(
        self, client, ingested_repo, repo, db_session, control_executor
    ):
        await start_playground(client, ingested_repo, task_override="mine")
        await wait_for(control_executor.dispatched.is_set)

        other = (
            await client.get(f"/api/repos/{repo['id']}/playground/sessions")
        ).json()
        assert other == []

    async def test_limit_is_bounded_loudly(self, client, ingested_repo):
        for bad in (0, 101, -1):
            response = await client.get(
                f"/api/repos/{ingested_repo['id']}/playground/sessions?limit={bad}"
            )
            assert response.status_code == 422, (bad, response.text)


# -----------------------------------------------------------------------------
# Durable reads (the fix for "outputs weren't saved")
# -----------------------------------------------------------------------------


class TestResultAfterSessionIsGone:
    async def test_transcript_survives_the_session_being_swept(
        self, client, ingested_repo, db_session, control_executor
    ):
        """This used to 404 about data sitting in the database."""
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)
        await finish_run(
            db_session,
            session_id,
            control_executor,
            "[agent] line one\n[agent] line two",
            "passed",
        )
        forget_session(session_id)

        response = await client.get(f"/api/playground/{session_id}/result")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed"
        assert "line one" in body["logs"]
        assert "line two" in body["logs"]

    async def test_a_result_read_from_the_run_says_so(
        self, client, ingested_repo, db_session, control_executor
    ):
        """R1: `diff: null` from the run record must be distinguishable from
        `diff: null` meaning the agent changed nothing.

        The playground work branch is deleted once its diff has been computed,
        so the diff genuinely cannot be rebuilt. `source` is what lets the UI
        render the true sentence rather than the misleading one.
        """
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)
        await finish_run(db_session, session_id, control_executor, "out\n", "passed")
        forget_session(session_id)

        body = (await client.get(f"/api/playground/{session_id}/result")).json()
        assert body["source"] == "run"
        assert body["diff"] is None

    async def test_a_live_session_still_reports_source_session(
        self, client, ingested_repo, control_executor
    ):
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)

        body = (await client.get(f"/api/playground/{session_id}/result")).json()
        assert body["source"] == "session"

    async def test_a_failed_run_carries_its_error_out_of_the_run_record(
        self, client, ingested_repo, db_session, control_executor
    ):
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)
        control_executor.release.set()
        run = await load_run(db_session, session_id)
        step = (
            await db_session.execute(
                select(StepRun).where(StepRun.pipeline_run_id == run.id)
            )
        ).scalars().first()
        step.error = "needs ANTHROPIC_API_KEY to run the claude-code CLI"
        step.status = "failed"
        run.status = "failed"
        await db_session.commit()
        forget_session(session_id)

        body = (await client.get(f"/api/playground/{session_id}/result")).json()
        assert body["status"] == "failed"
        assert "ANTHROPIC_API_KEY" in body["error"]

    async def test_a_session_that_never_existed_is_still_a_404(self, client):
        response = await client.get(
            "/api/playground/11111111-2222-3333-4444-555555555555/result"
        )
        assert response.status_code == 404


class TestStatusAfterSessionIsGone:
    async def test_status_falls_back_to_the_run(
        self, client, ingested_repo, db_session, control_executor
    ):
        """The reload path reads status FIRST to decide whether to re-open the
        stream, so it has to answer for a run whose session was swept."""
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)
        await finish_run(db_session, session_id, control_executor, "x\n", "passed")
        forget_session(session_id)

        response = await client.get(f"/api/playground/{session_id}/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed"
        assert body["source"] == "run"

    async def test_a_live_session_status_is_unchanged(
        self, client, ingested_repo, control_executor
    ):
        session_id = await start_playground(client, ingested_repo, task_override="hi")
        await wait_for(control_executor.dispatched.is_set)

        body = (await client.get(f"/api/playground/{session_id}/status")).json()
        assert body["status"] in ("queued", "running")
        assert body["source"] == "session"

    async def test_unknown_session_is_a_404(self, client):
        response = await client.get(
            "/api/playground/11111111-2222-3333-4444-555555555555/status"
        )
        assert response.status_code == 404


# -----------------------------------------------------------------------------
# The status vocabulary translation, in one place
# -----------------------------------------------------------------------------


class TestRunStatusTranslation:
    """Pure translation, no I/O - kept async only to match the module mark."""

    async def test_every_run_status_has_a_session_spelling(self):
        from app.models import RunStatus

        for status in RunStatus:
            assert session_status_for_run(status.value) in (
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
            )

    async def test_an_unmapped_status_is_not_reported_as_success(self):
        """A state nobody taught the table about is not a completed run."""
        assert session_status_for_run("something-new") == "failed"
        assert session_status_for_run(None) == "failed"
