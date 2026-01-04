#!/bin/bash
# LazyAF Test Runner Script (Bash)
#
# Usage:
#   ./scripts/test.sh <target> [type]
#
# Targets:
#   backend   - Python backend tests
#   frontend  - Svelte frontend tests
#   all       - Both backend and frontend
#
# Examples:
#   ./scripts/test.sh backend unit
#   ./scripts/test.sh backend integration
#   ./scripts/test.sh backend coverage
#   ./scripts/test.sh frontend e2e
#   ./scripts/test.sh frontend e2e:stories
#   ./scripts/test.sh frontend e2e:ui
#   ./scripts/test.sh frontend unit
#   ./scripts/test.sh all

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
MAGENTA='\033[0;35m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

show_help() {
    echo -e "${CYAN}"
    cat << 'EOF'

LazyAF Test Runner
==================

Usage: ./scripts/test.sh <target> [type]

BACKEND TESTS (Python/pytest)
-----------------------------
  ./scripts/test.sh backend              Run all backend tests
  ./scripts/test.sh backend unit         Run unit tests only
  ./scripts/test.sh backend integration  Run integration tests
  ./scripts/test.sh backend demo         Run demo/workflow tests
  ./scripts/test.sh backend coverage     Run with coverage report

FRONTEND TESTS (Playwright/Vitest)
----------------------------------
  ./scripts/test.sh frontend             Show frontend test options
  ./scripts/test.sh frontend e2e         Run all E2E tests
  ./scripts/test.sh frontend e2e:ui      Open Playwright UI (interactive)
  ./scripts/test.sh frontend e2e:stories Run customer story tests
  ./scripts/test.sh frontend e2e:p0      Run P0 critical tests only
  ./scripts/test.sh frontend e2e:cards   Run card lifecycle tests
  ./scripts/test.sh frontend e2e:playground  Run playground tests
  ./scripts/test.sh frontend unit        Run Vitest unit tests
  ./scripts/test.sh frontend unit:watch  Run unit tests in watch mode

ALL TESTS
---------
  ./scripts/test.sh all                  Run backend + frontend tests

SETUP
-----
  ./scripts/test.sh install              Install test dependencies

DOCKER (isolated environment - no backend)
------------------------------------------
  ./scripts/test.sh docker build         Build test container
  ./scripts/test.sh docker e2e:mocked    Run mocked tests (no backend)
  ./scripts/test.sh docker shell         Open shell in test container

DOCKER-FULL (with backend)
--------------------------
  ./scripts/test.sh docker-full build    Build test + backend containers
  ./scripts/test.sh docker-full e2e      Run all E2E tests with backend
  ./scripts/test.sh docker-full e2e:stories  Run story tests with backend

EOF
    echo -e "${NC}"
}

run_backend_tests() {
    local test_type="${1:-all}"

    cd "$PROJECT_ROOT/backend"

    case "$test_type" in
        unit)
            echo -e "\n${CYAN}=== Backend Unit Tests ===${NC}"
            uv run pytest ../tdd/unit -v --tb=short
            ;;
        integration)
            echo -e "\n${CYAN}=== Backend Integration Tests ===${NC}"
            uv run pytest ../tdd/integration -v --tb=short
            ;;
        demo)
            echo -e "\n${CYAN}=== Backend Demo Tests ===${NC}"
            uv run pytest ../tdd/demos -v -s --tb=long
            ;;
        coverage)
            echo -e "\n${CYAN}=== Backend Tests with Coverage ===${NC}"
            uv run pytest ../tdd/unit ../tdd/integration \
                --cov=app \
                --cov-report=html \
                --cov-report=term-missing \
                --cov-fail-under=70
            echo -e "\n${GREEN}Coverage report: backend/htmlcov/index.html${NC}"
            ;;
        all|*)
            echo -e "\n${CYAN}=== All Backend Tests ===${NC}"
            uv run pytest ../tdd -v --tb=short --ignore=../tdd/frontend
            ;;
    esac
}

show_frontend_options() {
    echo -e "${CYAN}"
    cat << 'EOF'

Frontend Test Options
=====================

E2E Tests (Playwright):
  e2e           Run all E2E tests
  e2e:ui        Open Playwright UI (recommended for development)
  e2e:headed    Run with visible browser
  e2e:stories   Customer story tests
  e2e:p0        P0 critical tests (cards + failures)
  e2e:critical  Critical failure handling
  e2e:cards     Card lifecycle tests
  e2e:pipeline  Pipeline tests
  e2e:runner    Runner visibility tests
  e2e:playground Agent playground tests
  e2e:debug     Debug re-run tests
  e2e:realtime  Real-time sync tests
  e2e:report    Open last test report

Unit Tests (Vitest):
  unit          Run unit tests
  unit:watch    Watch mode
  unit:coverage With coverage

Example: ./scripts/test.sh frontend e2e:ui

EOF
    echo -e "${NC}"
}

run_frontend_tests() {
    local test_type="$1"

    # Tests run from tdd/frontend where playwright config and package.json live
    cd "$PROJECT_ROOT/tdd/frontend"

    case "$test_type" in
        e2e:ui)
            echo -e "\n${CYAN}=== Frontend E2E (Playwright UI) ===${NC}"
            pnpm test:e2e:ui
            ;;
        e2e:stories)
            echo -e "\n${CYAN}=== Frontend E2E (Stories) ===${NC}"
            pnpm test:e2e:stories
            ;;
        e2e:p0)
            echo -e "\n${CYAN}=== Frontend E2E (P0 Critical) ===${NC}"
            pnpm test:e2e:p0
            ;;
        e2e:critical)
            echo -e "\n${CYAN}=== Frontend E2E (Critical Failures) ===${NC}"
            pnpm test:e2e:critical
            ;;
        e2e:cards)
            echo -e "\n${CYAN}=== Frontend E2E (Card Tests) ===${NC}"
            pnpm test:e2e:cards
            ;;
        e2e:pipeline)
            echo -e "\n${CYAN}=== Frontend E2E (Pipeline Tests) ===${NC}"
            pnpm test:e2e:pipeline
            ;;
        e2e:runner)
            echo -e "\n${CYAN}=== Frontend E2E (Runner Tests) ===${NC}"
            pnpm test:e2e:runner
            ;;
        e2e:playground)
            echo -e "\n${CYAN}=== Frontend E2E (Playground Tests) ===${NC}"
            pnpm test:e2e:playground
            ;;
        e2e:debug)
            echo -e "\n${CYAN}=== Frontend E2E (Debug Re-run Tests) ===${NC}"
            pnpm test:e2e:debug-rerun
            ;;
        e2e:realtime)
            echo -e "\n${CYAN}=== Frontend E2E (Realtime Sync) ===${NC}"
            pnpm test:e2e:realtime
            ;;
        e2e:headed)
            echo -e "\n${CYAN}=== Frontend E2E (Headed Browser) ===${NC}"
            pnpm test:e2e:headed
            ;;
        e2e:report)
            echo -e "\n${CYAN}=== Opening E2E Report ===${NC}"
            pnpm test:e2e:report
            ;;
        e2e)
            echo -e "\n${CYAN}=== Frontend E2E (All) ===${NC}"
            pnpm test:e2e:all
            ;;
        unit:watch)
            echo -e "\n${CYAN}=== Frontend Unit Tests (Watch) ===${NC}"
            pnpm test:unit:watch
            ;;
        unit:coverage)
            echo -e "\n${CYAN}=== Frontend Unit Tests (Coverage) ===${NC}"
            pnpm test:unit:coverage
            ;;
        unit)
            echo -e "\n${CYAN}=== Frontend Unit Tests ===${NC}"
            pnpm test:unit
            ;;
        "")
            show_frontend_options
            ;;
        *)
            echo -e "${RED}Unknown frontend test type: $test_type${NC}"
            echo -e "${YELLOW}Run './scripts/test.sh frontend' to see options${NC}"
            exit 1
            ;;
    esac
}

# Main
TARGET="${1:-help}"
TEST_TYPE="${2:-}"

case "$TARGET" in
    backend)
        run_backend_tests "$TEST_TYPE"
        ;;
    frontend)
        run_frontend_tests "$TEST_TYPE"
        ;;
    all)
        echo -e "\n${MAGENTA}========================================${NC}"
        echo -e "${MAGENTA}  Running All Tests${NC}"
        echo -e "${MAGENTA}========================================${NC}\n"

        run_backend_tests ""
        run_frontend_tests "e2e"
        ;;
    install)
        echo -e "\n${CYAN}=== Installing Test Dependencies ===${NC}"

        echo -e "\n${YELLOW}Installing frontend test dependencies...${NC}"
        cd "$PROJECT_ROOT/tdd/frontend"
        pnpm install

        echo -e "\n${YELLOW}Installing Playwright browsers...${NC}"
        npx playwright install

        echo -e "\n${GREEN}Done! Run './scripts/test.sh frontend e2e:ui' to start testing.${NC}"
        ;;
    docker)
        cd "$PROJECT_ROOT"
        case "$TEST_TYPE" in
            build)
                echo -e "\n${CYAN}=== Building Test Container ===${NC}"
                docker compose -f docker-compose.test.yml build test
                ;;
            shell)
                echo -e "\n${CYAN}=== Opening Shell in Test Container ===${NC}"
                docker compose -f docker-compose.test.yml run --rm test /bin/bash
                ;;
            "")
                echo -e "${CYAN}"
                cat << 'DOCKEREOF'

Docker Test Options (no backend)
================================

  ./scripts/test.sh docker build       Build the test container
  ./scripts/test.sh docker shell       Open bash shell in container
  ./scripts/test.sh docker e2e:mocked  Run mocked tests (recommended)

For tests WITH backend, use: ./scripts/test.sh docker-full

DOCKEREOF
                echo -e "${NC}"
                ;;
            *)
                echo -e "\n${CYAN}=== Running Tests in Docker (no backend) ===${NC}"
                docker compose -f docker-compose.test.yml run --rm test "test:$TEST_TYPE"
                ;;
        esac
        ;;
    docker-full)
        cd "$PROJECT_ROOT"
        case "$TEST_TYPE" in
            build)
                echo -e "\n${CYAN}=== Building Test + Backend Containers ===${NC}"
                docker compose -f docker-compose.test.yml build test-with-backend backend
                ;;
            shell)
                echo -e "\n${CYAN}=== Opening Shell in Test Container ===${NC}"
                docker compose -f docker-compose.test.yml run --rm test-with-backend /bin/bash
                ;;
            "")
                echo -e "${CYAN}"
                cat << 'DOCKEREOF'

Docker-Full Test Options (with backend)
=======================================

  ./scripts/test.sh docker-full build       Build test + backend containers
  ./scripts/test.sh docker-full e2e         Run all E2E tests
  ./scripts/test.sh docker-full e2e:stories Run story tests
  ./scripts/test.sh docker-full e2e:p0      Run P0 critical tests
  ./scripts/test.sh docker-full shell       Open shell in test container

First time? Run: ./scripts/test.sh docker-full build

DOCKEREOF
                echo -e "${NC}"
                ;;
            *)
                echo -e "\n${CYAN}=== Running Tests in Docker (with backend) ===${NC}"
                docker compose -f docker-compose.test.yml run --rm test-with-backend "test:$TEST_TYPE"
                ;;
        esac
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        show_help
        exit 1
        ;;
esac
