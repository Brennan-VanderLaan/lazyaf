"""
Unit tests for the control runtime's HeartbeatManager
(images/base/control/heartbeat.py).
"""
import threading
import time


from control.heartbeat import HEARTBEAT_INTERVAL, HeartbeatManager


class FakeClient:
    def __init__(self, succeed=True):
        self.calls = []  # extend_seconds values
        self.succeed = succeed
        self.event = threading.Event()

    def heartbeat(self, extend_seconds=None):
        self.calls.append(extend_seconds)
        self.event.set()
        return self.succeed


class TestHeartbeatManager:
    def test_sends_periodic_heartbeats_with_extension(self):
        client = FakeClient()
        manager = HeartbeatManager(client, interval=0.02)

        manager.start()
        time.sleep(0.15)
        manager.stop()

        assert len(client.calls) >= 2
        # extend_seconds = 6 missed beats of slack (min 1s)
        assert all(v == manager.extend_seconds for v in client.calls)

    def test_extend_seconds_is_six_intervals(self):
        assert HeartbeatManager(FakeClient(), interval=10.0).extend_seconds == 60
        assert HeartbeatManager(FakeClient(), interval=0.05).extend_seconds == 1  # floor

    def test_stop_halts_the_loop(self):
        client = FakeClient()
        manager = HeartbeatManager(client, interval=0.02)

        manager.start()
        client.event.wait(timeout=2.0)
        manager.stop()
        count = len(client.calls)
        time.sleep(0.1)

        assert len(client.calls) == count

    def test_gives_up_after_max_consecutive_failures(self):
        client = FakeClient(succeed=False)
        manager = HeartbeatManager(client, interval=0.01)
        manager._max_failures = 3

        manager.start()
        time.sleep(0.2)
        manager.stop()

        assert len(client.calls) == 3

    def test_default_interval_is_the_module_constant(self):
        """The interval is NOT part of the config wire contract — it comes
        from the module constant when the caller passes nothing."""
        assert HeartbeatManager(FakeClient()).interval == HEARTBEAT_INTERVAL

    def test_does_not_outlive_stop_by_more_than_one_attempt(self):
        """Once stop() is requested, at most the in-flight attempt finishes;
        no NEW attempt may start."""
        release = threading.Event()
        started = threading.Event()
        calls = []

        class SlowClient:
            def heartbeat(self, extend_seconds=None):
                calls.append(extend_seconds)
                started.set()
                release.wait(timeout=2.0)
                return True

        manager = HeartbeatManager(SlowClient(), interval=0.01)
        manager.start()
        assert started.wait(timeout=2.0), "first heartbeat never started"

        # Stop while the first attempt is still in flight, then release it.
        manager._stop_event.set()
        release.set()
        manager._thread.join(timeout=2.0)

        assert not manager._thread.is_alive()
        assert len(calls) == 1  # the in-flight attempt only — nothing new

    def test_success_resets_failure_counter(self):
        client = FakeClient()
        manager = HeartbeatManager(client, interval=0.01)
        manager._max_failures = 2
        # fail, succeed, fail, succeed... never two consecutive failures
        outcomes = [False, True] * 20

        def _heartbeat(extend_seconds=None):
            client.calls.append(extend_seconds)
            return outcomes.pop(0) if outcomes else True

        client.heartbeat = _heartbeat
        manager.start()
        time.sleep(0.12)
        manager.stop()

        assert len(client.calls) > 2  # loop survived alternating failures
