#!/bin/bash
# LazyAF Test Runner Script
# Usage: ./scripts/test.sh [unit|integration|demo|all|coverage]

set -e

cd "$(dirname "$0")/../backend"

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
        echo "Usage: $0 [unit|integration|demo|all|coverage]"
        echo ""
        echo "  unit        - Run fast isolated unit tests"
        echo "  integration - Run API and database tests"
        echo "  demo        - Run workflow demonstrations"
        echo "  coverage    - Run tests with coverage report"
        echo "  all         - Run all tests (default)"
        exit 1
        ;;
esac
