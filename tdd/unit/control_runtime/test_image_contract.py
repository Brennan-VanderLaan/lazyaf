"""
Contract tests for the REAL image files (images/**) — Phase 12.3, agent tree
added in 12.5.

Deliberately minimal (the adversarial review pruned the grep-Dockerfile test
theater): the built images are exercised for real in
tdd/integration/services/images/test_base_image_real_docker.py, which owns
labels, users, entrypoint behavior, HOME persistence and the control-mode
round trip. What remains here is ONLY what has no real-Docker equivalent:

- build-input hygiene rules that only text can pin (LF endings, no :latest,
  the .dockerignore/tree_hash pairing, the cache env block, child-image
  build-time install escapes)
- the 12.5 image TREE (base -> agent-base -> {claude, gemini}), because
  re-parenting is a one-line edit that no green build would notice: an image
  still built FROM lazyaf-base:dev works fine right up until an agent step
  runs `python3 -m runner_common.agent_wrapper` in it
- tombstones for retired/parked artifacts
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGES = REPO_ROOT / "images"


def _dockerfile(subdir: str) -> str:
    return (IMAGES / subdir / "Dockerfile").read_text(encoding="utf-8")


BASE_DOCKERFILE = _dockerfile("base")
AGENT_BASE_DOCKERFILE = _dockerfile("agent-base")
CLAUDE_DOCKERFILE = _dockerfile("claude")
GEMINI_DOCKERFILE = _dockerfile("gemini")
TEST_RUNNER_DOCKERFILE = _dockerfile("test-runner")
DEBUG_SIDECAR_DOCKERFILE = _dockerfile("debug-sidecar")


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


class TestAgentRuntimeImage:
    """lazyaf-agent-base (12.5): the agent runtime, and the mock agent image.

    `agent: mock` resolves HERE rather than to a fourth image whose only
    content would be "agent-base minus nothing".
    """

    def test_agent_base_extends_base_dev_tag(self):
        assert "FROM lazyaf-base:dev" in AGENT_BASE_DOCKERFILE

    def test_agent_base_declares_the_agent_runtime_capability(self):
        """A POSITIVE declaration, read by exactly one preflight assertion so
        an agent step pointed at lazyaf-test-runner:dev gets a clear message
        instead of `ModuleNotFoundError: runner_common` 30s into the step.
        Never used for mode selection — that stays on the inherited
        lazyaf.control-layer label."""
        assert "LABEL lazyaf.agent-runtime=1" in AGENT_BASE_DOCKERFILE

    def test_agent_base_inherits_rather_than_restates_control_layer(self):
        """lazyaf.control-layer=1 comes from base, so
        image_supports_control_layer needs no change. Restating it here would
        make the label two-sourced (R3)."""
        assert "LABEL lazyaf.control-layer" not in AGENT_BASE_DOCKERFILE

    def test_agent_base_installs_runner_common_into_the_image(self):
        """PIP_USER=1 is baked and points at the RUNTIME volume, which is
        shadowed at run time: without the PIP_USER=0 escape the install
        vanishes and every agent step dies on import."""
        assert "COPY runner-common/ /opt/runner-common/" in AGENT_BASE_DOCKERFILE
        assert "PIP_USER=0 pip install" in AGENT_BASE_DOCKERFILE
        assert "/opt/runner-common" in AGENT_BASE_DOCKERFILE

    def test_agent_base_verifies_the_wrapper_entrypoint(self):
        """The fixed agent-step command is `python3 -m
        runner_common.agent_wrapper`; the build fails loudly if packaging
        regressed rather than a step failing 30 seconds in."""
        assert "runner_common.agent_wrapper" in AGENT_BASE_DOCKERFILE

    def test_runner_common_is_not_vendored_under_images(self):
        """R3, the whole reason for the staged build context: ONE copy of the
        executors in git. scripts/build_images.py stages
        REPO_ROOT/runner-common into the context at build time."""
        assert not (IMAGES / "agent-base" / "runner-common").exists()
        assert not (IMAGES / "agent-base" / "runner_common").exists()

    def test_agent_base_restamps_content_hash(self):
        assert "LABEL lazyaf.content-hash=$CONTENT_HASH" in AGENT_BASE_DOCKERFILE


class TestChildImages:
    """No real-Docker suite covers the child images yet — text rules stay."""

    def test_claude_extends_agent_base_dev_tag(self):
        """Re-parented in 12.5: claude needs runner-common in the image or
        the wrapper command cannot resolve."""
        assert "FROM lazyaf-agent-base:dev" in CLAUDE_DOCKERFILE
        assert "FROM lazyaf-base:dev" not in CLAUDE_DOCKERFILE

    def test_gemini_extends_agent_base_dev_tag(self):
        assert "FROM lazyaf-agent-base:dev" in GEMINI_DOCKERFILE
        assert "FROM lazyaf-base:dev" not in GEMINI_DOCKERFILE

    def test_test_runner_extends_base_dev_tag(self):
        """test-runner is NOT an agent image: it stays on plain base, and a
        step that points an agent at it fails the preflight."""
        assert "FROM lazyaf-base:dev" in TEST_RUNNER_DOCKERFILE
        assert "lazyaf.agent-runtime" not in TEST_RUNNER_DOCKERFILE

    def test_claude_installs_node_and_cli(self):
        assert "nodejs" in CLAUDE_DOCKERFILE
        assert "@anthropic-ai/claude-code" in CLAUDE_DOCKERFILE

    def test_gemini_installs_node_and_cli(self):
        assert "nodejs" in GEMINI_DOCKERFILE
        assert "@google/gemini-cli" in GEMINI_DOCKERFILE

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
        assert "PIP_USER=0 pip install" in AGENT_BASE_DOCKERFILE
        assert "NPM_CONFIG_PREFIX=/usr/local npm install" in TEST_RUNNER_DOCKERFILE
        assert "NPM_CONFIG_PREFIX=/usr/local npm install" in CLAUDE_DOCKERFILE
        assert "NPM_CONFIG_PREFIX=/usr/local npm install" in GEMINI_DOCKERFILE

    def test_children_restamp_content_hash(self):
        for df in (
            AGENT_BASE_DOCKERFILE,
            CLAUDE_DOCKERFILE,
            GEMINI_DOCKERFILE,
            TEST_RUNNER_DOCKERFILE,
        ):
            assert "LABEL lazyaf.content-hash=$CONTENT_HASH" in df


class TestNoPhantomLatest:
    def test_no_latest_tag_anywhere_in_images_or_build_script(self):
        """Grep-able rule: `:dev` is the only moving tag; failure_01 died
        assuming `:latest` pre-existed."""
        files = [p for p in IMAGES.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        files.append(REPO_ROOT / "scripts" / "build_images.py")
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in (
                "lazyaf-base",
                "lazyaf-agent-base",
                "lazyaf-claude",
                "lazyaf-gemini",
                "lazyaf-test-runner",
                "lazyaf-debug-sidecar",
            ):
                assert f"{name}:latest" not in text, (path, name)

    def test_debug_sidecar_declares_the_inverted_capability_label(self):
        """12.7 UNPARKS debug-sidecar; the tombstone becomes a contract.

        (gemini was on the same tombstone list as fiction through 12.4 —
        12.5 made it real. Same story here: the sidecar exists now, so the
        assertion that has to hold is not "absent" but "absent from the
        control layer". It is the ONLY image in the tree declaring
        lazyaf.control-layer=0, and it must derive FROM lazyaf-base:dev so
        it inherits uid 1000 and joins the content-hash chain — a root
        Ubuntu sidecar would leave root-owned files in the workspace the
        resumed step then trips over."""
        assert (IMAGES / "debug-sidecar" / "Dockerfile").exists()
        assert "FROM lazyaf-base:dev" in DEBUG_SIDECAR_DOCKERFILE
        assert "LABEL lazyaf.control-layer=0" in DEBUG_SIDECAR_DOCKERFILE
        assert "LABEL lazyaf.debug-sidecar=1" in DEBUG_SIDECAR_DOCKERFILE
        assert "LABEL lazyaf.content-hash=$CONTENT_HASH" in DEBUG_SIDECAR_DOCKERFILE
        # No other image may claim the sidecar marker or invert the
        # control-layer label.
        for other in (
            BASE_DOCKERFILE,
            AGENT_BASE_DOCKERFILE,
            CLAUDE_DOCKERFILE,
            GEMINI_DOCKERFILE,
            TEST_RUNNER_DOCKERFILE,
        ):
            assert "lazyaf.debug-sidecar" not in other
            assert "lazyaf.control-layer=0" not in other

    def test_agent_wrapper_not_ported(self):
        """12.5 rebuilds it as a runner-common shim; do not copy it now."""
        assert not (IMAGES / "base" / "control" / "agent_wrapper.py").exists()
