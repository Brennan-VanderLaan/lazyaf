"""
Tests for scripts/verify_executor.py - the dogfood exit-gate ratchet.

The HTTP layer is stubbed by monkeypatching urllib.request.urlopen (R6:
the seam is the real stdlib call the script makes inside a step
container; no backend needed).

12.5 grew the gate four assertions - agent steps route local, the agent step
carries real token counts, EVERY passed step has a StepUsage row, and the
legacy runner queue is empty - so this module grew the fake backend to serve
the endpoints those read: the run rollup, the per-step usage read, and the
runner pool status.

12.6 grew it five more, and changed the shape of the oldest one. A step's
LANE is now derived from its pipeline DEFINITION (`requires:` -> remote,
otherwise local) rather than being the constant "local", so the fixtures
carry pipeline configs and the fake backend serves GET /api/runners.

Every assertion has a NEGATIVE test: a gate assertion nobody has watched
fail is a gate assertion that does not exist. For 12.6 that means, one test
each - a pinned step that ran local, a non-pinned step that ran remote, a
pipeline with no pinned step at all, an empty fleet, a fleet of tombstone
rows, a remote step with no runner_id, and a remote step naming a runner
nobody has heard of.
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


LOOPBACK_RUNNER_ID = "dogfood-loopback"


def step_run(
    index,
    executor,
    name=None,
    status="passed",
    logs="a log line\n",
    step_run_id=None,
    runner_id=None,
):
    """One StepRun as GET /api/pipeline-runs/{id} projects it.

    `runner_id` is the StepExecution's assignment (12.6 assertion 10). It is
    always emitted, null on the local lane, so a gate reading it cannot
    confuse "this projection has no such field" with "this step had no
    runner".
    """
    return {
        "id": step_run_id or f"sr-{index}",
        "step_index": index,
        "step_name": name or f"step-{index}",
        "executor": executor,
        "status": status,
        "logs": logs,
        "runner_id": runner_id,
    }


def make_run(step_runs, run_id="run-1", pipeline_id="pipe-1"):
    return {"id": run_id, "pipeline_id": pipeline_id, "step_runs": step_runs}


def make_pipeline(step_types, pipeline_id="pipe-1", requires=None):
    """A pipeline DEFINITION.

    `requires` maps a step index to a requirements dict. Its mere PRESENCE
    is what routes a step to the remote lane (12.6), so the gate re-derives
    the expected executor from exactly this - never from what happened.
    """
    requires = requires or {}
    steps = []
    for i, t in enumerate(step_types):
        step = {"type": t, "config": {}}
        if i in requires:
            step["config"]["requires"] = requires[i]
        steps.append(step)
    return {"id": pipeline_id, "steps": steps}


def runner_row(runner_id=LOOPBACK_RUNNER_ID, status="idle", connection="websocket"):
    """One row of GET /api/runners (the registry snapshot)."""
    return {
        "id": runner_id,
        "name": runner_id,
        "runner_type": "generic",
        "status": status,
        "labels": {"arch": "amd64", "has": ["docker", "remote-lane"]},
        "current_step_execution_id": None,
        "protocol_version": 1,
        "agent_version": "12.6",
        "connected_at": "2026-08-30T00:00:00Z",
        "last_heartbeat": "2026-08-30T00:00:10Z",
        "created_at": "2026-08-30T00:00:00Z",
        "connection": connection,
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
    runners=None,
):
    """Monkeypatch urllib so the script sees a coherent fake backend.

    `runners` is the GET /api/runners snapshot (12.6 assertion 9). The
    default is one live, socket-backed loopback runner - the dogfood shape -
    so a test that is not ABOUT the fleet does not have to describe one.
    Pass `[]` for the empty-fleet case, and rows with connection="none" for
    the tombstone case.
    """
    if rollup is None:
        rollup = derive_rollup(run, pipeline)
    if runners is None:
        runners = [runner_row()]

    routes = {
        f"{base}/api/pipeline-runs/{run['id']}": run,
        f"{base}/api/pipelines/{pipeline['id']}": pipeline,
        f"{base}/api/pipeline-runs/{run['id']}/usage": rollup,
        f"{base}/api/runners": runners,
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


#: The `requires:` block the dogfood remote lane pins on - a label only the
#: loopback runner-agent carries.
REMOTE_PIN = {"has": ["remote-lane"]}


def script_and_agent(
    *,
    agent_executor="remote",
    agent_status="passed",
    probe_executor="remote",
    probe_runner_id=LOOPBACK_RUNNER_ID,
    agent_runner_id=LOOPBACK_RUNNER_ID,
):
    """The 12.6 dogfood shape in miniature.

    Three steps, two lanes:
      0 tier1        script, NO `requires:`  -> local  (assertion 11)
      1 remote-probe script, `requires:`     -> remote (assertion 8)
      2 mock-agent   agent,  `requires:`     -> remote (assertion 12)

    The agent step is on the REMOTE lane because 12.6 moves it there: US-2
    then has continuous coverage on the remote path on every push, while
    tdd/e2e/test_us2_card_loop.py keeps covering it locally in T3.
    """
    run = make_run(
        [
            step_run(0, "local", name="tier1"),
            step_run(1, probe_executor, name="remote-probe", runner_id=probe_runner_id),
            step_run(
                2,
                agent_executor,
                name="mock-agent",
                status=agent_status,
                runner_id=agent_runner_id,
            ),
        ]
    )
    pipeline = make_pipeline(
        ["script", "script", "agent"], requires={1: REMOTE_PIN, 2: REMOTE_PIN}
    )
    return run, pipeline


# -----------------------------------------------------------------------------
# 12.2-INT: executor routing
# -----------------------------------------------------------------------------


class TestVerifyRun:
    def test_every_step_on_its_declared_lane_passes(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s) and 1 agent step run(s)" in msg
        assert "2 remote" in msg

    def test_an_off_lane_executor_fails(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "legacy", name="tier2"),
                step_run(2, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ]
        )
        pipeline = make_pipeline(
            ["script", "script", "agent"], requires={2: REMOTE_PIN}
        )
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)
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
                step_run(1, "remote", runner_id=LOOPBACK_RUNNER_ID),
                step_run(2, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(
            ["script", "docker", "agent"], requires={1: REMOTE_PIN}
        )
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s) and 1 agent step run(s)" in msg

    def test_missing_step_type_defaults_to_script(self, monkeypatch):
        run = make_run([step_run(0, "legacy")])
        pipeline = {"id": "pipe-1", "steps": [{}]}  # no "type" key, no config
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)

    def test_a_step_definition_without_a_config_key_routes_local(self, monkeypatch):
        """`requires:` lives under `config`, which older definitions omit.

        A missing config must mean "no pin" (local), never a crash - the
        gate reads pipeline definitions it did not write.
        """
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "remote", name="probe", runner_id=LOOPBACK_RUNNER_ID),
                step_run(2, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "script", "agent"], requires={1: REMOTE_PIN})
        del pipeline["steps"][0]["config"]
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


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
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ]
        )
        return run, make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})

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
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
                step_run(2, "local", name="verify-executor", logs=""),
            ]
        )
        pipeline = make_pipeline(
            ["script", "agent", "script"], requires={1: REMOTE_PIN}
        )
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
    """12.5: agent steps left the legacy queue, and the gate says so.
    12.6: they moved on again, to the remote lane, and the gate says that."""

    def test_agent_step_on_the_legacy_queue_fails(self, monkeypatch):
        run, pipeline = script_and_agent(agent_executor="legacy")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)
        assert "mock-agent" in str(exc.value)
        assert "(agent)" in str(exc.value)

    def test_a_pinned_agent_step_that_fell_back_to_local_fails(self, monkeypatch):
        """12.6 assertion 12's routing half.

        The mock-agent step carries `requires:` from this phase on. If the
        router quietly stopped honouring the pin, the step would still pass,
        still deliver logs and still report usage - it would simply have
        stopped covering the remote path, which is the one thing this step
        exists for.
        """
        run, pipeline = script_and_agent(agent_executor="local")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)
        assert "mock-agent" in str(exc.value)
        assert "expected 'remote'" in str(exc.value)

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
        assert "3 StepUsage row(s) incl. 1 agent row(s)" in msg

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
        """12.6 assertion 12's usage half: the channel had to cross a host
        boundary, and a remote agent step with no StepUsage row is exactly
        the regression that would prove it did not."""
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            rollup=derive_rollup(run, pipeline, missing=(2,)),
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
            rollup=derive_rollup(run, pipeline, tokenless=(2,)),
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
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
                step_run(2, "local", name="tier3", status="running", logs=""),
            ]
        )
        pipeline = make_pipeline(
            ["script", "agent", "script"], requires={1: REMOTE_PIN}
        )
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


class TestMain:
    @pytest.fixture(autouse=True)
    def _clear_injected_env(self, monkeypatch):
        """The runtime injects the whole LAZYAF_* contract into every step
        container, so these tests inherit REAL values when the suite runs
        inside the dogfood tier - LAZYAF_STEP_INDEX in particular made
        main() skip a step the fixture data does not have, and the tests
        passed on the host while failing in CI. Start from a clean
        contract and let each test declare exactly what it needs.
        """
        for name in (
            "LAZYAF_BACKEND_URL",
            "LAZYAF_PIPELINE_RUN_ID",
            "LAZYAF_STEP_INDEX",
            "LAZYAF_STEP_RUN_ID",
            "LAZYAF_EXECUTION_KEY",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_missing_run_id_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("LAZYAF_PIPELINE_RUN_ID", raising=False)
        monkeypatch.delenv("LAZYAF_BACKEND_URL", raising=False)

        with pytest.raises(SystemExit) as exc:
            verify_executor.main()
        assert "LAZYAF_PIPELINE_RUN_ID is not set" in str(exc.value)

    def test_env_contract_drives_urls(self, monkeypatch, capsys):
        base = "http://backend-e2e:8000"
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ],
            run_id="abc123",
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
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
        # The rollup is fetched ONCE and shared by the usage gate and the
        # remote-lane gate - a second read of the same rows would be the
        # gate drifting into two views of one fact.
        assert calls.count(f"{base}/api/pipeline-runs/abc123/usage") == 1
        # The registry snapshot is the LAST read: assertion 9 replaced 12.5's
        # `queued_jobs == 0` when the queue it read was deleted.
        assert calls[-1] == f"{base}/api/runners"

    def test_main_passes_own_index_from_env(self, monkeypatch, capsys):
        run = make_run(
            [
                step_run(0, "local"),
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
                step_run(2, "local", name="me", logs=""),
            ],
            run_id="r42",
        )
        pipeline = make_pipeline(
            ["script", "agent", "script"], requires={1: REMOTE_PIN}
        )
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
            [
                step_run(0, "local"),
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ],
            run_id="r9",
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
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
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ]
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "tie-back is dark" in str(exc.value)

    def test_clean_run_passes_the_gate(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local", name="T1", logs="real log line\n[lazyaf] exit code: 0\n"),
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ]
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
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
                    "remote",
                    name="mock-agent",
                    runner_id=LOOPBACK_RUNNER_ID,
                    logs=(
                        "[agent] agent=claude-code model=x\n"
                        "[agent] WARNING: usage scrape failed: no result "
                        "object in the claude CLI output\n"
                    ),
                ),
            ]
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
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


# -----------------------------------------------------------------------------
# 12.6: the remote lane (assertions 8-12)
# -----------------------------------------------------------------------------


class TestRemoteLaneRouting:
    """Assertions 8 and 11: a step runs on the lane its DEFINITION asks for.

    These are two halves of one rule and both directions are regressions.
    A pinned step that fell back to local means remote execution stopped
    working and nothing else in the gate would notice. A non-pinned step
    that ran remote means routing flipped globally, which would move the
    whole dogfood suite onto a single runner and look like success right up
    until that runner was absent.
    """

    def test_a_pinned_step_that_ran_local_fails(self, monkeypatch):
        run, pipeline = script_and_agent(probe_executor="local")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)
        assert "remote-probe" in str(exc.value)
        assert "expected 'remote'" in str(exc.value)
        assert "has `requires:` block" in str(exc.value)

    def test_an_unpinned_step_that_ran_remote_fails(self, monkeypatch):
        """Assertion 11, the direction nobody instinctively tests."""
        run, pipeline = script_and_agent()
        run["step_runs"][0]["executor"] = "remote"
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run on the lane" in str(exc.value)
        assert "tier1" in str(exc.value)
        assert "expected 'local'" in str(exc.value)
        assert "no `requires:` block" in str(exc.value)

    def test_a_pipeline_with_no_pinned_step_at_all_fails(self, monkeypatch):
        """The ratchet's own tombstone.

        Deleting the `remote-probe` step (or dropping its `requires:` block)
        from test-suite.yaml would leave a gate that passes over a system
        with no working remote execution at all - the exact fake-green a
        prior attempt shipped when its polling-removal test self-skipped.
        """
        run = make_run(
            [
                step_run(0, "local", name="tier1"),
                step_run(1, "local", name="mock-agent"),
            ]
        )
        pipeline = make_pipeline(["script", "agent"])
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "REMOTE LANE was not exercised" in str(exc.value)
        assert "vacuous pass = fail" in str(exc.value)
        assert "test-suite.yaml" in str(exc.value)

    def test_a_remote_step_with_empty_logs_still_fails_the_control_probe(
        self, monkeypatch
    ):
        """Assertion 8's second half.

        The whole claim of 12.6's channel split is that the step container
        keeps POSTing its own logs to /api/steps/{id}/logs, from whatever
        host it runs on. A passed remote step with only backend-written
        marker lines means that claim is false.
        """
        run, pipeline = script_and_agent()
        run["step_runs"][1]["logs"] = "[lazyaf] exit code: 0" + chr(10)
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "delivered no logs" in str(exc.value)
        assert "remote-probe" in str(exc.value)


class TestConnectedRunnerGate:
    """Assertion 9: at least one runner is alive AND socket-backed.

    This is the assertion that replaces 12.5's `queued_jobs == 0` when the
    job queue is deleted. Its shape is deliberately the inverse: 12.5
    asserted that a subsystem was IDLE, 12.6 asserts that a subsystem is
    ALIVE - and an empty fleet is the failure, never the pass.
    """

    def test_an_empty_fleet_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline, runners=[])

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "NO runners at all" in str(exc.value)
        assert "vacuous pass" in str(exc.value)

    def test_a_tombstone_row_does_not_count_as_a_runner(self, monkeypatch):
        """connection='none' is a row the registry holds no socket for.

        The DB alone cannot tell a live idle runner from one left behind by
        a crashed backend process - both rows say 'idle'. `connection` is
        stamped from the registry's live socket table for exactly this
        assertion, so a fleet of tombstones must fail.
        """
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            runners=[runner_row(status="idle", connection="none")],
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "alive and socket-backed" in str(exc.value)
        assert "TOMBSTONE" in str(exc.value)
        assert LOOPBACK_RUNNER_ID in str(exc.value)

    def test_a_dead_or_disconnected_runner_does_not_count(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            runners=[
                runner_row(runner_id="gone-1", status="dead"),
                runner_row(runner_id="gone-2", status="disconnected"),
            ],
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "alive and socket-backed" in str(exc.value)

    def test_a_busy_runner_counts_as_alive(self, monkeypatch):
        """The gate may run while another remote step is still executing."""
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch, run, pipeline, runners=[runner_row(status="busy")]
        )

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "1 socket-backed runner(s) live" in msg


class TestRemoteAssignmentGate:
    """Assertion 10: the remote step names a runner that actually exists.

    executor='remote' says which code path ran. It does not say a machine
    was ever involved: a RemoteExecutor that gave up with "no runner
    matched" writes the same value. The StepExecution's runner_id is the
    assignment CAS's own output, so reading it back and checking it against
    the registry snapshot is what closes that gap.
    """

    def test_a_remote_step_without_a_runner_id_fails(self, monkeypatch):
        run, pipeline = script_and_agent(probe_runner_id=None)
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "no StepExecution.runner_id" in str(exc.value)
        assert "remote-probe" in str(exc.value)

    def test_a_runner_id_the_registry_never_heard_of_fails(self, monkeypatch):
        """A stale assignment: the step names a runner that has since been
        forgotten, or was never enrolled at all."""
        run, pipeline = script_and_agent(probe_runner_id="ghost-runner")
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "never heard of" in str(exc.value)
        assert "ghost-runner" in str(exc.value)
        assert LOOPBACK_RUNNER_ID in str(exc.value)

    def test_a_local_step_needs_no_runner_id(self, monkeypatch):
        """Only the remote lane is checked - a local step legitimately has
        runner_id NULL, and requiring one would make the gate un-passable."""
        run, pipeline = script_and_agent()
        assert run["step_runs"][0]["runner_id"] is None

        stub_backend(monkeypatch, run, pipeline)
        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")

    def test_runner_id_is_read_from_the_usage_rollup_when_the_step_run_omits_it(
        self, monkeypatch
    ):
        """Whichever projection carries the field, the gate reads it there.

        The assignment is one fact; which read surface exposes it is an API
        detail the gate must not be brittle about.
        """
        run, pipeline = script_and_agent(
            probe_runner_id=None, agent_runner_id=None
        )
        for sr in run["step_runs"]:
            del sr["runner_id"]
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            if row["step_name"] in ("remote-probe", "mock-agent"):
                row["runner_id"] = LOOPBACK_RUNNER_ID

        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert LOOPBACK_RUNNER_ID in msg

    def test_the_ok_message_names_the_runners_that_did_the_work(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "remote steps assigned to" in msg
        assert LOOPBACK_RUNNER_ID in msg
