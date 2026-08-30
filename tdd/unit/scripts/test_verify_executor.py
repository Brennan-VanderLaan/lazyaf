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


def make_pipeline(step_types, pipeline_id="pipe-1", requires=None, configs=None):
    """A pipeline DEFINITION.

    `requires` maps a step index to a requirements dict. Its mere PRESENCE
    is what routes a step to the remote lane (12.6), so the gate re-derives
    the expected executor from exactly this - never from what happened.

    `configs` maps a step index to extra step-config keys (14.x: `agent`,
    `endpoint`, `harness`), for the same reason: the harness lane is derived
    from the DEFINITION, never from the outcome.
    """
    requires = requires or {}
    configs = configs or {}
    steps = []
    for i, t in enumerate(step_types):
        step = {"type": t, "config": dict(configs.get(i) or {})}
        if i in requires:
            step["config"]["requires"] = requires[i]
        step["name"] = f"step-{i}"
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


def usage_row(
    sr,
    *,
    tokens=True,
    cost_source="unknown",
    provider="self-hosted",
    input_tokens=42,
    output_tokens=17,
    cost_usd="0.000000",
    raw=None,
):
    """One rollup row for a StepRun (and, with `raw`, the per-step detail)."""
    row = {
        "usage_id": f"u-{sr['id']}",
        "step_execution_id": f"se-{sr['id']}",
        "step_run_id": sr["id"],
        "step_index": sr["step_index"],
        "step_name": sr["step_name"],
        "provider": provider,
        "model": "mock",
        "role": None,
        "input_tokens": input_tokens if tokens else None,
        "output_tokens": output_tokens if tokens else None,
        "cost_usd": cost_usd,
        "cost_source": cost_source,
        "wall_clock_ms": 1234,
        "container_seconds": 2.0,
    }
    if raw is not None:
        row["raw"] = raw
    return row


# -----------------------------------------------------------------------------
# 14.x: the self-hosted harness lane (assertions 13-18)
# -----------------------------------------------------------------------------

#: How many turns the mock endpoint's happy script takes.
HARNESS_TURNS = 6
TOOLS_ENDPOINT = "dogfood-mock"
TEXT_ENDPOINT = "dogfood-mock-notools"


def summed_tokens(turns=HARNESS_TURNS):
    """What a CORRECT accumulator reports over `turns` mock turns."""
    triangular = turns * (turns + 1) // 2
    return (
        verify_executor.MOCK_PROMPT_TOKENS_PER_TURN * triangular,
        verify_executor.MOCK_COMPLETION_TOKENS_PER_TURN * triangular,
    )


def last_turn_tokens(turns=HARNESS_TURNS):
    """What a LAST-RESPONSE-WINS bug reports - the number assertion 13 rejects."""
    return (
        verify_executor.MOCK_PROMPT_TOKENS_PER_TURN * turns,
        verify_executor.MOCK_COMPLETION_TOKENS_PER_TURN * turns,
    )


def harness_config(endpoint=TOOLS_ENDPOINT, mode=None, model=None):
    """The step-config shape a dogfood harness step carries."""
    config = {"agent": "openai-harness", "commit": False}
    if model is not None:
        config["model"] = model
    else:
        config["endpoint"] = endpoint
    if mode is not None:
        config["harness"] = {"mode": mode, "max_iterations": 8}
    return config


def harness_raw(mode="tools", turns=HARNESS_TURNS, **overrides):
    record = {
        "endpoint_name": TOOLS_ENDPOINT if mode == "tools" else TEXT_ENDPOINT,
        "endpoint_reach": "direct",
        "mode": mode,
        "turns": turns,
        "turns_without_usage": 0,
        "stop_reason": "finish",
        "finish_status": "success",
        "tool_calls": {"finish": 1},
        "tool_errors": 0,
        "malformed_responses": 0,
        "probe_drift": False,
    }
    record.update(overrides)
    return {"harness": record}


def harness_usage_row(sr, *, mode="tools", turns=HARNESS_TURNS, **overrides):
    tokens_in, tokens_out = summed_tokens(turns)
    kwargs = {
        "provider": "openai-compatible",
        "cost_source": "gpu-node",
        "cost_usd": "0.000042",
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "raw": harness_raw(mode=mode, turns=turns),
    }
    kwargs.update(overrides)
    return usage_row(sr, **kwargs)


def endpoint_row(name, *, probe_status="ok", probe_age_seconds=120.0):
    """One row of GET /api/model-endpoints, capability snapshot included."""
    return {
        "id": f"ep-{name}",
        "name": name,
        "base_url": f"http://mock-endpoint:8099/{name}/v1",
        "model": "mock-model",
        "reach": "direct",
        "enabled": True,
        "probe_status": probe_status,
        "probe_age_seconds": probe_age_seconds,
        "capabilities": {
            "supports_tools": probe_status == "ok",
            "supports_streaming": True,
            "reports_usage": True,
            "context_window": 32768,
            "probe_status": probe_status,
            "probe_age_seconds": probe_age_seconds,
            "stale": False,
        },
    }


def default_endpoints():
    return [
        endpoint_row(TOOLS_ENDPOINT, probe_status="ok"),
        endpoint_row(TEXT_ENDPOINT, probe_status="degraded"),
    ]


def append_harness(run, pipeline, *, statuses=("passed", "passed"), configs=None):
    """Bolt the two 14.x dogfood harness steps onto any run/pipeline pair.

    Assertions 13-18 REFUSE a pipeline with no `agent: openai-harness` step
    (vacuous pass = fail, R4), so every fixture that drives verify_run end to
    end has to carry them. A test about a 12.x assertion should not have to
    describe the 14.x lane, so it gets appended in one line instead.
    """
    if configs is None:
        configs = [
            harness_config(TOOLS_ENDPOINT),
            harness_config(TEXT_ENDPOINT, mode="text"),
        ]
    base = len(pipeline["steps"])
    for offset, config in enumerate(configs):
        index = base + offset
        name = f"harness-{offset}"
        pipeline["steps"].append({"type": "agent", "name": name, "config": config})
        run["step_runs"].append(
            step_run(index, "local", name=name, status=statuses[offset])
        )
    return run, pipeline


def derive_rollup(run, pipeline, *, exclude=(), missing=(), tokenless=()):
    """A rollup covering every PASSED step of the run.

    `exclude` drops steps by index legitimately (the gate's own stdout-mode
    step); `missing` drops them ILLEGITIMATELY (the dark-channel case);
    `tokenless` keeps the row but nulls its token counts.
    """
    step_types = {i: s.get("type", "script") for i, s in enumerate(pipeline["steps"])}
    harness_modes = {
        i: verify_executor.step_harness_mode(s)
        for i, s in enumerate(pipeline["steps"])
        if (s.get("config") or {}).get("agent") == "openai-harness"
    }
    rows = []
    for sr in run["step_runs"]:
        if sr["step_index"] in exclude or sr["step_index"] in missing:
            continue
        if sr.get("status") != "passed":
            continue
        index = sr["step_index"]
        if index in harness_modes:
            rows.append(
                harness_usage_row(
                    sr,
                    mode="text" if harness_modes[index] == "text" else "tools",
                )
            )
            continue
        is_agent = step_types.get(index) == "agent"
        rows.append(
            usage_row(
                sr,
                tokens=is_agent and index not in tokenless,
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
    endpoints=None,
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
    if endpoints is None:
        endpoints = default_endpoints()

    routes = {
        f"{base}/api/pipeline-runs/{run['id']}": run,
        f"{base}/api/pipelines/{pipeline['id']}": pipeline,
        f"{base}/api/pipeline-runs/{run['id']}/usage": rollup,
        f"{base}/api/runners": runners,
        f"{base}/api/model-endpoints": endpoints,
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
    harness_statuses=("passed", "passed"),
    harness_configs=None,
):
    """The 12.6 + 14.x dogfood shape in miniature.

    Five steps, two lanes, three agents:
      0 tier1                 script, NO `requires:` -> local  (assertion 11)
      1 remote-probe          script, `requires:`    -> remote (assertion 8)
      2 mock-agent            agent,  `requires:`    -> remote (assertion 12)
      3 harness-probe         agent (openai-harness, tools)    (13-15, 17, 18)
      4 harness-probe-notools agent (openai-harness, text)     (16)

    The two harness steps route LOCAL: they carry no `requires:`, and a
    `direct` endpoint must not flip a step to the remote lane on its own
    (that is the regression assertion 11 exists to catch, and 14.x keeps it
    true).
    """
    if harness_configs is None:
        harness_configs = {
            3: harness_config(TOOLS_ENDPOINT),
            4: harness_config(TEXT_ENDPOINT, mode="text"),
        }
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
            step_run(3, "local", name="harness-probe", status=harness_statuses[0]),
            step_run(
                4, "local", name="harness-probe-notools", status=harness_statuses[1]
            ),
        ]
    )
    pipeline = make_pipeline(
        ["script", "script", "agent", "agent", "agent"],
        requires={1: REMOTE_PIN, 2: REMOTE_PIN},
        configs=harness_configs,
    )
    # The DEFINITION carries the same names the run does, because the gate
    # reports a failing step by the name its definition gives it.
    for step, sr in zip(pipeline["steps"], run["step_runs"]):
        step["name"] = sr["step_name"]
    return run, pipeline


# -----------------------------------------------------------------------------
# 12.2-INT: executor routing
# -----------------------------------------------------------------------------


class TestVerifyRun:
    def test_every_step_on_its_declared_lane_passes(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s) and 3 agent step run(s)" in msg
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
        append_harness(run, pipeline)
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "OK: 2 script step run(s) and 3 agent step run(s)" in msg

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
        append_harness(run, pipeline)
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
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
        return append_harness(run, pipeline)

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
        append_harness(run, pipeline)
        stub_backend(
            monkeypatch, run, pipeline, rollup=derive_rollup(run, pipeline, exclude=(2,))
        )

        msg = verify_executor.verify_run(
            "http://backend:8000", "run-1", self_index=2
        )
        assert "OK: 2 script step run(s) and 3 agent step run(s)" in msg

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
        assert "5 StepUsage row(s) incl. 3 agent row(s)" in msg

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
        append_harness(run, pipeline)
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
        append_harness(run, pipeline)
        calls = stub_backend(monkeypatch, run, pipeline, base=base)

        monkeypatch.setenv("LAZYAF_PIPELINE_RUN_ID", "abc123")
        monkeypatch.setenv("LAZYAF_BACKEND_URL", base)

        verify_executor.main()
        out = capsys.readouterr().out
        assert "OK: 1 script step run(s) and 3 agent step run(s)" in out
        assert calls[:3] == [
            f"{base}/api/pipeline-runs/abc123",
            f"{base}/api/pipelines/pipe-1",
            f"{base}/api/pipeline-runs/abc123/usage",
        ]
        # The rollup is fetched ONCE and shared by the usage gate and the
        # remote-lane gate - a second read of the same rows would be the
        # gate drifting into two views of one fact.
        assert calls.count(f"{base}/api/pipeline-runs/abc123/usage") == 1
        # The registry snapshot precedes the endpoint registry: assertion 9
        # replaced 12.5's `queued_jobs == 0` when the queue it read was
        # deleted, and 14.x assertion 17 reads the endpoint registry last.
        assert f"{base}/api/runners" in calls
        assert calls[-1] == f"{base}/api/model-endpoints"

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
        append_harness(run, pipeline)
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
        append_harness(run, pipeline)
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
        append_harness(run, pipeline)
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
            if row.get("provider") != "openai-compatible":
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


# -----------------------------------------------------------------------------
# 14.x: the self-hosted harness lane (assertions 13-18)
#
# Every one of the six has a NEGATIVE case here, because an assertion nobody
# has watched fail is an assertion that does not exist. Assertion 13 in
# particular is the only alarm on the token accumulator: a harness that kept
# the LAST turn instead of summing every turn would under-report every
# self-hosted step forever, cost nothing, fail nothing, and quietly destroy
# M13's cost axis.
# -----------------------------------------------------------------------------


def _rollup_with(run, pipeline, mutate):
    rollup = derive_rollup(run, pipeline)
    for row in rollup["steps"]:
        if row.get("provider") == "openai-compatible":
            mutate(row)
    return rollup


class TestHarnessTokenSummation:
    """Assertion 13."""

    def test_summed_tokens_pass(self, monkeypatch):
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        harness_rows = [
            r for r in rollup["steps"] if r["provider"] == "openai-compatible"
        ]
        assert len(harness_rows) == 2
        assert harness_rows[0]["input_tokens"] == summed_tokens()[0]
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "summed their tokens across turns" in msg

    def test_last_response_wins_fails(self, monkeypatch):
        """THE bug this assertion exists for: the accumulator REPLACED each
        turn's usage instead of adding it, so the row equals the biggest
        single turn rather than the sum of all of them."""
        run, pipeline = script_and_agent()

        def keep_last(row):
            row["input_tokens"], row["output_tokens"] = last_turn_tokens()

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, keep_last)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "NOT summed across" in str(exc.value)
        assert str(summed_tokens()[0]) in str(exc.value)

    def test_tokens_just_below_the_sum_still_fail(self, monkeypatch):
        """Off-by-one on the boundary: the check is STRICTLY greater than the
        largest single turn, so a row equal to it is a failure."""
        run, pipeline = script_and_agent()

        def boundary(row):
            row["input_tokens"] = last_turn_tokens()[0]
            row["output_tokens"] = summed_tokens()[1]

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, boundary)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "NOT summed across" in str(exc.value)

    def test_a_single_turn_run_is_refused_as_degenerate(self, monkeypatch):
        """With one turn, summed and last-response are the same number - the
        inequality proves nothing, so the gate refuses the run rather than
        passing on evidence that cannot discriminate."""
        run, pipeline = script_and_agent()

        def one_turn(row):
            row["raw"] = harness_raw(turns=1)
            row["input_tokens"], row["output_tokens"] = summed_tokens(1)

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, one_turn)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "at least 2 turns" in str(exc.value)

    def test_null_tokens_fail(self, monkeypatch):
        run, pipeline = script_and_agent()

        def blank(row):
            row["input_tokens"] = None
            row["output_tokens"] = None

        rollup = _rollup_with(run, pipeline, blank)
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        # The 12.5 usage gate catches this FIRST, which is correct - "the
        # agent step's usage row is empty of numbers" is the more general
        # failure and it fires for every agent step, harness or not.
        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "empty of numbers" in str(exc.value)
        assert "harness-probe" in str(exc.value)

        # Assertion 13's OWN null-token branch, asserted directly, so the
        # more general gate does not shadow it into never being exercised.
        with pytest.raises(SystemExit) as exc2:
            verify_executor.verify_harness_lane(
                "http://backend:8000", run, pipeline, rollup
            )
        assert "must have both" in str(exc2.value)

    def test_a_wrong_provider_fails(self, monkeypatch):
        """A harness step billed as `anthropic` is a step that ran somewhere
        other than where its definition says."""
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            if row.get("provider") == "openai-compatible":
                row["provider"] = "anthropic"
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "expected 'openai-compatible'" in str(exc.value)


class TestHarnessNodePricing:
    """Assertion 14: the gpu-node pricing branch runs on every push."""

    def test_unpriced_endpoint_fails(self, monkeypatch):
        run, pipeline = script_and_agent()

        def unpriced(row):
            row["cost_source"] = "unknown"
            row["cost_usd"] = None

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, unpriced)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "expected 'gpu-node'" in str(exc.value)

    def test_gpu_node_with_a_null_cost_fails(self, monkeypatch):
        run, pipeline = script_and_agent()

        def priceless(row):
            row["cost_usd"] = None

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, priceless)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "priced source with no price" in str(exc.value)

    def test_a_zero_rate_is_a_real_price(self, monkeypatch):
        """`0.000000` is owned hardware with no marginal cash cost - a REAL
        cost figure, not an absence. It must pass."""
        run, pipeline = script_and_agent()

        def owned(row):
            row["cost_usd"] = "0.000000"

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, owned)
        )

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


class TestHarnessScrapeMarker:
    """Assertion 15."""

    def test_the_scrape_marker_on_a_harness_step_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        for sr in run["step_runs"]:
            if sr["step_name"] == "harness-probe":
                sr["logs"] = (
                    "[agent] turn 1/8\n"
                    + verify_executor.SCRAPE_FAILED_LOG_MARKER
                    + ": endpoint reported no usage block in any of 6 turns\n"
                )
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "usage scrape" in str(exc.value)


class TestHarnessFallbackLane:
    """Assertion 16: the no-tools fallback protocol runs on every push."""

    def test_forced_text_step_recorded_text_mode(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        msg = verify_executor.verify_run("http://backend:8000", "run-1")
        assert "forced-text: harness-probe-notools" in msg

    def test_a_text_pinned_step_that_ran_tools_mode_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            if row["step_name"] == "harness-probe-notools":
                row["raw"] = harness_raw(mode="tools")
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "did not run the no-tools fallback protocol" in str(exc.value)

    def test_a_missing_malformed_responses_key_fails(self, monkeypatch):
        """0 is a fine value. The KEY missing means the fallback parser never
        accounted for itself, which is how that path rots unnoticed."""
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        for row in rollup["steps"]:
            if row["step_name"] == "harness-probe-notools":
                raw = harness_raw(mode="text")
                del raw["harness"]["malformed_responses"]
                row["raw"] = raw
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "malformed_responses" in str(exc.value)

    def test_a_pipeline_with_no_forced_text_step_fails(self, monkeypatch):
        run, pipeline = script_and_agent(
            harness_configs={
                3: harness_config(TOOLS_ENDPOINT),
                4: harness_config(TOOLS_ENDPOINT, mode="tools"),
            }
        )
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "no step of this pipeline pins" in str(exc.value)


class TestHarnessEndpointRegistry:
    """Assertion 17."""

    def test_a_missing_endpoint_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            endpoints=[endpoint_row(TEXT_ENDPOINT, probe_status="degraded")],
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert TOOLS_ENDPOINT in str(exc.value)
        assert "does not report it" in str(exc.value)

    def test_an_unprobed_endpoint_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            endpoints=[
                endpoint_row(TOOLS_ENDPOINT, probe_status="unprobed"),
                endpoint_row(TEXT_ENDPOINT, probe_status="degraded"),
            ],
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "probe_status" in str(exc.value)

    def test_a_stale_capability_record_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        stub_backend(
            monkeypatch,
            run,
            pipeline,
            endpoints=[
                endpoint_row(
                    TOOLS_ENDPOINT,
                    probe_status="ok",
                    probe_age_seconds=verify_executor.PROBE_TTL_SECONDS + 1,
                ),
                endpoint_row(TEXT_ENDPOINT, probe_status="degraded"),
            ],
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "probe_age_seconds" in str(exc.value)

    def test_degraded_is_acceptable_for_the_no_tools_endpoint(self, monkeypatch):
        """`degraded` is USABLE - it is what routes the fallback protocol.
        Only the tools-mode endpoint has to be `ok`."""
        run, pipeline = script_and_agent()
        stub_backend(monkeypatch, run, pipeline)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")

    def test_the_endpoint_model_sugar_resolves(self, monkeypatch):
        """`model: "endpoint:<name>"` is the ONE sugar spelling every model
        picker emits, and the gate has to read it the same way the backend
        resolver does - no second parser."""
        run, pipeline = script_and_agent(
            harness_configs={
                3: harness_config(model=f"endpoint:{TOOLS_ENDPOINT}"),
                4: harness_config(TEXT_ENDPOINT, mode="text"),
            }
        )
        stub_backend(monkeypatch, run, pipeline)

        assert "2 probed endpoint(s)" in verify_executor.verify_run(
            "http://backend:8000", "run-1"
        )


class TestHarnessCostSourceInvariant:
    """Assertion 18: a self-hosted row may never claim the provider billed us."""

    def test_cli_reported_on_an_openai_compatible_row_fails(self, monkeypatch):
        run, pipeline = script_and_agent()

        def liar(row):
            row["cost_source"] = "cli-reported"

        stub_backend(
            monkeypatch, run, pipeline, rollup=_rollup_with(run, pipeline, liar)
        )

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "cli-reported" in str(exc.value)
        assert "no self-hosted endpoint can make that claim" in str(exc.value)

    def test_cli_reported_on_a_real_cli_row_is_fine(self, monkeypatch):
        """The mock-agent step legitimately carries `cli-reported` - the
        invariant is scoped to the openai-compatible provider, not global."""
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        agent_row = next(r for r in rollup["steps"] if r["step_name"] == "mock-agent")
        assert agent_row["cost_source"] == "cli-reported"
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        assert "OK:" in verify_executor.verify_run("http://backend:8000", "run-1")


class TestHarnessLaneVacuousPass:
    """A gate that passes when the lane is absent is not a gate."""

    def test_a_pipeline_with_no_harness_step_fails(self, monkeypatch):
        run = make_run(
            [
                step_run(0, "local", name="tier1"),
                step_run(1, "remote", name="mock-agent", runner_id=LOOPBACK_RUNNER_ID),
            ]
        )
        pipeline = make_pipeline(["script", "agent"], requires={1: REMOTE_PIN})
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "no `agent: openai-harness` step" in str(exc.value)

    def test_a_harness_step_that_did_not_pass_fails(self, monkeypatch):
        run, pipeline = script_and_agent(harness_statuses=("failed", "passed"))
        stub_backend(monkeypatch, run, pipeline)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        assert "expected 'passed'" in str(exc.value)

    def test_a_harness_step_with_no_usage_row_fails(self, monkeypatch):
        run, pipeline = script_and_agent()
        rollup = derive_rollup(run, pipeline)
        rollup["steps"] = [
            r for r in rollup["steps"] if r["step_name"] != "harness-probe"
        ]
        stub_backend(monkeypatch, run, pipeline, rollup=rollup)

        with pytest.raises(SystemExit) as exc:
            verify_executor.verify_run("http://backend:8000", "run-1")
        # The 12.5 gate catches the missing row first, which is correct - it
        # is the more general failure. Either message names the step.
        assert "harness-probe" in str(exc.value)


class TestMockTokenConstantsDoNotDrift:
    """R3: verify_executor is stdlib-only and runs in a bare step container,
    so it carries a COPY of the mock endpoint's per-turn token law. This is
    the test that imports both and refuses to let the copies diverge - without
    it, a change to the mock would silently turn assertion 13 into a
    tautology."""

    def test_constants_match_the_mock_server(self):
        from tdd.shared.mock_openai import (
            MOCK_COMPLETION_TOKENS_PER_TURN,
            MOCK_PROMPT_TOKENS_PER_TURN,
        )

        assert (
            verify_executor.MOCK_PROMPT_TOKENS_PER_TURN
            == MOCK_PROMPT_TOKENS_PER_TURN
        )
        assert (
            verify_executor.MOCK_COMPLETION_TOKENS_PER_TURN
            == MOCK_COMPLETION_TOKENS_PER_TURN
        )

    def test_the_predicted_sums_match_the_mock_servers(self):
        from tdd.shared.mock_openai import (
            expected_summed_tokens,
            largest_single_turn_tokens,
        )

        for turns in (2, 6, 11):
            assert summed_tokens(turns) == expected_summed_tokens(turns)
            assert last_turn_tokens(turns) == largest_single_turn_tokens(turns)

    def test_the_dogfood_script_length_is_above_the_degenerate_floor(self):
        from tdd.shared.mock_openai import ACTION_SCRIPT_LENGTH

        assert ACTION_SCRIPT_LENGTH >= verify_executor.MIN_HARNESS_TURNS
