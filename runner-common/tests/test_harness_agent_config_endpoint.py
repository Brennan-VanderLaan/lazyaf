"""
The consumer side of the ``endpoint`` / ``harness`` blocks
(Milestone 14, design section 4.2).

``version`` stays 1. Two new OPTIONAL top-level keys, following the precedent
``agent_config.py`` already documents for ``spec_context``: an additive key
that an old consumer ignores and a new one defaults does NOT justify a version
bump, because bumping would strand every runner agent in the field mid-phase.

The three-way strictness, asserted here row by row:
  absent               -> ``None`` / ``{}`` (a pre-14 backend);
  present-but-wrong    -> printed reason, ``None`` return, exit 1;
  harness-agent, unusable endpoint -> printed reason, ``None`` return, exit 1.
"""
import json

import pytest

from runner_common.agent_config import (
    HARNESS_AGENT,
    AgentConfig,
    load_agent_config,
)
from runner_common.harness.loop import resolve_harness_mode
from tests.fixtures.openai import endpoint_block, harness_block


def write(tmp_path, **overrides):
    data = {
        "version": 1,
        "agent": "mock",
        "prompt": "do the thing",
        "repo": {"workdir": "/workspace/repo", "branch": "b"},
        "commit": {"enabled": True},
    }
    data.update(overrides)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# absent / present-but-wrong
# --------------------------------------------------------------------------

class TestThreeWayStrictness:
    def test_absent_blocks_default_and_do_not_break_a_pre_14_backend(self, tmp_path):
        cfg = load_agent_config(write(tmp_path))
        assert cfg is not None
        assert cfg.endpoint is None
        assert cfg.harness == {}

    def test_a_null_endpoint_is_the_same_as_absent(self, tmp_path):
        cfg = load_agent_config(write(tmp_path, endpoint=None, harness=None))
        assert cfg.endpoint is None
        assert cfg.harness == {}

    @pytest.mark.parametrize("value", ["not an object", 7, ["a"], True])
    def test_an_endpoint_that_is_not_an_object_is_a_refusal(
        self, tmp_path, capsys, value
    ):
        assert load_agent_config(write(tmp_path, endpoint=value)) is None
        assert "endpoint must be an object or null" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["nope", 7, ["a"]])
    def test_a_harness_that_is_not_an_object_is_a_refusal(
        self, tmp_path, capsys, value
    ):
        assert load_agent_config(write(tmp_path, harness=value)) is None
        assert "harness must be an object or null" in capsys.readouterr().err

    def test_the_blocks_round_trip_verbatim(self, tmp_path):
        endpoint = endpoint_block()
        harness = harness_block()
        cfg = load_agent_config(
            write(
                tmp_path,
                agent=HARNESS_AGENT,
                model=endpoint["model"],
                endpoint=endpoint,
                harness=harness,
            )
        )
        assert cfg.endpoint == endpoint
        assert cfg.harness == harness
        assert cfg.model == cfg.endpoint["model"], (
            "the top-level model and the endpoint's are set from one source"
        )


# --------------------------------------------------------------------------
# the harness agent's own requirements
# --------------------------------------------------------------------------

class TestHarnessAgentRequirements:
    def test_no_endpoint_block_is_a_refusal(self, tmp_path, capsys):
        assert load_agent_config(write(tmp_path, agent=HARNESS_AGENT)) is None
        assert "requires an endpoint block" in capsys.readouterr().err

    @pytest.mark.parametrize("missing", ["base_url", "model"])
    def test_an_empty_base_url_or_model_is_a_refusal(self, tmp_path, capsys, missing):
        endpoint = endpoint_block(**{missing: ""})
        assert (
            load_agent_config(
                write(tmp_path, agent=HARNESS_AGENT, endpoint=endpoint)
            )
            is None
        )
        assert f"requires endpoint.{missing}" in capsys.readouterr().err

    def test_auto_mode_against_an_unprobed_capability_record_is_a_refusal(
        self, tmp_path, capsys
    ):
        """The backend already refuses to dispatch an unprobed endpoint, so
        reaching this means the wire lied — and guessing here would silently
        route a tool-capable model down the fallback protocol."""
        endpoint = endpoint_block(capabilities={"supports_tools": None})
        assert (
            load_agent_config(
                write(
                    tmp_path,
                    agent=HARNESS_AGENT,
                    endpoint=endpoint,
                    harness=harness_block(mode="auto"),
                )
            )
            is None
        )
        error = capsys.readouterr().err
        assert "supports_tools is null" in error
        assert "probe the endpoint" in error

    def test_a_pinned_mode_works_even_against_an_unprobed_record(self, tmp_path):
        """Pinning is how M13 makes loop shape an independent variable."""
        endpoint = endpoint_block(capabilities={"supports_tools": None})
        cfg = load_agent_config(
            write(
                tmp_path,
                agent=HARNESS_AGENT,
                endpoint=endpoint,
                harness=harness_block(mode="text"),
            )
        )
        assert cfg is not None
        assert cfg.harness_mode == "text"

    def test_an_unknown_mode_is_a_refusal(self, tmp_path, capsys):
        assert (
            load_agent_config(
                write(
                    tmp_path,
                    agent=HARNESS_AGENT,
                    endpoint=endpoint_block(),
                    harness=harness_block(mode="creative"),
                )
            )
            is None
        )
        assert "unknown harness.mode" in capsys.readouterr().err

    def test_other_agents_are_unaffected_by_all_of_this(self, tmp_path):
        cfg = load_agent_config(
            write(tmp_path, agent="claude-code", model="claude-haiku-4-5")
        )
        assert cfg is not None
        assert cfg.endpoint is None


# --------------------------------------------------------------------------
# the ONE mode resolver, and the config property that calls it
# --------------------------------------------------------------------------

class TestHarnessModeResolution:
    @pytest.mark.parametrize(
        "mode,supports_tools,expected",
        [
            ("tools", True, "tools"),
            ("tools", False, "tools"),
            ("tools", None, "tools"),
            ("text", True, "text"),
            ("text", False, "text"),
            ("text", None, "text"),
            ("auto", True, "tools"),
            ("auto", False, "text"),
            (None, True, "tools"),
            (None, False, "text"),
        ],
    )
    def test_the_resolution_table(self, mode, supports_tools, expected):
        endpoint = endpoint_block(capabilities={"supports_tools": supports_tools})
        harness = {"mode": mode} if mode is not None else {}
        assert resolve_harness_mode(endpoint, harness) == expected

    def test_auto_plus_unknown_capability_raises_rather_than_guesses(self):
        endpoint = endpoint_block(capabilities={"supports_tools": None})
        with pytest.raises(ValueError) as caught:
            resolve_harness_mode(endpoint, {"mode": "auto"})
        assert "supports_tools is null" in str(caught.value)

    def test_the_config_property_and_the_executor_call_the_same_function(self):
        """Contract 4.3 assertion 7: one function decides tools vs text and
        both sides call it."""
        from runner_common.harness import HarnessExecutor

        endpoint = endpoint_block(capabilities={"supports_tools": False})
        harness = harness_block(mode="auto")
        cfg = AgentConfig(
            agent=HARNESS_AGENT,
            prompt="p",
            repo={"workdir": "/workspace/repo"},
            endpoint=endpoint,
            harness=harness,
        )
        assert cfg.harness_mode == HarnessExecutor(endpoint, harness).mode == "text"

    def test_a_non_string_mode_is_refused(self):
        with pytest.raises(ValueError):
            resolve_harness_mode(endpoint_block(), {"mode": 7})
