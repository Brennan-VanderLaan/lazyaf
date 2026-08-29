"""
Unit tests for the step-config producer (Phase 12.3).

Retirement note (wave2-123-wiring design): the assertions that pinned
`control_layer/environment.py` (XDG/PIP/NPM env dict) and
`control_layer/docker.py` (get_volume_mounts) were retired WITH those
modules - the env block is baked into `images/base/Dockerfile` as ENV lines
(pinned by the image contract test) and mounts are LocalExecutor's explicit
MountSpec machinery (pinned by its own suites).

12.3 dead-code sweep: the WorkspaceLayout / initialize_workspace /
get_workspace_paths / write_step_config classes were deleted alongside
their module halves - `images/base/entrypoint.sh` is the single owner of
the /workspace HOME skeleton, covered behaviorally by
`tdd/integration/services/test_home_persistence.py`. What remains here is
the KEPT producer side: `generate_step_config` - the frozen config file
contract the in-container runtime consumes (R3).
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Step Config File (the frozen producer contract - R3)
# -----------------------------------------------------------------------------

class TestStepConfigFile:
    """Tests that verify step config file generation."""

    def test_generates_step_config_json(self):
        """Generates the step config payload for the control runtime."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
        )

        assert config["step_id"] == "step-123"
        assert config["command"] == "python test.py"
        assert config["backend_url"] == "http://backend:8000"
        assert config["auth_token"] == "secret-token"

    def test_config_uses_frozen_key_names(self):
        """The wire contract keys are auth_token / working_directory (the
        audit renames the in-container consumer must match) - never the
        failure_01 names token / working_dir."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
        )

        assert "auth_token" in config
        assert "working_directory" in config
        assert "token" not in config
        assert "working_dir" not in config
        # command travels as the RAW user string (the runtime shell-wraps
        # it), never a pre-split list
        assert isinstance(config["command"], str)

    def test_config_includes_environment(self):
        """Step config includes environment variables."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
            environment={"DEBUG": "1"},
        )

        assert config["environment"]["DEBUG"] == "1"

    def test_config_includes_timeout(self):
        """Step config includes timeout."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
            timeout_seconds=3600,
        )

        assert config["timeout_seconds"] == 3600

    def test_config_shell_defaults_to_bash(self):
        """The 'shell' key travels in the config (contract #2): the runtime
        wraps the command with it, defaulting to bash."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
        )

        assert config["shell"] == "bash"

    def test_config_shell_is_overridable(self):
        """Images without bash declare shell explicitly (e.g. 'sh') in the
        step config; the producer passes it through verbatim."""
        from app.services.control_layer.workspace import generate_step_config

        config = generate_step_config(
            step_id="step-123",
            step_run_id="run-456",
            execution_key="exec-789:0:1",
            command="python test.py",
            backend_url="http://backend:8000",
            auth_token="secret-token",
            shell="sh",
        )

        assert config["shell"] == "sh"
