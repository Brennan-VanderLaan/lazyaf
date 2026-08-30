"""The execution_key LRU - Phase 12.6, section 4.4.

Test contract item 4 (section 8, Agent D). A reconnect-after-reassign, or a
backend restart re-dispatching the same ``execution_key``, must not run the
work twice. This is the AGENT half of remote idempotency; the other halves are
the backend's DB compare-and-swap and its step gate. ``RemoteExecutor``
deliberately has no idempotency cache of its own - LocalExecutor's in-process
dict cannot help across a restart, and putting one there would be a third
mechanism disagreeing with these two.
"""
from __future__ import annotations

import asyncio

from lazyaf_runner.session import IDEMPOTENCY_CACHE_SIZE, RunnerSession
from lazyaf_runner.types import StepOutcome

from conftest import FakeTransport, StubOrchestrator, make_config, make_step_config

REGISTERED = {"runner_id": "test-runner", "heartbeat_interval": 3600}


def execute_step(step_id: str, execution_key: str) -> dict:
    return {
        "type": "execute_step",
        "step_id": step_id,
        "execution_key": execution_key,
        "config": make_step_config(),
    }


def start_session(transport, orch):
    session = RunnerSession(
        make_config(), orch, transport, REGISTERED, log_flush_interval=0.01
    )
    return session, asyncio.create_task(session.serve())


async def wait_for_completions(transport, count: int, *, timeout: float = 3.0) -> list[dict]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        frames = transport.frames("step_complete")
        if len(frames) >= count:
            return frames
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"only {len(transport.frames('step_complete'))} step_complete frames after {timeout}s"
    )


# ---------------------------------------------------------------------------

async def test_repeated_execution_key_is_acked_and_answered_from_cache() -> None:
    orch = StubOrchestrator(outcome=StepOutcome(0))
    transport = FakeTransport()
    transport.push(execute_step("s1", "key-A"))
    _session, task = start_session(transport, orch)
    await wait_for_completions(transport, 1)

    # Same execution_key, different step_id: a reassignment of the same work.
    transport.push(execute_step("s1-again", "key-A"))
    completions = await wait_for_completions(transport, 2)

    assert len(orch.calls) == 1, "a cached execution_key must not reach the orchestrator"
    assert [frame["step_id"] for frame in transport.frames("ack")] == ["s1", "s1-again"], (
        "the repeat must still be ACKed, or the backend reassigns it again"
    )
    assert completions[1]["step_id"] == "s1-again"
    assert completions[1]["exit_code"] == completions[0]["exit_code"]
    assert completions[1]["error"] == completions[0]["error"]

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


async def test_cached_failure_is_replayed_verbatim() -> None:
    """Not just successes: replaying a success for a step that actually failed
    would turn a re-dispatch into a false green."""
    orch = StubOrchestrator(outcome=StepOutcome(9, "it went badly"))
    transport = FakeTransport()
    transport.push(execute_step("s1", "key-B"))
    _session, task = start_session(transport, orch)
    await wait_for_completions(transport, 1)

    transport.push(execute_step("s2", "key-B"))
    completions = await wait_for_completions(transport, 2)

    assert completions[1]["exit_code"] == 9
    assert completions[1]["error"] == "it went badly"
    assert len(orch.calls) == 1

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


async def test_a_different_execution_key_really_runs() -> None:
    """Guards the cache from becoming a black hole."""
    orch = StubOrchestrator()
    transport = FakeTransport()
    transport.push(execute_step("s1", "key-C"))
    _session, task = start_session(transport, orch)
    await wait_for_completions(transport, 1)

    transport.push(execute_step("s2", "key-D"))
    await wait_for_completions(transport, 2)

    assert len(orch.calls) == 2

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


async def test_cache_is_bounded_and_evicts_the_oldest() -> None:
    orch = StubOrchestrator()
    transport = FakeTransport()
    _session, task = start_session(transport, orch)

    # One at a time: MAX_CONCURRENT_STEPS is 1, so a batch push would be
    # refused as busy rather than queued - which is the correct behavior and
    # the wrong setup for this assertion.
    total = IDEMPOTENCY_CACHE_SIZE + 1
    for index in range(total):
        transport.push(execute_step(f"s{index}", f"key-{index}"))
        await wait_for_completions(transport, index + 1, timeout=5)
    assert len(orch.calls) == total

    # key-0 was evicted, so it runs again; the newest key is still cached.
    transport.push(execute_step("s0-again", "key-0"))
    await wait_for_completions(transport, total + 1, timeout=5)
    assert len(orch.calls) == total + 1, "the evicted key should have re-run"

    transport.push(execute_step("s-last-again", f"key-{total - 1}"))
    await wait_for_completions(transport, total + 2, timeout=5)
    assert len(orch.calls) == total + 1, "the newest key should still be cached"

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)


async def test_cache_does_not_survive_a_reconnect_it_should_not() -> None:
    """The LRU is per-CONNECTION state, and that is correct: it exists to make
    a reassignment across one reconnect safe, not to memoize a runner's whole
    history. A fresh session re-running a key the backend re-dispatched is the
    backend's CAS problem, and it has one."""
    orch = StubOrchestrator()
    first = FakeTransport()
    first.push(execute_step("s1", "key-E"))
    _session, task = start_session(first, orch)
    await wait_for_completions(first, 1)
    first.push_close()
    await asyncio.wait_for(task, timeout=5)

    second = FakeTransport()
    second.push(execute_step("s1", "key-E"))
    _session2, task2 = start_session(second, orch)
    await wait_for_completions(second, 1)
    assert len(orch.calls) == 2

    second.push_close()
    await asyncio.wait_for(task2, timeout=5)


async def test_a_step_with_no_execution_key_is_never_cached() -> None:
    orch = StubOrchestrator()
    transport = FakeTransport()
    transport.push(execute_step("s1", ""))
    _session, task = start_session(transport, orch)
    await wait_for_completions(transport, 1)

    transport.push(execute_step("s2", ""))
    await wait_for_completions(transport, 2)
    assert len(orch.calls) == 2, "an empty key must not match another empty key"

    transport.push_close()
    await asyncio.wait_for(task, timeout=5)
