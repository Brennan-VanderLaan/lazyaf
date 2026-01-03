"""
Time manipulation helpers for Phase 12 testing.

These helpers allow tests to freeze time, advance time, and test timeout scenarios
without actually waiting.
"""

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Generator
from unittest.mock import patch


class FrozenTime:
    """
    A frozen time that can be manually advanced.

    This replaces datetime.utcnow(), datetime.now(), and time.time()
    with controllable versions for testing.
    """

    def __init__(self, start_time: datetime | None = None):
        """
        Initialize with a starting time.

        Args:
            start_time: Starting time (defaults to now)
        """
        self._current_time = start_time or datetime.utcnow()
        self._start_real_time = time.time()

    @property
    def current_time(self) -> datetime:
        """Get current frozen time."""
        return self._current_time

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0):
        """
        Advance time by the specified duration.

        Args:
            seconds: Seconds to advance
            minutes: Minutes to advance
            hours: Hours to advance
        """
        total_seconds = seconds + (minutes * 60) + (hours * 3600)
        self._current_time += timedelta(seconds=total_seconds)

    def advance_to(self, target: datetime):
        """
        Advance time to a specific datetime.

        Args:
            target: Target datetime (must be in the future)

        Raises:
            ValueError if target is in the past
        """
        if target < self._current_time:
            raise ValueError(f"Cannot go backwards: {target} < {self._current_time}")
        self._current_time = target

    def time(self) -> float:
        """Replacement for time.time()."""
        elapsed = (self._current_time - datetime.utcnow()).total_seconds()
        return self._start_real_time + elapsed

    def utcnow(self) -> datetime:
        """Replacement for datetime.utcnow()."""
        return self._current_time

    def now(self, tz=None) -> datetime:
        """Replacement for datetime.now()."""
        if tz:
            return self._current_time.replace(tzinfo=tz)
        return self._current_time


@contextmanager
def freeze_time(
    at: datetime | str | None = None
) -> Generator[FrozenTime, None, None]:
    """
    Context manager to freeze time.

    Usage:
        with freeze_time() as frozen:
            print(datetime.utcnow())  # Returns frozen time
            frozen.advance(seconds=30)
            print(datetime.utcnow())  # 30 seconds later

        # Or with a specific start time:
        with freeze_time(datetime(2024, 1, 1)) as frozen:
            ...

    Args:
        at: Starting time (datetime or ISO string, defaults to now)

    Yields:
        FrozenTime controller
    """
    if isinstance(at, str):
        start_time = datetime.fromisoformat(at)
    else:
        start_time = at

    frozen = FrozenTime(start_time)

    # Create wrapper classes that use frozen time
    original_datetime = datetime

    class FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return frozen.utcnow()

        @classmethod
        def now(cls, tz=None):
            return frozen.now(tz)

    with patch("time.time", frozen.time):
        with patch("datetime.datetime", FrozenDateTime):
            yield frozen


class AsyncTimeController:
    """
    Controller for async time-based tests.

    Provides helpers for testing timeout scenarios without actually waiting.
    """

    def __init__(self):
        self._pending_sleeps: list[tuple[float, asyncio.Event]] = []
        self._original_sleep = asyncio.sleep

    async def mock_sleep(self, delay: float):
        """
        Mock asyncio.sleep that can be instantly resolved.

        Sleeps are tracked and can be resolved with advance().
        """
        event = asyncio.Event()
        self._pending_sleeps.append((delay, event))
        await event.wait()

    def advance(self, seconds: float):
        """
        Advance time, resolving any pending sleeps.

        Args:
            seconds: Seconds to advance
        """
        remaining = seconds
        resolved = []

        for delay, event in self._pending_sleeps:
            if delay <= remaining:
                event.set()
                resolved.append((delay, event))
                remaining -= delay

        for item in resolved:
            self._pending_sleeps.remove(item)

    def resolve_all_sleeps(self):
        """Resolve all pending sleeps immediately."""
        for _, event in self._pending_sleeps:
            event.set()
        self._pending_sleeps.clear()

    @property
    def pending_sleep_count(self) -> int:
        """Number of pending sleeps."""
        return len(self._pending_sleeps)


@contextmanager
def mock_async_sleep() -> Generator[AsyncTimeController, None, None]:
    """
    Context manager to mock asyncio.sleep for testing.

    Usage:
        with mock_async_sleep() as controller:
            # Start some async code that sleeps
            task = asyncio.create_task(code_that_sleeps())

            # Advance time to resolve sleeps
            controller.advance(30)

            # Or resolve all at once
            controller.resolve_all_sleeps()

    Yields:
        AsyncTimeController for controlling time
    """
    controller = AsyncTimeController()

    with patch("asyncio.sleep", controller.mock_sleep):
        yield controller


async def wait_with_timeout(
    coro,
    timeout: float,
    on_timeout: Callable[[], None] | None = None,
):
    """
    Wait for a coroutine with timeout, with optional timeout callback.

    Args:
        coro: Coroutine to wait for
        timeout: Timeout in seconds
        on_timeout: Optional callback to run on timeout

    Returns:
        Result of the coroutine

    Raises:
        TimeoutError if timeout exceeded
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        if on_timeout:
            on_timeout()
        raise TimeoutError(f"Operation timed out after {timeout}s")


class HeartbeatSimulator:
    """
    Simulates heartbeat timing for testing timeout scenarios.

    Usage:
        simulator = HeartbeatSimulator(interval=10, timeout=30)

        # Simulate heartbeats
        simulator.beat()
        simulator.advance(15)  # 15s since last beat
        assert not simulator.is_dead

        simulator.advance(20)  # 35s since last beat
        assert simulator.is_dead
    """

    def __init__(self, interval: float = 10.0, timeout: float = 30.0):
        """
        Initialize heartbeat simulator.

        Args:
            interval: Expected heartbeat interval
            timeout: Timeout after which entity is considered dead
        """
        self.interval = interval
        self.timeout = timeout
        self._last_beat = 0.0
        self._current_time = 0.0

    def beat(self):
        """Record a heartbeat."""
        self._last_beat = self._current_time

    def advance(self, seconds: float):
        """Advance time."""
        self._current_time += seconds

    @property
    def time_since_beat(self) -> float:
        """Time since last heartbeat."""
        return self._current_time - self._last_beat

    @property
    def is_dead(self) -> bool:
        """Check if entity should be considered dead (timeout exceeded)."""
        return self.time_since_beat > self.timeout

    @property
    def is_overdue(self) -> bool:
        """Check if heartbeat is overdue (past interval but not dead)."""
        return self.time_since_beat > self.interval and not self.is_dead

    def reset(self):
        """Reset simulator state."""
        self._last_beat = 0.0
        self._current_time = 0.0
