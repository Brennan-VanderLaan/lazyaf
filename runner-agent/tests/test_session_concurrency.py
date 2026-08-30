"""The receive loop keeps serving while a step runs - section 4.3.

Test contract item 3 (section 8, Agent D). failure_01 executed steps INLINE in
the receive loop, so ``cancel_step``, ``drain`` and ``ping`` were unserviceable
for the whole duration of a step: the agent looked dead exactly when it was
busiest, and a user's cancel did nothing until the thing they were cancelling
finished on its own.
"""
from __future__ import annotations

import asyncio

import pytest

from lazyaf_runner.session import MAX_CONCURRENT_STEPS, RunnerSession

from conftest import FakeTransport, StubOrchestrator, make_config, make_step_config

REGISTERED = {"runner_id": "test-runner", "heartbeat_interval": 3600}


def execute_step(step_id="s1", execution_key="k1", **config_overrides) -> dict:
    return {
        "type": "execute_step",
        "step_id": step_id,
        "execution_key": execution_key,
        "config": make_step_config(**config_overrides),
    }


def start_session(transport, orch, **kwargs):
    session = RunnerSession(
        make_config(), orch, transport, REGISTERED, log_flush_interval=0.01, **kwargs
    )
    return session, asyncio.create_task(session.serve())


async def finish(transport, task) -> None:
    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


# ---------------------------------------------------------------------------

async def test_cancel_is_served_while_a_step_runs() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch)

    await asyncio.wait_for(orch.started.wait(), timeout=2)
    assert session.busy

    transport.push({"type": "cancel_step", "step_id": "s1", "reason": "user cancelled"})
    completion = await transport.wait_for("step_complete", timeout=2)

    assert completion["exit_code"] == 143
    assert completion["error"] == "cancelled: user cancelled"
    await finish(transport, task)


async def test_ping_is_served_while_a_step_runs() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push({"type": "ping"})
    await transport.wait_for("pong", timeout=2)

    orch.release.set()
    await finish(transport, task)


async def test_second_execute_step_while_busy_is_refused_and_not_acked() -> None:
    """error{code:"busy"} and NO ack, so the backend's ACK timeout reassigns it
    to a free runner. Acking and queueing would hide a scheduling bug behind
    latency; silently dropping would hang the pipeline."""
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step("s1", "k1"))
    session, task = start_session(transport, orch)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push(execute_step("s2", "k2"))
    error = await transport.wait_for("error", timeout=2)

    assert error["code"] == "busy"
    assert error["fatal"] is False
    assert "s1" in error["message"]
    acks = [frame["step_id"] for frame in transport.frames("ack")]
    assert acks == ["s1"], "the refused assignment must NOT be acked"
    assert len(orch.calls) == 1, "the refused assignment must not reach the orchestrator"

    orch.release.set()
    await finish(transport, task)
    assert MAX_CONCURRENT_STEPS == 1


async def test_ack_precedes_execution() -> None:
    """The ACK budget is 5s; waiting for the orchestrator to get going first is
    how a slow workspace clone turns into a spurious reassignment."""
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch)

    ack = await transport.wait_for("ack", timeout=2)
    assert ack["step_id"] == "s1"

    orch.release.set()
    await finish(transport, task)


async def test_cancel_for_a_different_step_is_ignored() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step("s1", "k1"))
    session, task = start_session(transport, orch)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push({"type": "cancel_step", "step_id": "someone-elses", "reason": "no"})
    await asyncio.sleep(0.05)
    assert orch.observed_cancel is not None and not orch.observed_cancel.is_set()
    assert not transport.frames("step_complete")

    orch.release.set()
    await finish(transport, task)


async def test_cleanup_workspace_is_dispatched_off_the_receive_loop() -> None:
    orch = StubOrchestrator()
    transport = FakeTransport()
    transport.push({"type": "cleanup_workspace", "retain_key": "run-42"})
    transport.push({"type": "ping"})
    _session, task = start_session(transport, orch)

    await transport.wait_for("pong", timeout=2)
    await asyncio.sleep(0.05)
    assert orch.cleaned == ["run-42"]
    await finish(transport, task)


async def test_cleanup_without_a_retain_key_is_a_no_op() -> None:
    orch = StubOrchestrator()
    transport = FakeTransport()
    transport.push({"type": "cleanup_workspace", "retain_key": ""})
    transport.push({"type": "ping"})
    _session, task = start_session(transport, orch)
    await transport.wait_for("pong", timeout=2)
    assert orch.cleaned == []
    await finish(transport, task)


async def test_unknown_message_type_does_not_kill_the_connection() -> None:
    """A backend speaking a newer OPTIONAL frame must not disconnect a runner
    that is mid-step."""
    orch = StubOrchestrator()
    transport = FakeTransport()
    transport.push({"type": "some_future_frame", "payload": 1})
    transport.push({"type": "ping"})
    _session, task = start_session(transport, orch)

    await transport.wait_for("pong", timeout=2)
    await finish(transport, task)


async def test_unparseable_frame_does_not_kill_the_connection() -> None:
    orch = StubOrchestrator()
    transport = FakeTransport()
    session, task = start_session(transport, orch)
    # Bypass the JSON encoding the fake normally does.
    await transport._inbound.put("{not json")  # noqa: SLF001
    transport.push({"type": "ping"})
    await transport.wait_for("pong", timeout=2)
    await finish(transport, task)


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------

async def test_drain_finishes_the_current_step_then_closes() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch, drain_grace=5)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push({"type": "drain", "reason": "backend shutting down"})
    await asyncio.sleep(0.05)
    assert session.draining
    assert not transport.frames("step_complete"), "drain must not abort the running step"

    orch.release.set()
    await asyncio.wait_for(task, timeout=5)
    assert transport.frames("step_complete")[0]["exit_code"] == 0
    assert transport.closed == (1000, "drained")


async def test_a_draining_runner_refuses_new_assignments() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step("s1", "k1"))
    session, task = start_session(transport, orch, drain_grace=5)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push({"type": "drain", "reason": "bye"})
    await asyncio.sleep(0.02)
    transport.push(execute_step("s2", "k2"))
    error = await transport.wait_for("error", timeout=2)
    assert error["code"] == "draining"
    assert [frame["step_id"] for frame in transport.frames("ack")] == ["s1"]

    orch.release.set()
    await asyncio.wait_for(task, timeout=5)


async def test_drain_grace_expiry_still_closes() -> None:
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch, drain_grace=0.05)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push({"type": "drain", "reason": "now"})
    await asyncio.sleep(0.2)
    assert transport.closed is not None

    orch.release.set()
    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


# ---------------------------------------------------------------------------
# Disconnect under a live step
# ---------------------------------------------------------------------------

async def test_socket_drop_kills_the_running_step() -> None:
    """A container that outlives its connection is the split-brain the
    backend's step gate has to defend against. Ending it here makes the
    reassignment the backend is about to do clean."""
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push(execute_step())
    session, task = start_session(transport, orch)
    await asyncio.wait_for(orch.started.wait(), timeout=2)

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)

    assert orch.observed_cancel is not None and orch.observed_cancel.is_set()
    assert session.resume_step_id == "s1"


async def test_orchestrator_exception_becomes_a_failed_outcome() -> None:
    class Exploding(StubOrchestrator):
        async def run_step(self, assignment, *, on_log, cancel):
            raise RuntimeError("daemon went away")

    transport = FakeTransport()
    transport.push(execute_step())
    _session, task = start_session(transport, Exploding())

    completion = await transport.wait_for("step_complete", timeout=2)
    assert completion["exit_code"] == 1
    assert "daemon went away" in completion["error"]
    await finish(transport, task)


async def test_step_complete_always_carries_error_even_on_success() -> None:
    """`error` is null on success, never omitted: a consumer must not have to
    distinguish 'absent' from 'no error'."""
    transport = FakeTransport()
    transport.push(execute_step())
    _session, task = start_session(transport, StubOrchestrator())

    completion = await transport.wait_for("step_complete", timeout=2)
    assert "error" in completion
    assert completion["error"] is None
    assert completion["exit_code"] == 0
    await finish(transport, task)


async def test_fatal_error_frame_propagates_out_of_serve() -> None:
    from lazyaf_runner.session import FatalProtocolError

    transport = FakeTransport()
    transport.push({"type": "error", "code": "auth", "message": "nope", "fatal": True})
    _session, task = start_session(transport, StubOrchestrator())

    with pytest.raises(FatalProtocolError):
        await asyncio.wait_for(task, timeout=5)


async def test_non_fatal_error_frame_keeps_the_connection() -> None:
    transport = FakeTransport()
    transport.push({"type": "error", "code": "rate", "message": "slow down", "fatal": False})
    transport.push({"type": "ping"})
    _session, task = start_session(transport, StubOrchestrator())

    await transport.wait_for("pong", timeout=2)
    await finish(transport, task)
