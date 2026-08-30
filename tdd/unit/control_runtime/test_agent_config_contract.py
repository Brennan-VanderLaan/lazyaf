"""
Producer <-> consumer contract for the AGENT config file (12.5, cross-agent #1).

The backend's `control_layer.workspace.generate_agent_config` is the single
producer of `/workspace/.control/agent.<step_execution_id>.json`; the
in-container `runner_common.agent_config.load_agent_config` is the single
consumer. Both are driven HERE, in ONE process, over a REAL file - the same
shape as `test_config_contract.py` pins for the step config.

Direction of the assertion is the one the failure_01 `token`/`working_dir` bug
taught us: consumer-keys SUPERSET-OF producer-keys, and every producer key
must survive the file round trip under the SAME name with the SAME value.

Also pinned here, because they are the parts of the contract that only show up
when both sides are present:
- the agent config is a SECOND file, so it must NOT carry the step JWT or an
  API key (those live in the step config, which run.py deletes before the
  command starts),
- consume-once: the consumer deletes the file on load AND on parse failure,
- an unknown version is a refusal, not a best-effort parse.

The conftest of this package already puts both `images/base` and
`runner-common` on sys.path.
"""
import dataclasses
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
    PREVIOUS_STEP_LOGS_MAX_BYTES,
    agent_config_keys,
    generate_agent_config,
    truncate_previous_step_logs,
)

from runner_common.agent_config import (  # noqa: E402
    AgentConfig,
    load_agent_config,
    load_and_consume,
)


def _producer_payload(**overrides):
    kwargs = dict(
        agent="claude-code",
        prompt="You are implementing a feature for this project.",
        model="claude-haiku-4-5",
        agents_json='{"test-fixer": {"description": "d", "prompt": "p"}}',
        stream=True,
        card_id="c1d2",
        card_title="Add rate limiting to /api/repos",
        card_description="Long description",
        step_index=3,
        step_name="implement",
        previous_step_name="plan",
        previous_step_logs="the plan output",
        repo_id="r9f8",
        workdir="/workspace/repo",
        base_branch="main",
        branch="lazyaf/9f2a11c4",
        remote_url="http://backend:8000/git/r9f8.git",
        commit_enabled=True,
        commit_message="feat: Add rate limiting\n\nImplemented by LazyAF agent",
        push=True,
        allow_empty=False,
        mock_config=None,
        role=None,
    )
    kwargs.update(overrides)
    return generate_agent_config(**kwargs)


def _write(tmp_path, payload, name="agent.exec-1.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class TestAgentConfigRoundTrip:
    def test_every_producer_key_survives_the_file(self, tmp_path):
        """Identity mapping: producer key == consumer attribute, value
        preserved verbatim through a real JSON file."""
        produced = _producer_payload()
        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        for key, value in produced.items():
            assert hasattr(loaded, key), (
                f"producer emits key {key!r} the consumer does not have - "
                "extend AgentConfig and the loader together"
            )
            assert getattr(loaded, key) == value, key

    def test_consumer_keys_superset_of_producer_keys(self):
        produced = set(_producer_payload())
        consumer = {f.name for f in dataclasses.fields(AgentConfig)}

        assert produced <= consumer, (
            f"producer emits keys unknown to the consumer: {produced - consumer}"
        )
        assert consumer - produced == set(), (
            "consumer fields the producer never sends: "
            f"{sorted(consumer - produced)}"
        )

    def test_producer_key_list_is_the_documented_one(self):
        assert sorted(_producer_payload()) == sorted(agent_config_keys())

    def test_versions_agree_on_both_sides(self):
        from runner_common import agent_config as consumer_module

        assert AGENT_CONFIG_VERSION == consumer_module.AGENT_CONFIG_VERSION

    def test_agent_vocabulary_is_the_shared_one(self, tmp_path):
        """Contract #5: every agent the backend can dispatch must load.

        M14 note: `openai-harness` is the one agent that REQUIRES an endpoint
        block (there is no default endpoint - guessing which GPU to bill is
        not a recoverable mistake), and it refuses `agents_json` because the
        harness runs one loop and subagents belong in the graph. Those two
        refusals are pinned in
        `tdd/unit/control_runtime/test_endpoint_config_contract.py`; here the
        loop just supplies what the agent needs so the vocabulary assertion
        stays about the VOCABULARY.
        """
        from app.services.pipeline_executor import HARNESS_AGENT

        for agent in AGENT_TYPES:
            extra = {}
            if agent == HARNESS_AGENT:
                from tdd.unit.control_runtime.endpoint_contract import (
                    make_endpoint_payload,
                    make_harness_payload,
                )

                endpoint = make_endpoint_payload()
                extra = {
                    "endpoint": endpoint,
                    "harness": make_harness_payload(),
                    "model": endpoint["model"],
                    "agents_json": None,
                }
            produced = _producer_payload(agent=agent, **extra)
            loaded = load_agent_config(_write(tmp_path, produced))
            assert loaded is not None and loaded.agent == agent

    def test_unknown_agent_never_reaches_the_wire(self):
        with pytest.raises(ValueError, match="unknown agent"):
            _producer_payload(agent="acme-ai")

    def test_no_secret_travels_in_the_agent_file(self):
        """The step JWT and the provider API key live in the STEP config,
        which run.py deletes before the command runs. The agent file is the
        one file the wrapper opens; nothing secret may be in it."""
        blob = json.dumps(_producer_payload())
        for forbidden in ("auth_token", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                          "execution_key", "backend_url"):
            assert forbidden not in blob


class TestConsumeOnce:
    def test_loading_consumes_the_file(self, tmp_path):
        path = _write(tmp_path, _producer_payload())
        assert load_and_consume(path) is not None
        assert not path.exists()

    def test_parse_failure_also_consumes_the_file(self, tmp_path):
        path = tmp_path / "agent.exec-1.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_and_consume(path) is None
        assert not path.exists()

    def test_unknown_version_is_a_refusal(self, tmp_path, capsys):
        payload = {**_producer_payload(), "version": 2}
        assert load_agent_config(_write(tmp_path, payload)) is None
        assert "version" in capsys.readouterr().err


class TestRunPyBackstopSweep:
    """The control runtime is the consume-once BACKSTOP for the agent config.

    The wrapper deletes the file on load; `run.py` covers the paths where it
    never got that far (SIGKILLed before load, or a step whose command turned
    out not to be the wrapper). A rendered prompt - and, at 12.6.6, curated
    spec context - must not survive on a volume that outlives the step.
    """

    def test_surviving_agent_config_is_deleted_and_reported(
        self, tmp_path, capsys
    ):
        from control import run as control_run

        path = control_run.agent_config_path(tmp_path / "exec-1.json", "exec-1")
        assert path == tmp_path / "agent.exec-1.json"
        path.write_text("{}", encoding="utf-8")

        control_run.sweep_agent_config(path)

        assert not path.exists()
        assert "survived the step" in capsys.readouterr().err

    def test_absent_agent_config_is_silent(self, tmp_path, capsys):
        """The normal agent path: the wrapper already consumed it."""
        from control import run as control_run

        control_run.sweep_agent_config(tmp_path / "agent.exec-1.json")

        assert capsys.readouterr().err == ""

    def test_sweep_never_raises(self, tmp_path):
        """Housekeeping must never be able to take a step down with it."""
        from control import run as control_run

        control_run.sweep_agent_config(tmp_path)  # a DIRECTORY, not a file


class TestPreviousStepLogCap:
    def test_short_logs_pass_through_untouched(self):
        text, truncated = truncate_previous_step_logs("small output")
        assert text == "small output"
        assert truncated is False

    def test_absent_logs_are_none_not_empty_string(self):
        assert truncate_previous_step_logs(None) == (None, False)
        assert truncate_previous_step_logs("") == (None, False)

    def test_long_logs_are_head_truncated_with_a_visible_marker(self):
        logs = "x" * (PREVIOUS_STEP_LOGS_MAX_BYTES + 5000) + "TAIL-MARKER"
        text, truncated = truncate_previous_step_logs(logs)

        assert truncated is True
        assert text.endswith("TAIL-MARKER")  # the TAIL is what matters
        assert "truncated" in text.splitlines()[0]
        assert len(text.encode("utf-8")) <= PREVIOUS_STEP_LOGS_MAX_BYTES + 200

    def test_cap_is_measured_in_bytes_not_characters(self):
        """A log full of multi-byte output cannot blow the wire cap."""
        logs = "é" * PREVIOUS_STEP_LOGS_MAX_BYTES  # 2 bytes each
        text, truncated = truncate_previous_step_logs(logs)
        assert truncated is True
        assert len(text.encode("utf-8")) <= PREVIOUS_STEP_LOGS_MAX_BYTES + 200

    def test_truncation_flag_reaches_the_consumer(self, tmp_path):
        produced = _producer_payload(
            previous_step_logs="y" * (PREVIOUS_STEP_LOGS_MAX_BYTES + 10)
        )
        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        assert loaded.context["previous_step_logs_truncated"] is True
