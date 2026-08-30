"""
Unit tests for the 12.2.6 test-results transport in the control runtime
(images/base/control/run.py + backend_client.py).

Pins the pinned contracts #2/#3, client side:
- LAZYAF_TEST_RESULTS_PATH is injected PER-STEP into the step environment,
  pointing at test_results.<step_execution_id>.json next to the step config
  (in the image: /workspace/.control/)
- after the command exits, an existing manifest is POSTed to
  /api/steps/{step_id}/test-results (Bearer step token, log-tight retry
  budget) and the file is deleted on every path (consume-once)
- manifest delivery NEVER changes the step outcome; failures surface loudly
  in the terminal status error (the dropped-log-lines pattern)
- a MALFORMED manifest is untrusted input, not a crash: whatever the step
  wrote at that path, the runtime still reports its terminal status and
  still consumes the file

The harness (write_config / run_main / posts_to / fake sessions) lives in
this package's conftest.py.
"""
import json

import pytest

from control import run as control_run
from control.backend_client import BackendClient

MANIFEST = {
    "version": 1,
    "results": [
        {
            "lazyaf_test_id": "us1.card_to_merge",
            "status": "passed",
            "duration_ms": 42,
            "file_path": "tdd/e2e/test_thing.py",
        }
    ],
}


class TestSendTestResultsClient:
    def test_posts_manifest_to_test_results_endpoint(self, fake_session_factory):
        session = fake_session_factory()
        client = BackendClient("http://backend:8000", "exec-1", "tok-abc")

        assert client.send_test_results(MANIFEST) is True

        method, url, payload = session.requests[0]
        assert method == "POST"
        assert url == "http://backend:8000/api/steps/exec-1/test-results"
        assert payload == MANIFEST  # manifest is the body, verbatim

    def test_uses_log_tight_retry_budget(self, fake_session_factory, fast_retries):
        """Manifest delivery runs at step shutdown: same tight budget as
        /logs, never the patient status budget."""
        session = fake_session_factory([500])
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        assert client.send_test_results(MANIFEST) is False
        assert len(session.requests) == BackendClient.LOG_MAX_RETRIES

    def test_client_error_returns_false_without_retry(
        self, fake_session_factory, fast_retries
    ):
        session = fake_session_factory([409])
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        assert client.send_test_results(MANIFEST) is False
        assert len(session.requests) == 1  # 4xx (terminal step) not retried


class TestEnvInjection:
    def test_manifest_path_env_injected_per_step(self, run_main, posts_to, tmp_path):
        """Contract #2: LAZYAF_TEST_RESULTS_PATH points at
        test_results.<step_execution_id>.json NEXT TO the step config."""
        exit_code, session, config_file = run_main(
            'echo "MANIFEST_AT=$LAZYAF_TEST_RESULTS_PATH"'
        )

        assert exit_code == 0
        expected = str(tmp_path / "test_results.exec-1.json")
        all_lines = "".join(
            line["content"]
            for p in posts_to(session, "/logs")
            for line in p["lines"]
        )
        assert f"MANIFEST_AT={expected}" in all_lines

    def test_platform_value_overrides_user_environment(
        self, run_main, posts_to, tmp_path
    ):
        """A step must never write another step's manifest — the injected
        per-step path wins over a user-supplied one."""
        exit_code, session, _ = run_main(
            'echo "MANIFEST_AT=$LAZYAF_TEST_RESULTS_PATH"',
            environment={"LAZYAF_TEST_RESULTS_PATH": "/somewhere/else.json"},
        )

        assert exit_code == 0
        all_lines = "".join(
            line["content"]
            for p in posts_to(session, "/logs")
            for line in p["lines"]
        )
        assert "/somewhere/else.json" not in all_lines
        assert str(tmp_path / "test_results.exec-1.json") in all_lines


class TestManifestPickup:
    def test_manifest_posted_and_deleted(self, run_main, posts_to, tmp_path):
        manifest_file = tmp_path / "test_results.exec-1.json"
        manifest_file.write_text(json.dumps(MANIFEST))

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posts = posts_to(session, "/test-results")
        assert posts == [MANIFEST]
        assert not manifest_file.exists()  # consume-once

        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "error" not in final

    def test_no_manifest_no_post(self, run_main, posts_to):
        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        assert posts_to(session, "/test-results") == []

    def test_step_writes_manifest_through_injected_env(
        self, run_main, posts_to, tmp_path
    ):
        """End-to-end inside the runtime: the command writes to
        $LAZYAF_TEST_RESULTS_PATH and the runtime ships exactly that."""
        payload = json.dumps(MANIFEST)
        exit_code, session, _ = run_main(
            f"printf '%s' '{payload}' > \"$LAZYAF_TEST_RESULTS_PATH\""
        )

        assert exit_code == 0
        assert posts_to(session, "/test-results") == [MANIFEST]
        assert not (tmp_path / "test_results.exec-1.json").exists()

    def test_manifest_shipped_even_when_command_fails(
        self, run_main, posts_to, tmp_path
    ):
        """A red test run is exactly when the manifest matters most."""
        (tmp_path / "test_results.exec-1.json").write_text(json.dumps(MANIFEST))

        exit_code, session, _ = run_main("exit 3")

        assert exit_code == 3
        assert posts_to(session, "/test-results") == [MANIFEST]
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "failed"


class TestDeliveryFailureIsLoudButNonFatal:
    def test_failed_delivery_never_fails_the_step(
        self, selective_session_factory, run_main_with, posts_to, tmp_path
    ):
        session = selective_session_factory("/test-results")
        (tmp_path / "test_results.exec-1.json").write_text(json.dumps(MANIFEST))

        exit_code, _ = run_main_with("true")

        assert exit_code == 0  # the step itself still succeeds
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        # ...but the drop is LOUD in StepExecution.error (12.3 pattern)
        assert "test results manifest failed to reach backend" in final["error"]
        # consume-once holds on the failure path too
        assert not (tmp_path / "test_results.exec-1.json").exists()

    def test_unreadable_manifest_is_loud_and_consumed(
        self, run_main, posts_to, tmp_path
    ):
        manifest_file = tmp_path / "test_results.exec-1.json"
        manifest_file.write_text("not json {{{")

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        assert posts_to(session, "/test-results") == []
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "test results manifest unreadable" in final["error"]
        assert not manifest_file.exists()


# -----------------------------------------------------------------------------
# Malformed manifests: untrusted input written by the STEP's own command.
# The regression this class exists for: a bad manifest crashed the runtime
# BEFORE it reported a terminal status, so the step hung in "running" until
# the reaper took it. Terminal status reporting is non-negotiable.
# -----------------------------------------------------------------------------

MALFORMED = {
    "bare_list": [{"lazyaf_test_id": "a", "status": "passed"}],
    "bare_string": "not a manifest at all",
    "bare_number": 17,
    "null": None,
    "results_is_dict": {"version": 1, "results": {"a": "passed"}},
    "results_is_string": {"version": 1, "results": "passed"},
    "results_missing": {"version": 1},
    "entry_not_a_dict": {"version": 1, "results": ["nope", 3, None]},
    "entry_missing_id": {"version": 1, "results": [{"status": "passed"}]},
    "entry_missing_status": {"version": 1, "results": [{"lazyaf_test_id": "a"}]},
    "entry_id_wrong_type": {
        "version": 1,
        "results": [{"lazyaf_test_id": 5, "status": "passed"}],
    },
    "entry_id_empty": {
        "version": 1,
        "results": [{"lazyaf_test_id": "", "status": "passed"}],
    },
    "unknown_status": {
        "version": 1,
        "results": [{"lazyaf_test_id": "a", "status": "errored"}],
    },
    "empty_results": {"version": 1, "results": []},
    "deeply_nested": {"version": 1, "results": [{"lazyaf_test_id": {"a": [1, 2]}}]},
}


class TestMalformedManifestNeverBreaksTerminalStatus:
    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_terminal_status_still_reported_and_file_consumed(
        self, label, run_main, posts_to, tmp_path
    ):
        manifest_file = tmp_path / "test_results.exec-1.json"
        manifest_file.write_text(json.dumps(MALFORMED[label]))

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert not manifest_file.exists(), f"{label}: manifest survived"
        # nothing unusable ever reaches the backend
        assert posts_to(session, "/test-results") == [], label

    @pytest.mark.parametrize("label", sorted(MALFORMED))
    def test_malformed_manifest_is_loud(self, label, run_main, posts_to, tmp_path):
        (tmp_path / "test_results.exec-1.json").write_text(
            json.dumps(MALFORMED[label])
        )

        _, session, _ = run_main("true")

        final = posts_to(session, "/status")[-1]
        assert "test results manifest" in final.get("error", ""), label

    def test_partially_bad_manifest_ships_the_good_entries(
        self, run_main, posts_to, tmp_path
    ):
        """Coerce/skip: one broken entry must not cost the whole run's
        tie-back data."""
        (tmp_path / "test_results.exec-1.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "results": [
                        {"lazyaf_test_id": "a.good", "status": "passed",
                         "duration_ms": 3, "file_path": "tdd/x/test_a.py"},
                        {"lazyaf_test_id": "b.bad", "status": "exploded"},
                        "not-even-a-dict",
                        {"lazyaf_test_id": "c.good", "status": "failed",
                         "duration_ms": None, "file_path": None},
                    ],
                }
            )
        )

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posts = posts_to(session, "/test-results")
        assert len(posts) == 1
        assert [r["lazyaf_test_id"] for r in posts[0]["results"]] == [
            "a.good",
            "c.good",
        ]
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "b.bad" in final["error"]

    def test_wrong_typed_fields_are_coerced_not_fatal(
        self, run_main, posts_to, tmp_path
    ):
        (tmp_path / "test_results.exec-1.json").write_text(
            json.dumps(
                {
                    "version": "one",
                    "results": [
                        {
                            "lazyaf_test_id": "a.coerce",
                            "status": "passed",
                            "duration_ms": "12",
                            "file_path": 99,
                            "surprise": {"nested": True},
                        }
                    ],
                }
            )
        )

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        (payload,) = posts_to(session, "/test-results")
        assert payload["version"] == 1
        assert payload["results"] == [
            {
                "lazyaf_test_id": "a.coerce",
                "status": "passed",
                "duration_ms": None,  # "12" is not a number -> nulled
                "file_path": None,  # 99 is not a string -> nulled
            }
        ]

    def test_huge_manifest_still_reports_terminal_status(
        self, run_main, posts_to, tmp_path
    ):
        """10k entries, half of them junk — throughput is not the point,
        NOT crashing before the terminal status is."""
        results = []
        for i in range(5000):
            results.append(
                {
                    "lazyaf_test_id": f"bulk.ok.{i}",
                    "status": "passed",
                    "duration_ms": i,
                    "file_path": f"tdd/bulk/test_{i}.py",
                }
            )
            results.append({"lazyaf_test_id": f"bulk.bad.{i}"})
        (tmp_path / "test_results.exec-1.json").write_text(
            json.dumps({"version": 1, "results": results})
        )

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        (payload,) = posts_to(session, "/test-results")
        assert len(payload["results"]) == 5000
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"

    def test_manifest_file_that_is_a_directory_is_not_fatal(
        self, run_main, posts_to, tmp_path
    ):
        """os.open on a directory raises IsADirectoryError/PermissionError —
        an OSError shape the old code did not have to survive on Windows."""
        (tmp_path / "test_results.exec-1.json").mkdir()

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "test results manifest" in final["error"]

    def test_unexpected_exception_in_shipping_is_swallowed(
        self, monkeypatch, run_main, posts_to, tmp_path
    ):
        """ANY exception is a delivery failure, never a lost terminal
        status — including one from code that is 'supposed' to be safe."""
        (tmp_path / "test_results.exec-1.json").write_text(json.dumps(MANIFEST))

        def boom(_raw):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr(control_run, "normalize_manifest", boom)

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "validator exploded" in final["error"]
        assert not (tmp_path / "test_results.exec-1.json").exists()

    def test_send_raising_is_swallowed(self, monkeypatch, run_main, posts_to, tmp_path):
        (tmp_path / "test_results.exec-1.json").write_text(json.dumps(MANIFEST))

        def boom(self, manifest):
            raise ConnectionResetError("backend hung up mid-POST")

        monkeypatch.setattr(BackendClient, "send_test_results", boom)

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        final = posts_to(session, "/status")[-1]
        assert final["status"] == "completed"
        assert "backend hung up mid-POST" in final["error"]
