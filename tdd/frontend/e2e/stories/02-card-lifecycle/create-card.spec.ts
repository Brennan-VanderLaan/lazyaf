/**
 * E2E Tests - Create Card
 *
 * Story: As a user, I need to create cards that describe work for AI agents.
 * This is the entry point for the core value proposition.
 *
 * Priority: P0 - Core happy path
 *
 * Run with: pnpm test:e2e --grep "Create Card"
 */

import { test, expect } from '@playwright/test';

test.describe('Create Card', () => {
  // Requires a repo to be selected
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // TODO: Ensure a repo is selected (may need fixture or API setup)
  });

  test.describe('Open Create Modal', () => {
    test('New Card button is visible when repo selected', async ({ page }) => {
      const newCardBtn = page.locator('.btn-create').or(
        page.locator('[data-testid="new-card-btn"]')
      );
      await expect(newCardBtn).toBeVisible();
    });

    test('clicking New Card opens create modal', async ({ page }) => {
      const newCardBtn = page.locator('.btn-create').or(
        page.locator('[data-testid="new-card-btn"]')
      );
      await newCardBtn.click();

      // Modal should appear
      const modal = page.locator('.modal, [role="dialog"]');
      await expect(modal).toBeVisible();
    });
  });

  test.describe('Card Form Fields', () => {
    test.skip('form has title field (required)', async ({ page }) => {
      // Open create modal
      // Assert: title input exists
      // Assert: title is required (has required attribute or validation)
    });

    test.skip('form has description field', async ({ page }) => {
      // Open create modal
      // Assert: description textarea exists
    });

    test.skip('form has runner type selector', async ({ page }) => {
      // Open create modal
      // Assert: runner type dropdown/select exists
      // Assert: options include "Any", "Claude Code", "Gemini"
    });

    test.skip('form has step type selector', async ({ page }) => {
      // Open create modal
      // Assert: step type selector exists
      // Assert: options include "AI Agent", "Shell Script", "Docker Container"
    });

    test.skip('selecting script step shows command field', async ({ page }) => {
      // Open create modal
      // Select "Shell Script" step type
      // Assert: command input appears
    });

    test.skip('selecting docker step shows image field', async ({ page }) => {
      // Open create modal
      // Select "Docker Container" step type
      // Assert: image input appears
      // Assert: command input appears
    });

    test.skip('form has agent file selector', async ({ page }) => {
      // Open create modal
      // Assert: agent file multi-select exists
    });
  });

  test.describe('Create Card - Success', () => {
    test.skip('can create card with title only', async ({ page }) => {
      // Open modal
      // Enter title: "Test card"
      // Click Create
      // Assert: modal closes
      // Assert: card appears in "To Do" column
    });

    test.skip('can create card with full details', async ({ page }) => {
      // Open modal
      // Enter title
      // Enter description
      // Select runner type
      // Select step type
      // Click Create
      // Assert: card appears in "To Do"
      // Assert: card shows configured details
    });

    test.skip('new card appears in To Do column', async ({ page }) => {
      // Create a card
      // Assert: card is in first/To Do column
      // Assert: card has correct title
    });

    test.skip('card shows step type indicator', async ({ page }) => {
      // Create card with agent step type
      // Assert: card shows agent icon/indicator
    });
  });

  test.describe('Create Card - Validation', () => {
    test.skip('shows error when title is empty', async ({ page }) => {
      // Open modal
      // Leave title empty
      // Click Create
      // Assert: validation error shown
      // Assert: modal stays open
    });

    test.skip('trims whitespace from title', async ({ page }) => {
      // Enter "  My Card  " as title
      // Create
      // Assert: card title is "My Card"
    });
  });

  test.describe('Create Card - Cancel', () => {
    test.skip('can cancel card creation', async ({ page }) => {
      // Open modal
      // Enter some data
      // Click Cancel
      // Assert: modal closes
      // Assert: no card created
    });

    test.skip('can close modal with Escape key', async ({ page }) => {
      // Open modal
      // Press Escape
      // Assert: modal closes
    });

    test.skip('can close modal by clicking backdrop', async ({ page }) => {
      // Open modal
      // Click outside modal
      // Assert: modal closes
    });
  });
});
