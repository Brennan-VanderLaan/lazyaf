# Testing Quick Reference

Run commands from the project root using the test scripts, or from `tdd/frontend/` directly.

## Quick Start

```bash
# From project root - use the test script
.\scripts\test.ps1 frontend e2e:ui    # PowerShell
./scripts/test.sh frontend e2e:ui     # Bash

# Or from tdd/frontend directory
cd tdd/frontend
pnpm install   # First time only
pnpm test:e2e:ui
```

---

## E2E Test Commands

### By Category

| Command | Description | Backend Required |
|---------|-------------|------------------|
| `pnpm test:e2e:mocked` | Smoke tests with mocked backend | No |
| `pnpm test:e2e:stories` | All customer journey tests | No* |
| `pnpm test:e2e:critical` | Critical failure handling (P0) | No* |
| `pnpm test:e2e:ui-tests` | UI completeness tests | No |
| `pnpm test:e2e:realtime` | Real-time sync tests | No* |
| `pnpm test:e2e:real` | Full integration tests | Yes |
| `pnpm test:e2e:all` | Everything | Partial |

*These tests can run with mocked responses, but full coverage needs backend

### By Feature

| Command | Tests |
|---------|-------|
| `pnpm test:e2e:p0` | Card lifecycle + Critical failures |
| `pnpm test:e2e:cards` | All card-related tests |
| `pnpm test:e2e:pipeline` | Pipeline lifecycle tests |
| `pnpm test:e2e:runner` | Runner visibility tests |
| `pnpm test:e2e:debug-rerun` | Debug re-run tests (Phase 12.7) |
| `pnpm test:e2e:playground` | Agent playground tests (Phase 11) |

### Interactive Modes

| Command | Description |
|---------|-------------|
| `pnpm test:e2e:ui` | Playwright UI - see tests, time-travel debug |
| `pnpm test:e2e:headed` | Watch browser while tests run |
| `pnpm test:e2e:debug` | Step-through debugging |
| `pnpm test:e2e:report` | Open last test report in browser |

### Specific Tests

```bash
# Run tests matching a pattern
pnpm test:e2e --grep "Create Card"
pnpm test:e2e --grep "Runner Status"

# Run a specific file
pnpm test:e2e ../tdd/frontend/e2e/stories/02-card-lifecycle/create-card.spec.ts

# Run a specific directory
pnpm test:e2e ../tdd/frontend/e2e/stories/07-agent-playground/
```

---

## Unit Test Commands

| Command | Description |
|---------|-------------|
| `pnpm test:unit` | Run all unit tests once |
| `pnpm test:unit:watch` | Watch mode - rerun on changes |
| `pnpm test:unit:coverage` | Run with coverage report |

---

## Test Directory Structure

```
tdd/frontend/e2e/
├── smoke/                      # Quick sanity checks
├── stories/
│   ├── 01-setup/               # Repo/agent setup
│   ├── 02-card-lifecycle/      # Cards (P0)
│   ├── 03-pipeline-lifecycle/  # Pipelines
│   ├── 04-runner-visibility/   # Runners
│   ├── 05-git-operations/      # Git
│   ├── 06-debug-rerun/         # Debug (12.7)
│   └── 07-agent-playground/    # Playground (11)
├── critical-failures/          # Error handling (P0)
├── ui-completeness/            # UI quality
└── realtime-sync/              # Multi-user
```

---

## Running with Backend (Real Tier)

```bash
# Terminal 1: Start backend with test mode
cd backend
LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app --reload

# Terminal 2: Run real tier tests
cd frontend
pnpm test:e2e:real
```

---

## Debugging Failed Tests

1. **View the report**: `pnpm test:e2e:report`
2. **Use UI mode**: `pnpm test:e2e:ui` then click on failed test
3. **Run headed**: `pnpm test:e2e:headed --grep "Test Name"`
4. **Step through**: `pnpm test:e2e:debug --grep "Test Name"`

Test artifacts (screenshots, videos, traces) are in `tdd/frontend/e2e-results/`

---

## Priority Guide

| Priority | What | Run Command |
|----------|------|-------------|
| P0 | Must work | `pnpm test:e2e:p0` |
| P1 | Core features | `pnpm test:e2e:stories` |
| P2 | Polish | `pnpm test:e2e:ui-tests` + `pnpm test:e2e:realtime` |

---

## Common Patterns

### Run tests for a specific story

```bash
# Card lifecycle
pnpm test:e2e ../tdd/frontend/e2e/stories/02-card-lifecycle/

# Agent playground
pnpm test:e2e ../tdd/frontend/e2e/stories/07-agent-playground/
```

### Run only implemented tests (skip skipped)

```bash
# By default, test.skip() tests are skipped
# When you implement a test, remove .skip and it will run
```

### Watch a specific test file during development

```bash
# Use Playwright UI and select the file
pnpm test:e2e:ui
```
