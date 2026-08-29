"""
Heartbeat Manager.

Background thread that sends periodic heartbeats to backend to prove the step
is still alive. Each heartbeat requests a telemetry timeout extension of
``6 * interval`` seconds (StepExecution.timeout_at only — the backend
executor's hard deadline does not move at 12.3).
"""
import threading
from typing import Optional

# Seconds between heartbeats. Module constant — intentionally NOT part of the
# step config wire contract (see test_config_contract.py). The client's
# heartbeat retry budget (BackendClient.HEARTBEAT_*) must stay below this.
HEARTBEAT_INTERVAL = 10.0


class HeartbeatManager:
    """Manages periodic heartbeats to backend.

    Shutdown discipline: after ``stop()`` is called, at most the ONE attempt
    already in flight completes — the loop re-checks the stop event before
    every new attempt, and each attempt is bounded by the client's capped
    heartbeat retry budget (< interval).
    """

    def __init__(self, client, interval: Optional[float] = None):
        """
        Initialize heartbeat manager.

        Args:
            client: BackendClient instance
            interval: Seconds between heartbeats (default: HEARTBEAT_INTERVAL)
        """
        self.client = client
        self.interval = HEARTBEAT_INTERVAL if interval is None else interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._max_failures = 30  # ~5 minutes at 10s interval

    @property
    def extend_seconds(self) -> int:
        """Timeout extension requested per heartbeat (6 missed beats of slack)."""
        return max(1, int(6 * self.interval))

    def start(self) -> None:
        """Start heartbeat thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="heartbeat",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeat thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _heartbeat_loop(self) -> None:
        """Background heartbeat loop."""
        while not self._stop_event.is_set():
            success = self.client.heartbeat(extend_seconds=self.extend_seconds)

            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_failures:
                    # Backend unreachable for too long - stop trying
                    print(
                        "[control] WARNING: Backend unreachable, stopping heartbeat",
                        flush=True,
                    )
                    break

            # Wait for next interval or stop event
            self._stop_event.wait(timeout=self.interval)
