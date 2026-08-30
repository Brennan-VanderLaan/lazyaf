"""
Tests for scripts/verify_executor.py - the 12.2-INT exit-gate ratchet.

The HTTP layer is stubbed by monkeypatching urllib.request.urlopen (R6:
the seam is the real stdlib call the script makes inside a step
container; no backend needed).
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify_executor.py"

_spec = importlib.util.spec_from_file_location("verify_executor", SCRIPT)
verify_executor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_executor)


class FakeResponse(io.BytesIO):
    """Minimal context-manager response wrapping a JSON payload."""

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def stub_backend(monkeypatch, run, pipeline, base="http://backend:8000"):
    """Monkeypatch urllib so the script sees the given run + pipeline."""
    routes = {
        f"{base}/api/pipeline-runs/{run['id']}": run,
        f"{base}/api/pipelines/{pipeline['id']}": pipeline,
    }
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected URL fetched: {url}")
        return FakeResponse(routes[url])

    monkeypatch.setattr(
        verify_executor.urllib.request, "urlopen", fake_urlopen
    )
    return calls


def make_run(step_runs, run_id="run-1", pipeline_id="pipe-1"):
    return {"id": run_id, "pipeline_id": pipeline_id, "step_runs": step_runs}


def make_pipeline(step_types, pipeline_id="pipe-1"):
    return {
        "id": pipeline_id,
        "steps": [{"type": t} for t in step_types],
    }


def step_run(index, executor, name=None, status="passed", logs="a log line\n"):
    return {
        "step_index": index,
        "step_name": name or f"step-{index}",
        "executor": executor,
        "status": status,
        "logs": logs,
    }


class TestVerifyRun:
    def test_all_local_passes(self, monkeypatch):
        run = make_run([step_run(0, "local"), step_run(1, "local")])
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s)" in msg

    def test_non_local_executor_fails(self, monkeypatch):
        run = make_run(
            [step_run(0, "local"), step_run(1, "legacy", name="tier2")]
        )
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "steps not executed by LocalExecutor" in str(exc.value)
        assert "tier2" in str(exc.value)
        assert "legacy" in str(exc.value)

    def test_vacuous_pass_is_failure(self, monkeypatch):
        """No script/docker step runs at all -> fail loudly (R4)."""
        run = make_run([])
        pipeline = make_pipeline([])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "vacuous pass = fail" in str(exc.value)

    def test_only_non_script_steps_is_vacuous_failure(self, monkeypatch):
        run = make_run([step_run(0, "legacy")])
        pipeline = make_pipeline(["agent"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "vacuous pass = fail" in str(exc.value)

    def test_non_script_steps_are_skipped_but_script_checked(self, monkeypatch):
        run = make_run(
            [step_run(0, "legacy"), step_run(1, "local"), step_run(2, "local")]
        )
        pipeline = make_pipeline(["agent", "script", "docker"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s)" in msg

    def test_missing_step_type_defaults_to_script(self, monkeypatch):
        run = make_run([step_run(0, "legacy")])
        pipeline = {"id": "pipe-1", "steps": [{}]}  # no "type" key
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "steps not executed by LocalExecutor" in str(exc.value)


class TestControlPathLogProbe:
    """12.3: a PASSED script step with empty logs = the control-layer
    reporting path (POST /api/steps/{id}/logs) silently failed."""

    def test_passed_step_with_empty_logs_fails(self, monkeypatch):
        run = make_run([
            step_run(0, "local"),
            step_run(1, "local", name="tier1", logs=""),
        ])
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)
        assert "tier1" in str(exc.value)

    def test_whitespace_only_logs_fail(self, monkeypatch):
        run = make_run([step_run(0, "local", logs="  \n")])
        pipeline = make_pipeline(["script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)

    def test_marker_only_logs_fail(self, monkeypatch):
        """Clobber-shaped case: the backend appended its own '[lazyaf] '
        marker line but the in-container runtime delivered NOTHING - the
        probe must not count backend-written markers as delivered logs."""
        run = make_run([
            step_run(0, "local", logs="[lazyaf] exit code: 0\n"),
        ])
        pipeline = make_pipeline(["script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)

    def test_multiple_marker_lines_and_whitespace_fail(self, monkeypatch):
        run = make_run([
            step_run(
                0,
                "local",
                logs="[lazyaf] exit code: 0\n  \n[lazyaf] something else\n",
            ),
        ])
        pipeline = make_pipeline(["script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)

    def test_real_logs_plus_marker_pass(self, monkeypatch):
        """A healthy control-mode step: runtime-delivered log lines plus
        the backend's trailing marker line."""
        run = make_run([
            step_run(0, "local", logs="hello\nworld\n[lazyaf] exit code: 0\n"),
        ])
        pipeline = make_pipeline(["script"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 1 script step run(s)" in msg

    def test_own_step_is_exempt_from_log_check(self, monkeypatch):
        """The verify step's own logs are still streaming - never
        self-fail on an empty own row."""
        run = make_run([
            step_run(0, "local"),
            step_run(1, "local", name="verify-executor", logs=""),
        ])
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run(
            "http://backend:8000", "run-1", self_index=1
        )
        assert "OK: 2 script step run(s)" in msg

    def test_non_terminal_step_not_log_checked(self, monkeypatch):
        """A still-running step legitimately has no logs committed yet."""
        run = make_run([
            step_run(0, "local"),
            step_run(1, "local", status="running", logs=""),
        ])
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s)" in msg

    def test_main_passes_own_index_from_env(self, monkeypatch, capsys):
        run = make_run([
            step_run(0, "local"),
            step_run(1, "local", name="me", logs=""),
        ], run_id="r42")
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "r42")
        monkeypatch.setenv("LAZYAF_STEP_INDEX", "1")
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        verify_executor.main()
        assert "OK: 2 script step run(s)" in capsys.readouterr().out


class TestMain:
    def test_missing_run_id_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("LAZYAF_PIPELINE_RUN_ID", raising=False)
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        with pytest.raises(SystemExit) as exc:
            verify_executor.main()
        assert "LAZYAF_PIPELINE_RUN_ID is not set" in str(exc.value)

    def test_env_contract_drives_urls(self, monkeypatch, capsys):
        base = "http://backend-e2e:8000"
        run = make_run([step_run(0, "local")], run_id="abc123")
        pipeline = make_pipeline(["script"])
        calls = stub_backend(monkeypatch, run, pipeline, base=base)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "abc123")
        monkeypatch.setenv("LAZYAF_BACKEND_URL", base)

        verify_executor.main()
        out = capsys.readouterr().out
        assert "OK: 1 script step run(s)" in out
        assert calls == [
            f"{base}/api/pipeline-runs/abc123",
            f"{base}/api/pipelines/pipe-1",
        ]

    def test_default_backend_url(self, monkeypatch, capsys):
        run = make_run([step_run(0, "local")], run_id="r9")
        pipeline = make_pipeline(["script"])
        calls = stub_backend(monkeypatch, run, pipeline)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "r9")
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        verify_executor.main()
        assert calls[0].startswith("http://backend:8000/")


class TestManifestDeliveryGate:
    """12.2.6 ratchet: manifest delivery is non-fatal to the STEP by design,
    so the GATE is the only place its silence gets broken. A dogfood run
    once shipped three manifests into 404s and still gated clean."""

    def _run(self, logs: str) -> dict:
        return {
            "pipeline_id": "p1",
            "step_runs": [
                {
                    "step_index": 0,
                    "step_name": "T1",
                    "executor": "local",
                    "status": "passed",
                    "logs": logs,
                }
            ],
        }

    def _pipeline(self) -> dict:
        return {"steps": [{"type": "script"}]}

    def test_manifest_delivery_failure_fails_the_gate(self, monkeypatch):
        import scripts.verify_executor as ve

        run = self._run(
            "real log line\n"
            "[control] WARNING: test results manifest failed to reach backend "
            "after 3 attempts\n"
        )
        monkeypatch.setattr(
            ve, "fetch_json",
            lambda base, path: run if "pipeline-runs" in path else self._pipeline(),
        )
        with pytest.raises(SystemExit) as exc:
            ve.verify_run("http://backend:8000", "r1")
        assert "tie-back is dark" in str(exc.value)

    def test_clean_run_passes_the_gate(self, monkeypatch):
        import scripts.verify_executor as ve

        run = self._run("real log line\n[lazyaf] exit code: 0\n")
        monkeypatch.setattr(
            ve, "fetch_json",
            lambda base, path: run if "pipeline-runs" in path else self._pipeline(),
        )
        assert "no manifest delivery problems" in ve.verify_run(
            "http://backend:8000", "r1"
        )
