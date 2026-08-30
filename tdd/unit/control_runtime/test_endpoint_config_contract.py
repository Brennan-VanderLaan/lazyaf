"""Producer <-> consumer contract for the M14 endpoint/harness blocks.

Design section 4.3, all seven assertions, plus the backend-side/container-side
constant pins that only a one-process test can make.

PRODUCER: `app.services.control_layer.workspace.generate_agent_config` (agent
C). CONSUMER: `runner_common.agent_config.load_agent_config` (agent B). SHARED
DECLARATION: `tdd/unit/control_runtime/endpoint_contract.py` (agent C, imported
by both sides' tests) - cross-agent contract #2.

The direction of every assertion is the one the failure_01 `token`/`working_dir`
bug taught us and `test_agent_config_contract.py` already applies: the consumer
must understand every key the producer emits, under the SAME name with the SAME
value, through a REAL file.
"""
import json
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.services.control_layer.workspace import (  # noqa: E402
    AGENT_CONFIG_VERSION,
    AGENT_TYPES,
    ENDPOINT_BLOCK_KEYS as PRODUCER_ENDPOINT_KEYS,
    ENDPOINT_CAPABILITY_KEYS as PRODUCER_CAPABILITY_KEYS,
    ENDPOINT_PRICING_KEYS as PRODUCER_PRICING_KEYS,
    HARNESS_AGENT as PRODUCER_HARNESS_AGENT,
    HARNESS_BLOCK_KEYS as PRODUCER_HARNESS_KEYS,
    agent_config_keys,
    generate_agent_config,
)
from app.services.model_endpoints.secrets import HARNESS_API_KEY_ENV  # noqa: E402
from app.services.pipeline_executor import (  # noqa: E402
    HARNESS_DEFAULT_MAX_ITERATIONS,
    HARNESS_DEFAULT_MAX_TOTAL_TOKENS,
    HARNESS_MAX_TOOL_CALLS_PER_TURN,
    HARNESS_SHELL_TIMEOUT,
    HARNESS_TIME_RESERVE,
    HARNESS_TOOL_OUTPUT_MAX_BYTES,
    harness_soft_deadline,
)

from runner_common.agent_config import load_agent_config  # noqa: E402
from runner_common.harness import constants as harness_constants  # noqa: E402
from runner_common.harness.loop import (  # noqa: E402
    resolve_harness_mode,
    soft_deadline_seconds,
)

from tdd.unit.control_runtime.endpoint_contract import (  # noqa: E402
    CAPABILITY_KEYS,
    ENDPOINT_BLOCK_KEYS,
    HARNESS_AGENT,
    HARNESS_KEYS,
    PRICING_KEYS,
    SECRET_SENTINEL,
    make_endpoint_payload,
    make_harness_payload,
)


def _payload(**overrides):
    endpoint = overrides.pop("endpoint", make_endpoint_payload())
    kwargs = dict(
        agent=HARNESS_AGENT,
        prompt="Implement the feature.",
        model=(endpoint or {}).get("model"),
        agents_json=None,
        stream=True,
        card_id="c1d2",
        card_title="Add rate limiting",
        card_description="d",
        step_index=3,
        step_name="implement",
        repo_id="r9f8",
        workdir="/workspace/repo",
        base_branch="main",
        branch="lazyaf/agent-8a44c1b2",
        remote_url="http://backend:8000/git/r9f8.git",
        endpoint=endpoint,
        harness=make_harness_payload(),
    )
    kwargs.update(overrides)
    return generate_agent_config(**kwargs)


def _write(tmp_path, payload, name="agent.exec-1.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 4.3.1 - zero key loss through a real file, in one process
# --------------------------------------------------------------------------

class TestRoundTrip:
    def test_every_produced_key_survives_the_file(self, tmp_path):
        produced = _payload()
        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        assert loaded.endpoint == produced["endpoint"]
        assert loaded.harness == produced["harness"]

    def test_the_two_new_keys_are_documented_top_level_keys(self):
        assert "endpoint" in agent_config_keys()
        assert "harness" in agent_config_keys()
        assert sorted(_payload()) == sorted(agent_config_keys())

    def test_the_version_stays_1_because_the_keys_are_additive(self, tmp_path):
        """Bumping strands every runner agent in the field mid-phase, which is
        exactly what the additive-key rule exists to avoid."""
        produced = _payload()
        assert produced["version"] == AGENT_CONFIG_VERSION == 1
        assert load_agent_config(_write(tmp_path, produced)).version == 1

    def test_a_non_harness_agent_still_produces_a_loadable_config(self, tmp_path):
        produced = _payload(agent="claude-code", endpoint=None, harness=None,
                            model="claude-haiku-4-5")
        loaded = load_agent_config(_write(tmp_path, produced))
        assert loaded is not None
        assert loaded.endpoint is None and loaded.harness == {}


# --------------------------------------------------------------------------
# 4.3.2 - the key sets are EXACT on both sides
# --------------------------------------------------------------------------

class TestKeySets:
    def test_producer_declares_the_shared_key_sets(self):
        """The producer's own tuples ARE the shared module's tuples. If this
        fails, one of the two grew a key the other does not know about."""
        assert tuple(PRODUCER_ENDPOINT_KEYS) == ENDPOINT_BLOCK_KEYS
        assert tuple(PRODUCER_CAPABILITY_KEYS) == CAPABILITY_KEYS
        assert tuple(PRODUCER_PRICING_KEYS) == PRICING_KEYS
        assert tuple(PRODUCER_HARNESS_KEYS) == HARNESS_KEYS

    def test_the_rendered_blocks_carry_exactly_those_keys(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload()))
        assert tuple(loaded.endpoint) == ENDPOINT_BLOCK_KEYS
        assert tuple(loaded.endpoint["capabilities"]) == CAPABILITY_KEYS
        assert tuple(loaded.endpoint["pricing"]) == PRICING_KEYS
        assert tuple(loaded.harness) == HARNESS_KEYS

    def test_the_dispatch_builders_produce_the_same_keys(self):
        """`pipeline_executor` is what actually fills these blocks in
        production; the fixture must not be the only thing that agrees."""
        from types import SimpleNamespace

        from app.services.pipeline_executor import (
            endpoint_wire_block,
            harness_wire_block,
        )

        endpoint = _fake_endpoint()
        block = endpoint_wire_block(endpoint)
        assert tuple(block) == ENDPOINT_BLOCK_KEYS
        assert tuple(block["capabilities"]) == CAPABILITY_KEYS
        assert tuple(block["pricing"]) == PRICING_KEYS
        assert tuple(harness_wire_block({}, endpoint, 1800)) == HARNESS_KEYS
        assert isinstance(endpoint, SimpleNamespace)


def _fake_endpoint(**overrides):
    """A duck-typed ModelEndpoint. Deliberately not the ORM class: this test
    must not need a database to pin a wire shape."""
    from types import SimpleNamespace

    fields = dict(
        id="e7c1a4b2",
        name="local-4090",
        base_url="http://172.17.0.1:11434/v1",
        model="qwen2.5-coder:32b",
        server_kind="ollama",
        reach="runner-local",
        auth_style="bearer",
        auth_secret_ref="LAZYAF_ENDPOINT_LOCAL_4090",
        auth_header_name=None,
        request_timeout_seconds=300,
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        effective_context_window=32768,
        max_output_tokens=4096,
        probe_status="ok",
        probed_at=None,
        probed_from="runner:workshop-1",
        probe_age_seconds=3821.0,
        probe_stale=False,
        gpu_node_id="endpoint:local-4090",
        gpu_fraction=1.0,
        priced=True,
        max_concurrency=1,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------
# 4.3.3 - top-level model == endpoint.model
# --------------------------------------------------------------------------

class TestModelAgreement:
    def test_the_top_level_model_equals_the_endpoints_model(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload()))
        assert loaded.model == loaded.endpoint["model"] == "qwen2.5-coder:32b"

    def test_a_disagreement_is_refused_in_the_producer(self):
        with pytest.raises(ValueError, match="disagrees with endpoint.model"):
            _payload(model="claude-haiku-4-5")

    def test_an_absent_model_is_filled_from_the_endpoint(self):
        assert _payload(model=None)["model"] == "qwen2.5-coder:32b"


# --------------------------------------------------------------------------
# 4.3.4 - the secret is nowhere; only the NAME travels
# --------------------------------------------------------------------------

class TestSecretContainment:
    def test_the_block_carries_the_variable_name_and_never_a_value(self, tmp_path):
        rendered = _write(tmp_path, _payload()).read_text(encoding="utf-8")
        assert HARNESS_API_KEY_ENV in rendered
        assert SECRET_SENTINEL not in rendered

    def test_a_planted_secret_never_reaches_the_agent_file(self, tmp_path, monkeypatch):
        """The value lives ONLY in `secret_environment`, which is a different
        file that run.py deletes before the command starts."""
        monkeypatch.setenv("LAZYAF_ENDPOINT_LOCAL_4090", SECRET_SENTINEL)
        from app.services.pipeline_executor import (
            agent_secret_environment,
            endpoint_wire_block,
        )

        endpoint = _fake_endpoint()
        secret_env = agent_secret_environment(HARNESS_AGENT, "implement", endpoint)
        assert secret_env == {HARNESS_API_KEY_ENV: SECRET_SENTINEL}

        block = endpoint_wire_block(endpoint)
        assert SECRET_SENTINEL not in json.dumps(block)
        assert block["auth_env"] == HARNESS_API_KEY_ENV

    def test_auth_style_none_produces_no_secret_entry_at_all(self):
        """The FIRST-CLASS case: LAN ollama genuinely has no key, and a
        dispatcher that makes 'no auth' the exceptional branch is one that
        will grow a fake key."""
        from app.services.pipeline_executor import (
            agent_secret_environment,
            endpoint_wire_block,
        )

        endpoint = _fake_endpoint(auth_style="none", auth_secret_ref=None)
        assert agent_secret_environment(HARNESS_AGENT, "implement", endpoint) == {}
        assert endpoint_wire_block(endpoint)["auth_env"] is None

    def test_proxy_reach_sends_no_key_to_the_container(self):
        """The one genuine advantage of proxy mode: the container
        authenticates with the step JWT it already holds."""
        from app.services.pipeline_executor import (
            agent_secret_environment,
            endpoint_wire_block,
        )

        endpoint = _fake_endpoint(reach="proxy")
        assert agent_secret_environment(HARNESS_AGENT, "implement", endpoint) == {}
        assert endpoint_wire_block(endpoint)["auth_env"] is None

    def test_a_missing_backend_variable_fails_at_dispatch_naming_it(self, monkeypatch):
        monkeypatch.delenv("LAZYAF_ENDPOINT_LOCAL_4090", raising=False)
        monkeypatch.delenv("LAZYAF_ENDPOINT_LOCAL_4090_FILE", raising=False)
        from app.services.pipeline_executor import agent_secret_environment

        with pytest.raises(ValueError) as excinfo:
            agent_secret_environment(HARNESS_AGENT, "implement", _fake_endpoint())
        assert "LAZYAF_ENDPOINT_LOCAL_4090" in str(excinfo.value)


# --------------------------------------------------------------------------
# 4.3.5 / 4.3.6 - the producer's two refusals
# --------------------------------------------------------------------------

class TestProducerRefusals:
    def test_an_unprobed_endpoint_is_refused(self):
        """A 30-minute agent step is not the place to discover the model
        cannot tool-call."""
        endpoint = make_endpoint_payload()
        endpoint["capabilities"] = dict(
            endpoint["capabilities"], probe_status="unprobed", supports_tools=None
        )
        with pytest.raises(ValueError, match="never been probed"):
            _payload(endpoint=endpoint)

    def test_an_endpoint_on_a_claude_step_is_refused(self):
        """Silent acceptance would be a step that looks self-hosted in the UI
        and bills Anthropic."""
        with pytest.raises(ValueError, match="must not carry an `endpoint` block"):
            _payload(agent="claude-code", model="qwen2.5-coder:32b")

    def test_a_harness_block_on_a_claude_step_is_refused(self):
        with pytest.raises(ValueError, match="must not carry a `harness` block"):
            generate_agent_config(
                agent="claude-code",
                prompt="p",
                harness=make_harness_payload(),
            )

    def test_the_harness_agent_without_an_endpoint_is_refused(self):
        with pytest.raises(ValueError, match="requires an `endpoint` block"):
            generate_agent_config(agent=HARNESS_AGENT, prompt="p")

    @pytest.mark.parametrize("field", ["base_url", "model"])
    def test_an_empty_required_endpoint_field_is_refused(self, field):
        endpoint = make_endpoint_payload(**{field: ""})
        with pytest.raises(ValueError, match=f"requires endpoint.{field}"):
            _payload(endpoint=endpoint, model=endpoint["model"] or None)

    def test_subagents_are_refused_because_the_harness_runs_one_loop(self):
        with pytest.raises(ValueError, match="does not support `agents_json`"):
            _payload(agents_json='{"a": {}}')


# --------------------------------------------------------------------------
# 4.3.7 - ONE mode resolver, called by both sides
# --------------------------------------------------------------------------

class TestModeResolution:
    def test_the_loaded_config_resolves_the_mode_the_backend_expected(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload()))
        assert loaded.harness_mode == resolve_harness_mode(
            loaded.endpoint, loaded.harness
        )
        assert loaded.harness_mode == "tools"

    def test_a_no_tools_endpoint_resolves_to_the_fallback_protocol(self, tmp_path):
        endpoint = make_endpoint_payload()
        endpoint["capabilities"] = dict(
            endpoint["capabilities"], supports_tools=False, probe_status="degraded"
        )
        loaded = load_agent_config(_write(tmp_path, _payload(endpoint=endpoint)))
        assert loaded.harness_mode == "text"

    def test_pinning_the_mode_beats_the_capability(self, tmp_path):
        """This is how M13 makes loop shape an independent variable: forcing
        `text` on a tool-capable model measures the fallback protocol's cost
        directly."""
        payload = _payload(harness=make_harness_payload(mode="text"))
        loaded = load_agent_config(_write(tmp_path, payload))
        assert loaded.harness_mode == "text"


# --------------------------------------------------------------------------
# The constants that exist on BOTH sides of the container boundary (R3).
#
# The backend image does not install runner-common, so `pipeline_executor`
# spells these itself. That is only safe because THIS test imports both
# modules in one process and asserts each pair - the same instrument
# AGENT_CONFIG_VERSION and SPEC_CONTEXT_PATH already use.
# --------------------------------------------------------------------------

class TestBudgetConstantsAgree:
    @pytest.mark.parametrize(
        "backend_value,container_name",
        [
            (HARNESS_DEFAULT_MAX_ITERATIONS, "DEFAULT_MAX_ITERATIONS"),
            (HARNESS_DEFAULT_MAX_TOTAL_TOKENS, "DEFAULT_MAX_TOTAL_TOKENS"),
            (HARNESS_MAX_TOOL_CALLS_PER_TURN, "MAX_TOOL_CALLS_PER_TURN"),
            (HARNESS_SHELL_TIMEOUT, "TOOL_SHELL_TIMEOUT"),
            (HARNESS_TOOL_OUTPUT_MAX_BYTES, "TOOL_OUTPUT_MAX_BYTES"),
            (HARNESS_TIME_RESERVE, "HARNESS_TIME_RESERVE"),
        ],
    )
    def test_each_budget_matches_its_container_side_twin(
        self, backend_value, container_name
    ):
        assert backend_value == getattr(harness_constants, container_name)

    @pytest.mark.parametrize("timeout", [1800, 300, 120, 119, 90, 30, 1, 0, None])
    def test_the_soft_deadline_rule_is_the_same_function_on_both_sides(self, timeout):
        assert harness_soft_deadline(timeout) == soft_deadline_seconds(timeout)

    def test_the_wire_value_is_what_the_consumer_reads(self, tmp_path):
        from app.services.pipeline_executor import harness_wire_block

        block = harness_wire_block({}, _fake_endpoint(), 1800)
        assert block["time_budget_seconds"] == soft_deadline_seconds(1800) == 1740

    def test_the_agent_name_is_spelled_the_same_in_every_module(self):
        from app.services.agent_run import AGENT_BY_RUNNER_TYPE
        from app.services.pipeline_executor import (
            AGENT_SECRET_ENV,
            AGENT_USAGE_PROVIDER,
            DEFAULT_AGENT_IMAGE,
            HARNESS_AGENT as EXECUTOR_HARNESS_AGENT,
        )
        from runner_common import agent_wrapper
        from runner_common.agent_config import HARNESS_AGENT as CONSUMER_HARNESS_AGENT

        assert (
            HARNESS_AGENT
            == PRODUCER_HARNESS_AGENT
            == EXECUTOR_HARNESS_AGENT
            == CONSUMER_HARNESS_AGENT
            == "openai-harness"
        )
        # Cross-agent contract #5: five sites, one commit, one test.
        assert HARNESS_AGENT in DEFAULT_AGENT_IMAGE
        assert HARNESS_AGENT in AGENT_SECRET_ENV
        assert HARNESS_AGENT in AGENT_USAGE_PROVIDER
        assert HARNESS_AGENT in AGENT_BY_RUNNER_TYPE
        assert HARNESS_AGENT in agent_wrapper.EXECUTORS
        assert HARNESS_AGENT in AGENT_TYPES

    def test_the_matrix_vocabulary_inherits_the_agent_for_free(self):
        """`schemas/experiment.AGENT_VOCABULARY` derives from
        AGENT_BY_RUNNER_TYPE, which is what lets a 12.6.5 matrix mix API and
        self-hosted models with NO schema change."""
        from app.schemas.experiment import AGENT_VOCABULARY

        assert HARNESS_AGENT in AGENT_VOCABULARY

    def test_the_provider_is_the_one_the_usage_channel_knows(self):
        from app.models.usage import UsageProvider
        from app.services.pipeline_executor import AGENT_USAGE_PROVIDER

        assert (
            AGENT_USAGE_PROVIDER[HARNESS_AGENT]
            == UsageProvider.OPENAI_COMPATIBLE.value
            == harness_constants.HARNESS_PROVIDER
        )
