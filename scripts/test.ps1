# LazyAF Test Runner Script (PowerShell)
#
# Usage:
#   .\scripts\test.ps1 <target> [type]
#
# Targets:
#   backend   - Python backend tests
#   frontend  - Svelte frontend tests
#   all       - Both backend and frontend
#
# Examples:
#   .\scripts\test.ps1 backend unit
#   .\scripts\test.ps1 backend integration
#   .\scripts\test.ps1 backend coverage
#   .\scripts\test.ps1 frontend e2e
#   .\scripts\test.ps1 frontend e2e:stories
#   .\scripts\test.ps1 frontend e2e:ui
#   .\scripts\test.ps1 frontend unit
#   .\scripts\test.ps1 all

param(
    [Parameter(Position=0)]
    [ValidateSet("backend", "frontend", "all", "help", "install", "docker", "docker-full")]
    [string]$Target = "help",

    [Parameter(Position=1)]
    [string]$TestType = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = "$PSScriptRoot\.."

function Show-Help {
    Write-Host @"

LazyAF Test Runner
==================

Usage: .\scripts\test.ps1 <target> [type]

BACKEND TESTS (Python/pytest)
-----------------------------
  .\scripts\test.ps1 backend              Run all backend tests
  .\scripts\test.ps1 backend unit         Run unit tests only
  .\scripts\test.ps1 backend integration  Run integration tests
  .\scripts\test.ps1 backend demo         Run demo/workflow tests
  .\scripts\test.ps1 backend coverage     Run with coverage report

FRONTEND TESTS (Playwright/Vitest)
----------------------------------
  .\scripts\test.ps1 frontend             Show frontend test options
  .\scripts\test.ps1 frontend e2e         Run all E2E tests
  .\scripts\test.ps1 frontend e2e:ui      Open Playwright UI (interactive)
  .\scripts\test.ps1 frontend e2e:stories Run customer story tests
  .\scripts\test.ps1 frontend e2e:p0      Run P0 critical tests only
  .\scripts\test.ps1 frontend e2e:cards   Run card lifecycle tests
  .\scripts\test.ps1 frontend e2e:playground  Run playground tests
  .\scripts\test.ps1 frontend unit        Run Vitest unit tests
  .\scripts\test.ps1 frontend unit:watch  Run unit tests in watch mode

ALL TESTS
---------
  .\scripts\test.ps1 all                  Run backend + frontend tests

SETUP
-----
  .\scripts\test.ps1 install              Install test dependencies

DOCKER (isolated environment - no backend)
------------------------------------------
  .\scripts\test.ps1 docker build         Build test container
  .\scripts\test.ps1 docker e2e:mocked    Run mocked tests (no backend)
  .\scripts\test.ps1 docker shell         Open shell in test container

DOCKER-FULL (with backend)
--------------------------
  .\scripts\test.ps1 docker-full build    Build test + backend containers
  .\scripts\test.ps1 docker-full e2e      Run all E2E tests with backend
  .\scripts\test.ps1 docker-full e2e:stories  Run story tests with backend

"@ -ForegroundColor Cyan
}

function Run-BackendTests {
    param([string]$Type)

    Push-Location "$ProjectRoot\backend"
    try {
        switch ($Type) {
            "unit" {
                Write-Host "`n=== Backend Unit Tests ===" -ForegroundColor Cyan
                uv run pytest ../tdd/unit -v --tb=short
            }
            "integration" {
                Write-Host "`n=== Backend Integration Tests ===" -ForegroundColor Cyan
                uv run pytest ../tdd/integration -v --tb=short
            }
            "demo" {
                Write-Host "`n=== Backend Demo Tests ===" -ForegroundColor Cyan
                uv run pytest ../tdd/demos -v -s --tb=long
            }
            "coverage" {
                Write-Host "`n=== Backend Tests with Coverage ===" -ForegroundColor Cyan
                uv run pytest ../tdd/unit ../tdd/integration `
                    --cov=app `
                    --cov-report=html `
                    --cov-report=term-missing `
                    --cov-fail-under=70
                Write-Host "`nCoverage report: backend/htmlcov/index.html" -ForegroundColor Green
            }
            default {
                Write-Host "`n=== All Backend Tests ===" -ForegroundColor Cyan
                uv run pytest ../tdd -v --tb=short --ignore=../tdd/frontend
            }
        }
    }
    finally {
        Pop-Location
    }
}

function Run-FrontendTests {
    param([string]$Type)

    # Tests run from tdd/frontend where playwright config and package.json live
    Push-Location "$ProjectRoot\tdd\frontend"
    try {
        switch -Wildcard ($Type) {
            "e2e:ui" {
                Write-Host "`n=== Frontend E2E (Playwright UI) ===" -ForegroundColor Cyan
                pnpm test:e2e:ui
            }
            "e2e:stories" {
                Write-Host "`n=== Frontend E2E (Stories) ===" -ForegroundColor Cyan
                pnpm test:e2e:stories
            }
            "e2e:p0" {
                Write-Host "`n=== Frontend E2E (P0 Critical) ===" -ForegroundColor Cyan
                pnpm test:e2e:p0
            }
            "e2e:critical" {
                Write-Host "`n=== Frontend E2E (Critical Failures) ===" -ForegroundColor Cyan
                pnpm test:e2e:critical
            }
            "e2e:cards" {
                Write-Host "`n=== Frontend E2E (Card Tests) ===" -ForegroundColor Cyan
                pnpm test:e2e:cards
            }
            "e2e:pipeline" {
                Write-Host "`n=== Frontend E2E (Pipeline Tests) ===" -ForegroundColor Cyan
                pnpm test:e2e:pipeline
            }
            "e2e:runner" {
                Write-Host "`n=== Frontend E2E (Runner Tests) ===" -ForegroundColor Cyan
                pnpm test:e2e:runner
            }
            "e2e:playground" {
                Write-Host "`n=== Frontend E2E (Playground Tests) ===" -ForegroundColor Cyan
                pnpm test:e2e:playground
            }
            "e2e:debug" {
                Write-Host "`n=== Frontend E2E (Debug Re-run Tests) ===" -ForegroundColor Cyan
                pnpm test:e2e:debug-rerun
            }
            "e2e:realtime" {
                Write-Host "`n=== Frontend E2E (Realtime Sync) ===" -ForegroundColor Cyan
                pnpm test:e2e:realtime
            }
            "e2e:headed" {
                Write-Host "`n=== Frontend E2E (Headed Browser) ===" -ForegroundColor Cyan
                pnpm test:e2e:headed
            }
            "e2e:report" {
                Write-Host "`n=== Opening E2E Report ===" -ForegroundColor Cyan
                pnpm test:e2e:report
            }
            "e2e" {
                Write-Host "`n=== Frontend E2E (All) ===" -ForegroundColor Cyan
                pnpm test:e2e:all
            }
            "unit:watch" {
                Write-Host "`n=== Frontend Unit Tests (Watch) ===" -ForegroundColor Cyan
                pnpm test:unit:watch
            }
            "unit:coverage" {
                Write-Host "`n=== Frontend Unit Tests (Coverage) ===" -ForegroundColor Cyan
                pnpm test:unit:coverage
            }
            "unit" {
                Write-Host "`n=== Frontend Unit Tests ===" -ForegroundColor Cyan
                pnpm test:unit
            }
            "" {
                Write-Host @"

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

Example: .\scripts\test.ps1 frontend e2e:ui

"@ -ForegroundColor Cyan
            }
            default {
                Write-Host "Unknown frontend test type: $Type" -ForegroundColor Red
                Write-Host "Run '.\scripts\test.ps1 frontend' to see options" -ForegroundColor Yellow
            }
        }
    }
    finally {
        Pop-Location
    }
}

# Main
switch ($Target) {
    "backend" {
        Run-BackendTests -Type $TestType
    }
    "frontend" {
        Run-FrontendTests -Type $TestType
    }
    "all" {
        Write-Host "`n========================================" -ForegroundColor Magenta
        Write-Host "  Running All Tests" -ForegroundColor Magenta
        Write-Host "========================================`n" -ForegroundColor Magenta

        Run-BackendTests -Type ""
        Run-FrontendTests -Type "e2e"
    }
    "install" {
        Write-Host "`n=== Installing Test Dependencies ===" -ForegroundColor Cyan

        Write-Host "`nInstalling frontend test dependencies..." -ForegroundColor Yellow
        Push-Location "$ProjectRoot\tdd\frontend"
        pnpm install
        Pop-Location

        Write-Host "`nInstalling Playwright browsers..." -ForegroundColor Yellow
        Push-Location "$ProjectRoot\tdd\frontend"
        npx playwright install
        Pop-Location

        Write-Host "`nDone! Run '.\scripts\test.ps1 frontend e2e:ui' to start testing." -ForegroundColor Green
    }
    "docker" {
        Push-Location "$ProjectRoot"
        try {
            switch ($TestType) {
                "build" {
                    Write-Host "`n=== Building Test Container ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml build test
                }
                "shell" {
                    Write-Host "`n=== Opening Shell in Test Container ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml run --rm test /bin/bash
                }
                "" {
                    Write-Host @"

Docker Test Options (no backend)
================================

  .\scripts\test.ps1 docker build       Build the test container
  .\scripts\test.ps1 docker shell       Open bash shell in container
  .\scripts\test.ps1 docker e2e:mocked  Run mocked tests (recommended)

For tests WITH backend, use: .\scripts\test.ps1 docker-full

"@ -ForegroundColor Cyan
                }
                default {
                    Write-Host "`n=== Running Tests in Docker (no backend) ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml run --rm test "test:$TestType"
                }
            }
        }
        finally {
            Pop-Location
        }
    }
    "docker-full" {
        Push-Location "$ProjectRoot"
        try {
            switch ($TestType) {
                "build" {
                    Write-Host "`n=== Building Test + Backend Containers ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml build test-with-backend backend
                }
                "shell" {
                    Write-Host "`n=== Opening Shell in Test Container ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml run --rm test-with-backend /bin/bash
                }
                "" {
                    Write-Host @"

Docker-Full Test Options (with backend)
=======================================

  .\scripts\test.ps1 docker-full build       Build test + backend containers
  .\scripts\test.ps1 docker-full e2e         Run all E2E tests
  .\scripts\test.ps1 docker-full e2e:stories Run story tests
  .\scripts\test.ps1 docker-full e2e:p0      Run P0 critical tests
  .\scripts\test.ps1 docker-full shell       Open shell in test container

First time? Run: .\scripts\test.ps1 docker-full build

"@ -ForegroundColor Cyan
                }
                default {
                    Write-Host "`n=== Running Tests in Docker (with backend) ===" -ForegroundColor Cyan
                    docker compose -f docker-compose.test.yml run --rm test-with-backend "test:$TestType"
                }
            }
        }
        finally {
            Pop-Location
        }
    }
    "help" {
        Show-Help
    }
    default {
        Show-Help
    }
}
