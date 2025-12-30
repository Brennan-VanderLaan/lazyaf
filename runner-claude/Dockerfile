# LazyAF Runner - Claude Code
# Build: docker build -t lazyaf-runner-claude:latest ./runner-claude
FROM python:3.12-slim

# Install system dependencies including sudo
RUN apt-get update && apt-get install -y \
    git \
    curl \
    gh \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies
RUN pip install --no-cache-dir requests

# Create non-root user for running Claude Code
# Claude Code refuses to run with --dangerously-skip-permissions as root
RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create workspace directory and give ownership to runner
RUN mkdir -p /workspace && chown -R runner:runner /workspace

# Create .claude directory for Claude Code CLI data (todos, debug, settings)
RUN mkdir -p /home/runner/.claude && chown -R runner:runner /home/runner/.claude

# Copy entrypoint script
COPY entrypoint.py /entrypoint.py
RUN chown runner:runner /entrypoint.py

# Switch to non-root user
USER runner

# Set working directory
WORKDIR /workspace

# Environment variables (set at runtime)
ENV BACKEND_URL="http://host.docker.internal:8000"
ENV RUNNER_TYPE="claude-code"
ENV RUNNER_NAME=""
ENV POLL_INTERVAL="5"
ENV ANTHROPIC_API_KEY=""
ENV GITHUB_TOKEN=""

ENTRYPOINT ["python", "/entrypoint.py"]
