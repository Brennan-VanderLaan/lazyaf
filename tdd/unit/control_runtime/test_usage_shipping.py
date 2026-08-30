"""
Unit tests for the 12.5 USAGE transport in the control runtime
(images/base/control/run.py + backend_client.py) - protocol channel #4.

Pins the SHIPPER half of cross-agent contracts #2/#3:
- LAZYAF_USAGE_PATH is injected PER-STEP into the step environment, pointing
  at usage.<step_execution_id>.json next to the step config (in the image:
  /workspace/.control/), and the platform value overrides a user-supplied one
- after the command exits, an existing manifest is POSTed to
  /api/steps/{step_id}/usage (Bearer step token, log-tight retry budget) and
  the file is deleted on every path (consume-once)
- run.py OWNS timing (wall_clock_ms, container_seconds) and node attribution
  (role / gpu_node_id / gpu_fraction from container env) and OVERWRITES
  whatever the wrapper wrote for them
- a step that produced NO manifest still yields a complete fallback record,
  so wall-clock and container time are complete across the whole graph
- telemetry NEVER fails a step: no malformed manifest, no 409, no exhausted
  retry budget, and no crash inside the shipper may change the exit code -
  problems surface loudly in the terminal status error instead

Everything the shipper emits is validated against the SHARED contract module
(tdd/unit/control_runtime/usage_contract.py), which the server side imports
too, so a drift on either side names the side that drifted.

The harness (write_config / run_main / posts_to / fake sessions) lives in this
package's conftest.py.
"""
import json
from pathlib import Path

import pytest

from control import run as control_run
from control.backend_client import BackendClient

from tdd.unit.control_runtime.usage_contract import (
    CANONICAL_MANIFEST,
    assert_manifest_conforms,
)

SHIPPER = "SHIPPER (control runtime run.py)"

# What the wrapper writes: everything IT owns, with the run.py-owned fields
# present but deliberately wrong, so the overwrite is observable.
WRAPPER_MANIFEST = {
    "version": 1,
    "provider": "anthropic",
    "model": "claude-haiku-4-5",
    "model_version": "claude-haiku-4-5-20260210",
    "input_tokens": 18422,
    "output_tokens": 3110,
    "cache_read_tokens": 240110,
    "cache_write_tokens": 12004,
    "cost_usd": "0.1841",
    "cost_source": "cli-reported",
    "wall_clock_ms": 999999,
    "container_seconds": 999.9,
    "gpu_node_id": "wrapper-should-not-set-this",
    "gpu_fraction": 0.25,
    "determinism": {"temperature": 0.0},
    "role": None,
    "raw": {"total_cost_usd": 0.1841},
}


def usage_posts(session, posts_to):
    return posts_to(session, "/usage")


class TestSendUsageClient:
    def test_posts_manifest_to_usage_endpoint(self, fake_session_factory):
        session = fake_session_factory()
        client = BackendClient("http://backend:8000", "exec-1", "tok-abc")

        assert client.send_usage(CANONICAL_MANIFEST) == 200

        method, url, payload = session.requests[0]
        assert method == "POST"
        assert url == "http://backend:8000/api/steps/exec-1/usage"
        assert payload == CANONICAL_MANIFEST  # body verbatim

    def test_uses_log_tight_retry_budget(self, fake_session_factory, fast_retries):
        """Delivery runs at step shutdown: same tight budget as /logs, never
        the patient status budget."""
        session = fake_session_factory([500])
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        assert client.send_usage(CANONICAL_MANIFEST) is None
        assert len(session.requests) == BackendClient.LOG_MAX_RETRIES

    def test_409_is_reported_as_a_status_not_retried(
        self, fake_session_factory, fast_retries
    ):
        """A terminal StepExecution answers 409: a non-retryable DROP. The
        caller must be able to TELL that apart from a network failure, which
        is why send_usage returns the status code."""
        session = fake_session_factory([409])
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        assert client.send_usage(CANONICAL_MANIFEST) == 409
        assert len(session.requests) == 1

    def test_422_is_reported_as_a_status(self, fake_session_factory, fast_retries):
        session = fake_session_factory([422])
        client = BackendClient("http://backend:8000", "exec-1", "tok")

        assert client.send_usage(CANONICAL_MANIFEST) == 422
        assert len(session.requests) == 1


class TestEnvInjection:
    def test_usage_path_env_injected_per_step(self, run_main, posts_to, tmp_path):
        """Contract #2: LAZYAF_USAGE_PATH points at
        usage.<step_execution_id>.json NEXT TO the step config."""
        exit_code, session, _ = run_main('echo "USAGE_AT=$LAZYAF_USAGE_PATH"')

        assert exit_code == 0
        expected = str(tmp_path / "usage.exec-1.json")
        lines = "".join(
            line["content"]
            for p in posts_to(session, "/logs")
            for line in p["lines"]
        )
        assert f"USAGE_AT={expected}" in lines

    def test_platform_value_overrides_user_environment(
        self, run_main, posts_to, tmp_path
    ):
        """A step must never write another step's accounting."""
        exit_code, session, _ = run_main(
            'echo "USAGE_AT=$LAZYAF_USAGE_PATH"',
            environment={"LAZYAF_USAGE_PATH": "/somewhere/else.json"},
        )

        assert exit_code == 0
        lines = "".join(
            line["content"]
            for p in posts_to(session, "/logs")
            for line in p["lines"]
        )
        assert "/somewhere/else.json" not in lines
        assert str(tmp_path / "usage.exec-1.json") in lines

    def test_usage_path_is_derived_next_to_the_config(self, tmp_path):
        path = control_run.usage_path(tmp_path / "sub" / "cfg.json", "exec-9")
        assert path == tmp_path / "sub" / "usage.exec-9.json"


class TestManifestPickup:
    def test_manifest_posted_and_deleted(self, run_main, posts_to, tmp_path):
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posts = usage_posts(session, posts_to)
        assert len(posts) == 1
        assert_manifest_conforms(posts[0], SHIPPER)
        assert not (tmp_path / "usage.exec-1.json").exists()  # consume-once

    def test_wrapper_owned_fields_survive_verbatim(
        self, run_main, posts_to, tmp_path
    ):
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        for key in (
            "provider", "model", "model_version", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "cost_usd", "cost_source", "determinism", "raw",
        ):
            assert posted[key] == WRAPPER_MANIFEST[key], key

    def test_runpy_overwrites_the_timing_fields(
        self, run_main, posts_to, tmp_path
    ):
        """R3, one writer per datum: run.py is the ONLY component present for
        script steps too, so timing has exactly one owner - and the wrapper's
        values are replaced, not merged."""
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert posted["wall_clock_ms"] != WRAPPER_MANIFEST["wall_clock_ms"]
        assert isinstance(posted["wall_clock_ms"], int)
        assert posted["wall_clock_ms"] >= 0
        assert posted["container_seconds"] != WRAPPER_MANIFEST["container_seconds"]
        assert posted["container_seconds"] >= 0

    def test_runpy_owns_node_attribution_from_container_env(
        self, run_main, posts_to, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ROLE", "planner")
        monkeypatch.setenv("LAZYAF_GPU_NODE_ID", "runpod-a100-80g")
        monkeypatch.setenv("LAZYAF_GPU_FRACTION", "0.5")
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert posted["role"] == "planner"
        assert posted["gpu_node_id"] == "runpod-a100-80g"
        assert posted["gpu_fraction"] == 0.5
        assert_manifest_conforms(posted, SHIPPER)

    def test_wrapper_gpu_values_are_not_trusted(
        self, run_main, posts_to, tmp_path, monkeypatch
    ):
        """Node attribution is the EXECUTOR's fact, not the step's: with no
        env set, a wrapper-supplied gpu_node_id must not reach the wire."""
        monkeypatch.delenv("LAZYAF_GPU_NODE_ID", raising=False)
        monkeypatch.delenv("LAZYAF_GPU_FRACTION", raising=False)
        monkeypatch.delenv("LAZYAF_ROLE", raising=False)
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert posted["gpu_node_id"] is None
        assert posted["gpu_fraction"] is None


class TestFallbackRecord:
    def test_script_step_with_no_manifest_still_posts_usage(
        self, run_main, posts_to
    ):
        """Every control-mode step produces a row from day one - a script
        step's `cost_source="unknown"` is the recorded fact that no provider
        reported anything, not a gap."""
        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        posts = usage_posts(session, posts_to)
        assert len(posts) == 1
        posted = posts[0]
        assert_manifest_conforms(posted, SHIPPER)
        assert posted["provider"] == "self-hosted"
        assert posted["cost_source"] == "unknown"
        assert posted["cost_usd"] is None
        assert posted["input_tokens"] is None
        assert isinstance(posted["wall_clock_ms"], int)
        assert posted["container_seconds"] is not None

    def test_missing_manifest_does_not_decorate_a_green_step(
        self, run_main, posts_to
    ):
        """Script steps NEVER write a manifest; warning about it on every
        green step would make the terminal status error meaningless."""
        exit_code, session, _ = run_main("true")

        assert exit_code == 0
        status = posts_to(session, "/status")[-1]
        assert status["status"] == "completed"
        assert not status.get("error")

    def test_fallback_provider_comes_from_container_env(
        self, run_main, posts_to, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_USAGE_PROVIDER", "anthropic")

        _exit, session, _ = run_main("true")

        assert usage_posts(session, posts_to)[0]["provider"] == "anthropic"

    def test_unknown_env_provider_falls_back_and_warns(
        self, run_main, posts_to, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_USAGE_PROVIDER", "acme-ai")

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert posted["provider"] == "self-hosted"  # never an invalid value
        assert_manifest_conforms(posted, SHIPPER)
        error = posts_to(session, "/status")[-1].get("error") or ""
        assert "acme-ai" in error  # loud, not silent


class TestMalformedManifestNeverFailsTheStep:
    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            json.dumps([1, 2, 3]),
            json.dumps("a string"),
            json.dumps({"version": 7, "provider": "anthropic"}),
            json.dumps({"version": 1, "provider": "acme", "cost_source": "??"}),
            json.dumps({"version": 1, "input_tokens": "many"}),
            json.dumps({"version": 1, "determinism": [], "raw": [1]}),
            json.dumps({"version": 1, "cost_usd": True}),
            json.dumps({"version": 1, "trial_iteration_id": "ti_1"}),
        ],
    )
    def test_garbage_still_yields_a_conforming_record_and_exit_zero(
        self, run_main, posts_to, tmp_path, body
    ):
        (tmp_path / "usage.exec-1.json").write_text(body)

        exit_code, session, _ = run_main("true")

        assert exit_code == 0  # telemetry NEVER fails a step
        posted = usage_posts(session, posts_to)[0]
        assert_manifest_conforms(posted, SHIPPER)
        assert not (tmp_path / "usage.exec-1.json").exists()
        # ... and the problem is LOUD
        assert posts_to(session, "/status")[-1].get("error")

    def test_float_dollars_are_stringified_not_forwarded(
        self, run_main, posts_to, tmp_path
    ):
        """api-surface 0: no floats for money, ever - not even from a
        hand-written manifest."""
        (tmp_path / "usage.exec-1.json").write_text(
            json.dumps({
                "version": 1, "provider": "anthropic",
                "cost_source": "cli-reported", "cost_usd": 0.1841,
            })
        )

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert isinstance(posted["cost_usd"], str)
        assert_manifest_conforms(posted, SHIPPER)

    def test_negative_token_counts_are_nulled_loudly(
        self, run_main, posts_to, tmp_path
    ):
        (tmp_path / "usage.exec-1.json").write_text(
            json.dumps({
                "version": 1, "provider": "anthropic",
                "cost_source": "unknown", "input_tokens": -5,
            })
        )

        _exit, session, _ = run_main("true")

        posted = usage_posts(session, posts_to)[0]
        assert posted["input_tokens"] is None
        assert "input_tokens" in (posts_to(session, "/status")[-1].get("error") or "")


class TestShipUsageIsTotallyDefensive:
    """`ship_usage` never raises, on ANY input, and always consumes."""

    class Exploding:
        def send_usage(self, manifest):
            raise RuntimeError("boom")

    def test_a_crashing_client_is_a_warning_not_an_exception(self, tmp_path):
        path = tmp_path / "usage.exec-1.json"
        path.write_text(json.dumps(WRAPPER_MANIFEST))

        warning = control_run.ship_usage(path, self.Exploding(), 10, 1.0)

        assert warning and "crashed" in warning
        assert not path.exists()

    def test_unreadable_file_is_a_warning_and_still_posts_the_fallback(
        self, tmp_path
    ):
        path = tmp_path / "usage.exec-1.json"
        path.write_text("{{{")
        sent = []

        class Client:
            def send_usage(self, manifest):
                sent.append(manifest)
                return 200

        warning = control_run.ship_usage(path, Client(), 10, 1.0)

        assert warning and "unreadable" in warning
        assert len(sent) == 1
        assert_manifest_conforms(sent[0], SHIPPER)
        assert not path.exists()

    def test_normalizer_never_raises_on_hostile_input(self):
        for hostile in (None, [], "x", 3, {"version": object()},
                        {"version": 1, "raw": object()}):
            manifest, _warnings = control_run.normalize_usage_manifest(
                hostile, 1, 1.0
            )
            assert_manifest_conforms(manifest, SHIPPER)

    def test_delivery_failure_is_loud_but_not_fatal(
        self, run_main, posts_to, tmp_path, selective_session_factory,
        run_main_with,
    ):
        selective_session_factory("/usage")
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        exit_code, _config = run_main_with("true")

        assert exit_code == 0  # the step still succeeded
        assert not (tmp_path / "usage.exec-1.json").exists()

    def test_409_drop_is_warned_and_the_exit_code_is_untouched(self, tmp_path):
        path = tmp_path / "usage.exec-1.json"
        path.write_text(json.dumps(WRAPPER_MANIFEST))

        class Terminal:
            def send_usage(self, manifest):
                return 409

        warning = control_run.ship_usage(path, Terminal(), 10, 1.0)

        assert warning and "409" in warning and "terminal" in warning
        assert not path.exists()

    def test_exhausted_budget_is_warned(self, tmp_path):
        path = tmp_path / "usage.exec-1.json"
        path.write_text(json.dumps(WRAPPER_MANIFEST))

        class Dead:
            def send_usage(self, manifest):
                return None

        warning = control_run.ship_usage(path, Dead(), 10, 1.0)

        assert warning and "backend" in warning
        assert not path.exists()


class TestTimeoutStillReportsUsage:
    def test_timed_out_step_posts_a_usage_row_and_a_timeout_status(
        self, run_main, posts_to, tmp_path
    ):
        """Risk-register case: a command that outlives its deadline is killed
        by the in-container watchdog, and the accounting still lands - the
        expensive steps are exactly the ones that time out."""
        exit_code, session, _ = run_main(
            "sleep 5", timeout_seconds=0.4
        )

        assert exit_code == 124
        status = posts_to(session, "/status")[-1]
        assert status["status"] == "timeout"
        posted = usage_posts(session, posts_to)
        assert len(posted) == 1
        assert_manifest_conforms(posted[0], SHIPPER)
        assert posted[0]["cost_source"] == "unknown"

    def test_usage_is_posted_before_the_terminal_status(
        self, run_main, tmp_path
    ):
        """Ordering matters: the endpoint 409s a terminal StepExecution."""
        (tmp_path / "usage.exec-1.json").write_text(json.dumps(WRAPPER_MANIFEST))

        _exit, session, _ = run_main("true")

        order = [url.rsplit("/", 1)[-1] for _m, url, _p in session.requests]
        assert "usage" in order
        assert order.index("usage") < len(order) - 1
        assert order[-1] == "status"
