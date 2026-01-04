/**
 * E2E Tests - Start Card Execution
 *
 * Story: As a user, I need to start a card so the AI agent executes the work.
 * This transitions the card from "To Do" to "In Progress".
 *
 * Priority: P0 - Core happy path
 *
 * Run with: pnpm test:e2e --grep "Start Card"
 */

import { test, expect } from '@playwright/test';

test.describe('Start Card Execution', () => {
  test.describe('Start from Board (Drag and Drop)', () => {
    test.skip('can drag card from To Do to In Progress', async ({ page }) => {
      // Have a card in To Do
      // Drag card to In Progress column
      // Assert: card moves to In Progress
      // Assert: card execution starts (API called)
    });

    test.skip('card shows starting state during transition', async ({ page }) => {
      // Drag card
      // Assert: brief loading/starting indicator
    });
  });

  test.describe('Start from Card Modal', () => {
    test.skip('Start button visible for To Do cards', async ({ page }) => {
      // Open card modal for a To Do card
      const startBtn = page.locator('[data-testid="start-card-btn"]').or(
        page.locator('button').filter({ hasText: /start/i })
      );
      await expect(startBtn).toBeVisible();
    });

    test.skip('Start button NOT visible for In Progress cards', async ({ page }) => {
      // Open card modal for In Progress card
      // Assert: Start button not visible or disabled
    });

    test.skip('clicking Start transitions card to In Progress', async ({ page }) => {
      // Open To Do card
      // Click Start
      // Assert: card status changes to In Progress
      // Assert: card moves to In Progress column
    });

    test.skip('Start button shows loading state while starting', async ({ page }) => {
      // Click Start
      // Assert: button shows "Starting..." or spinner
      // Assert: button is disabled during transition
    });
  });

  test.describe('Start - API Integration', () => {
    test.skip('calls POST /api/cards/{id}/start', async ({ page }) => {
      // Intercept API
      // Start card
      // Assert: correct endpoint called
    });

    test.skip('handles API error gracefully', async ({ page }) => {
      // Mock API to fail
      // Start card
      // Assert: error message shown
      // Assert: card stays in To Do
    });

    test.skip('handles no runners available', async ({ page }) => {
      // Mock API to return "no runners" error
      // Start card
      // Assert: clear error message about runners
    });
  });

  test.describe('Start - Card Gets Branch', () => {
    test.skip('started card gets a branch name assigned', async ({ page }) => {
      // Start card
      // Open card details
      // Assert: branch_name field is populated
    });

    test.skip('branch name follows pattern', async ({ page }) => {
      // Start card with title "Add login feature"
      // Assert: branch name like "card/add-login-feature" or similar
    });
  });

  test.describe('Start - WebSocket Updates', () => {
    test.skip('card status updates via WebSocket without refresh', async ({ page }) => {
      // Start card
      // Wait for WebSocket message
      // Assert: card updates to In Progress automatically
    });

    test.skip('multiple users see card start in real-time', async ({ browser }) => {
      // Two browser contexts
      // User A starts card
      // User B sees card move to In Progress (no refresh)
    });
  });
});
