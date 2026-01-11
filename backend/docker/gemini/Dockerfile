# LazyAF Gemini Image - Phase 12.3
# Base image + Gemini CLI for AI agent steps

FROM lazyaf-base:latest

# Install Gemini CLI (placeholder - adjust based on actual CLI)
# RUN pip install google-generativeai

# Copy agent wrapper script
COPY agent_wrapper/gemini_wrapper.py /control/agent_wrapper.py

# Keep base entrypoint
ENTRYPOINT ["python", "/control/run.py"]
