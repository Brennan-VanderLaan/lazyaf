FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    gh \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies
RUN pip install --no-cache-dir requests

# Create workspace directory
WORKDIR /workspace

# Copy entrypoint script
COPY entrypoint.py /entrypoint.py

# Environment variables (set at runtime)
ENV BACKEND_URL="http://host.docker.internal:8000"
ENV RUNNER_NAME=""
ENV POLL_INTERVAL="5"
ENV ANTHROPIC_API_KEY=""
ENV GITHUB_TOKEN=""

ENTRYPOINT ["python", "/entrypoint.py"]
