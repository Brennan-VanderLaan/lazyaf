"""`tdd/shared/wait.py` is the one wait helper every suite should reach for,
so its contract is pinned here rather than trusted.

What matters, in order: a wait that holds returns the VALUE (not `True`) the
moment it holds; a wait that does not hold fails as an assertion about the
product - naming `what`, the elapsed time and the last thing the predicate
saw; and the interval is a real sleep, because a helper that busy-spins is
a helper that makes the load it is supposed to survive.

Two clocks, deliberately. The poll SCHEDULE (how many polls, which sleeps,
where the boundary poll lands) is pinned under a fake clock, so those
assertions are exact and cannot fail on a loaded host - this file must not
become the sixth flake. The REALITY of the sleep is pinned against the wall
clock with bounds that load can only push the right way: an elapsed floor
(load makes sleeps longer, never shorter) and a poll-count ceiling (load
means fewer polls before the deadline, never more).
"""
import asyncio
import inspect
import re
import time
from types import SimpleNamespace

import pytest

from tdd.shared import wait
from tdd.shared.wait import wait_until, wait_until_sync


class Observed:
    """A falsy value with a repr that names what was seen.

    This is the shape a predicate returns when the observation itself is
    falsy (a status that is not yet the wanted one, an empty list) but the
    failure message still needs to say what it WAS: `still Observed('assigned')`.
    """

    def __init__(self, status):
        self.status = status

    def __bool__(self):
        return False

    def __repr__(self):
        return f"Observed({self.status!r})"


def counting(values):
    """A predicate that returns `values` in order (last one repeats) and
    counts its calls. Bails after 10,000 calls so a helper that stops
    sleeping fails this suite instead of hanging it under the fake clock."""
    calls = []

    def predicate():
        calls.append(None)
        assert len(calls) <= 10_000, "busy spin: the helper stopped sleeping"
        return values[min(len(calls), len(values)) - 1]

    predicate.calls = calls
    return predicate


class FakeClock:
    """A clock only the helper's own sleeps advance."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds):
        self.sleep(seconds)


@pytest.fixture
def clock(monkeypatch):
    """Swap the helper's `time` and `asyncio` module references - and ONLY the
    helper's, not the global modules pytest-asyncio's loop is running on."""
    clock = FakeClock()
    monkeypatch.setattr(
        wait, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    monkeypatch.setattr(wait, "asyncio", SimpleNamespace(sleep=clock.async_sleep))
    return clock


# -----------------------------------------------------------------------------
# Holds
# -----------------------------------------------------------------------------


class TestHolds:
    async def test_a_predicate_that_holds_at_once_returns_at_once(self, clock):
        predicate = counting([{"status": "pending"}])
        value = await wait_until(predicate, interval=0.5)
        assert value == {"status": "pending"}, "the VALUE comes back, not True"
        assert len(predicate.calls) == 1
        assert clock.sleeps == [], "no interval is slept before the first poll"

    def test_sync_form_holds_at_once(self, clock):
        predicate = counting(["assigned"])
        assert wait_until_sync(predicate, interval=0.5) == "assigned"
        assert len(predicate.calls) == 1
        assert clock.sleeps == []

    async def test_a_predicate_that_holds_after_n_polls_returns_its_first_truthy_value(
        self, clock
    ):
        predicate = counting([None, None, None, "pending"])
        value = await wait_until(predicate, interval=0.25)
        assert value == "pending"
        assert len(predicate.calls) == 4, "polled until it held, then stopped"
        assert clock.sleeps == [0.25, 0.25, 0.25], "one interval between polls"

    def test_sync_form_holds_after_n_polls(self, clock):
        predicate = counting([0, 0, 3])
        assert wait_until_sync(predicate, interval=0.25) == 3
        assert len(predicate.calls) == 3
        assert clock.sleeps == [0.25, 0.25]

    async def test_the_async_form_accepts_an_async_predicate(self):
        seen = []

        async def predicate():
            seen.append(None)
            await asyncio.sleep(0)  # a real await, as a DB read would be
            return len(seen) >= 3 and {"runner_id": None}

        assert await wait_until(predicate, interval=0.001) == {"runner_id": None}
        assert len(seen) == 3

    async def test_the_async_form_also_accepts_a_plain_sync_predicate(self):
        # Most predicates are `lambda: spy.calls` or `lambda: row.status == x`;
        # forcing every one of them to be a coroutine would push tests back
        # toward the inline sleep this helper replaces.
        predicate = counting([None, "held"])
        assert await wait_until(predicate, interval=0.001) == "held"

    def test_the_sync_form_refuses_an_async_predicate(self):
        # A coroutine object is truthy: without this refusal the sync form
        # would "hold" on the first poll and hand the test a coroutine.
        async def predicate():
            return "never awaited"

        with pytest.raises(TypeError, match="use `await wait_until"):
            wait_until_sync(predicate)


# -----------------------------------------------------------------------------
# Does not hold
# -----------------------------------------------------------------------------


class TestDeadline:
    async def test_the_deadline_fails_as_an_assertion_naming_what_and_the_last_value(
        self, clock
    ):
        predicate = counting([Observed("pending-check"), Observed("assigned")])
        with pytest.raises(AssertionError) as excinfo:
            await wait_until(
                predicate, timeout=1.0, interval=0.25,
                what="execution requeued as pending",
            )
        # Reads as the failed assertion it is: what was expected, what the
        # product showed instead, and for how long it was given to get there.
        assert str(excinfo.value) == (
            "execution requeued as pending: still Observed('assigned') "
            "after 1.0s (deadline 1s, 5 polls)"
        ), "the LAST value, not the first; the elapsed time; the bound"
        assert not isinstance(excinfo.value, asyncio.TimeoutError), (
            "a wait that runs out is a failed assertion about the product"
        )

    def test_sync_deadline_fails_the_same_way(self, clock):
        with pytest.raises(AssertionError) as excinfo:
            wait_until_sync(
                lambda: Observed("busy"), timeout=1.0, interval=0.25,
                what="gate released",
            )
        assert str(excinfo.value) == (
            "gate released: still Observed('busy') after 1.0s (deadline 1s, 5 polls)"
        )

    async def test_an_unnamed_wait_still_reads_as_an_assertion(self):
        # Real clock: the one deadline test that proves the real path raises.
        with pytest.raises(AssertionError) as excinfo:
            await wait_until(lambda: None, timeout=0.05, interval=0.01)
        assert re.match(r"^condition: still None after \d+\.\ds \(deadline 0\.05s, \d+ polls\)$",
                        str(excinfo.value)), str(excinfo.value)

    async def test_the_deadline_is_a_bound_not_a_guess(self, clock):
        # The last poll lands ON the deadline: a failure is reported at
        # `timeout`, not at `timeout + interval`, and the predicate is
        # consulted once more there in case the state held at the boundary.
        predicate = counting([None])
        with pytest.raises(AssertionError, match=r"after 1\.0s"):
            await wait_until(predicate, timeout=1.0, interval=0.75)
        assert clock.sleeps == [0.75, 0.25], "the second sleep is clipped to the deadline"
        assert len(predicate.calls) == 3, "t=0, t=0.75, t=1.0 - the boundary poll"

    def test_sync_deadline_is_a_bound_too(self, clock):
        predicate = counting([None])
        with pytest.raises(AssertionError, match=r"after 1\.0s"):
            wait_until_sync(predicate, timeout=1.0, interval=0.75)
        assert clock.sleeps == [0.75, 0.25]
        assert len(predicate.calls) == 3

    async def test_a_state_that_holds_at_the_boundary_is_seen(self, clock):
        # Two polls short of the deadline see nothing; the boundary poll sees
        # it. A helper that checked the clock BEFORE polling would miss this.
        predicate = counting([None, None, "held"])
        assert await wait_until(predicate, timeout=1.0, interval=0.75) == "held"
        assert clock.sleeps == [0.75, 0.25]

    def test_a_zero_deadline_still_polls_once(self, clock):
        # `timeout=0` is "assert now, with the helper's message", not "never look".
        predicate = counting([None])
        with pytest.raises(AssertionError):
            wait_until_sync(predicate, timeout=0)
        assert len(predicate.calls) == 1
        assert clock.sleeps == []

    async def test_a_predicate_that_raises_propagates(self):
        # Swallowing would turn a crash into "condition never held" - the
        # one diagnosis this helper exists to make rare.
        def predicate():
            raise RuntimeError("the row is gone")

        with pytest.raises(RuntimeError, match="the row is gone"):
            await wait_until(predicate, timeout=1.0)
        with pytest.raises(RuntimeError, match="the row is gone"):
            wait_until_sync(predicate, timeout=1.0)


# -----------------------------------------------------------------------------
# The interval is a real sleep (wall clock; every bound is load-monotone)
# -----------------------------------------------------------------------------


class TestInterval:
    # Floors carry slack because Windows' asyncio clock_resolution (~15.6 ms)
    # lets each timer fire that much early, and the suite runs there: four
    # 50 ms sleeps are >= 200 - 4*15.6 = 137 ms worst case. Load only makes
    # them longer.

    async def test_async_polls_are_spaced_by_the_interval(self):
        predicate = counting([None, None, None, None, "held"])
        start = time.monotonic()
        await wait_until(predicate, interval=0.05)
        assert time.monotonic() - start >= 0.12, "four sleeps of 50 ms, no busy spin"

    def test_sync_polls_are_spaced_by_the_interval(self):
        predicate = counting([None, None, None, None, "held"])
        start = time.monotonic()
        wait_until_sync(predicate, interval=0.05)
        assert time.monotonic() - start >= 0.12

    async def test_async_does_not_busy_spin_up_to_the_deadline(self):
        # A busy spin manages thousands of polls in 0.3 s; an honoured 50 ms
        # interval manages about seven. Load can only lower the count.
        predicate = counting([None])
        with pytest.raises(AssertionError):
            await wait_until(predicate, timeout=0.3, interval=0.05)
        assert len(predicate.calls) <= 9, len(predicate.calls)

    def test_sync_does_not_busy_spin_up_to_the_deadline(self):
        predicate = counting([None])
        with pytest.raises(AssertionError):
            wait_until_sync(predicate, timeout=0.3, interval=0.05)
        assert len(predicate.calls) <= 9, len(predicate.calls)

    def test_the_default_deadline_is_generous(self):
        # 20 s is the point: a loaded Docker host must fail only when the
        # product is wrong. Pin the number so nobody quietly trims it back
        # toward the 5 s guesses this helper replaced.
        assert wait.DEFAULT_TIMEOUT >= 20.0
        for fn in (wait_until, wait_until_sync):
            assert inspect.signature(fn).parameters["timeout"].default == wait.DEFAULT_TIMEOUT
