# LazyAF Gemini Agent Image (Phase 12.5)
# Build: python scripts/build_images.py  (tag: lazyaf-gemini:dev)
#
# Extends lazyaf-agent-base with:
# - Node.js 20 (required for the Gemini CLI)
# - @google/gemini-cli
#
# Inherits from agent-base: runner-common (so `python3 -m
# runner_common.agent_wrapper` resolves) and lazyaf.agent-runtime=1.
# Inherits from base: control layer + entrypoint, lazyaf.control-layer=1,
# HOME persistence env block. No USER directives — the base entrypoint drops
# to the lazyaf user via gosu at runtime.

FROM lazyaf-agent-base:dev

# Install Node.js (required for the Gemini CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install the Gemini CLI globally.
# NPM_CONFIG_PREFIX override: the baked env points npm at /workspace (the
# runtime volume, shadowed at run time) — build-time installs must land in
# the image under /usr/local.
RUN NPM_CONFIG_PREFIX=/usr/local npm install -g @google/gemini-cli

# Staleness label (stamped by scripts/build_images.py; chains agent-base's
# hash, which chains base's)
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH
