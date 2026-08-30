"""
The harness's cross-process contracts, both sides in ONE process
(wave 8 cross-agent contracts #3, #5 and #6; design sections 4.1 and 5.1).

WHY THIS FILE EXISTS SEPARATELY FROM ``test_endpoint_config_contract.py``.
That file (agent C's) pins the PRODUCER against the CONSUMER once
``generate_agent_config`` grows its ``endpoint``/``harness`` arguments. This
one pins the things that are true regardless of when that lands and that
nothing else can see at all: the container-side vocabulary against the
backend-side vocabulary, in one interpreter, with both real modules imported.

The conftest here already puts ``images/base`` and ``runner-common`` on
``sys.path``, and the tier runs from ``backend/`` so ``app.*`` is importable.
That is the only place in the repo where the step container's code and the
backend's code can be compared by identity rather than by eye.
"""
import json
from pathlib import Path

import pytest

from app.models.usage import UsageCostSource, UsageProvider
from app.services.model_endpoints.resolve import ENDPOINT_MODEL_PREFIX
from app.services.model_endpoints.secrets import HARNESS_API_KEY_ENV
from runner_common import agent_wrapper
from runner_common.agent_config import HARNESS_AGENT, load_agent_config
from runner_common.harness.constants import (
    HARNESS_COST_SOURCE,
    HARNESS_PROVIDER,
    HARNESS_TIME_RESERVE,
)
from runner_common.harness.loop import resolve_harness_mode, soft_deadline_seconds
from runner_common.usage import PROVIDER_BY_AGENT

# --------------------------------------------------------------------------
# The wire shape, key for key (design section 4.3's declared key sets).
# --------------------------------------------------------------------------

ENDPOINT_BLOCK_KEYS = (
    "id",
    "name",
    "base_url",
    "model",
    "server_kind",
    "reach",
    "auth_style",
    "auth_env",
    "auth_header",
    "request_timeout_seconds",
    "capabilities",
    "pricing",
)
CAPABILITY_KEYS = (
    "supports_tools",
    "supports_streaming",
    "reports_usage",
    "context_window",
    "max_output_tokens",
    "probe_status",
    "probed_at",
    "probed_from",
    "probe_age_seconds",
    "stale",
)
PRICING_KEYS = ("gpu_node_id", "gpu_fraction", "priced")
HARNESS_KEYS = (
    "mode",
    "max_iterations",
    "max_total_tokens",
    "time_budget_seconds",
    "max_tool_calls_per_turn",
    "shell_timeout_seconds",
    "tool_output_max_bytes",
    "temperature",
    "top_p",
    "seed",
    "require_changes",
    "debug_transcript",
)


def endpoint_payload(**overrides):
    payload = {
        "id": "e7c1a4b2-0000-4000-8000-000000000001",
        "name": "local-4090",
        "base_url": "http://172.17.0.1:11434/v1",
        "model": "qwen2.5-coder:32b",
        "server_kind": "ollama",
        "reach": "runner-local",
        "auth_style": "bearer",
        "auth_env": HARNESS_API_KEY_ENV,
        "auth_header": None,
        "request_timeout_seconds": 300,
        "capabilities": {
            "supports_tools": True,
            "supports_streaming": True,
            "reports_usage": True,
            "context_window": 32768,
            "max_output_tokens": 4096,
            "probe_status": "ok",
            "probed_at": "2026-08-30T09:14:22Z",
            "probed_from": "runner:workshop-1",
            "probe_age_seconds": 3821,
            "stale": False,
        },
        "pricing": {
            "gpu_node_id": "endpoint:local-4090",
            "gpu_fraction": 1.0,
            "priced": True,
        },
    }
    payload.update(overrides)
    return payload


def harness_payload(**overrides):
    payload = {
        "mode": "auto",
        "max_iterations": 40,
        "max_total_tokens": 400000,
        "time_budget_seconds": soft_deadline_seconds(1800),
        "max_tool_calls_per_turn": 4,
        "shell_timeout_seconds": 120,
        "tool_output_max_bytes": 8192,
        "temperature": 0,
        "top_p": None,
        "seed": 7,
        "require_changes": True,
        "debug_transcript": False,
    }
    payload.update(overrides)
    return payload


def write_config(tmp_path, **overrides):
    endpoint = overrides.pop("endpoint", endpoint_payload())
    harness = overrides.pop("harness", harness_payload())
    data = {
        "version": 1,
        "agent": HARNESS_AGENT,
        "model": endpoint["model"],
        "stream": True,
        "prompt": "Implement the feature.",
        "repo": {
            "repo_id": "r9",
            "workdir": "/workspace/repo",
            "base_branch": "main",
            "branch": "lazyaf/agent-8a44c1b2",
            "remote_url": "http://backend:8000/git/r9.git",
        },
        "commit": {"enabled": True, "message": "feat: x", "push": True},
        "endpoint": endpoint,
        "harness": harness,
    }
    data.update(overrides)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# contract #3 — ONE container-side variable name
# --------------------------------------------------------------------------

class TestApiKeyVariableName:
    def test_the_backend_names_it_and_the_container_reads_it_off_the_wire(
        self, tmp_path
    ):
        """The harness never spells ``LAZYAF_ENDPOINT_API_KEY`` as a literal.
        It reads ``endpoint.auth_env``, which the backend fills from its own
        ``HARNESS_API_KEY_ENV`` — so there is exactly one definition, in
        ``app.services.model_endpoints.secrets``."""
        cfg = load_agent_config(write_config(tmp_path))
        assert cfg is not None
        assert cfg.endpoint["auth_env"] == HARNESS_API_KEY_ENV

    def test_the_container_side_spells_no_second_copy_of_the_name(self):
        harness_dir = (
            Path(agent_wrapper.__file__).parent / "harness"
        )
        for path in sorted(harness_dir.glob("*.py")):
            assert HARNESS_API_KEY_ENV not in path.read_text(encoding="utf-8"), (
                f"{path.name} spells the container-side key variable as a "
                "literal; it must come from endpoint.auth_env"
            )

    def test_the_agent_config_carries_the_NAME_and_never_a_VALUE(self, tmp_path):
        sentinel = "sk-planted-secret-value-000"
        path = write_config(tmp_path)
        rendered = path.read_text(encoding="utf-8")
        assert HARNESS_API_KEY_ENV in rendered
        assert sentinel not in rendered


# --------------------------------------------------------------------------
# contract #4 — ONE `endpoint:` sugar spelling, and no second parser
# --------------------------------------------------------------------------

def test_the_container_has_no_second_endpoint_prefix_parser():
    """A STRING LITERAL ``"endpoint:"`` anywhere container-side would be a
    second parser for the sugar. (The word also appears as a parameter name
    and in prose, which is why this walks constants rather than grepping.)"""
    import ast

    harness_dir = Path(agent_wrapper.__file__).parent / "harness"
    for path in sorted(harness_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # a docstring is prose, not a parser
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert ENDPOINT_MODEL_PREFIX not in node.value, (
                    f"{path.name} spells the '{ENDPOINT_MODEL_PREFIX}' sugar; "
                    "the ONE parser is resolve_step_endpoint, backend-side"
                )


# --------------------------------------------------------------------------
# contract #5 — the agent vocabulary agrees on both sides
# --------------------------------------------------------------------------

class TestAgentVocabulary:
    def test_the_container_side_names_it_in_every_place_it_must(self):
        assert HARNESS_AGENT == "openai-harness"
        assert HARNESS_AGENT in agent_wrapper.EXECUTORS
        assert HARNESS_AGENT in PROVIDER_BY_AGENT

    def test_the_provider_is_the_backends_own_vocabulary_value(self):
        assert PROVIDER_BY_AGENT[HARNESS_AGENT] == UsageProvider.OPENAI_COMPATIBLE.value
        assert HARNESS_PROVIDER == UsageProvider.OPENAI_COMPATIBLE.value

    def test_a_harness_row_may_never_claim_the_provider_billed_us(self):
        """`cli-reported` is what the board reads as "the provider billed us
        this amount", and no self-hosted endpoint can make that claim."""
        assert HARNESS_COST_SOURCE != UsageCostSource.CLI_REPORTED.value
        assert HARNESS_COST_SOURCE in {source.value for source in UsageCostSource}

    def test_the_node_priced_branch_is_the_one_that_supplies_dollars(self):
        assert UsageCostSource.GPU_NODE.value == "gpu-node"


# --------------------------------------------------------------------------
# the wire shape, both sides
# --------------------------------------------------------------------------

class TestWireShape:
    def test_the_consumer_loads_the_designed_blocks_with_zero_key_loss(
        self, tmp_path
    ):
        cfg = load_agent_config(write_config(tmp_path))
        assert tuple(cfg.endpoint) == ENDPOINT_BLOCK_KEYS
        assert tuple(cfg.endpoint["capabilities"]) == CAPABILITY_KEYS
        assert tuple(cfg.endpoint["pricing"]) == PRICING_KEYS
        assert tuple(cfg.harness) == HARNESS_KEYS

    def test_the_top_level_model_equals_the_endpoints_model(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path))
        assert cfg.model == cfg.endpoint["model"]

    def test_the_version_stays_1_because_the_keys_are_additive(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path))
        assert cfg.version == 1

    def test_a_pre_14_payload_still_loads(self, tmp_path):
        """An old backend that knows nothing about endpoints must not strand a
        new runner agent — the whole reason these keys are additive."""
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agent": "claude-code",
                    "prompt": "p",
                    "repo": {"workdir": "/workspace/repo"},
                }
            ),
            encoding="utf-8",
        )
        cfg = load_agent_config(path)
        assert cfg is not None
        assert cfg.endpoint is None and cfg.harness == {}


# --------------------------------------------------------------------------
# the soft deadline has exactly one source
# --------------------------------------------------------------------------

class TestSoftDeadline:
    def test_it_sits_strictly_inside_the_watchdogs_hard_deadline(self):
        timeout = 1800
        budget = soft_deadline_seconds(timeout)
        assert budget == timeout - HARNESS_TIME_RESERVE
        assert budget < timeout, (
            "the harness must stop itself in time to commit, push and write "
            "telemetry inside the container watchdog's hard deadline"
        )

    def test_a_short_step_gets_half_its_timeout_rather_than_a_negative_budget(self):
        assert soft_deadline_seconds(90) == 45
        assert soft_deadline_seconds(30) == 15

    def test_the_wire_value_is_what_the_consumer_reads(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path))
        assert cfg.harness["time_budget_seconds"] == soft_deadline_seconds(1800)


# --------------------------------------------------------------------------
# the mode resolver is shared by the config and the executor
# --------------------------------------------------------------------------

class TestModeResolution:
    def test_the_config_property_calls_the_one_resolver(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path))
        assert cfg.harness_mode == resolve_harness_mode(cfg.endpoint, cfg.harness)
        assert cfg.harness_mode == "tools"

    def test_an_unprobed_capability_record_is_refused_at_load(self, tmp_path, capsys):
        endpoint = endpoint_payload()
        endpoint["capabilities"] = dict(
            endpoint["capabilities"], supports_tools=None, probe_status="unprobed"
        )
        assert load_agent_config(write_config(tmp_path, endpoint=endpoint)) is None
        assert "supports_tools is null" in capsys.readouterr().err
