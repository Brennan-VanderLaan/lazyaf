"""
Unit tests for the control runtime's config loader (images/base/control/config.py).

Ported from failure_01's test_control_layer_protocol.py with the audit's
contract fixes applied: `auth_token` (was `token`), `working_directory`
(was `working_dir`), `command` is a STRING (was a list), and invalid configs
explain themselves on stderr instead of returning a silent None.
"""
import json

from control.config import load_step_config


def _write_config(tmp_path, **overrides):
    data = {
        "step_id": "exec-123",
        "step_run_id": "sr-456",
        "execution_key": "run-1:0:sr-456",
        "command": "echo hello",
        "backend_url": "http://backend:8000",
        "auth_token": "jwt-abc",
        "environment": {"CI": "true"},
        "timeout_seconds": 3600,
        "working_directory": "/workspace/repo",
    }
    data.update(overrides)
    config_file = tmp_path / "step_config.json"
    config_file.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
    return config_file


class TestConfigReading:
    def test_reads_full_config(self, tmp_path):
        config_file = _write_config(tmp_path, shell="sh")

        config = load_step_config(config_file)

        assert config is not None
        assert config.step_id == "exec-123"
        assert config.step_run_id == "sr-456"
        assert config.execution_key == "run-1:0:sr-456"
        assert config.backend_url == "http://backend:8000"
        assert config.auth_token == "jwt-abc"
        assert config.command == "echo hello"
        assert config.working_directory == "/workspace/repo"
        assert config.environment == {"CI": "true"}
        assert config.timeout_seconds == 3600
        assert config.shell == "sh"

    def test_config_with_defaults(self, tmp_path):
        """Only required fields -> documented defaults."""
        config_file = tmp_path / "step_config.json"
        config_file.write_text(json.dumps({
            "step_id": "exec-1",
            "backend_url": "http://backend:8000",
            "auth_token": "t",
            "command": "ls",
        }))

        config = load_step_config(config_file)

        assert config is not None
        assert config.working_directory == "/workspace/repo"
        assert config.environment == {}
        assert config.timeout_seconds == 3600
        assert config.shell == "bash"
        assert config.step_run_id == ""
        assert config.execution_key == ""

    def test_shipping_knobs_are_not_config_fields(self, tmp_path):
        """heartbeat/log-batch knobs were phantom fields the producer never
        sent — they are module constants now, and stray keys in the file are
        ignored rather than resurrecting them."""
        config = load_step_config(
            _write_config(tmp_path, heartbeat_interval=5, log_batch_size=50)
        )

        assert config is not None
        for phantom in ("heartbeat_interval", "log_batch_size", "log_batch_interval"):
            assert not hasattr(config, phantom), phantom

    def test_missing_config_returns_none_with_stderr(self, tmp_path, capsys):
        config = load_step_config(tmp_path / "nonexistent.json")

        assert config is None
        assert "not found" in capsys.readouterr().err

    def test_invalid_json_returns_none_with_stderr(self, tmp_path, capsys):
        config_file = tmp_path / "step_config.json"
        config_file.write_text("not valid json {{{")

        config = load_step_config(config_file)

        assert config is None
        assert "JSON" in capsys.readouterr().err

    def test_missing_required_key_names_the_key(self, tmp_path, capsys):
        config_file = _write_config(tmp_path, auth_token=None)

        config = load_step_config(config_file)

        assert config is None
        assert "auth_token" in capsys.readouterr().err

    def test_list_command_rejected(self, tmp_path, capsys):
        """The producer contract sends command as a raw string; the old
        failure_01 list shape must be refused loudly, not half-worked."""
        config_file = _write_config(tmp_path, command=["echo", "hello"])

        config = load_step_config(config_file)

        assert config is None
        assert "string" in capsys.readouterr().err

    def test_old_field_names_rejected(self, tmp_path, capsys):
        """failure_01's `token`/`working_dir` names must NOT load — one wire
        contract, main's producer names only."""
        config_file = tmp_path / "step_config.json"
        config_file.write_text(json.dumps({
            "step_id": "exec-1",
            "backend_url": "http://backend:8000",
            "token": "t",  # old name
            "command": "ls",
            "working_dir": "/elsewhere",  # old name
        }))

        config = load_step_config(config_file)

        assert config is None
        assert "auth_token" in capsys.readouterr().err
