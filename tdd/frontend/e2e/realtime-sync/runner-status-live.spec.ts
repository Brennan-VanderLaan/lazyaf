/**
 * E2E Tests - Runner Status Live
 *
 * Story: As a user, I need to see runner pool changes in real-time
 * to know when runners connect/disconnect.
 *
 * Priority: P2 - Operational visibility
 *
 * Run with: pnpm test:e2e --grep "Runner Status Live"
 */

import { test, expect } from '@playwright/test';

test.describe('Runner Status Live', () => {
  test.describe('Runner Connect/Disconnect', () => {
    test.skip('UI updates when runner connects', async ({ page }) => {
      await page.goto('/');

      // Initial state
      // Simulate runner_status WebSocket event (new runner)
      // Assert: runner appears in panel
      // Assert: pool stats update
    });

    test.skip('UI updates when runner disconnects', async ({ page }) => {
      // Runner in list
      // Simulate disconnect event
      // Assert: runner shows disconnected or removed
      // Assert: stats update
    });

    test.skip('runner state transition visible', async ({ page }) => {
      // Runner idle
      // Simulate busy event
      // Assert: runner shows busy state
    });
  });

  test.describe('WebSocket Events', () => {
    test.skip('runner_status event updates store', async ({ page }) => {
      // Simulate runner_status event
      // Assert: runnersStore updated
    });

    test.skip('handles all runner states', async ({ page }) => {
      // For each state: idle, busy, assigned, connecting, disconnected, dead
      // Send event
      // Assert: UI reflects state
    });
  });

  test.describe('Pool Stats Real-Time', () => {
    test.skip('connected count updates live', async ({ page }) => {
      // Runner connects
      // Assert: connected stat increments
    });

    test.skip('ready/busy counts update live', async ({ page }) => {
      // Runner goes busy
      // Assert: ready decrements, busy increments
    });
  });

  test.describe('Multi-User Runner Visibility', () => {
    test.skip('all users see runner changes', async ({ browser }) => {
      // Two users viewing runner panel
      // Runner connects
      // Assert: both users see it
    });
  });
});
