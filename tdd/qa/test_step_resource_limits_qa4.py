"""QA-4: what a step container is allowed to consume.

``LocalExecutor.execute_step`` builds ``run_kwargs`` at
backend/app/services/execution/local_executor.py:756 and sets ``mem_limit``
only when the step config explicitly asked for one (:773). Nothing sets
``nano_cpus``, ``pids_limit`` or ``ulimits`` at all, and there is no
platform-level default.

On a platform whose whole premise is running commands an AI wrote, an
unbounded ``script`` step is the shortest path from "the agent had a bad day"
to "the host is gone".

This test needs the docker CLI to observe the running container's HostConfig;
it skips when docker is unavailable.
"""

import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_support import api, graph, start_run, step

pytestmark = [pytest.mark.qa4, pytest.mark.containers]

#: The label LocalExecutor stamps on every step container
#: (local_executor.py:67). Containers are otherwise unnamed, so this label is
#: the only handle an operator has on them.
PIPELINE_RUN_LABEL = "lazyaf.pipeline_run_id"


def _inspect(container_id: str, fmt: str) -> str:
    return subprocess.run(
        ["docker", "inspect", container_id, "--format", fmt],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()


@pytest.fixture()
def running_step_container(create_pipeline, seeded_repo_id):
    """Start a slow step and hand back the id of its live container."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available; cannot inspect the step container")

    status, pipeline = create_pipeline({
        "name": "qa4-resource-limits",
        "steps_graph": graph([step("a", "sleep 45")], [], ["a"]),
    })
    assert status == 201, repr(pipeline)[:300]
    status, run = start_run(pipeline["id"])
    if status != 200:
        pytest.skip(f"could not start run: {status}")

    deadline = time.time() + 240
    while time.time() < deadline:
        found = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label={PIPELINE_RUN_LABEL}={run['id']}"],
            capture_output=True, text=True, timeout=60,
        ).stdout.split()
        if found:
            yield found[0]
            api("POST", f"/api/pipeline-runs/{run['id']}/cancel")
            return
        status, body = api("GET", f"/api/pipeline-runs/{run['id']}")
        if status != 200 or not isinstance(body, dict) or "status" not in body:
            pytest.skip("QA sandbox was reset mid-test")
        if body["status"] not in ("running", "pending"):
            pytest.skip(f"step never produced a container (run ended {body['status']})")
        time.sleep(2)
    pytest.skip("step container never appeared")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-21: step containers run with NO memory limit. "
        "local_executor.py:773 sets mem_limit only when the step config asked "
        "for one, and there is no platform default - observed HostConfig "
        "Memory=0, MemorySwap=0. An AI-authored `script` step that allocates "
        "without bound takes the whole host with it, and on the shipped "
        "compose files that host also runs the LazyAF backend."
    ),
)
def test_step_container_has_a_memory_limit(running_step_container):
    memory = _inspect(running_step_container, "{{.HostConfig.Memory}}")
    assert memory not in ("0", "", "<no value>"), f"HostConfig.Memory={memory!r}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA4-21: step containers run with NO cpu limit. Observed "
        "HostConfig NanoCpus=0 and CpuShares=0, so one runaway step starves "
        "every other step and the backend itself."
    ),
)
def test_step_container_has_a_cpu_limit(running_step_container):
    nano_cpus = _inspect(running_step_container, "{{.HostConfig.NanoCpus}}")
    shares = _inspect(running_step_container, "{{.HostConfig.CpuShares}}")
    assert nano_cpus not in ("0", "", "<no value>") or shares not in ("0", "", "<no value>"), (
        f"NanoCpus={nano_cpus!r} CpuShares={shares!r}"
    )


def test_step_container_is_not_privileged(running_step_container):
    """Verified correct - keep it that way."""
    assert _inspect(running_step_container, "{{.HostConfig.Privileged}}") == "false"


def test_step_container_carries_the_run_label(running_step_container):
    """Verified correct: containers are unnamed but labelled, so an operator
    can still find and reap orphans by pipeline run."""
    labels = _inspect(running_step_container, "{{.Config.Labels}}")
    assert PIPELINE_RUN_LABEL in labels, labels
