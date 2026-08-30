"""
Tests for scripts/verify_executor.py - the dogfood exit-gate ratchet.

The HTTP layer is stubbed by monkeypatching urllib.request.urlopen (R6:
the seam is the real stdlib call the script makes inside a step
container; no backend needed).

12.5 grew the gate four assertions - agent steps route local, the agent step
carries real token counts, EVERY passed step has a StepUsage row, and the
legacy runner queue is empty - so this module grew the fake backend to serve
the endpoints those read: the run rollup, the per-step usage read, and the
runner pool status. Every one of them has a NEGATIVE test: a gate assertion
nobody has watched fail is a gate assertion that does not exist.
"""
import importlib.util
import io
import json
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


# -----------------------------------------------------------------------------
# Fixture builders
# -----------------------------------------------------------------------------


def step_run(
    index,
    executor,
    name=None,
    status="passed",
    logs="a log line\n",
    step_run_id=None,
):
    return {
        "id": step_run_id or f"sr-{index}",
        "step_index": index,
        "step_name": name or f"step-{index}",
        "executor": executor,
        "status": status,
        "logs": logs,
    }


def make_run(step_runs, run_id="run-1", pipeline_id="pipe-1"):
    return {"id": run_id, "pipeline_id": pipeline_id, "step_runs": step_runs}


def make_pipeline(step_types, pipeline_id="pipe-1"):
    return {
        "id": pipeline_id,
        "steps": [{"type": t} for t in step_types],
    }


def usage_row(sr, *, tokens=True, cost_source="unknown"):
    """One rollup row for a StepRun."""
    return {
        "usage_id": f"u-{sr['id']}",
        "step_execution_id": f"se-{sr['id']}",
        "step_run_id": sr["id"],
        "step_index": sr["step_index"],
        "step_name": sr["step_name"],
        "provider": "self-hosted",
        "model": "mock",
        "role": None,
        "input_tokens": 42 if tokens else None,
        "output_tokens": 17 if tokens else None,
        "cost_usd": "0.000000",
        "cost_source": cost_source,
        "wall_clock_ms": 1234,
        "container_seconds": 2.0,
    }


def derive_rollup(run, pipeline, *, exclude=(), missing=(), tokenless=()):
    """A rollup covering every PASSED step of the run.

    `exclude` drops steps by index legitimately (the gate's own stdout-mode
    step); `missing` drops them ILLEGITIMATELY (the dark-channel case);
    `tokenless` keeps the row but nulls its token counts.
    """
    step_types = {i: s.get("type", "script") for i, s in enumerate(pipeline["steps"])}
    rows = []
    for sr in run["step_runs"]:
        if sr["step_index"] in exclude or sr["step_index"] in missing:
            continue
        if sr.get("status") != "passed":
            continue
        is_agent = step_types.get(sr["step_index"]) == "agent"
        rows.append(
            usage_row(
                sr,
                tokens=is_agent and sr["step_index"] not in tokenless,
                cost_source="cli-reported" if is_agent else "unknown",
            )
        )
    return {
        "pipeline_run_id": run["id"],
        "total_cost_usd": "0.000000",
        "cost_coverage": 1.0,
        "step_count": len(rows),
        "by_role": {},
        "by_source": {},
        "steps": rows,
    }


def stub_backend(
    monkeypatch,
    run,
    pipeline,
    base="http://backend:8000",
    rollup=None,
    queued_jobs=0,
):
    """Monkeypatch urllib so the script sees a coherent fake backend."""
    if rollup is None:
        rollup = derive_rollup(run, pipeline)

    routes = {
        f"{base}/api/pipeline-runs/{run['id']}": run,
        f"{base}/api/pipelines/{pipeline['id']}": pipeline,
        f"{base}/api/pipeline-runs/{run['id']}/usage": rollup,
        f"{base}/api/runners/status": {
            "total_runners": 1,
            "idle_runners": 1,
            "busy_runners": 0,
            "offline_runners": 0,
            "queued_jobs": queued_jobs,
            "pending_jobs": 0,
        },
    }
    for row in rollup["steps"]:
        routes[f"{base}/api/steps/{row['step_execution_id']}/usage"] = row

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


def script_and_agent(*, agent_executor="local", agent_status="passed"):
    """The dogfood shape in miniature: one script step and one agent step."""
    run = make_run(
        [
            step_run(0, "local", name="tier1"),
            step_run(1, agent_executor, name="mock-agent", status=agent_status),
        ]
    )
    pipeline = make_pipeline(["script", "agent"])
    return run, pipeline


# -----------------------------------------------------------------------------
# 12.2-INT: executor routing
# -----------------------------------------------------------------------------


class TestVerifyRun:
    def test_all_local_passes(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 1 script step run(s) and 1 agent step run(s)" in msg

    def test_non_local_executor_fails(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "legacy", name="tier2"),
                step_run(2, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "script", "agent"])
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

    def test_only_agent_steps_is_vacuous_failure(self, monkeypatch):
        run = make_run([step_run(0, "local")])
        pipeline = make_pipeline(["agent"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "vacuous pass = fail" in str(exc.value)

    def test_docker_steps_are_checked_alongside_script(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "local"),
                step_run(2, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "docker", "agent"])
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s) and 1 agent step run(s)" in msg

    def test_missing_step_type_defaults_to_script(self, monkeypatch):
        run = make_run([step_run(0, "legacy")])
        pipeline = {"id": "pipe-1", "steps": [{}]}  # no "type" key
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "steps not executed by LocalExecutor" in str(exc.value)


# -----------------------------------------------------------------------------
# 12.3: the control-path log probe
# -----------------------------------------------------------------------------


class TestControlPathLogProbe:
    """12.3: a PASSED script step with empty logs = the control-layer
    reporting path (POST /api/steps/{id}/logs) silently failed."""

    def _run(self, logs, name="tier1", status="passed"):
        run = make_run(
            [
                step_run(0, "local", name=name, status=status, logs=logs),
                step_run(1, "local", name="mock-agent"),
            ]
        )
        return run, make_pipeline(["script", "agent"])

    def test_passed_step_with_empty_logs_fails(self, monkeypatch):
        run, pipeline = self._run("")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)
        assert "tier1" in str(exc.value)

    def test_whitespace_only_logs_fail(self, monkeypatch):
        run, pipeline = self._run("  \n")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)

    def test_marker_only_logs_fail(self, monkeypatch):
        """Clobber-shaped case: the backend appended its own '[lazyaf] '
        marker line but the in-container runtime delivered NOTHING - the
        probe must not count backend-written markers as delivered logs."""
        run, pipeline = self._run("[lazyaf] exit code: 0\n")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)

    def test_real_logs_plus_marker_pass(self, monkeypatch):
        """A healthy control-mode step: runtime-delivered log lines plus
        the backend's trailing marker line."""
        run, pipeline = self._run("hello\nworld\n[lazyaf] exit code: 0\n")
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")

    def test_own_step_is_exempt_from_log_check(self, monkeypatch):
        """The verify step's own logs are still streaming - never
        self-fail on an empty own row."""
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "local", name="mock-agent"),
                step_run(2, "local", name="verify-executor", logs=""),
            ]
        )
        pipeline = make_pipeline(["script", "agent", "script"])
        stub_backend(
            monkeypatch, run, pipeline, rollup=derive_rollup(run, pipeline, exclude=(2,))
        )

        msg = verify_executor.verify_run(
            "http://backend:8000", "run-1", self_index=2
        )
        assert "OK: 2 script step run(s) and 1 agent step run(s)" in msg

    def test_non_terminal_step_not_log_checked(self, monkeypatch):
        """A still-running step legitimately has no logs committed yet."""
        run, pipeline = self._run("", status="running")
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


# -----------------------------------------------------------------------------
# 12.5: agent steps, the usage channel, runner idleness
# -----------------------------------------------------------------------------


class TestAgentStepRouting:
    """12.5: agent steps left the legacy queue, and the gate says so."""

    def test_agent_step_on_the_legacy_queue_fails(self, monkeypatch):
        run, pipeline = script_and_agent(agent_executor="legacy")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "steps not executed by LocalExecutor" in str(exc.value)
        assert "mock-agent" in str(exc.value)
        assert "(agent)" in str(exc.value)

    def test_pipeline_without_an_agent_step_fails(self, monkeypatch):
        """The ratchet only ratchets if its absence is loud (R4/R7).

        Deleting the mock-agent step from test-suite.yaml would otherwise
        leave a green gate that no longer covers the agent path at all.
        """
        run = make_run([step_run(0, "local"), step_run(1, "local")])
        pipeline = make_pipeline(["script", "script"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "no agent step runs found" in str(exc.value)


class TestUsageChannelGate:
    """12.5: telemetry never fails a STEP - which is why it must fail the GATE."""

    def test_complete_usage_passes(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "2 StepUsage row(s) incl. 1 agent row(s)" in msg

    def test_a_missing_row_for_a_script_step_fails(self, monkeypatch):
        """The dark-channel case: the agent reported, the script step did not."""
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            rollup=derive_rollup(run, pipeline, missing=(0,)),
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "usage channel dropped rows" in str(exc.value)
        assert "tier1" in str(exc.value)

    def test_a_missing_agent_row_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            rollup=derive_rollup(run, pipeline, missing=(1,)),
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "usage channel dropped rows" in str(exc.value)
        assert "mock-agent" in str(exc.value)

    def test_agent_row_without_token_counts_fails(self, monkeypatch):
        """A row is not enough: the numbers are the point (M13's cost axis)."""
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            rollup=derive_rollup(run, pipeline, tokenless=(1,)),
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "empty of numbers" in str(exc.value)
        assert "input_tokens is null" in str(exc.value)

    def test_unknown_cost_source_on_a_script_step_is_accepted(self, monkeypatch):
        """'the provider told us nothing' is a recorded fact, not a gap.

        Script steps have no CLI to report dollars; run.py posts them the
        fallback record. Failing the gate on that would make the ratchet
        un-passable by design.
        """
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        assert rollup["steps"][0]["cost_source"] == "unknown"
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")

    def test_running_steps_are_not_required_to_have_usage_yet(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local", name="tier1"),
                step_run(1, "local", name="mock-agent"),
                step_run(2, "local", name="tier3", status="running", logs=""),
            ]
        )
        pipeline = make_pipeline(["script", "agent", "script"])
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


class TestRunnerIdlenessGate:
    """12.5: no default path enqueues, and idleness is asserted (R1)."""

    def test_idle_queue_passes(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline, queued_jobs=0)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "runner queue idle (queued_jobs=0)" in msg

    def test_a_queued_job_fails_the_gate(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline, queued_jobs=3)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "3 job(s) are sitting in the legacy runner queue" in str(exc.value)


class TestMain:
    def test_missing_run_id_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("LAZYAF_PIPELINE_RUN_ID", raising=False)
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        with pytest.raises(SystemExit) as exc:
            verify_executor.main()
        assert "LAZYAF_PIPELINE_RUN_ID is not set" in str(exc.value)

    def test_env_contract_drives_urls(self, monkeypatch, capsys):
        base = "http://backend-e2e:8000"
        run = make_run(
            [step_run(0, "local"), step_run(1, "local", name="mock-agent")],
            run_id="abc123",
        )
        pipeline = make_pipeline(["script", "agent"])
        calls = stub_backend(monkeypatch, run, pipeline, base=base)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "abc123")
        monkeypatch.setenv("LAZYAF_BACKEND_URL", base)

        verify_executor.main()
        out = capsys.readouterr().out
        assert "OK: 1 script step run(s) and 1 agent step run(s)" in out
        assert calls[:3] == [
            f"{base}/api/pipeline-runs/abc123",
            f"{base}/api/pipelines/pipe-1",
            f"{base}/api/pipeline-runs/abc123/usage",
        ]
        assert calls[-1] == f"{base}/api/runners/status"

    def test_main_passes_own_index_from_env(self, monkeypatch, capsys):
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "local", name="mock-agent"),
                step_run(2, "local", name="me", logs=""),
            ],
            run_id="r42",
        )
        pipeline = make_pipeline(["script", "agent", "script"])
        stub_backend(
            monkeypatch, run, pipeline, rollup=derive_rollup(run, pipeline, exclude=(2,))
        )

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "r42")
        monkeypatch.setenv("LAZYAF_STEP_INDEX", "2")
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        verify_executor.main()
        assert "OK: 2 script step run(s)" in capsys.readouterr().out

    def test_default_backend_url(self, monkeypatch, capsys):
        run = make_run(
            [step_run(0, "local"), step_run(1, "local", name="mock-agent")],
            run_id="r9",
        )
        pipeline = make_pipeline(["script", "agent"])
        calls = stub_backend(monkeypatch, run, pipeline)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "r9")
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        verify_executor.main()
        assert calls[0].startswith("http://backend:8000/")


class TestManifestDeliveryGate:
    """12.2.6 ratchet: manifest delivery is non-fatal to the STEP by design,
    so the GATE is the only place its silence gets broken. A dogfood run
    once shipped three manifests into 404s and still gated clean."""

    def test_manifest_delivery_failure_fails_the_gate(self, monkeypatch):
        run = make_run(
            [
                step_run(
                    0,
                    "local",
                    name="T1",
                    logs=(
                        "real log line\n"
                        "[control] WARNING: test results manifest failed to "
                        "reach backend after 3 attempts\n"
                    ),
                ),
                step_run(1, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "agent"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "tie-back is dark" in str(exc.value)

    def test_clean_run_passes_the_gate(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local", name="T1", logs="real log line\n[lazyaf] exit code: 0\n"),
                step_run(1, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "agent"])
        stub_backend(monkeypatch, run, pipeline)

        assert "no manifest delivery problems" in verify_executor.verify_run(
            "http://backend:8000", "run-1"
        )


class TestUsageScrapeFailureGate:
    """12.5 F3.1: 'the provider reported nothing' and 'we could not read
    what the provider reported' are DIFFERENT facts.

    The second one means a vendor changed its CLI output, which silently
    records every future step of that agent at zero cost. It never fails a
    step (telemetry must not), so the gate is the only place it can surface.
    Both of the wrapper's signals get a negative test: a gate assertion
    nobody has watched fail is a gate assertion that does not exist.
    """

    def _scraped_rollup(self, run, pipeline, reason="the CLI printed prose"):
        """The usual rollup, with the agent row's stored `raw` stamped as a
        scrape failure - exactly what the wrapper writes through run.py."""
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            if row["step_name"] == "mock-agent":
                row["raw"] = {"_scrape_failed": True, "_scrape_error": reason}
        return rollup

    def test_a_scrape_failure_marker_in_the_stored_row_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            rollup=self._scraped_rollup(run, pipeline),
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "SCRAPE FAILURE" in str(exc.value)
        assert "the CLI printed prose" in str(exc.value)
        assert "mock-agent" in str(exc.value)

    def test_the_wrappers_log_marker_fails_the_gate(self, monkeypatch):
        """The other signal: the wrapper announces it on the step's log
        stream, so a run whose usage POST never landed is still caught."""
        run = make_run(
            [
                step_run(0, "local", name="tier1"),
                step_run(
                    1,
                    "local",
                    name="mock-agent",
                    logs=(
                        "[agent] agent=claude-code model=x\n"
                        "[agent] WARNING: usage scrape failed: no result "
                        "object in the claude CLI output\n"
                    ),
                ),
            ]
        )
        pipeline = make_pipeline(["script", "agent"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "VENDOR OUTPUT CHANGE" in str(exc.value)
        assert "mock-agent" in str(exc.value)

    def test_a_plain_unknown_cost_source_is_still_accepted(self, monkeypatch):
        """The whole point of the distinction: an honest 'nobody told us a
        price' row must keep passing, or the ratchet is un-passable."""
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        rollup["steps"][0]["cost_source"] = "unknown"
        rollup["steps"][0]["raw"] = {"tokens_only": True}
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        assert "no scrape failures" in verify_executor.verify_run(
            "http://backend:8000", "run-1"
        )

    def test_a_null_raw_is_not_a_scrape_failure(self, monkeypatch):
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            row["raw"] = None
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")
