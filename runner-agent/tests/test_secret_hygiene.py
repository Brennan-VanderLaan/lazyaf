"""Secrets never leave `control_files` - cross-agent contract #9, section 4.3.

Test contract item 5 (section 8, Agent D). 12.6 gives `secret_environment` and
the step JWT a NEW SHAPE of exposure: they now cross a network inside
``execute_step.config``. The containment rules are unchanged - file-only on the
container side, never in ``container.environment``, never in a log line - but
"never in a log line" now has to survive an agent that is naturally tempted to
log the config it just received in order to debug a remote host it cannot ssh
into.

So: the agent logs ``sorted(config.keys())``, the image, the volume and the
resolved backend URL. Nothing else. This file drives a real assignment carrying
a sentinel through the whole session and asserts the sentinel appears in no
emitted log record and in no outbound frame.
"""
from __future__ import annotations

import asyncio
import json
import logging

from lazyaf_runner.orchestrator.docker_orch import DockerOrchestrator
from lazyaf_runner.session import RunnerSession
from lazyaf_runner.types import StepAssignment
from lazyaf_runner.workspace import DockerWorkspaceProvisioner

from conftest import FakeDockerClient, make_config, make_step_config

SENTINEL = "sk-ANTHROPIC-DO-NOT-LEAK-9f2a11c4"
STEP_JWT = "eyJhbGciOiJIUzI1NiJ9.SENTINEL-STEP-TOKEN.sig"
STEP_IMAGE = "lazyaf-base:dev"
CLONE_IMAGE = "python:3.12"

REGISTERED = {"runner_id": "test-runner", "heartbeat_interval": 3600}


def secret_config() -> dict:
    return make_step_config(
        control_files={
            "/workspace/.control/se1.json": {
                "step_id": "se1",
                "auth_token": STEP_JWT,
                "command": "echo hi",
                "environment": {"ANTHROPIC_API_KEY": SENTINEL},
            }
        }
    )


def build_orchestrator(client: FakeDockerClient) -> DockerOrchestrator:
    config = make_config(orchestrator="docker")
    return DockerOrchestrator(
        config,
        client=client,
        provisioner=DockerWorkspaceProvisioner(client, network=config.step_network),
    )


# ---------------------------------------------------------------------------

async def test_no_sentinel_in_any_log_record_or_frame(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)

    from conftest import FakeTransport

    transport = FakeTransport()
    transport.push(
        {
            "type": "execute_step",
            "step_id": "s1",
            "execution_key": "k1",
            "config": secret_config(),
        }
    )
    session = RunnerSession(
        make_config(), orch, transport, REGISTERED, log_flush_interval=0.01
    )
    task = asyncio.create_task(session.serve())
    await transport.wait_for("step_complete", timeout=5)
    transport.push_close()
    await asyncio.wait_for(task, timeout=5)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert SENTINEL not in emitted, "the provider key reached a log record"
    assert STEP_JWT not in emitted, "the step JWT reached a log record"

    wire = json.dumps(transport.sent)
    assert SENTINEL not in wire, "the provider key was echoed back over the socket"
    assert STEP_JWT not in wire, "the step JWT was echoed back over the socket"


async def test_secrets_reach_the_container_only_through_put_archive() -> None:
    """The tar goes onto the volume; `docker inspect` shows nothing."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    assignment = StepAssignment(step_id="s1", execution_key="k1", config=secret_config())

    await orch.run_step(assignment, on_log=lambda lines: None, cancel=asyncio.Event())

    container = [c for c in client.containers.created if c.image == STEP_IMAGE][-1]
    env_blob = json.dumps(container.kwargs["environment"])
    assert SENTINEL not in env_blob, "a secret entered inspectable container env"
    assert STEP_JWT not in env_blob, "the step JWT entered inspectable container env"

    archive = container.archives[0][1]
    assert SENTINEL.encode() in archive
    assert STEP_JWT.encode() in archive


async def test_redacted_summary_carries_names_not_values() -> None:
    assignment = StepAssignment(step_id="s1", execution_key="k1", config=secret_config())
    summary = assignment.redacted_summary()
    blob = json.dumps(summary)

    assert SENTINEL not in blob
    assert STEP_JWT not in blob
    # It is still USEFUL: the keys, the image, the volume and the backend URL
    # are what you actually need to debug a host you cannot ssh into.
    assert summary["config_keys"] == [
        "backend_url",
        "container",
        "control_files",
        "protocol_version",
        "workspace",
    ]
    assert summary["control_file_paths"] == ["/workspace/.control/se1.json"]
    assert summary["image"] == STEP_IMAGE
    assert summary["volume"] == "lazyaf-ws-run1"


async def test_runner_log_lines_never_carry_the_config() -> None:
    """The runner-origin lines are the OTHER place a config could leak: they
    are appended to StepRun.logs and rendered in the UI."""
    client = FakeDockerClient(images=[STEP_IMAGE, CLONE_IMAGE])
    orch = build_orchestrator(client)
    emitted: list[str] = []
    assignment = StepAssignment(step_id="s1", execution_key="k1", config=secret_config())

    await orch.run_step(
        assignment, on_log=lambda lines: emitted.extend(lines), cancel=asyncio.Event()
    )

    assert emitted, "the run emitted no runner lines at all - assertion would be vacuous"
    joined = "\n".join(emitted)
    assert SENTINEL not in joined
    assert STEP_JWT not in joined


async def test_config_redaction_survives_an_agent_side_failure(caplog) -> None:
    """The tempting place to dump the config is the error path. It must not."""
    caplog.set_level(logging.DEBUG)
    client = FakeDockerClient(images=[CLONE_IMAGE])  # step image absent
    orch = build_orchestrator(client)
    emitted: list[str] = []
    assignment = StepAssignment(step_id="s1", execution_key="k1", config=secret_config())

    outcome = await orch.run_step(
        assignment, on_log=lambda lines: emitted.extend(lines), cancel=asyncio.Event()
    )

    assert outcome.exit_code != 0
    blob = "\n".join(emitted) + "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in blob
    assert STEP_JWT not in blob


def test_config_redaction_is_not_an_allowlist_of_known_secret_names() -> None:
    """A denylist of key names ('token', 'secret', ...) would miss the next
    secret anyone adds. The summary emits key NAMES and four fixed fields, so a
    new secret key is safe by construction rather than by remembering to add it.
    """
    assignment = StepAssignment(
        step_id="s1",
        execution_key="k1",
        config=make_step_config(
            control_files={
                "/workspace/.control/se1.json": {
                    "a_key_nobody_thought_of": "TOTALLY-NEW-SENTINEL"
                }
            }
        ),
    )
    assert "TOTALLY-NEW-SENTINEL" not in json.dumps(assignment.redacted_summary())
