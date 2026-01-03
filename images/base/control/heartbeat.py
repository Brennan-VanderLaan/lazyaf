"""
Heartbeat Manager.

Background thread that sends periodic heartbeats to backend
to prove step is still alive.
"""
import threading
from typing import Optional


class HeartbeatManager:
    """Manages periodic heartbeats to backend."""

    def __init__(self, client, interval: float = 10.0):
        """
        Initialize heartbeat manager.

        Args:
            client: BackendClient instance
            interval: Seconds between heartbeats
        """
        self.client = client
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._max_failures = 30  # ~5 minutes at 10s interval

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
            success = self.client.heartbeat()

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
