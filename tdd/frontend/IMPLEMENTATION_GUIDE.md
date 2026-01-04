# Test Implementation Guide for LLMs

This is a quick-reference guide for AI assistants implementing E2E tests. Read this first.

---

## Quick Start Workflow

```
1. User says: "Implement tests in [filename]"
2. Read the test file to understand the story
3. Read related components/stores (see mapping below)
4. Convert test.skip() to test() with real implementation
5. Run: pnpm test:e2e --grep "[Test Name]"
6. Add data-testid to components if selectors fail
```

---

## File → Component Mapping

When implementing tests, read these files first:

### Card Lifecycle Tests (`stories/02-card-lifecycle/`)
```
Test File                    → Read These Files
─────────────────────────────────────────────────
create-card.spec.ts          → frontend/src/lib/components/CardModal.svelte
                             → frontend/src/lib/stores/cards.ts
                             → frontend/src/lib/api/types.ts (Card interface)

start-card.spec.ts           → frontend/src/lib/components/CardItem.svelte
                             → frontend/src/lib/stores/cards.ts
                             → backend/app/routers/cards.py (start endpoint)

execution-visibility.spec.ts → frontend/src/lib/components/ExecutionPanel.svelte
                             → frontend/src/lib/stores/websocket.ts
                             → fixtures/websocket-messages/step-messages.ts

review-card.spec.ts          → frontend/src/lib/components/ReviewModal.svelte
                             → frontend/src/lib/components/DiffViewer.svelte

retry-failed.spec.ts         → frontend/src/lib/components/CardItem.svelte
                             → Look for retry/rerun buttons

card-status-sync.spec.ts     → frontend/src/lib/stores/websocket.ts
                             → Look for card_updated handlers
```

### Pipeline Lifecycle Tests (`stories/03-pipeline-lifecycle/`)
```
Test File                    → Read These Files
─────────────────────────────────────────────────
create-pipeline.spec.ts      → frontend/src/lib/components/PipelineEditor.svelte
run-pipeline.spec.ts         → frontend/src/lib/components/PipelineRunner.svelte
trigger-on-card.spec.ts      → backend/app/services/executor.py
```

### Runner Visibility Tests (`stories/04-runner-visibility/`)
```
Test File                    → Read These Files
─────────────────────────────────────────────────
pool-status.spec.ts          → frontend/src/lib/components/RunnerPanel.svelte
                             → frontend/src/lib/stores/runners.ts
runner-states.spec.ts        → fixtures/websocket-messages/runner-messages.ts
no-runners-ux.spec.ts        → frontend/src/lib/components/RunnerPanel.svelte
```

### Debug Re-Run Tests (`stories/06-debug-rerun/`)
```
Test File                    → Read These Files
─────────────────────────────────────────────────
*.spec.ts                    → frontend/src/lib/components/DebugRerunModal.svelte
                             → frontend/src/lib/stores/debug.ts
                             → fixtures/websocket-messages/debug-messages.ts
```

### Agent Playground Tests (`stories/07-agent-playground/`)
```
Test File                    → Read These Files
─────────────────────────────────────────────────
configure-test.spec.ts       → frontend/src/lib/pages/PlaygroundPage.svelte (lines 1-200)
                             → frontend/src/lib/stores/playground.ts
                             → frontend/src/lib/api/types.ts (PlaygroundConfig)

run-test.spec.ts             → frontend/src/lib/pages/PlaygroundPage.svelte (execution logic)
                             → frontend/src/lib/stores/playground.ts (SSE handling)
                             → backend/app/routers/playground.py (endpoints)

view-results.spec.ts         → frontend/src/lib/pages/PlaygroundPage.svelte (diff viewer)
                             → frontend/src/lib/components/DiffViewer.svelte
                             → fixtures/sse-messages/playground-messages.ts

save-to-branch.spec.ts       → frontend/src/lib/pages/PlaygroundPage.svelte
                             → backend/app/routers/playground.py (save endpoint)

session-management.spec.ts   → frontend/src/lib/stores/playground.ts
                             → backend/app/services/playground_service.py

compare-runs.spec.ts         → May require new components (run history)
                             → frontend/src/lib/stores/playground.ts
```

**IMPORTANT**: Playground uses SSE (Server-Sent Events), NOT WebSocket!
- See "SSE vs WebSocket" section below for mocking patterns

---

## Common Selectors Pattern

Use these `data-testid` conventions:

```typescript
// Buttons
'[data-testid="create-card-button"]'
'[data-testid="submit-button"]'
'[data-testid="cancel-button"]'
'[data-testid="close-button"]'

// Inputs
'[data-testid="card-title-input"]'
'[data-testid="card-description-input"]'

// Lists/Items
'[data-testid="card-item"]'           // Each card in list
'[data-testid="runner-item"]'         // Each runner in panel
'[data-testid="step-item"]'           // Each step in pipeline

// Panels/Containers
'[data-testid="runner-panel"]'
'[data-testid="card-board"]'
'[data-testid="pipeline-editor"]'

// Modals
'[data-testid="card-modal"]'
'[data-testid="review-modal"]'
'[data-testid="debug-modal"]'

// Status indicators
'[data-testid="connection-status"]'
'[data-testid="run-status"]'
'[data-status="completed"]'           // Attribute selector
```

---

## WebSocket Event Simulation

### Option 1: Direct page.evaluate (Mocked Tier)

```typescript
// The app exposes a test handler on window
await page.evaluate((message) => {
  window.__testWebSocketHandler__(message);
}, {
  type: 'runner_status',
  payload: { id: 'runner-1', status: 'idle' }
});
```

### Option 2: Use Fixtures

```typescript
import { runnerStatusMessages } from '../../fixtures/websocket-messages/runner-messages';

await page.evaluate((msg) => window.__testWebSocketHandler__(msg), runnerStatusMessages.idle);
```

### Option 3: Real Backend (Real Tier)

```typescript
// Just perform actions - WebSocket events come naturally
await page.click('[data-testid="start-card-button"]');
// Backend sends real WebSocket events
await expect(page.locator('[data-testid="card-status"]')).toHaveText('In Progress');
```

---

## SSE Event Simulation (Playground Only)

The Agent Playground uses Server-Sent Events (SSE), not WebSocket. Here's how to mock SSE:

### Option 1: Route Interception

```typescript
test('playground shows streaming logs', async ({ page }) => {
  // Intercept the SSE endpoint
  await page.route('**/api/playground/*/stream', async route => {
    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
      },
      body: [
        'event: status\ndata: {"status": "running"}\n\n',
        'event: log\ndata: {"line": "Cloning repository..."}\n\n',
        'event: log\ndata: {"line": "Running agent..."}\n\n',
        'event: complete\ndata: {"status": "completed"}\n\n',
      ].join(''),
    });
  });

  await page.goto('/playground');
  await page.fill('[data-testid="task-input"]', 'Fix the bug');
  await page.click('[data-testid="test-once-button"]');

  await expect(page.locator('[data-testid="log-panel"]')).toContainText('Cloning repository');
});
```

### Option 2: Use SSE Fixtures

```typescript
import { sseLogSequence } from '../../fixtures/sse-messages/playground-messages';

await page.route('**/api/playground/*/stream', async route => {
  await route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    body: sseLogSequence.join(''),
  });
});
```

### SSE Event Types for Playground

| Event | Data | Purpose |
|-------|------|---------|
| `log` | `{"line": "..."}` | Single log line |
| `logs_batch` | `{"lines": [...]}` | Batch of log lines |
| `status` | `{"status": "running"}` | Status change |
| `complete` | `{"status": "completed", "diff": "..."}` | Test finished |
| `error` | `{"error": "..."}` | Error occurred |
| `ping` | `{}` | Keep-alive |

---

## Test Implementation Template

```typescript
import { test, expect } from '@playwright/test';

test.describe('Feature Name', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Setup: wait for initial load
    await expect(page.locator('[data-testid="app-loaded"]')).toBeVisible();
  });

  test('does the expected thing', async ({ page }) => {
    // Arrange: Set up state
    // ...

    // Act: Perform action
    await page.click('[data-testid="action-button"]');

    // Assert: Verify result
    await expect(page.locator('[data-testid="result"]')).toBeVisible();
  });

  test('handles error case', async ({ page }) => {
    // Arrange: Set up error condition
    await page.route('**/api/endpoint', route =>
      route.fulfill({ status: 500, body: 'Server Error' })
    );

    // Act
    await page.click('[data-testid="action-button"]');

    // Assert: Error state visible
    await expect(page.locator('[data-testid="error-message"]')).toBeVisible();
  });
});
```

---

## Adding data-testid to Components

When a selector fails, add `data-testid` to the Svelte component:

```svelte
<!-- frontend/src/lib/components/SomeComponent.svelte -->

<!-- BEFORE -->
<button on:click={handleClick}>Submit</button>

<!-- AFTER -->
<button data-testid="submit-button" on:click={handleClick}>Submit</button>
```

For dynamic items:
```svelte
{#each items as item}
  <div data-testid="item" data-item-id={item.id}>
    {item.name}
  </div>
{/each}
```

---

## State Transitions to Test

### Card Status Flow
```
todo → in_progress → in_review → done
                  ↘            ↗
                    → failed →
```

### Runner State Flow
```
disconnected → connecting → idle → assigned → busy → idle
                              ↓                      ↓
                            dead ←─────────────────←
```

### Step Execution Flow
```
pending → preparing → running → completing → completed
                         ↓
                      → failed
                      → cancelled
```

### Debug Session Flow
```
pending → starting → running → paused → resumed → running → completed
                        ↓                            ↓
                     aborted                      failed
```

### Playground Session Flow
```
idle → queued → running → completed
                   ↓
                → failed
                → cancelled
```

---

## Fixtures Quick Reference

### Runner Messages
```typescript
import {
  runnerStatusMessages,      // { idle, assigned, busy, disconnected, dead, connecting }
  runnerLifecycleSequence,   // Full connect → work → disconnect
  runnerDeathSequence,       // Work then crash
  runnerRecoverySequence,    // Dead → reconnect
} from '../../fixtures/websocket-messages/runner-messages';
```

### Step Messages
```typescript
import {
  stepStatusMessages,        // { pending, preparing, running, completing, completed, failed, cancelled }
  stepLogsMessages,          // { startup, progress, completion, error }
  successfulStepSequence,    // Full success flow
  failedStepSequence,        // Failure flow
} from '../../fixtures/websocket-messages/step-messages';
```

### Debug Messages
```typescript
import {
  debugStatusMessages,       // { pending, starting, running, paused, resumed, completed, aborted, failed }
  debugBreakpointMessages,   // { step0, step1, step2, step3, step4 }
  debugResumeMessages,       // { fromStep0, fromStep1, fromStep2 }
  successfulDebugSequence,   // Start → breakpoint → resume → complete
  abortedDebugSequence,      // Start → breakpoint → abort
} from '../../fixtures/websocket-messages/debug-messages';
```

### Playground SSE Messages (NOT WebSocket!)
```typescript
import {
  playgroundStatusEvents,    // { queued, running, completed, failed, cancelled }
  playgroundLogEvents,       // { startup, progress, agentOutput, error }
  successfulPlaygroundSSE,   // Full success sequence as SSE body string
  failedPlaygroundSSE,       // Failure sequence
  createLogEvent,            // Helper: createLogEvent("message") → SSE string
  createStatusEvent,         // Helper: createStatusEvent("running") → SSE string
} from '../../fixtures/sse-messages/playground-messages';

// Usage with page.route():
await page.route('**/api/playground/*/stream', route =>
  route.fulfill({
    headers: { 'Content-Type': 'text/event-stream' },
    body: successfulPlaygroundSSE,
  })
);
```

---

## Common Gotchas

### 1. Async state updates
```typescript
// BAD: Immediate check after action
await page.click('[data-testid="button"]');
expect(await page.textContent('[data-testid="result"]')).toBe('Done'); // May fail

// GOOD: Wait for state
await page.click('[data-testid="button"]');
await expect(page.locator('[data-testid="result"]')).toHaveText('Done');
```

### 2. WebSocket timing
```typescript
// BAD: Send message immediately
await page.goto('/');
await page.evaluate((msg) => window.__testWebSocketHandler__(msg), message);

// GOOD: Wait for WebSocket to connect
await page.goto('/');
await expect(page.locator('[data-testid="ws-connected"]')).toBeVisible();
await page.evaluate((msg) => window.__testWebSocketHandler__(msg), message);
```

### 3. Modal animations
```typescript
// BAD: Click immediately after modal opens
await page.click('[data-testid="open-modal"]');
await page.click('[data-testid="modal-button"]'); // May fail

// GOOD: Wait for modal to be interactive
await page.click('[data-testid="open-modal"]');
await expect(page.locator('[data-testid="modal"]')).toBeVisible();
await page.click('[data-testid="modal-button"]');
```

### 4. List item selectors
```typescript
// BAD: Generic selector gets wrong item
await page.click('[data-testid="card-item"]'); // Clicks first

// GOOD: Be specific
await page.click('[data-testid="card-item"]:has-text("Fix login bug")');
// OR
await page.click('[data-testid="card-item"][data-card-id="123"]');
```

---

## Priority Order for Implementation

If asked to implement tests without specific priority:

1. **P0 First**: `stories/02-card-lifecycle/` and `critical-failures/`
2. **P1 Second**: Other stories directories
3. **P2 Last**: `ui-completeness/` and `realtime-sync/`

Within each directory, implement in file order (01, 02, 03...).

---

## When You're Stuck

1. **Can't find component**: Run `grep -r "relevant-text" frontend/src/`
2. **Don't know the API**: Check `backend/app/routers/` for endpoints
3. **Unsure about WebSocket events**: Check `frontend/src/lib/stores/websocket.ts`
4. **Need test data**: Check `fixtures/` directory
5. **Selector not working**: Add `data-testid` to the component
