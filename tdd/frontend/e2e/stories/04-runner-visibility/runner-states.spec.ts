/**
 * E2E Tests - Runner States
 *
 * Story: As a user, I need to see individual runner states
 * to understand pool health and debug issues.
 *
 * Phase 12.6 introduced 6 runner states:
 * - disconnected, connecting, idle, assigned, busy, dead
 *
 * Priority: P1 - Operational visibility
 *
 * Run with: pnpm test:e2e --grep "Runner States"
 */

import { test, expect } from '@playwright/test';

test.describe('Runner States Display', () => {
  test.describe('Runner List', () => {
    test.skip('shows list of connected runners', async ({ page }) => {
      await page.goto('/');

      // Expand runner list
      await page.locator('.btn-toggle').click();

      // Assert: runner items visible
      const runnerItems = page.locator('.runner-item');
      await expect(runnerItems.first()).toBeVisible();
    });

    test.skip('runners grouped by type', async ({ page }) => {
      // Assert: runner-group headers visible
      // Assert: groups like "claude-code", "gemini"
    });

    test.skip('shows runner name or ID', async ({ page }) => {
      // Assert: runner-name visible
    });

    test.skip('shows runner status', async ({ page }) => {
      // Assert: runner-status text visible
    });
  });

  test.describe('State: Idle', () => {
    test.skip('idle runner shows green indicator', async ({ page }) => {
      // Runner in idle state
      // Assert: status dot is green
      // Assert: status text is "Ready"
    });

    test.skip('idle runner can receive work', async ({ page }) => {
      // Assert: no "Executing" indicator
    });
  });

  test.describe('State: Busy', () => {
    test.skip('busy runner shows yellow/warning indicator', async ({ page }) => {
      // Runner in busy state
      // Assert: status dot is yellow/warning color
      // Assert: status text is "Executing"
    });

    test.skip('busy runner shows current job info', async ({ page }) => {
      // Assert: shows "Executing step" indicator
      // Assert: job-icon visible
    });
  });

  test.describe('State: Assigned', () => {
    test.skip('assigned runner shows transitional state', async ({ page }) => {
      // Runner just received job, ACK pending
      // Assert: status is "Starting..."
      // Assert: warning/yellow color
    });
  });

  test.describe('State: Connecting', () => {
    test.skip('connecting runner shows blue indicator', async ({ page }) => {
      // Runner WebSocket open, registration pending
      // Assert: status is "Connecting"
      // Assert: blue/primary color
    });
  });

  test.describe('State: Disconnected', () => {
    test.skip('disconnected runner shows gray indicator', async ({ page }) => {
      // Runner lost connection
      // Assert: status is "Offline"
      // Assert: gray/muted color
    });
  });

  test.describe('State: Dead', () => {
    test.skip('dead runner shows red indicator', async ({ page }) => {
      // Runner crashed/heartbeat timeout
      // Assert: status is "Dead"
      // Assert: red/error color
    });

    test.skip('dead runner indicates problem', async ({ page }) => {
      // Assert: clearly distinguishable from disconnected
    });
  });

  test.describe('State Transitions', () => {
    test.skip('runner transitions idle -> busy when assigned work', async ({ page }) => {
      // Start a card
      // Assert: runner goes from idle to busy
    });

    test.skip('runner transitions busy -> idle when work completes', async ({ page }) => {
      // Card completes
      // Assert: runner returns to idle
    });

    test.skip('transitions visible in real-time', async ({ page }) => {
      // No refresh needed for state changes
    });
  });
});
