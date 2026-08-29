"""
Contract tests for the REAL image files (images/**) — Phase 12.3.

Deliberately minimal (the adversarial review pruned the grep-Dockerfile test
theater): the built images are exercised for real in
tdd/integration/services/images/test_base_image_real_docker.py, which owns
labels, users, entrypoint behavior, HOME persistence and the control-mode
round trip. What remains here is ONLY what has no real-Docker equivalent:

- build-input hygiene rules that only text can pin (LF endings, no :latest,
  the .dockerignore/tree_hash pairing, the cache env block, child-image
  build-time install escapes)
- tombstones for retired/parked artifacts
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGES = REPO_ROOT / "images"

BASE_DOCKERFILE = (IMAGES / "base" / "Dockerfile").read_text()
CLAUDE_DOCKERFILE = (IMAGES / "claude" / "Dockerfile").read_text()
TEST_RUNNER_DOCKERFILE = (IMAGES / "test-runner" / "Dockerfile").read_text()


class TestBuildInputHygiene:
    def test_entrypoint_lf_line_endings(self):
        """CRLF in the entrypoint breaks the shebang inside the Linux image —
        no real-Docker test can see this once the image builds green on a
        LF checkout."""
        raw = (IMAGES / "base" / "control" / "entrypoint.sh").read_bytes()
        assert b"\r\n" not in raw

    def test_base_dockerignore_excludes_bytecode(self):
        """scripts/build_images.py's tree_hash skips __pycache__; the build
        context must skip it too, or the content-hash label can describe an
        image whose /control differs by stray host bytecode."""
        dockerignore = IMAGES / "base" / ".dockerignore"
        assert dockerignore.exists(), "images/base/.dockerignore missing"
        text = dockerignore.read_text()
        # Patterns anchor at the context root: nested control/__pycache__
        # needs the **/ prefix or the ignore silently does nothing.
        assert "**/__pycache__" in text
        assert "**/*.pyc" in text

    def test_bakes_cache_persistence_env_block(self):
        """The retired control_layer/environment.py block lives as ENV lines.
        The real-Docker suite proves HOME persists but not each cache knob."""
        for line in (
            "XDG_CACHE_HOME=/workspace/home/.cache",
            "XDG_CONFIG_HOME=/workspace/home/.config",
            "XDG_DATA_HOME=/workspace/home/.local/share",
            "PIP_CACHE_DIR=/workspace/home/.cache/pip",
            "PIP_USER=1",
            "PYTHONUSERBASE=/workspace/home/.local",
            "NPM_CONFIG_PREFIX=/workspace/home/.npm-global",
        ):
            assert line in BASE_DOCKERFILE, line
        assert "/workspace/home/.local/bin" in BASE_DOCKERFILE  # PATH


class TestChildImages:
    """No real-Docker suite covers the child images yet — text rules stay."""

    def test_claude_extends_base_dev_tag(self):
        assert "FROM lazyaf-base:dev" in CLAUDE_DOCKERFILE

    def test_test_runner_extends_base_dev_tag(self):
        assert "FROM lazyaf-base:dev" in TEST_RUNNER_DOCKERFILE

    def test_claude_installs_node_and_cli(self):
        assert "nodejs" in CLAUDE_DOCKERFILE
        assert "@anthropic-ai/claude-code" in CLAUDE_DOCKERFILE

    def test_test_runner_quotes_pytest_version_spec(self):
        """Unquoted pytest>=7.0 is a shell redirect (the failure_01 bug)."""
        assert '"pytest>=7.0"' in TEST_RUNNER_DOCKERFILE

    def test_test_runner_installs_uv_into_image_path(self):
        """uv must land in the image, not under the volume-shadowed HOME."""
        assert "uv" in TEST_RUNNER_DOCKERFILE
        assert "UV_INSTALL_DIR=/usr/local/bin" in TEST_RUNNER_DOCKERFILE

    def test_build_time_installs_escape_the_volume_shadowed_env(self):
        """PIP_USER=1 / NPM_CONFIG_PREFIX point at the runtime volume; child
        build steps must override them or their installs vanish at run time."""
        assert "PIP_USER=0 pip install" in TEST_RUNNER_DOCKERFILE
        assert "NPM_CONFIG_PREFIX=/usr/local npm install" in TEST_RUNNER_DOCKERFILE
        assert "NPM_CONFIG_PREFIX=/usr/local npm install" in CLAUDE_DOCKERFILE

    def test_children_restamp_content_hash(self):
        for df in (CLAUDE_DOCKERFILE, TEST_RUNNER_DOCKERFILE):
            assert "LABEL lazyaf.content-hash=$CONTENT_HASH" in df


class TestNoPhantomLatest:
    def test_no_latest_tag_anywhere_in_images_or_build_script(self):
        """Grep-able rule: `:dev` is the only moving tag; failure_01 died
        assuming `:latest` pre-existed."""
        files = [p for p in IMAGES.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        files.append(REPO_ROOT / "scripts" / "build_images.py")
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "lazyaf-base:latest" not in text, path
            assert "lazyaf-claude:latest" not in text, path
            assert "lazyaf-test-runner:latest" not in text, path

    def test_discarded_images_not_ported(self):
        """gemini (fiction) and debug-sidecar (parked for 12.7) stay out."""
        assert not (IMAGES / "gemini").exists()
        assert not (IMAGES / "debug-sidecar").exists()

    def test_agent_wrapper_not_ported(self):
        """12.5 rebuilds it as a runner-common shim; do not copy it now."""
        assert not (IMAGES / "base" / "control" / "agent_wrapper.py").exists()
