"""
Package-local cover for the curated spec context (12.6.6), consumer side.

WHY THIS EXISTS ALONGSIDE `tdd/unit/control_runtime/test_spec_context_*.py`.
`runner-common` is vendored into the runner-agent image without the `tdd/`
tree, so `pytest` run inside that image sees only this package. The gated
suites under `tdd/` (the ones `scripts/run_tier.py` actually selects) hold the
producer<->consumer contract and the wrapper behaviour; this file is the
minimum that keeps the CONSUMER honest when the backend is not on the path at
all - the loader's shape rules and the two properties everything else reads
through.

It deliberately does NOT restate the wire shape: that lives in exactly one
place, `tdd/unit/control_runtime/spec_context_contract.py` (R3), and a second
copy here would be the drift the contract module exists to prevent.
"""
import json

from runner_common.agent_config import (
    SPEC_CONTEXT_FILENAME,
    AgentConfig,
    load_agent_config,
)


def _payload(**overrides):
    data = {
        "version": 1,
        "agent": "mock",
        "prompt": "Implement the feature.",
        "repo": {"repo_id": "r9", "workdir": "/workspace/repo"},
    }
    data.update(overrides)
    return data


def _write(tmp_path, payload):
    path = tmp_path / "agent.exec-1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestTheFilenameConstant:
    def test_the_wrapper_names_a_markdown_file(self):
        """Pinned against the backend's constant in the gated contract test;
        pinned against ACCIDENT here."""
        assert SPEC_CONTEXT_FILENAME == "spec_context.md"


class TestLoaderShapeRules:
    def test_absent_key_is_no_bundle(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload()))
        assert loaded is not None
        assert loaded.spec_context is None
        assert loaded.has_spec_context is False

    def test_null_is_no_bundle(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload(spec_context=None)))
        assert loaded is not None
        assert loaded.has_spec_context is False

    def test_a_dict_with_markdown_is_a_bundle(self, tmp_path):
        bundle = {"markdown": "## Spec Context\n\nintent\n", "dropped": []}
        loaded = load_agent_config(_write(tmp_path, _payload(spec_context=bundle)))
        assert loaded is not None
        assert loaded.spec_markdown == bundle["markdown"]
        assert loaded.has_spec_context is True

    def test_a_string_spec_context_is_refused_loudly(self, tmp_path, capsys):
        payload = _payload(spec_context="## Spec Context")
        assert load_agent_config(_write(tmp_path, payload)) is None
        assert "spec_context" in capsys.readouterr().err

    def test_non_string_markdown_is_refused_loudly(self, tmp_path, capsys):
        payload = _payload(spec_context={"markdown": ["a", "b"]})
        assert load_agent_config(_write(tmp_path, payload)) is None
        assert "markdown" in capsys.readouterr().err


class TestProperties:
    def test_empty_markdown_is_not_a_bundle(self):
        cfg = AgentConfig(
            agent="mock", prompt="p", repo={}, spec_context={"markdown": ""}
        )
        assert cfg.spec_markdown is None
        assert cfg.has_spec_context is False

    def test_null_markdown_is_not_a_bundle(self):
        cfg = AgentConfig(
            agent="mock", prompt="p", repo={}, spec_context={"markdown": None}
        )
        assert cfg.spec_markdown is None
        assert cfg.has_spec_context is False

    def test_missing_markdown_key_is_not_a_bundle(self):
        cfg = AgentConfig(agent="mock", prompt="p", repo={}, spec_context={})
        assert cfg.spec_markdown is None
        assert cfg.has_spec_context is False

    def test_the_default_is_no_bundle(self):
        cfg = AgentConfig(agent="mock", prompt="p", repo={})
        assert cfg.spec_context is None
        assert cfg.has_spec_context is False
