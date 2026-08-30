"""The bounded outbound log queue - Phase 12.6, section 1.7.

Telemetry never blocks execution and never wedges the socket. That is the same
hard rule the 12.3 log budget follows, and it has three consequences that are
each testable:

* the sink is synchronous and bounded - an orchestrator emitting faster than
  the socket drains cannot stall the step it is describing;
* overflow drops the OLDEST lines and says so, because the TAIL of a step's
  runner-origin output is what explains a failure;
* one frame never exceeds what the backend will accept, or the lines vanish
  silently exactly when a step is at its noisiest.

Everything here is driven through the public path (a real orchestrator emitting
through the real sink into a real session) rather than by poking private
buffers, so a refactor of the buffering strategy that preserves the behavior
does not have to rewrite these tests.
"""
from __future__ import annotations

import asyncio

from lazyaf_runner.session import (
    AGENT_OUTBOUND_QUEUE,
    MAX_LOG_LINE_BYTES,
    MAX_LOG_LINES_PER_MESSAGE,
    TRUNCATION_MARKER,
    RunnerSession,
    truncate_line,
)
from lazyaf_runner.types import StepOutcome

from conftest import FakeTransport, StubOrchestrator, make_config, make_step_config

REGISTERED = {"runner_id": "test-runner", "heartbeat_interval": 3600}


class EmittingOrchestrator(StubOrchestrator):
    """Emits a fixed burst of lines in ONE synchronous sink call.

    One call matters: it makes the overflow arithmetic deterministic, because
    nothing can drain the buffer part-way through.
    """

    def __init__(self, lines: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._burst = lines

    async def run_step(self, assignment, *, on_log, cancel):
        self.calls.append(assignment)
        self.observed_cancel = cancel
        on_log(self._burst)
        self.started.set()
        return self._outcome


async def run_burst(lines: list[str], *, outcome: StepOutcome | None = None) -> FakeTransport:
    transport = FakeTransport()
    transport.push(
        {
            "type": "execute_step",
            "step_id": "s1",
            "execution_key": "k1",
            "config": make_step_config(),
        }
    )
    orch = EmittingOrchestrator(lines, outcome=outcome)
    session = RunnerSession(
        make_config(), orch, transport, REGISTERED, log_flush_interval=0.01
    )
    task = asyncio.create_task(session.serve())
    await transport.wait_for("step_complete", timeout=5)
    transport.push_close()
    await asyncio.wait_for(task, timeout=5)
    return transport


def emitted_lines(transport: FakeTransport) -> list[str]:
    lines: list[str] = []
    for frame in transport.frames("log"):
        lines.extend(frame["lines"])
    return lines


# ---------------------------------------------------------------------------

async def test_every_line_arrives_when_under_the_limit() -> None:
    transport = await run_burst([f"line-{i}" for i in range(50)])
    assert emitted_lines(transport) == [f"line-{i}" for i in range(50)]


async def test_logs_are_flushed_before_step_complete() -> None:
    """A `step_complete` that overtakes the explanation of the failure it
    reports is a log nobody can read."""
    transport = await run_burst(["something went wrong"], outcome=StepOutcome(1, "boom"))
    types = [frame["type"] for frame in transport.sent]
    assert types.index("log") < types.index("step_complete")


async def test_overflow_drops_the_oldest_and_says_how_many() -> None:
    overflow = 200
    total = AGENT_OUTBOUND_QUEUE + overflow
    transport = await run_burst([f"line-{i}" for i in range(total)])
    lines = emitted_lines(transport)

    assert lines[0] == f"WARNING: {overflow} log lines dropped (back-pressure)"
    body = lines[1:]
    assert len(body) == AGENT_OUTBOUND_QUEUE
    # The OLDEST went: the tail is what explains a failure.
    assert body[0] == f"line-{overflow}"
    assert body[-1] == f"line-{total - 1}"


async def test_no_warning_line_when_nothing_was_dropped() -> None:
    transport = await run_burst([f"line-{i}" for i in range(10)])
    assert not any("back-pressure" in line for line in emitted_lines(transport))


async def test_frames_respect_the_per_message_line_cap() -> None:
    total = MAX_LOG_LINES_PER_MESSAGE + 100
    transport = await run_burst([f"line-{i}" for i in range(total)])

    frames = transport.frames("log")
    assert all(len(frame["lines"]) <= MAX_LOG_LINES_PER_MESSAGE for frame in frames)
    assert len(emitted_lines(transport)) == total


async def test_frames_carry_an_increasing_seq() -> None:
    """`seq` is forensic: a gap across a reconnect becomes a visible warning
    rather than silent loss."""
    transport = await run_burst([f"line-{i}" for i in range(MAX_LOG_LINES_PER_MESSAGE + 5)])
    seqs = [frame["seq"] for frame in transport.frames("log")]
    assert len(seqs) >= 2
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_every_frame_names_its_step() -> None:
    transport = await run_burst(["a", "b"])
    assert all(frame["step_id"] == "s1" for frame in transport.frames("log"))


async def test_an_overlong_line_is_truncated_not_dropped() -> None:
    huge = "x" * (MAX_LOG_LINE_BYTES * 2)
    transport = await run_burst([huge, "after"])
    lines = emitted_lines(transport)

    assert lines[0].endswith(TRUNCATION_MARKER)
    assert len(lines[0].encode("utf-8")) <= MAX_LOG_LINE_BYTES
    assert lines[1] == "after", "truncation must not swallow the following line"


def test_truncate_line_leaves_short_lines_untouched() -> None:
    assert truncate_line("hello") == "hello"


def test_truncate_line_never_produces_invalid_utf8() -> None:
    """A multi-byte character straddling the cut must not become mojibake."""
    line = "é" * MAX_LOG_LINE_BYTES  # 2 bytes each
    truncated = truncate_line(line)
    assert truncated.endswith(TRUNCATION_MARKER)
    truncated.encode("utf-8").decode("utf-8")  # must not raise


async def test_the_sink_does_not_block_the_orchestrator() -> None:
    """The whole point of the bounded deque: an orchestrator emitting far more
    than the socket can carry still returns promptly."""
    import time

    started = time.perf_counter()
    await run_burst([f"line-{i}" for i in range(AGENT_OUTBOUND_QUEUE * 3)])
    assert time.perf_counter() - started < 5.0


async def test_lines_with_no_owning_step_are_discarded_not_misfiled() -> None:
    """Without a step_id the backend has no StepRun to append to; sending them
    anyway would either error or attach a step's output to the wrong run."""
    transport = FakeTransport()
    session = RunnerSession(
        make_config(), StubOrchestrator(), transport, REGISTERED, log_flush_interval=0.01
    )
    task = asyncio.create_task(session.serve())
    await asyncio.sleep(0.05)
    assert transport.frames("log") == []
    transport.push_close()
    await asyncio.wait_for(task, timeout=5)
