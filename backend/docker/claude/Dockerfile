# LazyAF Claude Image - Phase 12.3
# Base image + Claude CLI for AI agent steps

FROM lazyaf-base:latest

# Install Node.js for Claude CLI
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Copy agent wrapper script
COPY agent_wrapper/claude_wrapper.py /control/agent_wrapper.py

# Keep base entrypoint
ENTRYPOINT ["python", "/control/run.py"]
