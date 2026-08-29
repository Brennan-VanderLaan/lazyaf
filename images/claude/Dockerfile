# LazyAF Claude Agent Image (Phase 12.3)
# Build: python scripts/build_images.py  (tag: lazyaf-claude:dev)
#
# Extends lazyaf-base with:
# - Node.js (required for Claude Code CLI)
# - Claude Code CLI
#
# Inherits from base: control layer + entrypoint, lazyaf.control-layer label,
# HOME persistence env block. No USER directives — the base entrypoint drops
# to the lazyaf user via gosu at runtime.

FROM lazyaf-base:dev

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

# Staleness label (stamped by scripts/build_images.py; includes base's hash)
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH
