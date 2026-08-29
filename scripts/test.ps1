# LazyAF Test Runner Script (PowerShell)
# Usage: .\scripts\test.ps1 [unit|integration|demo|e2e|graph|slow|tier|images|all|coverage]

param(
    [Parameter(Position=0)]
    [ValidateSet("unit", "integration", "demo", "e2e", "graph", "slow", "tier", "images", "all", "coverage", "help")]
    [string]$TestType = "all",

    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$ProjectRoot = Split-Path $ScriptDir -Parent
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"

# E2E test configuration
$BackendPort = 8765
$FrontendPort = 5174
$BackendUrl = "http://localhost:$BackendPort"
$FrontendUrl = "http://localhost:$FrontendPort"

function Get-Python {
    # `python` is the Windows convention; fall back to `py`/`python3`.
    foreach ($candidate in @("python", "py", "python3")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }
    throw "No Python interpreter found (tried python, py, python3)"
}

function Invoke-RunTier {
    # Single-sourced CI tiers: selection + junitxml + R4 gate all live in
    # scripts/run_tier.py (same script the dogfood pipeline runs).
    param([string[]]$Tiers)

    $python = Get-Python
    & $python (Join-Path $ScriptDir "run_tier.py") @Tiers
    if ($LASTEXITCODE -ne 0) {
        throw "run_tier.py failed (exit $LASTEXITCODE)"
    }
}

function Cleanup {
    Write-Host "Cleaning up..." -ForegroundColor Yellow

    # Stop Docker backend - only stop specific e2e containers, not all
    # containers. The vite dev server is Playwright-owned (see
    # playwright.config.ts) and torn down by Playwright itself - this script
    # only manages the compose stack.
    Write-Host "Stopping E2E backend containers..."
    Push-Location $ProjectRoot
    try {
        & cmd.exe /c "docker compose stop backend-e2e runner-mock-e2e" 2>$null
        & cmd.exe /c "docker compose rm -f backend-e2e runner-mock-e2e" 2>$null
    }
    catch { }
    Pop-Location
}

function Wait-ForService {
    param(
        [string]$Url,
        [string]$Name,
        [int]$MaxAttempts = 30
    )

    Write-Host "Waiting for $Name at $Url..."
    $attempt = 1

    while ($attempt -le $MaxAttempts) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                Write-Host "$Name is ready!" -ForegroundColor Green
                return $true
            }
        }
        catch {
            # Service not ready yet
        }

        Start-Sleep -Seconds 1
        $attempt++
    }

    Write-Host "ERROR: $Name failed to start at $Url" -ForegroundColor Red
    return $false
}

function Start-E2EBackend {
    Write-Host "Starting E2E backend via Docker on port $BackendPort..." -ForegroundColor Cyan
    Push-Location $ProjectRoot

    try {
        # Rebuild and start e2e containers to ensure latest code.
        # The compose override enables the env-gated test-mode API
        # (LAZYAF_TEST_MODE) that the Playwright specs reset/seed through.
        $ComposeFiles = "-f docker-compose.yml -f frontend/e2e/compose.test-mode.yml"
        Write-Host "Building e2e containers..."
        & cmd.exe /c "docker compose $ComposeFiles --profile e2e build"

        # Start backend container with e2e profile
        & cmd.exe /c "docker compose $ComposeFiles --profile e2e up -d backend-e2e runner-mock-e2e"

        # Wait for container health check
        Write-Host "Waiting for backend container to be healthy..."
        $maxAttempts = 30
        $attempt = 1

        while ($attempt -le $maxAttempts) {
            try {
                $health = & cmd.exe /c "docker inspect --format={{.State.Health.Status}} lazyaf-backend-e2e-1" 2>$null
                if ($health -and $health.Trim() -eq "healthy") {
                    Write-Host "Backend container is healthy!" -ForegroundColor Green
                    return
                }
            }
            catch { }

            Start-Sleep -Seconds 1
            $attempt++
        }

        # Fallback to HTTP check
        if (-not (Wait-ForService -Url "$BackendUrl/health" -Name "Backend")) {
            throw "Backend failed to start"
        }
    }
    finally {
        Pop-Location
    }
}

function Run-E2ETests {
    # Playwright OWNS the vite dev server (reuseExistingServer: false,
    # --strictPort): it starts it on $FrontendUrl's port and fails loudly if
    # a stray process is already squatting there. Do not pre-start vite here.
    param(
        [string[]]$Args
    )

    Write-Host "Running E2E tests..." -ForegroundColor Cyan
    Push-Location $FrontendDir

    try {
        $env:BACKEND_URL = $BackendUrl
        $env:FRONTEND_URL = $FrontendUrl

        $argsString = ($Args -join " ")
        & cmd.exe /c "npx playwright test $argsString"
    }
    finally {
        Pop-Location
    }
}

function Show-Help {
    Write-Host "Usage: .\scripts\test.ps1 [unit|integration|demo|e2e|graph|slow|tier|images|all|coverage]" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  unit        - Run fast isolated unit tests"
    Write-Host "  integration - Run API and database tests"
    Write-Host "  demo        - Run workflow demonstrations"
    Write-Host "  e2e         - Run full E2E tests (starts Docker backend; Playwright owns vite)"
    Write-Host "  graph       - Run graph pipeline E2E tests (starts Docker backend only)"
    Write-Host "  slow        - Run the @slow full-stack e2e tests in the compose stack (no CI tier runs these)"
    Write-Host "  tier        - Run gated CI tier(s) via scripts/run_tier.py (e.g. 'tier T1')"
    Write-Host "  images      - Build the 12.3 step images via scripts/build_images.py (--check lists stale)"
    Write-Host "  coverage    - Run tests with coverage report"
    Write-Host "  all         - Run the no-Docker CI tiers T1 + T3 (default; T2 needs Docker)"
    Write-Host ""
    Write-Host "E2E options (after 'e2e'):" -ForegroundColor Cyan
    Write-Host "  --headed    - Run with visible browser"
    Write-Host "  --debug     - Debug mode with inspector"
    Write-Host "  --ui        - Open Playwright UI"
    Write-Host ""
    Write-Host "Graph options (after 'graph'):" -ForegroundColor Cyan
    Write-Host "  -k 'pattern'  - Run tests matching pattern"
    Write-Host "  --tb=long     - Show full tracebacks"
    Write-Host ""
    Write-Host "Note: 'all' runs the no-Docker gated tiers only." -ForegroundColor Yellow
    Write-Host "      Use 'tier T2' for Docker integration, 'e2e'/'graph' for full E2E."
}

# Register cleanup handler
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Cleanup }

try {
    switch ($TestType) {
        "unit" {
            Push-Location $BackendDir
            try {
                Write-Host "Running unit tests..." -ForegroundColor Cyan
                uv run pytest ../tdd/unit -v --tb=short
            }
            finally {
                Pop-Location
            }
        }
        "integration" {
            Push-Location $BackendDir
            try {
                Write-Host "Running integration tests..." -ForegroundColor Cyan
                uv run pytest ../tdd/integration -v --tb=short
            }
            finally {
                Pop-Location
            }
        }
        "demo" {
            Push-Location $BackendDir
            try {
                Write-Host "Running demo tests..." -ForegroundColor Cyan
                uv run pytest ../tdd/demos -v -s --tb=long
            }
            finally {
                Pop-Location
            }
        }
        "e2e" {
            Write-Host "Running E2E tests (full browser + API tests)..." -ForegroundColor Cyan

            try {
                Start-E2EBackend

                # Run Playwright browser tests (Playwright starts + owns vite)
                Write-Host "Running Playwright browser tests..." -ForegroundColor Yellow
                Run-E2ETests -Args $ExtraArgs

                # Run Python API e2e tests against the running backend
                Write-Host "Running Python API e2e tests..." -ForegroundColor Yellow
                & cmd.exe /c "docker exec -w /app -e PYTHONPATH=/app -e E2E_BACKEND_URL=http://localhost:8000 lazyaf-backend-e2e-1 uv run pytest /tdd/e2e -v --tb=short"
            }
            finally {
                Cleanup
            }
        }
        "graph" {
            Write-Host "Running graph pipeline E2E tests (with Docker)..." -ForegroundColor Cyan

            try {
                Start-E2EBackend

                # Run Python graph pipeline e2e tests against the running backend
                Write-Host "Running graph pipeline tests..." -ForegroundColor Yellow
                $argsString = ($ExtraArgs -join " ")
                # E2E_BACKEND_URL points to localhost:8000 inside the container
                if ($argsString) {
                    & cmd.exe /c "docker exec -w /app -e PYTHONPATH=/app -e E2E_BACKEND_URL=http://localhost:8000 lazyaf-backend-e2e-1 uv run pytest /tdd/e2e/test_graph_pipeline.py -v --tb=short $argsString"
                } else {
                    & cmd.exe /c "docker exec -w /app -e PYTHONPATH=/app -e E2E_BACKEND_URL=http://localhost:8000 lazyaf-backend-e2e-1 uv run pytest /tdd/e2e/test_graph_pipeline.py -v --tb=short"
                }
            }
            finally {
                Cleanup
            }
        }
        "coverage" {
            Push-Location $BackendDir
            try {
                Write-Host "Running all tests with coverage..." -ForegroundColor Cyan
                uv run pytest ../tdd/unit ../tdd/integration `
                    --cov=app `
                    --cov-report=html `
                    --cov-report=term-missing `
                    --cov-fail-under=70
                Write-Host "Coverage report: backend/htmlcov/index.html" -ForegroundColor Green
            }
            finally {
                Pop-Location
            }
        }
        "slow" {
            # The @slow full-stack e2e tests (control layer, real card
            # execution, graph pipeline). They run in NO CI tier today
            # (stated exclusion per R4 - see tdd/README.md): they need this
            # compose stack, which the legacy runner cannot host. Dogfood CI
            # picks them up at Phase 12.4/12.5.
            Write-Host "Running slow full-stack e2e tests (compose stack, in-container)..." -ForegroundColor Cyan

            try {
                Start-E2EBackend

                $argsString = ($ExtraArgs -join " ")
                & cmd.exe /c "docker exec -w /app -e PYTHONPATH=/app -e E2E_BACKEND_URL=http://localhost:8000 lazyaf-backend-e2e-1 uv run pytest /tdd/e2e -m slow -v --tb=short $argsString"
            }
            finally {
                Cleanup
            }
        }
        "tier" {
            if (-not $ExtraArgs -or $ExtraArgs.Count -eq 0) {
                throw "Usage: .\scripts\test.ps1 tier T1 [T2 T3 ...]"
            }
            Invoke-RunTier -Tiers $ExtraArgs
        }
        "images" {
            # 12.3 step images (lazyaf-base:dev -> lazyaf-claude:dev /
            # lazyaf-test-runner:dev). Single-sourced in
            # scripts/build_images.py (dependency order, content-hash
            # staleness skip, --check / --force). The dogfood pipeline and
            # the control-layer/HOME-persistence tests need these tags on the
            # local daemon; a missing tag fails a step loudly with
            # "Image not found: lazyaf-base:dev" by design.
            $python = Get-Python
            & $python (Join-Path $ScriptDir "build_images.py") @ExtraArgs
            if ($LASTEXITCODE -ne 0) {
                throw "build_images.py failed (exit $LASTEXITCODE)"
            }
        }
        "all" {
            # No-Docker lanes only: T1 + T3 via the single-sourced tier
            # script. T2 (Docker-dependent) is deliberately excluded - run
            # '.\scripts\test.ps1 tier T2' with Docker up, or the pipeline.
            Write-Host "Running all no-Docker tiers (T1 + T3)..." -ForegroundColor Cyan
            Write-Host "Note: T2 (Docker) and slow E2E excluded - see 'tier T2' / 'e2e' / 'graph'" -ForegroundColor Yellow
            Invoke-RunTier -Tiers @("T1", "T3")
        }
        "help" {
            Show-Help
        }
    }
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Cleanup
    exit 1
}
