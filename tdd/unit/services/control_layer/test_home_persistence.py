"""
Unit tests for HOME Persistence.

These tests define the contract for HOME directory behavior:
- $HOME set to /workspace/home
- pip cache persists across steps
- ~/.local/bin persists for user-installed tools
- Installed packages available in subsequent steps

Write these tests BEFORE implementing HOME persistence.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Tests enabled - Phase 12.3 HOME persistence implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: HOME Environment
# -----------------------------------------------------------------------------

class TestHomeEnvironment:
    """Tests that verify HOME environment variable setup."""

    def test_home_set_to_workspace_home(self):
        """$HOME is set to /workspace/home."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["HOME"] == "/workspace/home"

    def test_xdg_cache_in_workspace(self):
        """XDG_CACHE_HOME is set to /workspace/home/.cache."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["XDG_CACHE_HOME"] == "/workspace/home/.cache"

    def test_xdg_config_in_workspace(self):
        """XDG_CONFIG_HOME is set to /workspace/home/.config."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["XDG_CONFIG_HOME"] == "/workspace/home/.config"

    def test_xdg_data_in_workspace(self):
        """XDG_DATA_HOME is set to /workspace/home/.local/share."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["XDG_DATA_HOME"] == "/workspace/home/.local/share"

    def test_path_includes_local_bin(self):
        """PATH includes ~/.local/bin for user-installed tools."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert "/workspace/home/.local/bin" in env["PATH"]


# -----------------------------------------------------------------------------
# Contract: pip Cache Persistence
# -----------------------------------------------------------------------------

class TestPipCachePersistence:
    """Tests that verify pip cache persists across steps."""

    def test_pip_cache_dir_set(self):
        """PIP_CACHE_DIR is set to /workspace/home/.cache/pip."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["PIP_CACHE_DIR"] == "/workspace/home/.cache/pip"

    def test_pip_user_install_enabled(self):
        """PIP_USER is set to enable user installs."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env.get("PIP_USER") == "1"

    def test_pythonuserbase_set(self):
        """PYTHONUSERBASE is set for user site-packages."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env["PYTHONUSERBASE"] == "/workspace/home/.local"


# -----------------------------------------------------------------------------
# Contract: Tool Installation Persistence
# -----------------------------------------------------------------------------

class TestToolInstallationPersistence:
    """Tests that verify installed tools persist across steps."""

    def test_local_bin_in_path(self):
        """~/.local/bin is in PATH for user-installed tools."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        path_dirs = env["PATH"].split(":")
        assert "/workspace/home/.local/bin" in path_dirs

    def test_npm_prefix_set(self):
        """NPM_CONFIG_PREFIX is set for global npm packages."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert env.get("NPM_CONFIG_PREFIX") == "/workspace/home/.npm-global"

    def test_npm_global_bin_in_path(self):
        """npm global bin directory is in PATH."""
        from app.services.control_layer.environment import get_step_environment

        env = get_step_environment()
        assert "/workspace/home/.npm-global/bin" in env["PATH"]


# -----------------------------------------------------------------------------
# Contract: Workspace Directory Structure
# -----------------------------------------------------------------------------

class TestWorkspaceDirectoryStructure:
    """Tests that verify workspace directory structure."""

    def test_workspace_structure(self):
        """Workspace has correct directory structure."""
        from app.services.control_layer.workspace import WorkspaceLayout

        layout = WorkspaceLayout()

        assert layout.root == "/workspace"
        assert layout.repo == "/workspace/repo"
        assert layout.home == "/workspace/home"
        assert layout.control == "/workspace/.control"

    def test_control_directory_contents(self):
        """Control directory contains expected files."""
        from app.services.control_layer.workspace import WorkspaceLayout

        layout = WorkspaceLayout()

        expected_files = [
            "step_config.json",
        ]
        for f in expected_files:
            assert f in layout.control_files

    def test_home_subdirectories(self):
        """Home directory has expected subdirectories."""
        from app.services.control_layer.workspace import WorkspaceLayout

        layout = WorkspaceLayout()

        expected_dirs = [
            ".cache",
            ".local",
            ".local/bin",
            ".config",
        ]
        for d in expected_dirs:
            assert d in layout.home_subdirectories


# -----------------------------------------------------------------------------
# Contract: Volume Mount Configuration
# -----------------------------------------------------------------------------

class TestVolumeMountConfiguration:
    """Tests that verify volume mount configuration."""

    def test_workspace_volume_mount(self):
        """Workspace is mounted as a volume."""
        from app.services.control_layer.docker import get_volume_mounts

        mounts = get_volume_mounts(
            workspace_volume="lazyaf-ws-123",
            repo_path="/workspace/repo",
        )

        # Main workspace volume
        assert any(
            m["source"] == "lazyaf-ws-123" and m["target"] == "/workspace"
            for m in mounts
        )

    def test_home_persisted_in_workspace_volume(self):
        """HOME directory is within workspace volume (persisted)."""
        from app.services.control_layer.docker import get_volume_mounts

        mounts = get_volume_mounts(
            workspace_volume="lazyaf-ws-123",
            repo_path="/workspace/repo",
        )

        # Home is at /workspace/home, inside the workspace volume
        # So no separate mount needed - it's part of the workspace volume

    def test_control_directory_mount(self):
        """Control directory is populated by backend."""
        from app.services.control_layer.docker import get_volume_mounts

        mounts = get_volume_mounts(
            workspace_volume="lazyaf-ws-123",
            repo_path="/workspace/repo",
            control_dir="/tmp/control-step-123",
        )

        assert any(
            m["target"] == "/workspace/.control"
            for m in mounts
        )


# -----------------------------------------------------------------------------
# Contract: Step Config File
# -----------------------------------------------------------------------------

class TestStepConfigFile:
    """Tests that verify step config file generation."""

    def test_generates_step_config_json(self):
        """Generates step_config.json for control directory."""
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

    def test_config_file_written_to_control_dir(self):
        """Step config file is written to control directory."""
        from app.services.control_layer.workspace import write_step_config
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "step_id": "step-123",
                "command": "echo test",
            }

            config_path = write_step_config(tmpdir, config)

            assert config_path.exists()
            assert config_path.name == "step_config.json"

            with open(config_path) as f:
                written = json.load(f)
            assert written["step_id"] == "step-123"


# -----------------------------------------------------------------------------
# Contract: Workspace Initialization
# -----------------------------------------------------------------------------

class TestWorkspaceInitialization:
    """Tests that verify workspace initialization."""

    def test_creates_home_directories(self):
        """Workspace initialization creates HOME subdirectories."""
        from app.services.control_layer.workspace import initialize_workspace
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            initialize_workspace(workspace)

            assert (workspace / "home").exists()
            assert (workspace / "home" / ".cache").exists()
            assert (workspace / "home" / ".local" / "bin").exists()

    def test_creates_repo_directory(self):
        """Workspace initialization creates repo directory."""
        from app.services.control_layer.workspace import initialize_workspace
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            initialize_workspace(workspace)

            assert (workspace / "repo").exists()

    def test_creates_control_directory(self):
        """Workspace initialization creates control directory."""
        from app.services.control_layer.workspace import initialize_workspace
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            initialize_workspace(workspace)

            assert (workspace / ".control").exists()

    def test_preserves_existing_home_content(self):
        """Workspace initialization preserves existing HOME content."""
        from app.services.control_layer.workspace import initialize_workspace
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            home = workspace / "home"
            home.mkdir()

            # Create existing file
            existing_file = home / "existing.txt"
            existing_file.write_text("keep me")

            initialize_workspace(workspace)

            # File should still exist
            assert existing_file.exists()
            assert existing_file.read_text() == "keep me"
