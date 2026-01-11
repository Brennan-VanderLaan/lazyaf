# LazyAF Test Runner Script (PowerShell)
# Usage: .\scripts\test.ps1 [unit|integration|demo|e2e|e2e-quick|all|coverage]

param(
    [Parameter(Position=0)]
    [ValidateSet("unit", "integration", "demo", "e2e", "e2e-quick", "all", "coverage", "help")]
    [string]$TestType = "all",

    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$PlaywrightArgs
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

# Process handles for cleanup
$script:FrontendProcess = $null

function Cleanup {
    Write-Host "Cleaning up..." -ForegroundColor Yellow

    # Stop Docker backend - only stop specific e2e containers, not all containers
    Write-Host "Stopping E2E backend containers..."
    Push-Location $ProjectRoot
    try {
        & cmd.exe /c "docker compose stop backend-e2e runner-mock-e2e" 2>$null
        & cmd.exe /c "docker compose rm -f backend-e2e runner-mock-e2e" 2>$null
    }
    catch { }
    Pop-Location

    # Kill frontend process tree (cmd.exe + node/vite child processes)
    if ($script:FrontendProcess) {
        try {
            if (!$script:FrontendProcess.HasExited) {
                Write-Host "Stopping frontend (PID: $($script:FrontendProcess.Id)) and child processes..."
                # Kill the entire process tree using taskkill /T
                & cmd.exe /c "taskkill /PID $($script:FrontendProcess.Id) /T /F" 2>$null
            }
        }
        catch { }
    }

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
        # Rebuild and start e2e containers to ensure latest code
        Write-Host "Building e2e containers..."
        & cmd.exe /c "docker compose --profile e2e build"

        # Start backend container with e2e profile
        & cmd.exe /c "docker compose --profile e2e up -d backend-e2e runner-mock-e2e"

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

function Start-E2EFrontend {
    Write-Host "Starting E2E frontend on port $FrontendPort..." -ForegroundColor Cyan
    Push-Location $FrontendDir

    try {
        $env:VITE_BACKEND_URL = $BackendUrl

        # Start frontend in a separate minimized window to avoid terminal issues
        $script:FrontendProcess = Start-Process -FilePath "cmd.exe" `
            -ArgumentList "/c", "npm run dev -- --port $FrontendPort" `
            -WindowStyle Minimized -PassThru

        if (-not (Wait-ForService -Url $FrontendUrl -Name "Frontend")) {
            throw "Frontend failed to start"
        }
    }
    finally {
        Pop-Location
    }
}

function Run-E2ETests {
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
    Write-Host "Usage: .\scripts\test.ps1 [unit|integration|demo|e2e|e2e-quick|all|coverage]" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  unit        - Run fast isolated unit tests"
    Write-Host "  integration - Run API and database tests"
    Write-Host "  demo        - Run workflow demonstrations"
    Write-Host "  e2e         - Run full browser E2E tests (starts Docker backend & frontend)"
    Write-Host "  e2e-quick   - Run E2E API tests only (no browser, no Docker needed)"
    Write-Host "  coverage    - Run tests with coverage report"
    Write-Host "  all         - Run all tests except slow e2e (default)"
    Write-Host ""
    Write-Host "E2E options (after 'e2e'):" -ForegroundColor Cyan
    Write-Host "  --headed    - Run with visible browser"
    Write-Host "  --debug     - Debug mode with inspector"
    Write-Host "  --ui        - Open Playwright UI"
    Write-Host ""
    Write-Host "Note: 'all' excludes slow e2e tests that require Docker." -ForegroundColor Yellow
    Write-Host "      Use 'e2e' to run full browser tests with Docker containers."
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
            Write-Host "Running E2E tests (full browser tests)..." -ForegroundColor Cyan

            try {
                Start-E2EBackend
                Start-E2EFrontend
                Run-E2ETests -Args $PlaywrightArgs
            }
            finally {
                Cleanup
            }
        }
        "e2e-quick" {
            Push-Location $BackendDir
            try {
                Write-Host "Running E2E tests (API tests only, no browser)..." -ForegroundColor Cyan
                uv run pytest ../tdd/e2e -v --tb=short -m "not slow"
            }
            finally {
                Pop-Location
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
        "all" {
            Push-Location $BackendDir
            try {
                Write-Host "Running all tests (excluding slow e2e tests that need Docker)..." -ForegroundColor Cyan
                # Run unit, integration, demos normally
                uv run pytest ../tdd/unit ../tdd/integration ../tdd/demos -v --tb=short
                # Run e2e tests excluding slow ones (they require Docker containers via 'test.ps1 e2e')
                Write-Host "Running e2e quick tests..." -ForegroundColor Cyan
                uv run pytest ../tdd/e2e -v --tb=short -m "not slow"
            }
            finally {
                Pop-Location
            }
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
