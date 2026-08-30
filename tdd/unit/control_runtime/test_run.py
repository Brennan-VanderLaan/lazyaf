"""
End-to-end unit tests for the control runtime entrypoint
(images/base/control/run.py) with monkeypatched HTTP.

Runs run.main() for real — real config file, real bash subprocess — with the
requests session faked at the backend_client boundary. Proves the full
in-container flow: load config -> consume-once delete -> report running ->
execute -> report terminal status, including the timeout and dropped-log-lines
paths.

The harness (write_config / run_main / posts_to / fake sessions) lives in this
package's conftest.py — deliberately NOT copied per module, so a config-shape
change cannot leave one copy green.
"""
from control import run as control_run


class TestSuccessFlow:
    def test_full_flow_success(self, run_main, posts_to):
        exit_code, session, config_file = run_main("echo hello-world")

        assert exit_code == 0

        statuses = posts_to(session, "/status")
        assert statuses[0] == {"status": "running"}
        assert statuses[-1]["status"] == "completed"
        assert statuses[-1]["exit_code"] == 0
        assert "error" not in statuses[-1]

        # Logs went out in LogLine shape with trailing newlines
        logs = posts_to(session, "/logs")
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
    def test_failure_reports_exit_code_and_error(self, run_main, posts_to):
        exit_code, session, _ = run_main("echo doomed\nexit 3")

        assert exit_code == 3
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "failed"
        assert final["exit_code"] == 3
        assert "code 3" in final["error"]

    def test_missing_config_exits_1(
        self, monkeypatch, tmp_path, fake_session_factory, capsys
    ):
        fake_session_factory()
        monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nope.json"))

        assert control_run.main() == 1
        assert "Could not load step config" in capsys.readouterr().err


class TestTimeoutFlow:
    def test_timeout_reports_timeout_status(self, run_main, posts_to):
        exit_code, session, _ = run_main("sleep 30", timeout_seconds=0.3)

        assert exit_code == 124
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "timeout"
        assert final["exit_code"] == 124
        assert "timeout" in final["error"].lower()


class TestDroppedLogLines:
    def test_dropped_lines_surface_in_final_status_error(
        self, selective_session_factory, run_main_with, posts_to
    ):
        """When the log path budget is exhausted the terminal status carries
        the warning — silent log loss is not allowed."""
        session = selective_session_factory("/logs")

        exit_code, _ = run_main_with("echo lost-line")

        assert exit_code == 0  # the step itself succeeded
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "log lines failed to reach backend" in final["error"]
