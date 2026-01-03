# LazyAF Test Runner Image
# Build: docker build -t lazyaf-test-runner:latest ./images/test-runner
#
# This image extends lazyaf-base with:
# - uv (fast Python package manager)
# - Common test tools (pytest, coverage, etc.)
# - Node.js for frontend testing
#
# Use this for CI/CD pipelines that run Python and JavaScript tests.

FROM lazyaf-base:latest

# Switch to root for package installation
USER root

# Install Node.js 20.x for frontend testing
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
# Using standalone installer for simplicity
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/

# Pre-install common Python test tools globally
# These are available in all steps without needing to install
RUN pip install --no-cache-dir \
    pytest>=7.0 \
    pytest-cov \
    pytest-asyncio \
    coverage

# Pre-install common npm tools globally
RUN npm install -g \
    npm@latest \
    vitest

# Ensure lazyaf user owns the home directory cache
RUN chown -R lazyaf:lazyaf /workspace

# Switch back to non-root user
USER lazyaf

# Verify installations
RUN python --version \
    && node --version \
    && npm --version \
    && uv --version \
    && pytest --version
