# LazyAF Claude Agent Image
# Build: docker build -t lazyaf-claude:latest ./images/claude
#
# Extends base image with:
# - Node.js (required for Claude Code CLI)
# - Claude Code CLI

FROM lazyaf-base:latest

# Switch to root for installation
USER root

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Switch back to non-root user
USER lazyaf

# Environment for Claude
# These can be overridden at runtime
ENV CLAUDE_CODE_USE_BEDROCK=0
