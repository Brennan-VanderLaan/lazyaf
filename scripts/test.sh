#!/bin/bash
# LazyAF Test Runner Script
# Usage: ./scripts/test.sh [unit|integration|demo|e2e|e2e-quick|slow|tier|images|all|coverage]

set -e

SCRIPT_DIR="$(dirname "$0")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

# python3 on Linux; Git Bash on Windows sometimes only has `python`.
# Fall back to the literal name so only python-needing lanes fail (loudly).
PYTHON="$(command -v python3 || command -v python || echo python3)"

# E2E test configuration
BACKEND_PORT=8765
FRONTEND_PORT=5174
BACKEND_URL="http://localhost:$BACKEND_PORT"
FRONTEND_URL="http://localhost:$FRONTEND_PORT"

cleanup() {
    echo "Cleaning up..."
    # Stop Docker backend - only stop specific e2e containers, not all containers.
    # The vite dev server is Playwright-owned (see playwright.config.ts) and
    # torn down by Playwright itself - this script only manages the compose stack.
    echo "Stopping E2E backend containers..."
    docker compose stop backend-e2e runner-mock-e2e 2>/dev/null || true
    docker compose rm -f backend-e2e runner-mock-e2e 2>/dev/null || true
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

    # Rebuild and start e2e containers to ensure latest code.
    # The compose override enables the env-gated test-mode API
    # (LAZYAF_TEST_MODE) that the Playwright specs reset/seed through.
    local compose_files="-f docker-compose.yml -f frontend/e2e/compose.test-mode.yml"
    echo "Building e2e containers..."
    docker compose $compose_files --profile e2e build

    # Start backend container with e2e profile
    docker compose $compose_files --profile e2e up -d backend-e2e runner-mock-e2e

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

run_e2e_tests() {
    # Playwright OWNS the vite dev server (reuseExistingServer: false,
    # --strictPort): it starts it on $FRONTEND_URL's port and fails loudly if
    # a stray process is already squatting there. Do not pre-start vite here.
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

        # Run Playwright tests (Playwright starts + owns the vite server)
        shift  # Remove 'e2e' from args
        run_e2e_tests "$@"
        ;;
    e2e-quick)
        echo "Running E2E tests (API tests only, no browser)..."
        uv run pytest ../tdd/e2e -v --tb=short -m "not slow"
        ;;
    slow)
        # The @slow full-stack e2e tests (control layer, real card
        # execution, graph pipeline). They run in NO CI tier today (stated
        # exclusion per R4 - see tdd/README.md): they need this compose
        # stack, which the legacy runner cannot host. Dogfood CI picks them
        # up at Phase 12.4/12.5.
        echo "Running slow full-stack e2e tests (compose stack, in-container)..."
        trap cleanup EXIT

        start_e2e_backend
        shift  # Remove 'slow' from args
        docker exec -w /app -e PYTHONPATH=/app -e E2E_BACKEND_URL=http://localhost:8000 \
            lazyaf-backend-e2e-1 uv run pytest /tdd/e2e -m slow -v --tb=short "$@"
        ;;
    tier)
        # Single-sourced CI tiers: selection + junitxml + R4 gate all live in
        # scripts/run_tier.py (same script the dogfood pipeline runs).
        shift  # Remove 'tier' from args
        "$PYTHON" "$PROJECT_ROOT/scripts/run_tier.py" "$@"
        ;;
    images)
        # 12.3 step images (lazyaf-base:dev -> lazyaf-claude:dev /
        # lazyaf-test-runner:dev). Single-sourced in scripts/build_images.py
        # (dependency order, content-hash staleness skip, --check / --force).
        # The dogfood pipeline and the control-layer/HOME-persistence tests
        # need these tags on the local daemon; a missing tag fails a step
        # loudly with "Image not found: lazyaf-base:dev" by design.
        shift  # Remove 'images' from args
        "$PYTHON" "$PROJECT_ROOT/scripts/build_images.py" "$@"
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
        # No-Docker lanes only: T1 + T3 via the single-sourced tier script.
        # T2 (Docker-dependent) is deliberately excluded - run
        # `./scripts/test.sh tier T2` with Docker up, or the full pipeline.
        echo "Running all no-Docker tiers (T1 + T3)..."
        "$PYTHON" "$PROJECT_ROOT/scripts/run_tier.py" T1 T3
        ;;
    *)
        echo "Usage: $0 [unit|integration|demo|e2e|e2e-quick|slow|tier|images|all|coverage]"
        echo ""
        echo "  unit        - Run fast isolated unit tests"
        echo "  integration - Run API and database tests"
        echo "  demo        - Run workflow demonstrations"
        echo "  e2e         - Run full browser E2E tests (starts compose stack; Playwright owns vite)"
        echo "  e2e-quick   - Run E2E API tests only (no browser, no servers needed)"
        echo "  slow        - Run the @slow full-stack e2e tests in the compose stack (no CI tier runs these)"
        echo "  tier        - Run gated CI tier(s) via scripts/run_tier.py (e.g. 'tier T1', 'tier T2')"
        echo "  images      - Build the 12.3 step images via scripts/build_images.py (--check lists stale)"
        echo "  coverage    - Run tests with coverage report"
        echo "  all         - Run the no-Docker CI tiers T1 + T3 (default; T2 needs Docker)"
        echo ""
        echo "E2E options (after 'e2e'):"
        echo "  --headed    - Run with visible browser"
        echo "  --debug     - Debug mode with inspector"
        echo "  --ui        - Open Playwright UI"
        exit 1
        ;;
esac
