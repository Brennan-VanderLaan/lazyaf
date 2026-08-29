"""
Unit tests for the control runtime's BackendClient
(images/base/control/backend_client.py).

Ported from failure_01 and extended for the audit fixes:
- /logs payload wraps lines as LogLine objects with trailing newlines
  (plain strings 422 against the backend's LogsRequest schema)
- heartbeat carries extend_seconds
- log path uses a TIGHT retry budget and counts dropped lines
- heartbeat path uses a budget capped BELOW the heartbeat interval
- requests.Session is not thread-safe: attempts serialize through a lock
"""
import threading
import time

from control.backend_client import BackendClient
from control.heartbeat import HEARTBEAT_INTERVAL


def _client(fake_session_factory, codes=None):
    session = fake_session_factory(codes)
    client = BackendClient("http://backend:8000", "exec-1", "tok-abc")
    return client, session


class TestStatusReporting:
    def test_reports_status_running(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.report_status("running") is True

        method, url, payload = session.requests[0]
        assert method == "POST"
        assert url == "http://backend:8000/api/steps/exec-1/status"
        assert payload == {"status": "running"}

    def test_reports_status_completed_with_exit_code(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.report_status("completed", exit_code=0) is True

        _, _, payload = session.requests[0]
        assert payload == {"status": "completed", "exit_code": 0}

    def test_reports_status_failed_with_error(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.report_status("failed", exit_code=1, error="boom") is True

        _, _, payload = session.requests[0]
        assert payload == {"status": "failed", "exit_code": 1, "error": "boom"}

    def test_reports_status_timeout(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.report_status("timeout", exit_code=124, error="too slow")

        _, _, payload = session.requests[0]
        assert payload["status"] == "timeout"
        assert payload["exit_code"] == 124

    def test_sets_bearer_auth_header(self, fake_session_factory):
        client, session = _client(fake_session_factory)
        assert session.headers["Authorization"] == "Bearer tok-abc"


class TestLogLinePayload:
    def test_sends_logs_as_logline_objects(self, fake_session_factory):
        """THE contract fix: {"lines": [{"content": ..., "stream": ...}]},
        content keeps its trailing newline (router concatenates verbatim)."""
        client, session = _client(fake_session_factory)

        assert client.send_logs(["line 1\n", "line 2\n"]) is True

        method, url, payload = session.requests[0]
        assert url == "http://backend:8000/api/steps/exec-1/logs"
        assert payload == {
            "lines": [
                {"content": "line 1\n", "stream": "stdout"},
                {"content": "line 2\n", "stream": "stdout"},
            ]
        }

    def test_newline_added_when_missing(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        client.send_logs(["no newline"])

        _, _, payload = session.requests[0]
        assert payload["lines"][0]["content"] == "no newline\n"

    def test_empty_batch_is_a_noop(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.send_logs([]) is True
        assert session.requests == []


class TestHeartbeat:
    def test_heartbeat_sends_extend_seconds(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.heartbeat(extend_seconds=60) is True

        method, url, payload = session.requests[0]
        assert url == "http://backend:8000/api/steps/exec-1/heartbeat"
        assert payload == {"extend_seconds": 60}

    def test_heartbeat_without_extension(self, fake_session_factory):
        client, session = _client(fake_session_factory)

        assert client.heartbeat() is True
        assert session.requests[0][2] == {}


class TestRetryBudgets:
    def test_retries_on_server_error_then_succeeds(
        self, fake_session_factory, fast_retries
    ):
        client, session = _client(fake_session_factory, codes=[500, 500, 200])

        assert client.report_status("running") is True
        assert len(session.requests) == 3

    def test_client_error_is_not_retried(self, fake_session_factory, fast_retries):
        client, session = _client(fake_session_factory, codes=[422])

        assert client.report_status("running") is False
        assert len(session.requests) == 1

    def test_log_path_caps_retries_at_three(self, fake_session_factory, fast_retries):
        """Logs must not wedge the step: tight budget, not the patient one."""
        client, session = _client(fake_session_factory, codes=[500])

        assert client.send_logs(["a\n", "b\n"]) is False
        assert len(session.requests) == BackendClient.LOG_MAX_RETRIES == 3

    def test_dropped_log_lines_are_counted(self, fake_session_factory, fast_retries):
        client, session = _client(fake_session_factory, codes=[500])

        client.send_logs(["a\n", "b\n"])
        client.send_logs(["c\n"])

        assert client.dropped_log_lines == 3

    def test_successful_logs_do_not_count_as_dropped(self, fake_session_factory):
        client, _ = _client(fake_session_factory)

        client.send_logs(["a\n"])

        assert client.dropped_log_lines == 0

    def test_status_path_keeps_patient_budget(self, fake_session_factory, fast_retries):
        """Status retries beyond the log path's 3-attempt cap."""
        client, session = _client(fake_session_factory, codes=[500, 500, 500, 500, 200])

        assert client.report_status("completed", exit_code=0) is True
        assert len(session.requests) == 5

    def test_heartbeat_budget_capped(self, fake_session_factory, fast_retries):
        """A heartbeat attempt must give up fast — the patient budget could
        wedge the heartbeat thread for minutes past stop()."""
        client, session = _client(fake_session_factory, codes=[500])

        assert client.heartbeat(extend_seconds=60) is False
        assert len(session.requests) == BackendClient.HEARTBEAT_MAX_RETRIES == 2

    def test_heartbeat_worst_case_fits_inside_one_interval(self):
        """Static contract: retries * per-attempt HTTP timeout + max backoff
        between them stays below HEARTBEAT_INTERVAL, so one bounded attempt
        is the most the heartbeat thread can outlive stop() by."""
        worst_case = (
            BackendClient.HEARTBEAT_MAX_RETRIES
            * BackendClient.HEARTBEAT_REQUEST_TIMEOUT
            + (BackendClient.HEARTBEAT_MAX_RETRIES - 1)
            * (BackendClient.BASE_BACKOFF + 1.0)  # +1.0 = max jitter
        )
        assert worst_case < HEARTBEAT_INTERVAL
        assert BackendClient.HEARTBEAT_TOTAL_TIMEOUT < HEARTBEAT_INTERVAL


class TestSessionThreadSafety:
    def test_concurrent_calls_serialize_through_the_session(self, monkeypatch):
        """requests.Session is not thread-safe; status/logs/heartbeat run on
        three different threads. Attempts must never overlap in the session."""
        from control import backend_client as bc

        class ConcurrencyTrackingSession:
            def __init__(self):
                self.headers = {}
                self.active = 0
                self.max_active = 0
                self._meta_lock = threading.Lock()

            def request(self, method, url, timeout=None, json=None, **kwargs):
                with self._meta_lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.02)  # widen any overlap window
                with self._meta_lock:
                    self.active -= 1

                class R:
                    status_code = 200

                return R()

        session = ConcurrencyTrackingSession()
        monkeypatch.setattr(bc.requests, "Session", lambda: session)
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        threads = [
            threading.Thread(target=client.report_status, args=("running",)),
            threading.Thread(target=client.send_logs, args=(["a\n"],)),
            threading.Thread(target=client.heartbeat),
            threading.Thread(target=client.send_logs, args=(["b\n"],)),
            threading.Thread(target=client.report_status, args=("completed",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert session.max_active == 1
