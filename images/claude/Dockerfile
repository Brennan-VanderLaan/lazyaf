# LazyAF Claude Agent Image (Phase 12.3, re-parented onto agent-base in 12.5)
# Build: python scripts/build_images.py  (tag: lazyaf-claude:dev)
#
# Extends lazyaf-agent-base with:
# - Node.js (required for Claude Code CLI)
# - Claude Code CLI
#
# Inherits from agent-base: runner-common installed system-wide (so the fixed
# agent-step command `python3 -m runner_common.agent_wrapper` resolves) and
# the lazyaf.agent-runtime=1 capability label.
# Inherits from base: control layer + entrypoint, lazyaf.control-layer label,
# HOME persistence env block. No USER directives — the base entrypoint drops
# to the lazyaf user via gosu at runtime.

FROM lazyaf-agent-base:dev

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally.
# NPM_CONFIG_PREFIX override: the baked env points npm at /workspace (the
# runtime volume, shadowed at run time) — build-time installs must land in
# the image under /usr/local.
RUN NPM_CONFIG_PREFIX=/usr/local npm install -g @anthropic-ai/claude-code

# Environment for Claude (overridable at runtime)
ENV CLAUDE_CODE_USE_BEDROCK=0

# Staleness label (stamped by scripts/build_images.py; chains agent-base's
# hash, which chains base's)
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH
