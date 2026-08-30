"""Unit tests for the sole writer of StepRun.logs (Phase 12.6).

Two channels now carry log lines for one step - the step container over HTTP
POST /api/steps/{id}/logs, and the runner agent over the WebSocket `log`
frame - and cross-agent contract #6 gives them ONE writer.

The interesting assertions here are about BYTES, because that is where a
second writer would drift first: the container channel appends verbatim (the
runtime already sent the trailing newline) and the runner channel prefixes
and newline-terminates. Both go out as exactly one `step_log_batch` frame,
which is the 12.3 router contract the frontend already consumes.

Everything runs against a REAL session and the REAL ConnectionManager with a
capturing transport (R6): a mock cannot tell you the frame was serializable
or that exactly one of them was emitted.
"""
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.pipeline import (
    Pipeline,
    PipelineRun,
    StepExecution,
    StepExecutionStatus,
    StepRun,
)
from app.models.repo import Repo
from app.services.execution.step_logs import (
    RUNNER_LINE_PREFIX,
    SOURCE_CONTAINER,
    SOURCE_RUNNER,
    StepRunMissing,
    append_step_logs,
    format_lines,
)
from app.services.websocket import manager


class CapturingSocket:
    """A real transport that records what was actually put on the wire."""

    def __init__(self):
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def send_json(self, payload) -> None:  # pragma: no cover - manager uses one
        self.frames.append(payload)

    async def close(self, code: int = 1000) -> None:
        pass

    def of_type(self, message_type: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == message_type]


@pytest_asyncio.fixture
async def ws_socket():
    socket = CapturingSocket()
    manager.active_connections.append(socket)
    yield socket
    if socket in manager.active_connections:
        manager.active_connections.remove(socket)


@pytest_asyncio.fixture
async def execution(db_session):
    """A StepExecution with a real StepRun/PipelineRun chain behind it."""
    repo = Repo(id=str(uuid4()), name="log-writer-repo")
    pipeline = Pipeline(
        id=str(uuid4()), repo_id=repo.id, name="log-writer-pipeline", steps="[]"
    )
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=3,
        step_name="remote step",
        status="running",
        logs=None,
    )
    step_execution = StepExecution(
        id=str(uuid4()),
        step_run_id=step_run.id,
        execution_key=f"{run.id}:3:{step_run.id}",
        status=StepExecutionStatus.RUNNING.value,
    )
    db_session.add_all([repo, pipeline, run, step_run, step_execution])
    await db_session.commit()
    return step_execution


async def _logs_of(db, step_run_id: str) -> str:
    row = await db.get(StepRun, step_run_id)
    await db.refresh(row)
    return row.logs or ""


class TestContainerSource:
    """The /logs router's existing byte contract, now enforced in one place."""

    async def test_container_lines_append_verbatim(self, db_session, execution):
        """The runtime sends lines WITH their newline; the writer adds none."""
        await append_step_logs(
            db_session, execution, ["a\n", "b\n"], source=SOURCE_CONTAINER
        )

        assert await _logs_of(db_session, execution.step_run_id) == "a\nb\n"

    async def test_container_content_without_newline_is_not_padded(
        self, db_session, execution
    ):
        """Verbatim means verbatim: a line with no newline stays glued.

        This is not a nicety - the runtime streams partial chunks, and a
        writer that "helpfully" terminated them would split every chunk
        boundary into a spurious line.
        """
        await append_step_logs(db_session, execution, ["par", "tial"], source=SOURCE_CONTAINER)

        assert await _logs_of(db_session, execution.step_run_id) == "partial"

    async def test_one_step_log_batch_frame_per_call(
        self, db_session, execution, ws_socket
    ):
        """ONE frame carrying the whole batch, lines newline-rstripped."""
        await append_step_logs(
            db_session, execution, ["one\n", "two\n"], source=SOURCE_CONTAINER
        )

        assert ws_socket.of_type("step_log") == []
        assert [f["payload"] for f in ws_socket.of_type("step_log_batch")] == [
            {
                "pipeline_run_id": (
                    await db_session.get(StepRun, execution.step_run_id)
                ).pipeline_run_id,
                "step_index": 3,
                "lines": ["one", "two"],
            }
        ]

    async def test_appends_accumulate_across_calls(self, db_session, execution):
        await append_step_logs(db_session, execution, ["first\n"])
        await append_step_logs(db_session, execution, ["second\n"])

        assert await _logs_of(db_session, execution.step_run_id) == "first\nsecond\n"


class TestRunnerSource:
    """Runner-origin lines: the ones a step container cannot emit.

    A remote step that dies before its container starts explains itself
    through this channel and no other, which is exactly why the prefix has to
    be visible in the blob rather than implied by the frame it arrived on.
    """

    async def test_runner_lines_are_prefixed_and_terminated(
        self, db_session, execution
    ):
        await append_step_logs(
            db_session,
            execution,
            ["pulling lazyaf-test-runner:dev", "ERROR: docker daemon unreachable"],
            source=SOURCE_RUNNER,
        )

        assert await _logs_of(db_session, execution.step_run_id) == (
            "[runner] pulling lazyaf-test-runner:dev\n"
            "[runner] ERROR: docker daemon unreachable\n"
        )

    async def test_runner_line_with_its_own_newline_is_not_doubled(
        self, db_session, execution
    ):
        await append_step_logs(
            db_session, execution, ["already terminated\n"], source=SOURCE_RUNNER
        )

        assert await _logs_of(db_session, execution.step_run_id) == (
            "[runner] already terminated\n"
        )

    async def test_runner_lines_broadcast_with_the_prefix(
        self, db_session, execution, ws_socket
    ):
        """The UI sees the prefix too - one blob, two visibly distinct
        streams, which is the whole point of tagging them at all."""
        await append_step_logs(
            db_session, execution, ["provisioning workspace"], source=SOURCE_RUNNER
        )

        frames = ws_socket.of_type("step_log_batch")
        assert [f["payload"]["lines"] for f in frames] == [
            ["[runner] provisioning workspace"]
        ]

    async def test_both_sources_interleave_in_append_order(
        self, db_session, execution
    ):
        """Append order IS real order.

        The agent emits [runner] lines only BEFORE container start and AFTER
        it exits, so the two streams cannot overlap in time - the ordering
        guarantee is structural, not a timestamp heuristic.
        """
        await append_step_logs(db_session, execution, ["provisioning"], source=SOURCE_RUNNER)
        await append_step_logs(db_session, execution, ["step output\n"])
        await append_step_logs(db_session, execution, ["workspace retained"], source=SOURCE_RUNNER)

        assert await _logs_of(db_session, execution.step_run_id) == (
            "[runner] provisioning\n"
            "step output\n"
            "[runner] workspace retained\n"
        )


class TestEdges:
    async def test_empty_batch_writes_nothing_and_broadcasts_nothing(
        self, db_session, execution, ws_socket
    ):
        appended = await append_step_logs(db_session, execution, [])

        assert appended == 0
        assert await _logs_of(db_session, execution.step_run_id) == ""
        assert ws_socket.of_type("step_log_batch") == []

    async def test_missing_step_run_raises_step_run_missing(
        self, db_session, execution
    ):
        """A distinct exception, so the HTTP router can answer 404 and the WS
        endpoint can drop the frame - neither has to tell "no lines" apart
        from "no such step run"."""
        execution.step_run_id = "does-not-exist"

        with pytest.raises(StepRunMissing):
            await append_step_logs(db_session, execution, ["orphan\n"])

    async def test_missing_step_run_raises_even_for_an_empty_batch(
        self, db_session, execution
    ):
        """The lookup happens BEFORE the empty check, preserving the router's
        behavior that an empty POST to a vanished step run is still a 404."""
        execution.step_run_id = "does-not-exist"

        with pytest.raises(StepRunMissing):
            await append_step_logs(db_session, execution, [])

    async def test_unknown_source_is_a_loud_error(self, db_session, execution):
        with pytest.raises(ValueError, match="unknown log source"):
            await append_step_logs(db_session, execution, ["x\n"], source="syslog")

    async def test_returns_the_number_of_lines_appended(self, db_session, execution):
        assert await append_step_logs(db_session, execution, ["a\n", "b\n", "c\n"]) == 3


class TestFormatLines:
    """The pure renderer, asserted without a database."""

    def test_container_is_identity(self):
        assert format_lines(["a\n", "b"], SOURCE_CONTAINER) == ["a\n", "b"]

    def test_runner_prefixes_and_terminates(self):
        assert format_lines(["a", "b\n"], SOURCE_RUNNER) == [
            f"{RUNNER_LINE_PREFIX}a\n",
            f"{RUNNER_LINE_PREFIX}b\n",
        ]

    def test_runner_prefix_is_the_loopback_gate_marker(self):
        """The dogfood gate greps for this exact prefix to prove the AGENT
        (not the container) produced output on the remote lane."""
        assert RUNNER_LINE_PREFIX == "[runner] "

    def test_empty_input(self):
        assert format_lines([], SOURCE_RUNNER) == []
