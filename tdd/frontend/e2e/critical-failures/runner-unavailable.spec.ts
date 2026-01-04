/**
 * E2E Tests - Runner Unavailable
 *
 * Critical Failure Mode: User tries to start work but no runners available.
 * This is the #1 failure mode users care about.
 *
 * Priority: P0 - Critical failure handling
 *
 * Run with: pnpm test:e2e --grep "Runner Unavailable"
 */

import { test, expect } from '@playwright/test';

test.describe('Runner Unavailable', () => {
  test.describe('Clear Error Message', () => {
    test('shows helpful message when starting card with no runners', async ({ page }) => {
      // Block WebSocket to ensure no runners
      await page.route('**/ws/**', route => route.abort());

      // Mock start API to return no runners error
      await page.route('**/api/cards/*/start', route => {
        route.fulfill({
          status: 503,
          json: { detail: 'No runners available to execute this card' }
        });
      });

      await page.goto('/');

      // Try to start a card (assuming one exists)
      // Assert: error message about no runners
    });

    test.skip('error message suggests starting a runner', async ({ page }) => {
      // Error occurs
      // Assert: message includes actionable advice
      // Assert: mentions how to start a runner
    });

    test.skip('error message is not technical jargon', async ({ page }) => {
      // Assert: no stack traces
      // Assert: no raw error codes
      // Assert: human-readable language
    });
  });

  test.describe('Visual Indication', () => {
    test.skip('runner panel shows empty state prominently', async ({ page }) => {
      // No runners
      // Assert: "No runners connected" is visible
      // Assert: not hidden in collapsed panel
    });

    test.skip('pool stats show zero', async ({ page }) => {
      // No runners
      // Assert: all stats show 0
    });

    test.skip('help button is prominent', async ({ page }) => {
      // No runners
      // Assert: "?" button easily visible
      // Assert: suggests clicking it
    });
  });

  test.describe('Start Card Blocked', () => {
    test.skip('card stays in To Do after failed start', async ({ page }) => {
      // Try to start, no runners
      // Assert: card remains in To Do column
      // Assert: not stuck in limbo state
    });

    test.skip('can retry start after runners connect', async ({ page }) => {
      // Fail to start (no runners)
      // Runner connects
      // Retry start
      // Assert: succeeds this time
    });
  });

  test.describe('Pipeline Start Blocked', () => {
    test.skip('shows error when running pipeline with no runners', async ({ page }) => {
      // No runners
      // Try to run pipeline
      // Assert: clear error message
    });

    test.skip('pipeline run not created on failure', async ({ page }) => {
      // Fail to start pipeline
      // Assert: no orphan pipeline run created
    });
  });

  test.describe('Graceful Degradation', () => {
    test.skip('app remains functional with no runners', async ({ page }) => {
      // No runners
      // Assert: can still navigate
      // Assert: can still create cards
      // Assert: can still view repos
    });

    test.skip('read operations work without runners', async ({ page }) => {
      // Assert: can view existing cards
      // Assert: can view existing pipelines
      // Assert: can view diffs
    });
  });
});
