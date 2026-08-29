"""
End-to-end unit tests for the control runtime entrypoint
(images/base/control/run.py) with monkeypatched HTTP.

Runs run.main() for real — real config file, real bash subprocess — with the
requests session faked at the backend_client boundary. Proves the full
in-container flow: load config -> consume-once delete -> report running ->
execute -> report terminal status, including the timeout and dropped-log-lines
paths.
"""
import json

import pytest

from control import heartbeat as control_heartbeat
from control import run as control_run
from control.backend_client import BackendClient


def _write_config(tmp_path, command, **overrides):
    data = {
        "step_id": "exec-1",
        "step_run_id": "sr-1",
        "execution_key": "r:0:sr-1",
        "command": command,
        "backend_url": "http://backend:8000",
        "auth_token": "tok",
        "environment": {},
        "timeout_seconds": 30,
        "working_directory": str(tmp_path),
    }
    data.update(overrides)
    # Per-step config path (contract #1: /workspace/.control/<step_execution_id>.json)
    config_file = tmp_path / f"{data['step_id']}.json"
    config_file.write_text(json.dumps(data))
    return config_file


def _status_posts(session):
    return [p for m, u, p in session.requests if u.endswith("/status")]


def _log_posts(session):
    return [p for m, u, p in session.requests if u.endswith("/logs")]


@pytest.fixture
def quiet_heartbeat(monkeypatch):
    """The interval is a module constant now — keep tests heartbeat-quiet."""
    monkeypatch.setattr(control_heartbeat, "HEARTBEAT_INTERVAL", 60.0)


@pytest.fixture
def run_main(tmp_path, monkeypatch, fake_session_factory, quiet_heartbeat):
    def _run(command, codes=None, **config_overrides):
        session = fake_session_factory(codes)
        config_file = _write_config(tmp_path, command, **config_overrides)
        monkeypatch.setenv("CONFIG_PATH", str(config_file))
        exit_code = control_run.main()
        return exit_code, session, config_file

    return _run


class TestSuccessFlow:
    def test_full_flow_success(self, run_main):
        exit_code, session, config_file = run_main("echo hello-world")

        assert exit_code == 0

        statuses = _status_posts(session)
        assert statuses[0] == {"status": "running"}
        assert statuses[-1]["status"] == "completed"
        assert statuses[-1]["exit_code"] == 0
        assert "error" not in statuses[-1]

        # Logs went out in LogLine shape with trailing newlines
        logs = _log_posts(session)
        all_lines = [line for p in logs for line in p["lines"]]
        assert {"content": "hello-world\n", "stream": "stdout"} in all_lines

    def test_config_is_consumed_once(self, run_main):
        """The config file (and its token) must not survive the step: the
        volume persists and a stale config re-running a previous step is the
        landmine this phase exists to kill."""
        exit_code, _, config_file = run_main("true")

        assert exit_code == 0
        assert not config_file.exists()

    def test_config_deleted_even_on_failure(self, run_main):
        _, _, config_file = run_main("exit 3")

        assert not config_file.exists()

    def test_config_deleted_even_on_parse_failure(
        self, monkeypatch, tmp_path, fake_session_factory, capsys
    ):
        """The finally-unlink must fire on EVERY path: an unparseable config
        (and any token inside it) must not survive to a later step."""
        fake_session_factory()
        config_file = tmp_path / "exec-parse-fail.json"
        config_file.write_text("not valid json {{{")
        monkeypatch.setenv("CONFIG_PATH", str(config_file))

        assert control_run.main() == 1
        assert not config_file.exists()
        assert "Could not load step config" in capsys.readouterr().err


class TestFailureFlow:
    def test_failure_reports_exit_code_and_error(self, run_main):
        exit_code, session, _ = run_main("echo doomed\nexit 3")

        assert exit_code == 3
        final = _status_posts(session)[-1]
        assert final["status"] == "failed"
        assert final["exit_code"] == 3
        assert "code 3" in final["error"]

    def test_missing_config_exits_1(self, monkeypatch, tmp_path, fake_session_factory, capsys):
        fake_session_factory()
        monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nope.json"))

        assert control_run.main() == 1
        assert "Could not load step config" in capsys.readouterr().err


class TestTimeoutFlow:
    def test_timeout_reports_timeout_status(self, run_main):
        exit_code, session, _ = run_main("sleep 30", timeout_seconds=0.3)

        assert exit_code == 124
        final = _status_posts(session)[-1]
        assert final["status"] == "timeout"
        assert final["exit_code"] == 124
        assert "timeout" in final["error"].lower()


class TestDroppedLogLines:
    def test_dropped_lines_surface_in_final_status_error(
        self, run_main, monkeypatch, quiet_heartbeat
    ):
        """When the log path budget is exhausted the terminal status carries
        the warning — silent log loss is not allowed."""
        monkeypatch.setattr(BackendClient, "BASE_BACKOFF", 0.001)
        monkeypatch.setattr(BackendClient, "MAX_BACKOFF", 0.002)

        # /logs 500s forever; /status and /heartbeat succeed.
        class SelectiveSession:
            def __init__(self):
                self.headers = {}
                self.requests = []

            def request(self, method, url, timeout=None, json=None, **kwargs):
                self.requests.append((method, url, json))

                class R:
                    status_code = 500 if url.endswith("/logs") else 200

                return R()

        from control import backend_client as bc

        session = SelectiveSession()
        monkeypatch.setattr(bc.requests, "Session", lambda: session)

        # Drive main() directly (run_main would install its own all-200 session)
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config_file = tmp / "exec-1.json"
            config_file.write_text(_json.dumps({
                "step_id": "exec-1",
                "backend_url": "http://backend:8000",
                "auth_token": "tok",
                "command": "echo lost-line",
                "working_directory": str(tmp),
                "timeout_seconds": 30,
            }))
            monkeypatch.setenv("CONFIG_PATH", str(config_file))
            exit_code = control_run.main()

        assert exit_code == 0  # the step itself succeeded
        statuses = [p for m, u, p in session.requests if u.endswith("/status")]
        final = statuses[-1]
        assert final["status"] == "completed"
        assert "log lines failed to reach backend" in final["error"]
