# LazyAF Test Runner Script (PowerShell)
# Usage: .\scripts\test.ps1 [unit|integration|demo|all|coverage]

param(
    [Parameter(Position=0)]
    [ValidateSet("unit", "integration", "demo", "all", "coverage")]
    [string]$TestType = "all"
)

$ErrorActionPreference = "Stop"

Push-Location "$PSScriptRoot\..\backend"

try {
    switch ($TestType) {
        "unit" {
            Write-Host "Running unit tests..." -ForegroundColor Cyan
            uv run pytest ../tdd/unit -v --tb=short
        }
        "integration" {
            Write-Host "Running integration tests..." -ForegroundColor Cyan
            uv run pytest ../tdd/integration -v --tb=short
        }
        "demo" {
            Write-Host "Running demo tests..." -ForegroundColor Cyan
            uv run pytest ../tdd/demos -v -s --tb=long
        }
        "coverage" {
            Write-Host "Running all tests with coverage..." -ForegroundColor Cyan
            uv run pytest ../tdd/unit ../tdd/integration `
                --cov=app `
                --cov-report=html `
                --cov-report=term-missing `
                --cov-fail-under=70
            Write-Host "Coverage report: backend/htmlcov/index.html" -ForegroundColor Green
        }
        "all" {
            Write-Host "Running all tests..." -ForegroundColor Cyan
            uv run pytest ../tdd -v --tb=short
        }
    }
}
finally {
    Pop-Location
}
