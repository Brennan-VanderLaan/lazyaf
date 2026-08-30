"""Connect / register / reconnect - Phase 12.6, section 4.3.

Test contract item 2 (section 8, Agent D). Four failure_01 defects live here:
a flat 5s retry (reconnect storm), a `registered` wait with no timeout, a
reconnect loop over a permanently-invalid registration, and a heartbeat that
stops while a step runs.
"""
from __future__ import annotations

import asyncio

import pytest

from lazyaf_runner import __version__
from lazyaf_runner.client import (
    BACKOFF_CAP,
    EXIT_FATAL,
    EXIT_OK,
    PROTOCOL_VERSION,
    RunnerClient,
)
from lazyaf_runner.config import ConfigError

from conftest import FakeTransport, StubOrchestrator, make_config, make_step_config

REGISTERED = {
    "type": "registered",
    "runner_id": "test-runner",
    "protocol_version": 1,
    "heartbeat_interval": 10,
    "death_timeout": 30,
    "resume_action": "idle",
    "resume_step_id": None,
}


class UpperBoundRandom:
    """Deterministic stand-in for random.Random: always the top of the range.

    Makes the backoff SCHEDULE assertable while leaving the jitter itself
    tested separately (test_backoff_is_full_jitter).
    """

    def uniform(self, a: float, b: float) -> float:
        return b


def make_connector(transports):
    queue = list(transports)
    calls: list[tuple] = []

    async def connector(url, *, headers):
        calls.append((url, headers))
        if queue:
            return queue.pop(0)
        transport = FakeTransport()
        transport.push_close()
        return transport

    connector.calls = calls  # type: ignore[attr-defined]
    return connector


async def _noop_sleep(_delay: float) -> None:
    return None


def registered_transport(*extra, heartbeat_interval: int = 10) -> FakeTransport:
    transport = FakeTransport()
    transport.push({**REGISTERED, "heartbeat_interval": heartbeat_interval})
    for message in extra:
        transport.push(message)
    transport.push_close()
    return transport


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def test_register_then_registered() -> None:
    transport = registered_transport()
    orch = StubOrchestrator(capabilities={"orchestrator": "stub", "has": ["docker"]})
    client = RunnerClient(
        make_config(labels={"zone": "workshop"}),
        orch,
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )

    assert await client.run() == EXIT_OK

    register = transport.frames("register")[0]
    assert register["runner_id"] == "test-runner"
    assert register["runner_type"] == "generic"
    assert register["protocol_version"] == PROTOCOL_VERSION
    assert register["agent_version"] == __version__
    # Capabilities merged into labels, arch reported raw (backend normalizes).
    assert register["labels"]["zone"] == "workshop"
    assert register["labels"]["has"] == ["docker"]
    assert register["labels"]["arch"]
    # No `resume` on a first connection - the agent holds nothing.
    assert "resume" not in register


async def test_handshake_sends_bearer_token() -> None:
    connector = make_connector([registered_transport()])
    client = RunnerClient(
        make_config(token="s3cret"),
        StubOrchestrator(),
        connector=connector,
        sleep=_noop_sleep,
        max_attempts=1,
    )
    await client.run()

    url, headers = connector.calls[0]
    assert url == "ws://localhost:8000/ws/runner"
    assert headers["Authorization"] == "Bearer s3cret"


async def test_registered_timeout_retries_instead_of_blocking_forever() -> None:
    """A backend that accepts the socket and then says nothing must not wedge
    the agent - failure_01 blocked on recv() with no deadline."""
    silent = FakeTransport()  # never pushes anything: recv() hangs
    good = registered_transport()

    import lazyaf_runner.client as client_module

    original = client_module.REGISTRATION_TIMEOUT
    client_module.REGISTRATION_TIMEOUT = 0.05
    try:
        client = RunnerClient(
            make_config(),
            StubOrchestrator(),
            connector=make_connector([silent, good]),
            sleep=_noop_sleep,
            max_attempts=2,
        )
        assert await client.run() == EXIT_OK
    finally:
        client_module.REGISTRATION_TIMEOUT = original

    assert client.attempts == 2
    assert silent.closed is not None  # the dead socket was closed, not leaked
    assert good.frames("register")


async def test_non_fatal_registration_error_is_retried() -> None:
    rejected = FakeTransport()
    rejected.push({"type": "error", "code": "busy", "message": "try later", "fatal": False})
    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector([rejected, registered_transport()]),
        sleep=_noop_sleep,
        max_attempts=2,
    )
    assert await client.run() == EXIT_OK
    assert client.attempts == 2


async def test_fatal_error_exits_without_retrying() -> None:
    """auth / unsupported version do not heal; a fleet retrying one forever is
    a self-inflicted DDoS over a typo'd secret."""
    fatal = FakeTransport()
    fatal.push(
        {
            "type": "error",
            "code": "auth",
            "message": "authentication failed",
            "fatal": True,
        }
    )
    connector = make_connector([fatal])
    client = RunnerClient(
        make_config(), StubOrchestrator(), connector=connector, sleep=_noop_sleep
    )

    assert await client.run() == EXIT_FATAL
    assert len(connector.calls) == 1, "a fatal error must produce exactly one attempt"
    assert client.delays == [], "a fatal error must not schedule a reconnect"


async def test_unsupported_protocol_version_is_fatal() -> None:
    fatal = FakeTransport()
    fatal.push(
        {
            "type": "error",
            "code": "protocol_version",
            "message": "backend speaks protocol version(s) 1, runner offered 2",
            "fatal": True,
        }
    )
    client = RunnerClient(
        make_config(), StubOrchestrator(), connector=make_connector([fatal]), sleep=_noop_sleep
    )
    assert await client.run() == EXIT_FATAL


async def test_noise_before_registered_is_ignored() -> None:
    transport = FakeTransport()
    transport.push({"type": "pong"})
    transport.push(REGISTERED)
    transport.push_close()
    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )
    assert await client.run() == EXIT_OK


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

def test_backoff_is_capped_at_thirty_seconds() -> None:
    client = RunnerClient(make_config(), StubOrchestrator())
    for attempt in range(0, 40):
        assert 0.0 <= client.next_delay(attempt) <= BACKOFF_CAP


def test_backoff_is_full_jitter_not_a_fixed_delay() -> None:
    """100 simulated agents reconnecting at the same attempt number must NOT
    land in lockstep - that is the reconnect storm failure_01's flat 5s retry
    produced on every backend restart."""
    fleet = [RunnerClient(make_config(), StubOrchestrator()) for _ in range(100)]
    delays = [client.next_delay(5) for client in fleet]

    assert len(set(delays)) > 90, "delays are effectively identical - no jitter"
    spread = max(delays) - min(delays)
    assert spread > 8.0, f"jitter spread {spread:.2f}s is too narrow to decorrelate a fleet"
    # The fixed-delay baseline this replaces has a spread of exactly zero.
    assert spread > 0.0


async def test_backoff_resets_after_a_successful_registration() -> None:
    """ok -> fail -> fail -> ok. The exponent must restart from the success."""
    def failing() -> FakeTransport:
        transport = FakeTransport()
        transport.push({"type": "error", "code": "busy", "message": "no", "fatal": False})
        return transport

    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector(
            [registered_transport(), failing(), failing(), registered_transport()]
        ),
        sleep=_noop_sleep,
        rng=UpperBoundRandom(),
        max_attempts=4,
    )
    await client.run()

    assert client.delays == [1.0, 2.0, 4.0, 1.0]


async def test_invalid_config_exits_before_connecting() -> None:
    connector = make_connector([])
    client = RunnerClient(
        make_config(backend_url="http://remote.example:8000"),
        StubOrchestrator(),
        connector=connector,
        sleep=_noop_sleep,
    )
    assert await client.run() == EXIT_FATAL
    assert connector.calls == [], "plaintext guard must fire before any socket opens"


def test_plaintext_guard_message_names_the_override() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_config(backend_url="http://remote.example:8000").validate()
    assert "LAZYAF_RUNNER_ALLOW_INSECURE" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

async def test_heartbeats_keep_flowing_during_a_long_step() -> None:
    """A runner mid-step is the runner whose death matters most; a heartbeat
    that pauses for the step declares every long step dead."""
    orch = StubOrchestrator(hold=True)
    transport = FakeTransport()
    transport.push({**REGISTERED, "heartbeat_interval": 0.02})
    transport.push(
        {
            "type": "execute_step",
            "step_id": "s1",
            "execution_key": "k1",
            "config": make_step_config(),
        }
    )

    client = RunnerClient(
        make_config(),
        orch,
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )
    run_task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(orch.started.wait(), timeout=2)
        await asyncio.sleep(0.15)
        assert len(transport.frames("heartbeat")) >= 3, (
            "heartbeats stopped while a step was running"
        )
        assert not transport.frames("step_complete")
    finally:
        orch.release.set()
        transport.push_close()
        await asyncio.wait_for(run_task, timeout=5)

    assert transport.frames("step_complete")


async def test_first_heartbeat_is_immediate() -> None:
    """Sent on registration, not one interval later: a 10s gap before a runner's
    first sign of life is 10s of a fresh runner looking half-dead."""
    transport = FakeTransport()
    transport.push({**REGISTERED, "heartbeat_interval": 3600})
    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )
    run_task = asyncio.create_task(client.run())
    try:
        await transport.wait_for("heartbeat", timeout=2)
    finally:
        transport.push_close()
        await asyncio.wait_for(run_task, timeout=5)


# ---------------------------------------------------------------------------
# Reconnect / resume
# ---------------------------------------------------------------------------

async def test_resume_step_is_reported_on_the_next_register() -> None:
    """The socket dropped mid-step. The agent killed its container, so the next
    register carries the step id purely so the backend can reconcile."""
    orch = StubOrchestrator(hold=True)
    first = FakeTransport()
    first.push(REGISTERED)
    first.push(
        {
            "type": "execute_step",
            "step_id": "s-lost",
            "execution_key": "k-lost",
            "config": make_step_config(),
        }
    )
    second = registered_transport()

    client = RunnerClient(
        make_config(),
        orch,
        connector=make_connector([first, second]),
        sleep=_noop_sleep,
        max_attempts=2,
    )
    run_task = asyncio.create_task(client.run())
    await asyncio.wait_for(orch.started.wait(), timeout=2)
    first.push_close()
    await asyncio.wait_for(run_task, timeout=5)

    assert orch.observed_cancel is not None and orch.observed_cancel.is_set(), (
        "the step's container was not cancelled when the socket dropped"
    )
    register = second.frames("register")[0]
    assert register.get("resume") == {"step_id": "s-lost"}


async def test_continue_verdict_is_answered_with_a_terminal_frame() -> None:
    """The agent never truly continues across a reconnect (it killed the
    container). Saying so keeps a StepExecution from sitting in `running`
    forever with nothing on the other end."""
    transport = FakeTransport()
    transport.push(
        {**REGISTERED, "resume_action": "continue", "resume_step_id": "s-old"}
    )
    transport.push_close()
    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )
    await client.run()

    completions = transport.frames("step_complete")
    assert len(completions) == 1
    assert completions[0]["step_id"] == "s-old"
    assert completions[0]["exit_code"] == 143
    assert "reconnect" in completions[0]["error"]


async def test_abort_verdict_needs_no_frame() -> None:
    transport = FakeTransport()
    transport.push({**REGISTERED, "resume_action": "abort", "resume_step_id": "s-old"})
    transport.push_close()
    client = RunnerClient(
        make_config(),
        StubOrchestrator(),
        connector=make_connector([transport]),
        sleep=_noop_sleep,
        max_attempts=1,
    )
    await client.run()
    assert transport.frames("step_complete") == []
