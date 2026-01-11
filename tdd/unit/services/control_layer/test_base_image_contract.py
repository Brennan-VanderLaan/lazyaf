"""
Unit tests for Base Image Contract.

These tests define the contract for Docker base images:
- Python 3.12 and Git available
- Control layer at /control/run.py
- Default entrypoint runs control layer
- Required tools and directories present

Write these tests BEFORE building the base images.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Tests enabled - Phase 12.3 base image implemented

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Contract: Base Image Requirements
# -----------------------------------------------------------------------------

class TestBaseImageRequirements:
    """Tests that verify base image has required components."""

    def test_python_312_available(self):
        """Base image has Python 3.12+ available."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert contract.python_version >= (3, 12)

    def test_git_available(self):
        """Base image has git available."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert contract.has_git is True

    def test_curl_available(self):
        """Base image has curl available."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert contract.has_curl is True

    def test_control_layer_script_exists(self):
        """Base image has control layer at /control/run.py."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert contract.control_layer_path == "/control/run.py"

    def test_entrypoint_runs_control_layer(self):
        """Default entrypoint runs the control layer."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert contract.entrypoint == ["python", "/control/run.py"]

    def test_workspace_directory_exists(self):
        """Base image has /workspace directory."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert "/workspace" in contract.required_directories

    def test_control_directory_exists(self):
        """Base image has /control directory."""
        from app.services.control_layer.image import BaseImageContract

        contract = BaseImageContract()
        assert "/control" in contract.required_directories


# -----------------------------------------------------------------------------
# Contract: Dockerfile Generation
# -----------------------------------------------------------------------------

class TestDockerfileGeneration:
    """Tests that verify Dockerfile generation for base image."""

    def test_generates_valid_dockerfile(self):
        """Generates a valid Dockerfile for base image."""
        from app.services.control_layer.image import generate_base_dockerfile

        dockerfile = generate_base_dockerfile()

        assert "FROM python:3.12-slim" in dockerfile
        assert "RUN apt-get update" in dockerfile
        assert "git" in dockerfile
        assert "curl" in dockerfile

    def test_dockerfile_copies_control_layer(self):
        """Dockerfile copies control layer script."""
        from app.services.control_layer.image import generate_base_dockerfile

        dockerfile = generate_base_dockerfile()

        assert "COPY" in dockerfile
        assert "/control/" in dockerfile

    def test_dockerfile_sets_entrypoint(self):
        """Dockerfile sets correct entrypoint."""
        from app.services.control_layer.image import generate_base_dockerfile

        dockerfile = generate_base_dockerfile()

        assert 'ENTRYPOINT ["python", "/control/run.py"]' in dockerfile

    def test_dockerfile_creates_workspace(self):
        """Dockerfile creates workspace directory."""
        from app.services.control_layer.image import generate_base_dockerfile

        dockerfile = generate_base_dockerfile()

        assert "mkdir" in dockerfile.lower() or "WORKDIR /workspace" in dockerfile

    def test_dockerfile_sets_workdir(self):
        """Dockerfile sets working directory to /workspace."""
        from app.services.control_layer.image import generate_base_dockerfile

        dockerfile = generate_base_dockerfile()

        assert "WORKDIR /workspace" in dockerfile


# -----------------------------------------------------------------------------
# Contract: Agent-Specific Images
# -----------------------------------------------------------------------------

class TestAgentSpecificImages:
    """Tests that verify agent-specific image requirements."""

    def test_claude_image_extends_base(self):
        """Claude image extends base image."""
        from app.services.control_layer.image import generate_claude_dockerfile

        dockerfile = generate_claude_dockerfile()

        assert "FROM lazyaf-base" in dockerfile

    def test_claude_image_has_node(self):
        """Claude image has Node.js for Claude CLI."""
        from app.services.control_layer.image import generate_claude_dockerfile

        dockerfile = generate_claude_dockerfile()

        # Should install node
        assert "node" in dockerfile.lower() or "npm" in dockerfile.lower()

    def test_claude_image_has_agent_wrapper(self):
        """Claude image has agent wrapper script."""
        from app.services.control_layer.image import generate_claude_dockerfile

        dockerfile = generate_claude_dockerfile()

        assert "agent_wrapper" in dockerfile or "COPY" in dockerfile

    def test_gemini_image_extends_base(self):
        """Gemini image extends base image."""
        from app.services.control_layer.image import generate_gemini_dockerfile

        dockerfile = generate_gemini_dockerfile()

        assert "FROM lazyaf-base" in dockerfile


# -----------------------------------------------------------------------------
# Contract: Image Registry
# -----------------------------------------------------------------------------

class TestImageRegistry:
    """Tests that verify image naming and tagging."""

    def test_base_image_name(self):
        """Base image has correct name."""
        from app.services.control_layer.image import IMAGE_NAMES

        assert IMAGE_NAMES["base"] == "lazyaf-base"

    def test_claude_image_name(self):
        """Claude image has correct name."""
        from app.services.control_layer.image import IMAGE_NAMES

        assert IMAGE_NAMES["claude"] == "lazyaf-claude"

    def test_gemini_image_name(self):
        """Gemini image has correct name."""
        from app.services.control_layer.image import IMAGE_NAMES

        assert IMAGE_NAMES["gemini"] == "lazyaf-gemini"

    def test_script_image_name(self):
        """Script step image has correct name."""
        from app.services.control_layer.image import IMAGE_NAMES

        assert IMAGE_NAMES["script"] == "lazyaf-base"

    def test_image_tag_format(self):
        """Image tags follow version format."""
        from app.services.control_layer.image import get_image_tag

        tag = get_image_tag("base", version="1.0.0")
        assert tag == "lazyaf-base:1.0.0"

    def test_default_tag_is_latest(self):
        """Default image tag is 'latest'."""
        from app.services.control_layer.image import get_image_tag

        tag = get_image_tag("base")
        assert tag == "lazyaf-base:latest"


# -----------------------------------------------------------------------------
# Contract: Control Layer Script Content
# -----------------------------------------------------------------------------

class TestControlLayerScriptContent:
    """Tests that verify the control layer script content."""

    def test_script_reads_config(self):
        """Control layer script reads step config."""
        from app.services.control_layer.image import get_control_layer_script

        script = get_control_layer_script()

        assert "step_config.json" in script
        assert "/workspace/.control" in script

    def test_script_reports_status(self):
        """Control layer script reports status to backend."""
        from app.services.control_layer.image import get_control_layer_script

        script = get_control_layer_script()

        assert "report_status" in script or "status" in script

    def test_script_handles_signals(self):
        """Control layer script handles termination signals."""
        from app.services.control_layer.image import get_control_layer_script

        script = get_control_layer_script()

        assert "signal" in script.lower() or "sigterm" in script.lower()

    def test_script_streams_logs(self):
        """Control layer script streams logs to backend."""
        from app.services.control_layer.image import get_control_layer_script

        script = get_control_layer_script()

        assert "log" in script.lower()

    def test_script_sends_heartbeats(self):
        """Control layer script sends heartbeats."""
        from app.services.control_layer.image import get_control_layer_script

        script = get_control_layer_script()

        assert "heartbeat" in script.lower()
