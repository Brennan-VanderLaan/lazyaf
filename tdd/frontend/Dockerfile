# Dockerfile for Frontend E2E Tests
#
# Build:   docker build -t lazyaf-tests tdd/frontend
# Run:     docker run --rm -it lazyaf-tests pnpm test:e2e:stories
# UI Mode: See docker-compose.yml for interactive testing

FROM mcr.microsoft.com/playwright:v1.57.0-noble

WORKDIR /app

# Install pnpm
RUN npm install -g pnpm

# Copy package files first for better caching
COPY tdd/frontend/package.json tdd/frontend/pnpm-lock.yaml* ./tdd/frontend/
COPY frontend/package.json frontend/pnpm-lock.yaml* ./frontend/

# Install test dependencies
WORKDIR /app/tdd/frontend
RUN pnpm install --frozen-lockfile || pnpm install

# Install frontend dependencies (needed for dev server)
WORKDIR /app/frontend
RUN pnpm install --frozen-lockfile || pnpm install

# Copy the rest of the project
WORKDIR /app
COPY . .

# Set working directory for tests
WORKDIR /app/tdd/frontend

# Default command - show help
CMD ["pnpm", "test:e2e", "--help"]
