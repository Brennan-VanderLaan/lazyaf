# Frontend Testing Strategy for LazyAF Recovery

This document outlines the testing strategy for recovering the LazyAF frontend after Phase 12 backend changes.

## Current State Analysis

### Frontend Test Infrastructure: NONE

The frontend currently has:
- **No test framework configured** (no vitest, jest, or playwright in package.json)
- **No test scripts** in package.json
- **No existing test files** (.test.ts, .spec.ts)

### Backend Changes That Broke Frontend (Phase 12)

| Component | Old Behavior | New Behavior |
|-----------|-------------|--------------|
| Runner endpoints | HTTP polling `/api/runners`, `/api/runners/status` | **REMOVED** - WebSocket only |
| Runner states | `idle`, `busy`, `offline` | `disconnected`, `connecting`, `idle`, `assigned`, `busy`, `dead` |
| Runner logs | HTTP `/api/runners/{id}/logs` | **REMOVED** - WebSocket `step_logs` messages |
| Step execution | N/A | New states: `pending`, `preparing`, `running`, `completing`, `completed`, `failed`, `cancelled` |
| WebSocket messages | Basic types | New: `step_logs`, `step_status`, `runner_status` |

### Broken Frontend Components

1. **`runners.ts` store** - Calls removed HTTP endpoints (`runnersApi.list()`, `runnersApi.status()`)
2. **`RunnerPanel.svelte`** - Uses polling that 404s, filters by old states (`idle`, `busy`, `offline`)
3. **`types.ts`** - Has `RunnerStatus = 'idle' | 'busy' | 'offline'` (missing new states)
4. **`client.ts`** - Has `runners.list()`, `runners.status()`, `runners.logs()` that no longer exist

---

## Recommended Test Architecture

### Directory Structure

```
tdd/
├── frontend/                    # All frontend tests
│   ├── unit/                    # Fast isolated tests
│   │   ├── stores/              # Store logic tests
│   │   └── utils/               # Utility function tests
│   ├── integration/             # Component + store tests
│   │   ├── websocket/           # WebSocket message handling
│   │   └── api/                 # API client tests
│   ├── e2e/                     # End-to-end browser tests
│   │   ├── smoke/               # Critical path tests
│   │   └── scenarios/           # User workflow tests
│   ├── fixtures/                # Mock data and test utilities
│   │   ├── websocket-messages/  # Sample WS payloads
│   │   └── api-responses/       # Sample API responses
│   └── README.md                # This file
└── ...existing backend tests...
```

### Test Framework Recommendations

| Layer | Framework | Why |
|-------|-----------|-----|
| Unit | **Vitest** | Native ESM, fast, works with Svelte/Vite |
| Integration | **Vitest + @testing-library/svelte** | Test components with stores |
| E2E | **Playwright** | Cross-browser, reliable, good DX |

---

## Phase-by-Phase Test Plan

### Phase 1: Minimum Viable Fix (Prevent 404s)

**Goal**: Stop the frontend from crashing due to removed endpoints

**Tests to Write FIRST** (before fixing):

```typescript
// tdd/frontend/unit/stores/runners.test.ts

describe('Runner Store - Graceful Degradation', () => {
  it('should not crash when runner API returns 404', async () => {
    // Mock fetch to return 404
    // Call runnersStore.load()
    // Assert: no thrown error, store has error state
  });

  it('should set error state when polling fails', async () => {
    // Start polling with mocked failing endpoint
    // Assert: error store has meaningful message
  });
});
```

**What These Tests Validate**:
- Frontend doesn't break when endpoints are missing
- Users see error state, not blank screen

### Phase 2: Runner Visibility (WebSocket Rewrite)

**Goal**: Runners appear when they connect via WebSocket

**Tests to Write**:

```typescript
// tdd/frontend/integration/websocket/runner-updates.test.ts

describe('Runner WebSocket Updates', () => {
  it('should add runner to store when runner_status message received', () => {
    // Send mock WS message: { type: 'runner_status', payload: { id: '...', status: 'idle' } }
    // Assert: runnersStore contains the runner
  });

  it('should update runner state on state transition messages', () => {
    // Send: runner connects (status: idle)
    // Send: runner assigned (status: assigned)
    // Send: runner busy (status: busy)
    // Assert: store reflects each state
  });

  it('should handle all new runner states', () => {
    const states = ['disconnected', 'connecting', 'idle', 'assigned', 'busy', 'dead'];
    // For each state, send message and verify store accepts it
  });

  it('should remove runner when disconnected', () => {
    // Add runner, then send disconnect message
    // Assert: runner removed from store OR status is 'disconnected'
  });
});
```

**E2E Tests**:

```typescript
// tdd/frontend/e2e/scenarios/runner-visibility.spec.ts

test.describe('Runner Visibility', () => {
  test('shows empty state when no runners connected', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('[data-testid="runner-panel"]')).toContainText('No runners connected');
  });

  test('shows runner when one connects via WebSocket', async ({ page }) => {
    await page.goto('/');
    // Inject mock WS message (via page.evaluate or mock server)
    await expect(page.locator('[data-testid="runner-item"]')).toBeVisible();
  });

  test('updates runner status in real-time', async ({ page }) => {
    // Start with idle runner
    // Trigger status change
    // Assert: UI shows new status
  });
});
```

### Phase 3: Step Execution Status

**Goal**: Show execution substates in UI

**Tests to Write**:

```typescript
// tdd/frontend/integration/websocket/step-status.test.ts

describe('Step Execution Status Updates', () => {
  const stepStates = ['pending', 'preparing', 'running', 'completing', 'completed', 'failed', 'cancelled'];

  it.each(stepStates)('should handle %s step status', (status) => {
    // Send step_status message with given status
    // Assert: step in pipeline run has correct status
  });

  it('should update step logs in real-time', () => {
    // Send step_logs message
    // Assert: logs appear in correct step
  });
});
```

**E2E Tests**:

```typescript
// tdd/frontend/e2e/scenarios/pipeline-execution.spec.ts

test.describe('Pipeline Execution Visibility', () => {
  test('shows step progress during execution', async ({ page }) => {
    // Navigate to pipeline
    // Trigger run
    // Assert: progress bar updates
    // Assert: step statuses change from pending -> running -> completed
  });

  test('shows step logs while running', async ({ page }) => {
    // During execution, logs should stream
    await expect(page.locator('.logs-content')).not.toBeEmpty();
  });

  test('shows error state when step fails', async ({ page }) => {
    // Trigger failing step
    // Assert: error badge appears
    // Assert: error message displayed
  });
});
```

### Phase 4: Docker Command & Logs

**Tests to Write** (only if needed):

```typescript
// tdd/frontend/integration/api/docker-command.test.ts

describe('Docker Command API', () => {
  it('should fetch docker command for runner type', async () => {
    // Call runnersApi.dockerCommand('claude-code')
    // Assert: returns command string
  });
});
```

---

## Test Fixture Design

### WebSocket Message Fixtures

```typescript
// tdd/frontend/fixtures/websocket-messages/runner-messages.ts

export const runnerMessages = {
  connected: {
    type: 'runner_status',
    payload: {
      id: 'runner-1',
      name: 'test-runner',
      status: 'idle',
      runner_type: 'claude-code',
    },
  },

  stateTransitions: {
    idle: { type: 'runner_status', payload: { id: 'runner-1', status: 'idle' } },
    assigned: { type: 'runner_status', payload: { id: 'runner-1', status: 'assigned' } },
    busy: { type: 'runner_status', payload: { id: 'runner-1', status: 'busy' } },
    disconnected: { type: 'runner_status', payload: { id: 'runner-1', status: 'disconnected' } },
    dead: { type: 'runner_status', payload: { id: 'runner-1', status: 'dead' } },
  },
};
```

### Mock WebSocket Helper

```typescript
// tdd/frontend/fixtures/mock-websocket.ts

export class MockWebSocket {
  private handlers: Map<string, Function[]> = new Map();

  addEventListener(event: string, handler: Function) {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, []);
    }
    this.handlers.get(event)!.push(handler);
  }

  // Simulate receiving a message
  simulateMessage(data: object) {
    const handlers = this.handlers.get('message') || [];
    handlers.forEach(h => h({ data: JSON.stringify(data) }));
  }

  simulateOpen() {
    const handlers = this.handlers.get('open') || [];
    handlers.forEach(h => h());
  }

  simulateClose() {
    const handlers = this.handlers.get('close') || [];
    handlers.forEach(h => h());
  }
}
```

---

## What NOT to Test

### TypeScript Will Catch These
- Type mismatches (e.g., wrong RunnerStatus values)
- Missing required fields
- Wrong function signatures

**Recommendation**: Fix types first, let compiler catch issues

### Implementation Details to Avoid Testing
- Internal store structure (test behavior, not shape)
- CSS classes or styling
- Component internal state that isn't user-visible
- Specific DOM structure (use test IDs instead)

### Low-Value Tests
- Static text content
- Exact error message wording
- Animation timing
- Tooltip content

---

## CI/CD Integration

### Proposed Pipeline Stages

```yaml
# .github/workflows/frontend-tests.yml

stages:
  - typecheck:        # < 30s - svelte-check, tsc
  - unit:             # < 1min - vitest unit tests
  - integration:      # < 2min - vitest component tests
  - e2e:              # < 5min - playwright tests (headed or headless)

triggers:
  typecheck: every push
  unit: every push
  integration: PR only
  e2e: PR to main, manual
```

### Test Commands

```json
// package.json scripts to add
{
  "scripts": {
    "test": "vitest",
    "test:unit": "vitest run --dir tdd/frontend/unit",
    "test:integration": "vitest run --dir tdd/frontend/integration",
    "test:e2e": "playwright test",
    "test:e2e:ui": "playwright test --ui",
    "test:coverage": "vitest run --coverage"
  }
}
```

---

## Priority Order for Implementation

### Immediate (Before Any Fixes)

1. **Add Vitest + Playwright to package.json**
2. **Write smoke E2E test**: "App loads without console errors"
3. **Write runner store unit test**: "Handles missing API gracefully"

### Phase 1 Fixes

4. **Test**: Runner store error handling
5. **Test**: WebSocket connection status display

### Phase 2 Fixes

6. **Test**: WebSocket runner updates flow
7. **Test**: All 6 runner states handled
8. **E2E**: Runner appears when connected

### Phase 3 Fixes

9. **Test**: Step status WebSocket messages
10. **Test**: Step logs streaming
11. **E2E**: Pipeline execution visibility

---

## Summary

| Test Type | Framework | Runs | Purpose |
|-----------|-----------|------|---------|
| Unit | Vitest | Every push | Test store logic in isolation |
| Integration | Vitest + Testing Library | PRs | Test component + store interaction |
| E2E | Playwright | PRs to main | Test real user workflows |

**Key Principles**:
- Test behavior, not implementation
- Use test IDs, not DOM structure
- Mock at boundaries (WebSocket, fetch)
- Let TypeScript handle type errors
- E2E tests should be resilient to refactors
