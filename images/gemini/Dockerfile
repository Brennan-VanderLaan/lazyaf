# LazyAF Gemini Agent Image
# Build: docker build -t lazyaf-gemini:latest ./images/gemini
#
# Extends base image with:
# - Gemini SDK

FROM lazyaf-base:latest

# Switch to root for installation
USER root

# Install Gemini Python SDK
RUN pip install --no-cache-dir google-generativeai

# Switch back to non-root user
USER lazyaf
