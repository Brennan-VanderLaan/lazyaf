# Frontend E2E Testing Guide

This document provides everything needed to understand and implement E2E tests for LazyAF's frontend.

---

## Application Context (For LLMs)

**LazyAF** is a CI/CD automation platform with AI-powered code generation. Think of it as a self-hosted GitHub Actions replacement with built-in AI code assistance.

### Core Concepts

| Concept | Description |
|---------|-------------|
| **Card** | A work item (like a ticket). Cards can trigger AI execution or pipeline runs. Status: `todo` → `in_progress` → `in_review` → `done`/`failed` |
| **Pipeline** | A sequence of steps (build, test, deploy). Can be triggered manually, by cards, or by git push |
| **Runner** | An execution agent (Docker container or k8s pod) that runs pipeline steps. States: `disconnected`, `connecting`, `idle`, `assigned`, `busy`, `dead` |
| **Step** | A single unit within a pipeline. States: `pending`, `preparing`, `running`, `completing`, `completed`, `failed`, `cancelled` |
| **Agent File** | A markdown file with instructions for AI execution |
| **Debug Re-Run** | Ability to set breakpoints and re-run a failed pipeline, pausing at specific steps |
| **Agent Playground** | Ephemeral testing environment for AI agents. Run agents against repo states without creating cards. Critical for validating AI behavior. Uses SSE for log streaming. |

### Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Frontend (Svelte)                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐ │
│  │  Board   │  │ Pipeline │  │  Runner  │  │ Debug   │  │  Playground  │ │
│  │  View    │  │  Editor  │  │  Panel   │  │ Modal   │  │  (Phase 11)  │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬────┘  └──────┬───────┘ │
│       │             │             │              │              │         │
│  ┌────┴─────────────┴─────────────┴──────────────┴──────────────┤         │
│  │                    WebSocket Store                           │   SSE   │
│  │         (Real-time updates for ALL state changes)            │─────────┤
│  └──────────────────────────┬───────────────────────────────────┘         │
└─────────────────────────────┼─────────────────────────────────────────────┘
                              │ WebSocket                             │ SSE
                              ▼                                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                           Backend (FastAPI)                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐ │
│  │   API    │  │ Executor │  │  Runner  │  │  Debug  │  │  Playground  │ │
│  │ Routes   │  │ (Local/  │  │  Manager │  │ Ctrl    │  │  Service     │ │
│  │          │  │  Remote) │  │          │  │         │  │  (SSE)       │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────┘  └──────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

**Note**: The Playground uses SSE (Server-Sent Events) for log streaming, not WebSocket.
This is because playground sessions are ephemeral and don't need bidirectional communication.

### Key Technical Details

1. **WebSocket-First**: All real-time state comes via WebSocket. The frontend subscribes and updates stores reactively.
2. **Stores Pattern**: Svelte stores (`runners.ts`, `cards.ts`, `debug.ts`) hold state and are updated by WebSocket handlers.
3. **No HTTP Polling**: Runner status, step logs, card updates all come via WebSocket events.

---

## Test Organization

Tests are organized by **customer stories** rather than technical components. This ensures tests validate real user value.

```
tdd/frontend/e2e/
├── stories/                    # User journey tests
│   ├── 01-setup/               # Initial setup workflows
│   ├── 02-card-lifecycle/      # Card creation → execution → review (P0)
│   ├── 03-pipeline-lifecycle/  # Pipeline creation and execution
│   ├── 04-runner-visibility/   # Runner pool management
│   ├── 05-git-operations/      # Diff viewing, rebasing, conflicts
│   ├── 06-debug-rerun/         # Debug breakpoint workflows (Phase 12.7)
│   └── 07-agent-playground/    # AI agent testing sandbox (P1)
├── critical-failures/          # Error handling tests (P0)
├── ui-completeness/            # UI quality assurance
├── realtime-sync/              # Multi-user broadcast tests
├── smoke/                      # Quick sanity checks
└── scenarios/                  # Legacy - to be migrated
```

### Priority System

| Priority | Meaning | When to Implement |
|----------|---------|-------------------|
| **P0** | Must work or product is broken | Implement first, block releases |
| **P1** | Core functionality | Implement before P2 |
| **P2** | Quality/polish features | Implement as time allows |

**P0 tests** are in:
- `stories/02-card-lifecycle/` - The core value proposition
- `critical-failures/` - Error recovery scenarios

**P1 tests** include:
- `stories/07-agent-playground/` - Critical for AI development workflow

---

## How to Implement Tests

### Step 1: Understand the Test File

Each test file has:
1. A **user story** in the header comment
2. A **priority** level
3. A **run command** for focused testing
4. Skeleton tests marked with `test.skip()`

Example header:
```typescript
/**
 * E2E Tests - Create Card
 *
 * Story: As a developer, I want to create a card describing work to be done
 * so that the AI or a pipeline can execute it.
 *
 * Priority: P0 - Core functionality
 *
 * Run with: pnpm test:e2e --grep "Create Card"
 */
```

### Step 2: Read Related Application Code

Before implementing, read:

1. **The component being tested**: `frontend/src/lib/components/*.svelte`
2. **The relevant store**: `frontend/src/lib/stores/*.ts`
3. **API types**: `frontend/src/lib/api/types.ts`
4. **WebSocket message handlers**: `frontend/src/lib/stores/websocket.ts`

### Step 3: Convert `test.skip()` to `test()`

Replace skeleton with real implementation:

```typescript
// BEFORE (skeleton)
test.skip('can create card with title and description', async ({ page }) => {
  // Fill card form
  // Submit
  // Assert: card appears on board
});

// AFTER (implemented)
test('can create card with title and description', async ({ page }) => {
  await page.goto('/');

  // Open create card modal
  await page.click('[data-testid="create-card-button"]');

  // Fill form
  await page.fill('[data-testid="card-title-input"]', 'Fix login bug');
  await page.fill('[data-testid="card-description-input"]', 'Login fails on mobile');

  // Submit
  await page.click('[data-testid="card-submit-button"]');

  // Assert card appears
  await expect(page.locator('[data-testid="card-item"]')).toContainText('Fix login bug');
});
```

### Step 4: Use Appropriate Test Patterns

#### Pattern: Mocking WebSocket Events

For tests that need to simulate backend events without a real backend:

```typescript
import { MockWebSocket } from '../../fixtures/mock-websocket';
import { runnerStatusMessages } from '../../fixtures/websocket-messages/runner-messages';

test('runner appears when WebSocket event received', async ({ page }) => {
  await page.goto('/');

  // Inject mock WebSocket message via page.evaluate
  await page.evaluate((message) => {
    // Access the app's WebSocket handler
    window.__testWebSocketHandler__(message);
  }, runnerStatusMessages.idle);

  // Assert runner visible
  await expect(page.locator('[data-testid="runner-item"]')).toBeVisible();
});
```

#### Pattern: Multi-User Tests (Browser Contexts)

For testing real-time collaboration:

```typescript
test('User B sees card created by User A', async ({ browser }) => {
  // Create two isolated browser contexts
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();

  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();

  // Both navigate to same board
  await pageA.goto('/');
  await pageB.goto('/');

  // User A creates card
  await pageA.click('[data-testid="create-card-button"]');
  await pageA.fill('[data-testid="card-title-input"]', 'New Feature');
  await pageA.click('[data-testid="card-submit-button"]');

  // User B should see it within 2 seconds (real-time)
  await expect(pageB.locator('[data-testid="card-item"]'))
    .toContainText('New Feature', { timeout: 2000 });

  // Cleanup
  await contextA.close();
  await contextB.close();
});
```

#### Pattern: Testing Error States

```typescript
test('shows error when backend disconnects', async ({ page }) => {
  await page.goto('/');

  // Simulate WebSocket close
  await page.evaluate(() => {
    window.__testWebSocketClose__();
  });

  // Assert error state visible
  await expect(page.locator('[data-testid="connection-error"]')).toBeVisible();
  await expect(page.locator('[data-testid="reconnecting-indicator"]')).toBeVisible();
});
```

#### Pattern: Waiting for Async Operations

```typescript
test('pipeline run completes', async ({ page }) => {
  await page.goto('/pipelines/test-pipeline');

  // Start run
  await page.click('[data-testid="run-pipeline-button"]');

  // Wait for completion (may take time)
  await expect(page.locator('[data-testid="run-status"]'))
    .toHaveText('Completed', { timeout: 30000 });

  // Verify all steps show completed
  const steps = page.locator('[data-testid="step-status"]');
  await expect(steps).toHaveCount(3);
  for (let i = 0; i < 3; i++) {
    await expect(steps.nth(i)).toHaveAttribute('data-status', 'completed');
  }
});
```

---

## Test Fixtures

### WebSocket Message Fixtures

Located in `fixtures/websocket-messages/`:

| File | Purpose |
|------|---------|
| `runner-messages.ts` | Runner state transitions, lifecycle sequences |
| `step-messages.ts` | Step execution states, log messages |
| `debug-messages.ts` | Debug breakpoint, status, resume events |

Usage:
```typescript
import { runnerStatusMessages, runnerLifecycleSequence } from '../../fixtures/websocket-messages/runner-messages';
import { debugBreakpointMessages } from '../../fixtures/websocket-messages/debug-messages';

// Use pre-built messages
const idleRunner = runnerStatusMessages.idle;

// Or use sequences for complex scenarios
for (const message of runnerLifecycleSequence) {
  await simulateWebSocketMessage(page, message);
}
```

### MockWebSocket Helper

```typescript
import { MockWebSocket } from '../../fixtures/mock-websocket';

const ws = new MockWebSocket();
ws.simulateOpen();
ws.simulateMessage(runnerStatusMessages.idle);
ws.simulateClose();
```

---

## Two-Tier Test Architecture

### Mocked Tier (Fast)

- **No backend required**
- All API calls intercepted via `page.route()`
- WebSocket events simulated
- Runs in < 60 seconds
- Good for: UI logic, component behavior, form validation

```bash
pnpm test:e2e:mocked
```

### Real Tier (Comprehensive)

- **Requires backend** with `LAZYAF_TEST_MODE=true`
- Real database operations (reset between tests)
- Real WebSocket events
- AI calls mocked via `LAZYAF_MOCK_AI=true`
- Good for: Integration, workflows, data persistence

```bash
# Start backend first
cd backend && LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app

# Run tests
pnpm test:e2e:real
```

---

## Adding `data-testid` Attributes

When tests fail because selectors don't exist, add `data-testid` attributes to components:

```svelte
<!-- BEFORE -->
<button on:click={createCard}>Create</button>

<!-- AFTER -->
<button data-testid="create-card-button" on:click={createCard}>Create</button>
```

Common test IDs needed:

| Component | Test ID Pattern |
|-----------|-----------------|
| Cards | `card-item`, `card-title-input`, `card-submit-button` |
| Runners | `runner-panel`, `runner-item`, `runner-status-{state}` |
| Pipelines | `pipeline-editor`, `run-pipeline-button`, `step-item` |
| Modals | `{name}-modal`, `modal-close-button` |
| Forms | `{name}-form`, `{field}-input`, `{name}-submit-button` |

---

## Debug Re-Run Tests (Phase 12.7)

Phase 12.7 adds breakpoint-based debugging. These tests are in `stories/06-debug-rerun/`.

### Key Components

- `DebugRerunModal.svelte` - UI for selecting breakpoints
- `debug.ts` store - Manages debug session state
- WebSocket events: `debug_breakpoint`, `debug_status`, `debug_resume`

### Test Flow

```typescript
test('can set breakpoint and pause execution', async ({ page }) => {
  // Navigate to failed run
  await page.goto('/runs/failed-run-123');

  // Open debug modal
  await page.click('[data-testid="debug-rerun-button"]');

  // Select breakpoint at step 2
  await page.click('[data-testid="breakpoint-step-2"]');

  // Start debug run
  await page.click('[data-testid="start-debug-button"]');

  // Simulate breakpoint hit
  await simulateWebSocketMessage(page, debugBreakpointMessages.step2);

  // Assert paused state visible
  await expect(page.locator('[data-testid="debug-paused-indicator"]')).toBeVisible();
  await expect(page.locator('[data-testid="current-step"]')).toContainText('Run Tests');
});
```

---

## Agent Playground Tests (Phase 11)

The Agent Playground is critical for testing AI agent behavior against repo states. Tests are in `stories/07-agent-playground/`.

### Key Difference: SSE Instead of WebSocket

The Playground uses **Server-Sent Events (SSE)** for real-time log streaming, not WebSocket. This changes how you mock events in tests.

### Key Components

- `PlaygroundPage.svelte` - Main UI (configuration + results)
- `playground.ts` store - Session state, SSE connection management
- API endpoints: `/api/repos/{id}/playground/test`, `/api/playground/{session}/stream`

### Test Flow

```typescript
test('can run agent test and see logs', async ({ page }) => {
  await page.goto('/playground');

  // Configure test
  await page.fill('[data-testid="task-input"]', 'Fix the login bug');
  await page.selectOption('[data-testid="model-select"]', 'claude-sonnet-4-5');

  // Start test
  await page.click('[data-testid="test-once-button"]');

  // Assert: status changes
  await expect(page.locator('[data-testid="status"]')).toHaveText('Running');

  // Assert: logs appear (SSE streaming)
  await expect(page.locator('[data-testid="log-panel"]')).not.toBeEmpty();

  // Wait for completion
  await expect(page.locator('[data-testid="status"]'))
    .toHaveText('Completed', { timeout: 60000 });

  // Assert: diff visible
  await expect(page.locator('[data-testid="diff-viewer"]')).toBeVisible();
});
```

### Mocking SSE Events

```typescript
// For mocked tier tests, intercept SSE endpoint
await page.route('**/api/playground/*/stream', async route => {
  // Return SSE stream with mock events
  await route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    body: [
      'event: log\ndata: {"line": "Starting execution..."}\n\n',
      'event: log\ndata: {"line": "Cloning repo..."}\n\n',
      'event: status\ndata: {"status": "running"}\n\n',
      'event: complete\ndata: {"status": "completed", "diff": "..."}\n\n',
    ].join(''),
  });
});
```

### Playground-Specific Test IDs

| Element | Test ID |
|---------|---------|
| Task input | `task-input` |
| Branch selector | `branch-select` |
| Runner selector | `runner-select` |
| Model selector | `model-select` |
| Test Once button | `test-once-button` |
| Cancel button | `cancel-button` |
| Reset button | `reset-button` |
| Status indicator | `status` |
| Log panel | `log-panel` |
| Diff viewer | `diff-viewer` |
| Elapsed time | `elapsed-time` |

---

## What NOT to Test

### TypeScript Catches These
- Type mismatches
- Missing required fields
- Wrong function signatures

### Low-Value Tests
- Static text content
- Exact error message wording
- CSS styling
- Animation timing

### Implementation Details
- Internal store structure
- Component internal state
- DOM structure (use test IDs)

---

## Running Tests

```bash
# All E2E tests
pnpm test:e2e

# With Playwright UI (interactive)
pnpm test:e2e:ui

# Specific test file
pnpm test:e2e --grep "Create Card"

# Specific story
pnpm test:e2e --grep "Card Lifecycle"

# Only P0 tests (by pattern)
pnpm test:e2e e2e/stories/02-card-lifecycle e2e/critical-failures
```

---

## Checklist for Implementing a Test

- [ ] Read the user story in the file header
- [ ] Read related component/store code
- [ ] Replace `test.skip()` with `test()`
- [ ] Add `data-testid` attributes to components if needed
- [ ] Use fixtures for WebSocket messages
- [ ] Handle async waits with appropriate timeouts
- [ ] Test both success and error paths
- [ ] Run the test: `pnpm test:e2e --grep "Test Name"`
- [ ] Verify test passes/fails appropriately

---

## File Reference

| Path | Purpose |
|------|---------|
| `playwright.config.ts` | Test configuration, projects, timeouts |
| `e2e/global-setup.ts` | Runs before all tests |
| `e2e/global-teardown.ts` | Runs after all tests |
| `fixtures/mock-websocket.ts` | WebSocket simulation helper |
| `fixtures/websocket-messages/*.ts` | Pre-built message fixtures |
| `fixtures/page-objects/*.ts` | Page Object Models (POMs) |

---

## Common Issues

### "Timeout waiting for selector"
- Add `data-testid` to the element
- Increase timeout if operation is slow
- Check if element is conditionally rendered

### "WebSocket not connected"
- For mocked tests: ensure mock is set up before actions
- For real tests: ensure backend is running

### "Test passed locally but fails in CI"
- Add explicit waits for async operations
- Don't rely on timing assumptions
- Use `expect().toBeVisible()` instead of immediate assertions

---

## Summary

| Concept | Key Point |
|---------|-----------|
| Organization | Stories-based, not component-based |
| Priority | P0 = blocking, P1 = core, P2 = polish |
| Fixtures | Use pre-built WebSocket messages |
| Selectors | Always use `data-testid` |
| Two-tier | Mocked (fast) vs Real (comprehensive) |
| Async | Use appropriate timeouts, wait for state |
