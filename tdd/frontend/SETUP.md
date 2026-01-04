# Frontend Test Setup Guide

This guide explains how to set up and run the frontend tests for LazyAF.

## Prerequisites

- Node.js 18+ (or 20+ recommended)
- pnpm (package manager)

## Installation

### 1. Add Test Dependencies to Frontend

Add these to `frontend/package.json`:

```json
{
  "devDependencies": {
    // Existing deps...
    "vitest": "^3.0.0",
    "@testing-library/svelte": "^5.0.0",
    "@testing-library/jest-dom": "^6.0.0",
    "@playwright/test": "^1.48.0",
    "jsdom": "^25.0.0"
  },
  "scripts": {
    // Existing scripts...
    "test": "vitest --config ../tdd/frontend/vitest.config.ts",
    "test:unit": "vitest run --config ../tdd/frontend/vitest.config.ts --dir ../tdd/frontend/unit",
    "test:integration": "vitest run --config ../tdd/frontend/vitest.config.ts --dir ../tdd/frontend/integration",
    "test:coverage": "vitest run --config ../tdd/frontend/vitest.config.ts --coverage",
    "test:e2e": "playwright test --config ../tdd/frontend/playwright.config.ts",
    "test:e2e:ui": "playwright test --config ../tdd/frontend/playwright.config.ts --ui"
  }
}
```

### 2. Install Dependencies

```bash
cd frontend
pnpm install
pnpm exec playwright install  # Install browsers for E2E tests
```

### 3. Verify Setup

```bash
# Run unit tests
pnpm test:unit

# Run E2E smoke test (requires dev server)
pnpm test:e2e smoke/
```

## Running Tests

### Unit Tests

Fast, isolated tests that mock all dependencies:

```bash
# Watch mode (for development)
pnpm test

# Single run
pnpm test:unit

# With coverage
pnpm test:coverage
```

### Integration Tests

Tests that verify component + store interaction:

```bash
pnpm test:integration
```

### E2E Tests

Browser-based tests that test the full application:

```bash
# Run all E2E tests
pnpm test:e2e

# Run with interactive UI
pnpm test:e2e:ui

# Run specific test file
pnpm test:e2e smoke/app-loads.spec.ts

# Run specific test
pnpm test:e2e -g "homepage loads without JavaScript errors"
```

## Test Structure

```
tdd/frontend/
├── unit/                    # Vitest unit tests
│   └── stores/              # Store logic tests
├── integration/             # Vitest component tests
│   ├── websocket/           # WebSocket message handling
│   └── api/                 # API client tests
├── e2e/                     # Playwright browser tests
│   ├── smoke/               # Basic "does it work" tests
│   └── scenarios/           # User workflow tests
├── fixtures/                # Test data and mocks
│   ├── websocket-messages/  # Sample WS payloads
│   └── mock-websocket.ts    # WebSocket mock
├── vitest.config.ts         # Unit/integration config
├── playwright.config.ts     # E2E config
└── setup.ts                 # Global test setup
```

## Writing Tests

### Unit Test Example

```typescript
// tdd/frontend/unit/stores/runners.test.ts
import { describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import { runnersStore } from '$lib/stores/runners';

describe('runnersStore', () => {
  it('should start empty', () => {
    expect(get(runnersStore)).toHaveLength(0);
  });
});
```

### Integration Test Example

```typescript
// tdd/frontend/integration/websocket/runner-updates.test.ts
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import RunnerPanel from '$lib/components/RunnerPanel.svelte';

describe('RunnerPanel', () => {
  it('shows empty state initially', async () => {
    const { getByText } = render(RunnerPanel);
    expect(getByText(/no runners/i)).toBeInTheDocument();
  });
});
```

### E2E Test Example

```typescript
// tdd/frontend/e2e/scenarios/runner-visibility.spec.ts
import { test, expect } from '@playwright/test';

test('shows runner when connected', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('.runner-panel')).toBeVisible();
});
```

## Using Fixtures

### WebSocket Messages

```typescript
import {
  runnerStatusMessages,
  runnerLifecycleSequence,
} from '../../fixtures/websocket-messages/runner-messages';

// Use pre-built messages
ws.simulateMessage(runnerStatusMessages.idle);

// Use sequences
for (const msg of runnerLifecycleSequence) {
  ws.simulateMessage(msg);
}
```

### Mock WebSocket

```typescript
import { MockWebSocket, createMockWebSocketFactory } from '../../fixtures/mock-websocket';

// In test
const mockWsFactory = createMockWebSocketFactory();
vi.stubGlobal('WebSocket', mockWsFactory.factory);

// Get the WebSocket instance
const ws = mockWsFactory.getLatest();
ws.simulateOpen();
ws.simulateMessage({ type: 'runner_status', payload: {...} });
```

## CI Integration

Add to GitHub Actions:

```yaml
# .github/workflows/frontend-tests.yml
name: Frontend Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v2
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: 'pnpm'

      - name: Install dependencies
        run: pnpm install
        working-directory: frontend

      - name: Type check
        run: pnpm check
        working-directory: frontend

      - name: Unit tests
        run: pnpm test:unit
        working-directory: frontend

      - name: Integration tests
        run: pnpm test:integration
        working-directory: frontend

      - name: Install Playwright browsers
        run: pnpm exec playwright install --with-deps
        working-directory: frontend

      - name: E2E tests
        run: pnpm test:e2e
        working-directory: frontend
```

## Troubleshooting

### "Module not found" errors

Make sure path aliases are configured in `vitest.config.ts`:

```typescript
resolve: {
  alias: {
    '$lib': resolve(__dirname, '../../frontend/src/lib'),
  },
},
```

### WebSocket tests timing out

Ensure you're calling `ws.simulateOpen()` after creating the mock:

```typescript
const ws = mockWsFactory.getLatest();
ws.simulateOpen();  // Don't forget this!
```

### E2E tests failing to connect

Check that the dev server is running on the expected port (5173):

```bash
# Start dev server manually
cd frontend && pnpm dev

# Then run E2E tests
pnpm test:e2e
```

### Svelte component tests not rendering

Make sure `@testing-library/svelte` is installed and the test setup includes jsdom:

```typescript
// vitest.config.ts
test: {
  environment: 'jsdom',
}
```

## Tips

1. **Use `.todo()` for planned tests** - Documents intent without failing CI
2. **Test behavior, not implementation** - Focus on what users see
3. **Use test IDs** - Add `data-testid` attributes for stable selectors
4. **Mock at boundaries** - Mock fetch/WebSocket, not internal functions
5. **Keep E2E tests high-level** - Don't repeat unit test coverage
