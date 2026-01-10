#!/bin/bash
# LazyAF Test Runner Script
# Usage: ./scripts/test.sh [unit|integration|demo|e2e|all|coverage]

set -e

SCRIPT_DIR="$(dirname "$0")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

# E2E test configuration
BACKEND_PORT=8765
FRONTEND_PORT=5174
BACKEND_URL="http://localhost:$BACKEND_PORT"
FRONTEND_URL="http://localhost:$FRONTEND_PORT"

# PIDs for cleanup
FRONTEND_PID=""

cleanup() {
    echo "Cleaning up..."
    # Stop Docker backend - only stop specific e2e containers, not all containers
    echo "Stopping E2E backend containers..."
    docker compose stop backend-e2e runner-mock-e2e 2>/dev/null || true
    docker compose rm -f backend-e2e runner-mock-e2e 2>/dev/null || true

    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "Stopping frontend (PID: $FRONTEND_PID) and child processes..."
        # Kill the entire process group
        pkill -P "$FRONTEND_PID" 2>/dev/null || true
        kill "$FRONTEND_PID" 2>/dev/null || true
        wait "$FRONTEND_PID" 2>/dev/null || true
    fi
}

wait_for_service() {
    local url=$1
    local name=$2
    local max_attempts=30
    local attempt=1

    echo "Waiting for $name at $url..."
    while [ $attempt -le $max_attempts ]; do
        if curl -s "$url" > /dev/null 2>&1; then
            echo "$name is ready!"
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    echo "ERROR: $name failed to start at $url"
    return 1
}

start_e2e_backend() {
    echo "Starting E2E backend via Docker on port $BACKEND_PORT..."
    cd "$PROJECT_ROOT"

    # Rebuild and start e2e containers to ensure latest code
    echo "Building e2e containers..."
    docker compose --profile e2e build

    # Start backend container with e2e profile
    docker compose --profile e2e up -d backend-e2e runner-mock-e2e

    # Wait for container health check
    echo "Waiting for backend container to be healthy..."
    local max_attempts=30
    local attempt=1
    while [ $attempt -le $max_attempts ]; do
        local health=$(docker inspect --format='{{.State.Health.Status}}' lazyaf-backend-e2e-1 2>/dev/null || echo "starting")
        if [ "$health" = "healthy" ]; then
            echo "Backend container is healthy!"
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    # Fallback to HTTP check
    wait_for_service "$BACKEND_URL/health" "Backend"
}

start_e2e_frontend() {
    echo "Starting E2E frontend on port $FRONTEND_PORT..."
    cd "$FRONTEND_DIR"

    VITE_BACKEND_URL="$BACKEND_URL" npm run dev -- --port "$FRONTEND_PORT" &
    FRONTEND_PID=$!

    wait_for_service "$FRONTEND_URL" "Frontend"
}

run_e2e_tests() {
    echo "Running E2E tests..."
    cd "$FRONTEND_DIR"

    BACKEND_URL="$BACKEND_URL" FRONTEND_URL="$FRONTEND_URL" \
        npx playwright test "$@"
}

cd "$BACKEND_DIR"

case "${1:-all}" in
    unit)
        echo "Running unit tests..."
        uv run pytest ../tdd/unit -v --tb=short
        ;;
    integration)
        echo "Running integration tests..."
        uv run pytest ../tdd/integration -v --tb=short
        ;;
    demo)
        echo "Running demo tests..."
        uv run pytest ../tdd/demos -v -s --tb=long
        ;;
    e2e)
        echo "Running E2E tests (full browser tests)..."
        trap cleanup EXIT

        start_e2e_backend
        start_e2e_frontend

        # Run Playwright tests
        shift  # Remove 'e2e' from args
        run_e2e_tests "$@"
        ;;
    e2e-quick)
        echo "Running E2E tests (API tests only, no browser)..."
        uv run pytest ../tdd/e2e -v --tb=short -m "not slow"
        ;;
    coverage)
        echo "Running all tests with coverage..."
        uv run pytest ../tdd/unit ../tdd/integration \
            --cov=app \
            --cov-report=html \
            --cov-report=term-missing \
            --cov-fail-under=70
        echo "Coverage report: backend/htmlcov/index.html"
        ;;
    all)
        echo "Running all tests..."
        uv run pytest ../tdd -v --tb=short
        ;;
    *)
        echo "Usage: $0 [unit|integration|demo|e2e|e2e-quick|all|coverage]"
        echo ""
        echo "  unit        - Run fast isolated unit tests"
        echo "  integration - Run API and database tests"
        echo "  demo        - Run workflow demonstrations"
        echo "  e2e         - Run full browser E2E tests (starts backend & frontend)"
        echo "  e2e-quick   - Run E2E API tests only (no browser, no servers needed)"
        echo "  coverage    - Run tests with coverage report"
        echo "  all         - Run all tests (default)"
        echo ""
        echo "E2E options (after 'e2e'):"
        echo "  --headed    - Run with visible browser"
        echo "  --debug     - Debug mode with inspector"
        echo "  --ui        - Open Playwright UI"
        exit 1
        ;;
esac
