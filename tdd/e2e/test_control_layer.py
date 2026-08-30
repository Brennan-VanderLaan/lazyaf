"""
E2E acceptance for Phase 12.3: control-MODE local steps against a LIVE backend.

Every script step here runs on the real, locally-built `lazyaf-base:dev`
image, whose `lazyaf.control-layer=1` label switches LocalExecutor dispatch
into CONTROL MODE: the in-container control runtime executes the command and
reports through POST /api/steps/{id}/status|logs|heartbeat (StepRun rows +
step_update/step_log WS frames), while terminal state stays owned by the
executor's result event (container exit code = ground truth). These tests
drive that full round trip through the public API:

1. Status reporting (passed / failed on real exit codes)
2. Log streaming (stdout + stderr markers land in StepRun.logs)
3. HOME=/workspace/home persistence across steps (the 12.3 contract pair)
4. Script steps sharing the run workspace (+ the agent/script mix, promoted
   from strict-xfail at 12.5)
5. Error handling (in-container timeout enforcement, bad commands)

Requirements (loud, never a skip - R4):
- Docker reachable from this process.
- `lazyaf-base:dev` built on the daemon: `python scripts/build_images.py`
  (or `scripts/test.sh images`). The :dev tags are never pulled; a missing
  image FAILS these tests with the build hint - that loud failure is the
  rebuild trigger by design.

History note: the pre-12.3 version of this file polled for a "completed" run
status that the API never emits (RunStatus is passed/failed) and then
skipped on timeout - fake green. Terminal statuses and timeouts now FAIL.
"""

import asyncio

import docker as docker_sdk
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio, pytest.mark.slow]

CONTROL_IMAGE = "lazyaf-base:dev"
CONTROL_LAYER_LABEL = "lazyaf.control-layer"
BUILD_HINT = (
    f"{CONTROL_IMAGE} is required for the 12.3 control-layer e2e tests - "
    "build it with `python scripts/build_images.py` (or `scripts/test.sh "
    "images`); :dev tags are local-only and never pulled"
)

# Terminal vocabulary of PipelineRun.status as served by the API (RunStatus).
TERMINAL_STATUSES = {"passed", "failed", "cancelled"}
POLL_INTERVAL = 0.3


# -----------------------------------------------------------------------------
# Preconditions (loud by design)
# -----------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_control_image():
    """Fail (never skip) when the control-layer image is absent or unlabeled."""
    client = docker_sdk.from_env()
    client.ping()  # Docker down = loud failure (R4)
    try:
        image = client.images.get(CONTROL_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        pytest.fail(BUILD_HINT)
    assert image.labels.get(CONTROL_LAYER_LABEL) == "1", (
        f"{CONTROL_IMAGE} exists but lacks LABEL {CONTROL_LAYER_LABEL}=1 "
        f"(labels: {image.labels}) - stale build? {BUILD_HINT}"
    )
    return image


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def control_script_step(name: str, command: str, **extra) -> dict:
    """A script step pinned to the control-layer image => control mode."""
    step = {
        "name": name,
        "type": "script",
        "config": {"command": command, "image": CONTROL_IMAGE},
    }
    step.update(extra)
    return step


async def create_pipeline(api_client, test_repo: dict, name: str, steps: list[dict]) -> dict:
    response = await api_client.post(
        f"/api/repos/{test_repo['id']}/pipelines",
        json={"name": name, "steps": steps},
    )
    assert response.status_code == 201, f"pipeline create failed: {response.text}"
    return response.json()


async def start_run(api_client, pipeline_id: str) -> dict:
    response = await api_client.post(f"/api/pipelines/{pipeline_id}/run")
    assert response.status_code in (200, 201), f"run start failed: {response.text}"
    return response.json()


async def wait_for_terminal(api_client, run_id: str, timeout: float = 120.0) -> dict:
    """Poll the run to a terminal status; timing out FAILS (never skips)."""
    deadline = asyncio.get_event_loop().time() + timeout
    current = {}
    while asyncio.get_event_loop().time() < deadline:
        response = await api_client.get(f"/api/pipeline-runs/{run_id}")
        assert response.status_code == 200, response.text
        current = response.json()
        if current.get("status") in TERMINAL_STATUSES:
            return current
        await asyncio.sleep(POLL_INTERVAL)
    pytest.fail(
        f"pipeline run {run_id} did not reach a terminal status within "
        f"{timeout}s (last status: {current.get('status')!r})"
    )


async def run_to_terminal(api_client, test_repo, name, steps, timeout: float = 120.0) -> dict:
    pipeline = await create_pipeline(api_client, test_repo, name, steps)
    run = await start_run(api_client, pipeline["id"])
    return await wait_for_terminal(api_client, run["id"], timeout=timeout)


async def fetch_step_logs(api_client, run_id: str, step_index: int) -> dict:
    response = await api_client.get(
        f"/api/pipeline-runs/{run_id}/steps/{step_index}/logs"
    )
    assert response.status_code == 200, (
        f"logs endpoint failed for run {run_id} step {step_index}: {response.text}"
    )
    return response.json()


def step_runs_by_index(run: dict) -> dict[int, dict]:
    return {sr["step_index"]: sr for sr in run.get("step_runs", [])}


def assert_script_steps_local(run: dict) -> None:
    """R1 observability: every step of these pipelines must record
    executor='local' - a silent fallback to legacy is a failure, not a pass.
    Since 12.5 this covers AGENT steps too (they run in ephemeral
    control-mode containers, not on the polling queue)."""
    step_runs = run.get("step_runs", [])
    assert step_runs, f"run {run['id']} has no step_runs"
    for sr in step_runs:
        assert sr["executor"] == "local", (
            f"step {sr['step_index']} '{sr['step_name']}' ran on "
            f"executor={sr['executor']!r}, expected 'local'"
        )


# -----------------------------------------------------------------------------
# 1. Status reporting
# -----------------------------------------------------------------------------

class TestControlModeStatusReporting:
    async def test_success_round_trip(self, api_client, test_repo):
        """Exit 0 => run passed, step passed, marker logs landed via the
        control layer's POST /logs path."""
        run = await run_to_terminal(api_client, test_repo, "CL Status: success", [
            control_script_step("echo-step", "echo 'CL_ROUNDTRIP_MARKER_OK'"),
        ])

        assert run["status"] == "passed", f"run failed: {run}"
        assert run["steps_completed"] == 1
        assert_script_steps_local(run)
        step = step_runs_by_index(run)[0]
        assert step["status"] == "passed"

        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        assert "CL_ROUNDTRIP_MARKER_OK" in logs, f"marker missing from logs: {logs!r}"

    async def test_failed_on_nonzero_exit(self, api_client, test_repo):
        """Non-zero exit => run failed, step failed, exit code surfaced.
        Terminal state is owned by the executor's result event (container
        exit code = ground truth) even in control mode."""
        run = await run_to_terminal(api_client, test_repo, "CL Status: failure", [
            control_script_step("failing-step", "echo 'about to fail' && exit 17"),
        ])

        assert run["status"] == "failed", (
            f"pipeline should have failed, got: {run['status']}"
        )
        assert_script_steps_local(run)
        step = step_runs_by_index(run)[0]
        assert step["status"] == "failed"
        assert "17" in (step.get("error") or ""), (
            f"exit code missing from step error: {step.get('error')!r}"
        )
        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        assert "about to fail" in logs


# -----------------------------------------------------------------------------
# 2. Log streaming
# -----------------------------------------------------------------------------

class TestControlModeLogStreaming:
    async def test_stdout_logs_captured(self, api_client, test_repo):
        run = await run_to_terminal(api_client, test_repo, "CL Logs: stdout", [
            control_script_step("stdout-step", "echo 'STDOUT_MARKER_12345'"),
        ])

        assert run["status"] == "passed", f"run failed: {run}"
        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        assert "STDOUT_MARKER_12345" in logs, f"expected marker in logs: {logs!r}"

    async def test_stderr_logs_captured(self, api_client, test_repo):
        run = await run_to_terminal(api_client, test_repo, "CL Logs: stderr", [
            control_script_step("stderr-step", "echo 'STDERR_MARKER_67890' >&2"),
        ])

        assert run["status"] == "passed", f"run failed: {run}"
        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        assert "STDERR_MARKER_67890" in logs, f"expected marker in logs: {logs!r}"

    async def test_multiline_output_ordered(self, api_client, test_repo):
        """Batched POST /logs must preserve line order."""
        run = await run_to_terminal(api_client, test_repo, "CL Logs: ordering", [
            control_script_step(
                "ordered-step",
                "echo LINE_ALPHA; echo LINE_BRAVO; echo LINE_CHARLIE",
            ),
        ])

        assert run["status"] == "passed", f"run failed: {run}"
        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        for marker in ("LINE_ALPHA", "LINE_BRAVO", "LINE_CHARLIE"):
            assert marker in logs, f"{marker} missing from logs: {logs!r}"
        assert (
            logs.index("LINE_ALPHA") < logs.index("LINE_BRAVO") < logs.index("LINE_CHARLIE")
        ), f"log lines out of order: {logs!r}"


# -----------------------------------------------------------------------------
# 3. HOME persistence across steps (the PLAN 12.3 contract pair)
# -----------------------------------------------------------------------------

class TestHomePersistenceAcrossSteps:
    async def test_pip_install_persists_across_steps(self, api_client, test_repo):
        """Step 1 `pip install --user cowsay` (lands in /workspace/home/.local
        via the image's baked PIP_USER/PYTHONUSERBASE); step 2 - a DIFFERENT
        container - imports and runs it. The named-volume cross-step tool
        persistence contract (R6)."""
        run = await run_to_terminal(
            api_client, test_repo, "CL Home: pip persistence",
            [
                control_script_step(
                    "install-step",
                    "pip install --user cowsay && echo 'INSTALL_DONE'",
                    continue_in_context=True,
                ),
                control_script_step(
                    "use-step",
                    "python -m cowsay -t 'Persistence works!'",
                ),
            ],
            timeout=180.0,  # pip install pulls from PyPI
        )

        assert run["status"] == "passed", f"pipeline failed: {run}"
        assert run["steps_completed"] == 2
        assert_script_steps_local(run)
        assert "INSTALL_DONE" in ((await fetch_step_logs(api_client, run["id"], 0))["logs"] or "")
        step2_logs = (await fetch_step_logs(api_client, run["id"], 1))["logs"] or ""
        assert "Persistence works!" in step2_logs, (
            f"installed tool did not run in the next step: {step2_logs!r}"
        )

    async def test_file_created_in_home_persists(self, api_client, test_repo):
        """$HOME must be /workspace/home on the shared volume; a file written
        there in step 1 is readable in step 2's fresh container."""
        run = await run_to_terminal(api_client, test_repo, "CL Home: file persistence", [
            control_script_step(
                "create-file",
                'echo "HOME_IS=$HOME" && '
                'test "$HOME" = "/workspace/home" && '
                'echo "PERSISTENCE_TEST_DATA" > "$HOME/test_file.txt"',
                continue_in_context=True,
            ),
            control_script_step("read-file", 'cat "$HOME/test_file.txt"'),
        ])

        assert run["status"] == "passed", f"pipeline failed: {run}"
        assert_script_steps_local(run)
        step1_logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        assert "HOME_IS=/workspace/home" in step1_logs
        step2_logs = (await fetch_step_logs(api_client, run["id"], 1))["logs"] or ""
        assert "PERSISTENCE_TEST_DATA" in step2_logs, (
            f"expected persisted data in step 2 logs: {step2_logs!r}"
        )


# -----------------------------------------------------------------------------
# 4. Workspace sharing between steps
# -----------------------------------------------------------------------------

class TestMixedStepTypePipelines:
    async def test_script_creates_file_for_next_step(self, api_client, test_repo):
        """Script step writes into /workspace/repo; the next script step (a
        different container on the same named volume) reads it back."""
        run = await run_to_terminal(api_client, test_repo, "CL Workspace: script chain", [
            control_script_step(
                "create-artifact",
                "echo 'BUILD_ARTIFACT_CONTENT' > /workspace/repo/artifact.txt",
                continue_in_context=True,
            ),
            control_script_step(
                "verify-artifact",
                "test -f /workspace/repo/artifact.txt && cat /workspace/repo/artifact.txt",
            ),
        ])

        assert run["status"] == "passed", (
            f"pipeline failed - workspace did not persist between steps: {run}"
        )
        assert_script_steps_local(run)
        step2_logs = (await fetch_step_logs(api_client, run["id"], 1))["logs"] or ""
        assert "BUILD_ARTIFACT_CONTENT" in step2_logs

    async def test_agent_script_agent_pipeline_shares_workspace(
        self, api_client, test_repo, mock_config
    ):
        """Agent (mock) -> script -> agent (mock) all sharing one workspace.

        PROMOTED at 12.5 (it was strict-xfail through 12.4, exactly so the
        day it started passing would scream): agent steps no longer take the
        legacy job-queue path with their own checkout - they run in ephemeral
        control-mode containers on the SAME named workspace volume as script
        steps. The chain proves it end to end: agent 1 creates a file, the
        script step appends to that same file, and agent 3 does a
        search/replace that only resolves if it sees the script's line."""
        agent1_config = {
            "response_mode": "batch",
            "delay_ms": 50,
            "file_operations": [
                {
                    "action": "create",
                    "path": "agent_output.txt",
                    "content": "Created by agent step 1\n",
                }
            ],
            "output_events": [
                {"type": "content", "text": "Creating file..."},
                {"type": "complete", "text": "Done"},
            ],
            "exit_code": 0,
        }
        agent3_config = {
            "response_mode": "batch",
            "delay_ms": 50,
            "file_operations": [
                {
                    "action": "modify",
                    "path": "agent_output.txt",
                    "search": "Modified by script",
                    "replace": "Modified by script\nVerified by agent step 3",
                }
            ],
            "output_events": [
                {"type": "content", "text": "Reading file..."},
                {"type": "complete", "text": "Done"},
            ],
            "exit_code": 0,
        }

        run = await run_to_terminal(
            api_client, test_repo, "CL Workspace: agent-script-agent",
            [
                {
                    "name": "agent-create",
                    "type": "agent",
                    "config": {"runner_type": "mock", "mock_config": agent1_config},
                    "continue_in_context": True,
                },
                control_script_step(
                    "script-modify",
                    "echo 'Modified by script' >> /workspace/repo/agent_output.txt"
                    " && cat /workspace/repo/agent_output.txt",
                    continue_in_context=True,
                ),
                {
                    "name": "agent-verify",
                    "type": "agent",
                    "config": {"runner_type": "mock", "mock_config": agent3_config},
                },
            ],
        )

        assert run["status"] == "passed", f"pipeline failed: {run}"
        # 12.5: agent steps are dispatched to the LOCAL executor, not the
        # legacy queue. A silent fallback would still pass the workspace
        # assertion below (the legacy runner clones the repo itself), so
        # this is the assertion that actually pins the phase.
        assert_script_steps_local(run)
        step2_logs = (await fetch_step_logs(api_client, run["id"], 1))["logs"] or ""
        assert "Created by agent" in step2_logs, (
            f"script step should see the agent's file: {step2_logs!r}"
        )


# -----------------------------------------------------------------------------
# 5. Error handling
# -----------------------------------------------------------------------------

class TestControlLayerErrorHandling:
    async def test_timeout_kills_step(self, api_client, test_repo):
        """A step exceeding its timeout is killed and the run fails FAST.

        The step-level `timeout` field (5s) is the contract the control
        runtime enforces in-container (SIGTERM -> SIGKILL, exit 124), with
        the executor deadline (timeout + grace) as backstop. Either way the
        run must be failed well before the sleep would finish; the 60s poll
        budget enforces that."""
        run = await run_to_terminal(
            api_client, test_repo, "CL Errors: timeout",
            [
                control_script_step("slow-step", "sleep 300", timeout=5),
            ],
            timeout=60.0,
        )

        assert run["status"] == "failed", (
            f"step should have timed out and failed the run: {run}"
        )
        step = step_runs_by_index(run)[0]
        assert step["status"] == "failed"
        step_logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        error_and_logs = (step.get("error") or "") + step_logs
        assert (
            "timed out" in error_and_logs
            or "timeout" in error_and_logs.lower()
            or "124" in error_and_logs
        ), f"no timeout evidence on the failed step: {step.get('error')!r}"

    async def test_command_not_found_fails_gracefully(self, api_client, test_repo):
        run = await run_to_terminal(api_client, test_repo, "CL Errors: bad command", [
            control_script_step("bad-command", "this_command_does_not_exist_12345"),
        ])

        assert run["status"] == "failed", (
            f"step with a nonexistent command should fail the run: {run}"
        )
        step = step_runs_by_index(run)[0]
        assert step["status"] == "failed"
        logs = (await fetch_step_logs(api_client, run["id"], 0))["logs"] or ""
        evidence = logs + (step.get("error") or "")
        assert "this_command_does_not_exist_12345" in evidence or "127" in evidence, (
            f"no failure evidence for the bad command: logs={logs!r} "
            f"error={step.get('error')!r}"
        )
