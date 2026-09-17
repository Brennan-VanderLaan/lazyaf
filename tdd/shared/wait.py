"""Wait on the state a test ASSERTS, with a bounded deadline (R4).

ONE helper, sync and async, for every test that has to wait for something
another task, thread or socket does. It exists because T1 went red on one
test with 5948 passing and the run stopped before the Go tier ever ran -
and the failing test was a different one each time. `tdd/tier_floors.json`
names the five under T1 as "a real defect ... NOT ABSORBED BY THIS NOTE":

  tdd/integration/api/test_ws_runner_endpoint.py::TestTeardown::test_disconnect_mid_step_requeues_the_step
  tdd/integration/api/test_cards_api.py::TestStartingACardIsAtomic::test_five_simultaneous_retries_produce_one_new_run
  tdd/unit/control_runtime/test_executor.py::TestQuietProcessFlush::test_flushes_on_batch_size
  tdd/unit/execution/test_debug_gate.py::TestGatePausesAndResumes::test_a_paused_gate_broadcasts_debug_session_status
  tdd/unit/services/test_endpoint_scheduler.py::TestWaiting::test_the_waiter_wakes_and_is_admitted_when_the_slot_frees

They share one bug class, in two shapes:

1. **A fixed sleep** (`await asyncio.sleep(0.01)` at test_cards_api.py:889,
   `asyncio.sleep(0.1)` at test_endpoint_scheduler.py:269, a 0.8 s
   `threading.Timer` racing a 5 s `Event.wait` at test_executor.py:329-335,
   `asyncio.wait_for(..., timeout=5.0)` at test_debug_gate.py:667-671 and
   :709, windows of 0.3/0.4 s at test_endpoint_scheduler.py:252 and :299).
   A sleep encodes a guess about how fast the host is. On an idle laptop
   the guess holds; inside the dogfood container, with another suite
   sharing the CPU, it does not - and the failure names the test, never the
   guess. Widening the number is the same race with a longer fuse.

2. **A proxy signal.** test_ws_runner_endpoint.py:905-914 (`_settle`) polls
   the RUNNER row until it reads `disconnected`, then the test loads the
   EXECUTION row and asserts on it. But the endpoint's `finally:`
   (backend/app/routers/ws_runners.py:672-680) runs `connection.teardown()`,
   which writes the runner state AND requeues the execution as two separate
   operations. `_settle` returns the instant the first is visible; the
   assertion reads the second. `assert 'assigned' == 'pending'` is the
   window between them, and no timeout closes it - the test synchronized on
   a row it does not assert.

The rule both helpers enforce: the predicate observes the state the test is
about to assert, is polled until it holds, and the deadline is a BOUND on a
real wait, not a guess about its length. The default is 20 s - generous on
purpose. A loaded Docker host must fail only when the product is wrong, and
a test that holds within 20 s costs nothing extra for being allowed to.

On the deadline the helpers raise AssertionError, not TimeoutError, and the
message carries `what`, the elapsed time and the LAST value the predicate
returned - so the report reads "execution requeued as pending: still
'assigned' after 20.0s", a failed assertion about the product, not "a wait
gave up". Predicates that return the observed value (a status string, a row,
a count) instead of a bare bool get that for free; a predicate whose
observation is falsy can return an object whose repr names it.

Exceptions raised by the predicate propagate. Swallowing them would turn a
crash in the product into "condition never held", which is the one
diagnosis this module exists to make rare.

A test that genuinely cannot get a deterministic signal - nothing the code
under test sets, awaits or commits that the test can observe - must say so
in a comment and use one of these with the default deadline, never a sleep.
"""
import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["wait_until", "wait_until_sync"]

DEFAULT_TIMEOUT = 20.0
DEFAULT_INTERVAL = 0.02


def _never_held(
    what: str, last: Any, elapsed: float, timeout: float, polls: int
) -> AssertionError:
    # `what` names the assertion, `last` is what the product actually showed:
    # together they read as the failed assertion, which is what the failure IS.
    label = what or "condition"
    return AssertionError(
        f"{label}: still {last!r} after {elapsed:.1f}s "
        f"(deadline {timeout:g}s, {polls} polls)"
    )


async def wait_until(
    predicate: Callable[[], Any | Awaitable[Any]],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    what: str = "",
) -> Any:
    """Poll `predicate` (sync or async) until it returns a truthy value.

    Returns that value. Raises AssertionError once `timeout` seconds have
    passed with no truthy value, naming `what` and the last value seen.
    The predicate always runs at least once, and once more at the deadline,
    so a state that holds at the boundary is seen, not missed.
    """
    # time.monotonic() rather than loop.time(): it IS the default loop's
    # clock, and one clock for both forms lets tdd/unit/shared/test_wait.py
    # pin the poll schedule exactly instead of guessing at it with a stopwatch.
    start = time.monotonic()
    polls = 0
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        polls += 1
        if value:
            return value
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise _never_held(what, value, elapsed, timeout, polls)
        # Never sleep past the deadline: the final poll lands ON it, so a
        # failure is reported at `timeout`, not at `timeout + interval`.
        await asyncio.sleep(min(interval, timeout - elapsed))


def wait_until_sync(
    predicate: Callable[[], Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    what: str = "",
) -> Any:
    """`wait_until` for tests that are not running on an event loop.

    Same contract; sleeps with `time.sleep`, so it is the form for threaded
    tests (test_executor.py's gate) and for the TestClient-driven websocket
    suite, whose test bodies are synchronous.
    """
    start = time.monotonic()
    polls = 0
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            # A coroutine is truthy, so an async predicate handed to the sync
            # form would "hold" on the first poll and the test would assert on
            # a coroutine object. Refuse loudly instead of passing quietly.
            close = getattr(value, "close", None)
            if close is not None:
                close()  # no "coroutine was never awaited" noise on top
            raise TypeError(
                "wait_until_sync got an awaitable from its predicate; "
                "use `await wait_until(...)` for async predicates"
            )
        polls += 1
        if value:
            return value
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise _never_held(what, value, elapsed, timeout, polls)
        time.sleep(min(interval, timeout - elapsed))
