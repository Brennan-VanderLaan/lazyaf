"""
Tests for the agent config consumer (Phase 12.5, cross-agent contract #1).

The producer is the backend's
``app.services.control_layer.workspace.generate_agent_config``; the
producer<->consumer round trip is pinned separately by agent C's
``tdd/unit/control_runtime`` contract test, which loads a REAL producer
payload through this module in one process. What is pinned HERE is the
consumer's own contract:

- version 1 only (an unknown version is a refusal, not a best-effort parse),
- every rejection prints a reason naming the exact key — never a silent None,
- consume-once: the file is deleted on load AND on parse failure, because a
  config the wrapper could not understand is precisely the one that must not
  be re-read by the next step on the same (persistent) workspace volume.
"""
import json

import pytest

from runner_common.agent_config import (
    AGENT_CONFIG_PATH_ENV,
    AGENT_CONFIG_VERSION,
    AgentConfig,
    config_path_from_env,
    load_agent_config,
    load_and_consume,
)


def write_config(tmp_path, **overrides):
    """A minimal VALID agent config; overrides mutate one key at a time."""
    data = {
        "version": 1,
        "agent": "mock",
        "model": "claude-haiku-4-5",
        "stream": True,
        "prompt": "You are implementing a feature for this project.",
        "agents_json": None,
        "task": {"card_id": "c1", "card_title": "Add rate limiting", "step_index": 3},
        "context": {"previous_step_name": "plan", "previous_step_logs": "ok"},
        "repo": {
            "repo_id": "r9",
            "workdir": "/workspace/repo",
            "base_branch": "main",
            "branch": "lazyaf/9f2a11c4",
            "remote_url": "http://backend:8000/git/r9.git",
        },
        "commit": {"enabled": True, "message": "feat: x", "push": True},
        "mock_config": None,
        "role": None,
    }
    for key, value in overrides.items():
        if value is _DROP:
            data.pop(key, None)
        else:
            data[key] = value
    path = tmp_path / "agent.exec-1.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _Drop:
    pass


_DROP = _Drop()


class TestHappyPath:
    def test_loads_every_field(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path))

        assert isinstance(cfg, AgentConfig)
        assert cfg.version == AGENT_CONFIG_VERSION
        assert cfg.agent == "mock"
        assert cfg.model == "claude-haiku-4-5"
        assert cfg.stream is True
        assert cfg.prompt.startswith("You are implementing")
        assert cfg.task["card_title"] == "Add rate limiting"
        assert cfg.context["previous_step_name"] == "plan"
        assert cfg.repo["branch"] == "lazyaf/9f2a11c4"
        assert cfg.commit["push"] is True
        assert cfg.mock_config is None
        assert cfg.role is None

    def test_version_defaults_to_one_when_absent(self, tmp_path):
        """A producer that omits the key means version 1, not a refusal."""
        cfg = load_agent_config(write_config(tmp_path, version=_DROP))
        assert cfg is not None and cfg.version == 1

    def test_optional_containers_default_to_empty(self, tmp_path):
        cfg = load_agent_config(
            write_config(tmp_path, task=None, context=None, commit=None)
        )
        assert cfg.task == {} and cfg.context == {} and cfg.commit == {}

    def test_workdir_property_falls_back_to_image_repo_path(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path, repo={"repo_id": "r9"}))
        assert str(cfg.workdir).replace("\\", "/") == "/workspace/repo"

    def test_commit_helpers_are_false_when_commit_is_absent(self, tmp_path):
        cfg = load_agent_config(write_config(tmp_path, commit=None))
        assert cfg.commit_enabled is False
        assert cfg.push_enabled is False


class TestVersionPinning:
    @pytest.mark.parametrize("version", [0, 2, "1", None, 1.0, True])
    def test_unknown_version_is_a_loud_refusal(self, tmp_path, capsys, version):
        """A wrapper that half-understands its instructions is worse than one
        that refuses."""
        assert load_agent_config(write_config(tmp_path, version=version)) is None
        err = capsys.readouterr().err
        assert "unsupported version" in err
        assert repr(version) in err


class TestMissingKeysPrintTheirReason:
    @pytest.mark.parametrize("key", ["agent", "prompt", "repo"])
    def test_missing_required_key_names_the_key(self, tmp_path, capsys, key):
        assert load_agent_config(write_config(tmp_path, **{key: _DROP})) is None
        assert f"missing required key: {key}" in capsys.readouterr().err

    @pytest.mark.parametrize("key", ["agent", "prompt", "repo"])
    def test_empty_required_key_is_also_a_refusal(self, tmp_path, capsys, key):
        empty = {"agent": "", "prompt": "", "repo": {}}[key]
        assert load_agent_config(write_config(tmp_path, **{key: empty})) is None
        assert f"missing required key: {key}" in capsys.readouterr().err

    def test_wrong_typed_agent_names_the_type(self, tmp_path, capsys):
        assert load_agent_config(write_config(tmp_path, agent=["mock"])) is None
        assert "agent must be a string" in capsys.readouterr().err

    def test_wrong_typed_repo_names_the_type(self, tmp_path, capsys):
        assert load_agent_config(write_config(tmp_path, repo="main")) is None
        assert "repo must be an object" in capsys.readouterr().err

    def test_missing_file_names_the_path(self, tmp_path, capsys):
        missing = tmp_path / "nope.json"
        assert load_agent_config(missing) is None
        assert "config file not found" in capsys.readouterr().err

    def test_invalid_json_names_the_parse_error(self, tmp_path, capsys):
        path = tmp_path / "agent.exec-1.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_agent_config(path) is None
        assert "not valid JSON" in capsys.readouterr().err

    def test_non_object_top_level_is_refused(self, tmp_path, capsys):
        path = tmp_path / "agent.exec-1.json"
        path.write_text('["a"]', encoding="utf-8")
        assert load_agent_config(path) is None
        assert "expected a JSON object" in capsys.readouterr().err


class TestConsumeOnce:
    """The workspace volume outlives the step; the config must not."""

    def test_deleted_after_a_successful_load(self, tmp_path):
        path = write_config(tmp_path)
        cfg = load_and_consume(path)
        assert cfg is not None
        assert not path.exists()

    def test_deleted_after_a_parse_failure(self, tmp_path):
        path = tmp_path / "agent.exec-1.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_and_consume(path) is None
        assert not path.exists()

    def test_deleted_after_a_version_refusal(self, tmp_path):
        path = write_config(tmp_path, version=99)
        assert load_and_consume(path) is None
        assert not path.exists()

    def test_missing_file_is_not_an_error_on_consume(self, tmp_path):
        assert load_and_consume(tmp_path / "gone.json") is None


class TestPathFromEnv:
    def test_reads_the_announced_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AGENT_CONFIG_PATH_ENV, str(tmp_path / "agent.x.json"))
        assert config_path_from_env().name == "agent.x.json"

    def test_absent_env_var_is_a_loud_none(self, monkeypatch, capsys):
        monkeypatch.delenv(AGENT_CONFIG_PATH_ENV, raising=False)
        assert config_path_from_env() is None
        assert AGENT_CONFIG_PATH_ENV in capsys.readouterr().err
