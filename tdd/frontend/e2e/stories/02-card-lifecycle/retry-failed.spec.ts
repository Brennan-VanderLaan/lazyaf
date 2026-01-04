/**
 * E2E Tests - Retry Failed Card
 *
 * Story: As a user, when a card fails I need to be able to retry it
 * without recreating everything from scratch.
 *
 * Priority: P1 - Error recovery
 *
 * Run with: pnpm test:e2e --grep "Retry Failed"
 */

import { test, expect } from '@playwright/test';

test.describe('Retry Failed Card', () => {
  test.describe('Retry Button', () => {
    test.skip('Retry button visible for Failed cards', async ({ page }) => {
      // Open failed card
      const retryBtn = page.locator('[data-testid="retry-btn"]').or(
        page.locator('button').filter({ hasText: /retry/i })
      );
      await expect(retryBtn).toBeVisible();
    });

    test.skip('Retry button NOT visible for non-failed cards', async ({ page }) => {
      // Open To Do card
      // Assert: no retry button
    });

    test.skip('Retry also available for In Review cards', async ({ page }) => {
      // Open In Review card
      // Assert: Retry button visible (to re-run with changes)
    });
  });

  test.describe('Retry Execution', () => {
    test.skip('clicking Retry restarts card execution', async ({ page }) => {
      // Open failed card
      // Click Retry
      // Assert: card moves to In Progress
      // Assert: new execution starts
    });

    test.skip('retry uses existing branch', async ({ page }) => {
      // Retry failed card
      // Assert: same branch used (not new branch)
    });

    test.skip('retry preserves card configuration', async ({ page }) => {
      // Card with specific runner type, agent files
      // Retry
      // Assert: configuration preserved
    });

    test.skip('shows loading state during retry', async ({ page }) => {
      // Click Retry
      // Assert: button shows loading
      // Assert: button disabled
    });
  });

  test.describe('Retry with Modifications', () => {
    test.skip('can edit card before retry', async ({ page }) => {
      // Open failed card
      // Edit description
      // Click Retry
      // Assert: new description used
    });

    test.skip('can change runner type before retry', async ({ page }) => {
      // Open failed card (was Claude)
      // Change to Gemini
      // Retry
      // Assert: Gemini runner used
    });

    test.skip('can attach different agent files', async ({ page }) => {
      // Open failed card
      // Change agent files
      // Retry
      // Assert: new agents used
    });
  });

  test.describe('Failure Analysis', () => {
    test.skip('shows failure reason clearly', async ({ page }) => {
      // Open failed card
      // Assert: error message visible
      // Assert: error is readable/actionable
    });

    test.skip('shows logs up to failure', async ({ page }) => {
      // Open failed card
      // Assert: logs visible
      // Assert: can see what happened before failure
    });

    test.skip('shows which step failed (pipeline)', async ({ page }) => {
      // Failed pipeline card
      // Assert: failed step highlighted
      // Assert: successful steps shown as passed
    });
  });

  test.describe('Multiple Retries', () => {
    test.skip('can retry multiple times', async ({ page }) => {
      // Retry once, fails again
      // Retry again
      // Assert: allowed (no limit on retries)
    });

    test.skip('retry count or history shown', async ({ page }) => {
      // Card retried multiple times
      // Assert: attempt count or history visible
    });
  });
});
