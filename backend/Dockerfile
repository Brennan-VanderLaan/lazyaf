FROM python:3.12-slim

WORKDIR /app

# Build arg to include dev dependencies (for e2e testing)
ARG INCLUDE_DEV=false

# Install curl for healthcheck and git for tests
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml uv.lock ./

# Install dependencies (include dev deps if INCLUDE_DEV=true)
RUN if [ "$INCLUDE_DEV" = "true" ]; then uv sync --frozen --all-extras; else uv sync --frozen --no-dev; fi

# Copy application code
COPY app ./app

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
