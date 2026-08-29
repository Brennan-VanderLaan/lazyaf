# LazyAF Test Runner Image (Phase 12.3)
# Build: python scripts/build_images.py  (tag: lazyaf-test-runner:dev)
#
# Extends lazyaf-base with:
# - uv (fast Python package manager)
# - Common test tools (pytest, coverage, etc.)
# - Node.js + vitest for frontend testing
#
# Use this for CI/CD pipelines that run Python and JavaScript tests (the
# dogfood pipeline's sync-deps/tier steps run on this image).
# No USER directives — the base entrypoint drops to lazyaf via gosu.

FROM lazyaf-base:dev

# Install Node.js 20.x for frontend testing
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv into the image (NOT $HOME — the baked HOME is the runtime
# volume path, shadowed at run time)
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# Pre-install common Python test tools SYSTEM-WIDE.
# - PIP_USER=0 overrides the baked PIP_USER=1 (which targets the runtime
#   volume) for this build-time install.
# - "pytest>=7.0" is QUOTED: unquoted, `>=7.0` is a shell redirect that
#   installs bare `pytest` and writes a file named `=7.0`.
RUN PIP_USER=0 pip install --no-cache-dir \
    "pytest>=7.0" \
    pytest-cov \
    pytest-asyncio \
    coverage

# Pre-install common npm tools into the image (see NPM_CONFIG_PREFIX note)
RUN NPM_CONFIG_PREFIX=/usr/local npm install -g vitest

# Verify installations (build fails loudly if a tool is missing)
RUN python --version \
    && node --version \
    && npm --version \
    && uv --version \
    && pytest --version

# Staleness label (stamped by scripts/build_images.py; includes base's hash)
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH
