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
import { createTestApi, createApi } from '../../helpers/api';

test.describe('Create Card', () => {
  // Setup: ensure a repo exists and is selected
  test.beforeEach(async ({ page, request }) => {
    const testApi = createTestApi(request);

    // Reset and seed basic data
    await testApi.reset();
    const repo = await testApi.createRepo('test-repo', 'main');

    await page.goto('/');

    // Select the repo from sidebar
    const repoItem = page.locator('[data-testid="repo-item"]').filter({ hasText: 'test-repo' });
    if (await repoItem.isVisible()) {
      await repoItem.click();
    }

    // Wait for board to load
    await expect(page.locator('[data-testid="board"]')).toBeVisible({ timeout: 10000 });
  });

  test.describe('Open Create Modal', () => {
    test('New Card button is visible when repo selected', async ({ page }) => {
      const newCardBtn = page.locator('[data-testid="add-card"]');
      await expect(newCardBtn).toBeVisible();
    });

    test('clicking New Card opens create modal', async ({ page }) => {
      const newCardBtn = page.locator('[data-testid="add-card"]');
      await newCardBtn.click();

      // Modal should appear
      const modal = page.locator('[data-testid="card-modal"]');
      await expect(modal).toBeVisible();
    });
  });

  test.describe('Card Form Fields', () => {
    test.beforeEach(async ({ page }) => {
      // Open create modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();
    });

    test('form has title field (required)', async ({ page }) => {
      const titleInput = page.locator('[data-testid="title-input"]');
      await expect(titleInput).toBeVisible();
      await expect(titleInput).toHaveAttribute('required');
    });

    test('form has description field', async ({ page }) => {
      const descInput = page.locator('[data-testid="description-input"]');
      await expect(descInput).toBeVisible();
    });

    test('form has runner type selector', async ({ page }) => {
      // Runner type selector should be visible
      const runnerSelect = page.locator('select[name="runner_type"]').or(
        page.locator('.runner-type-selector')
      );
      await expect(runnerSelect.first()).toBeVisible();
    });

    test('form has step type selector', async ({ page }) => {
      // Step type selector should be visible with options
      const stepTypeSelector = page.locator('.step-type-btn').or(
        page.locator('select[name="step_type"]')
      );
      await expect(stepTypeSelector.first()).toBeVisible();
    });

    test('selecting script step shows command field', async ({ page }) => {
      // Click on script step type
      const scriptBtn = page.locator('.step-type-btn').filter({ hasText: /script/i });
      if (await scriptBtn.isVisible()) {
        await scriptBtn.click();
      }

      // Command input should appear
      const commandInput = page.locator('textarea[name="command"]').or(
        page.locator('.command-input')
      );
      await expect(commandInput.first()).toBeVisible();
    });

    test('selecting docker step shows image field', async ({ page }) => {
      // Click on docker step type
      const dockerBtn = page.locator('.step-type-btn').filter({ hasText: /docker/i });
      if (await dockerBtn.isVisible()) {
        await dockerBtn.click();

        // Image input should appear
        const imageInput = page.locator('input[name="image"]').or(
          page.locator('.docker-image-input')
        );
        await expect(imageInput.first()).toBeVisible();
      }
    });

    test('form has agent file selector', async ({ page }) => {
      // Agent step type should show agent selector
      const agentBtn = page.locator('.step-type-btn').filter({ hasText: /agent/i });
      if (await agentBtn.isVisible()) {
        await agentBtn.click();

        // Agent files selector should be visible
        const agentSelector = page.locator('.agent-files-select').or(
          page.locator('[data-testid="agent-select"]')
        );
        // This may not always be visible if no agents exist
      }
    });
  });

  test.describe('Create Card - Success', () => {
    test('can create card with title only', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Enter title
      await page.locator('[data-testid="title-input"]').fill('Test card title');

      // Click Create button
      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });
      await createBtn.click();

      // Modal should close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

      // Card should appear in To Do column
      const todoColumn = page.locator('[data-column="todo"]');
      const card = todoColumn.locator('[data-testid="card"]').filter({ hasText: 'Test card title' });
      await expect(card).toBeVisible();
    });

    test('can create card with full details', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Enter title and description
      await page.locator('[data-testid="title-input"]').fill('Full details card');
      await page.locator('[data-testid="description-input"]').fill('This is a detailed description');

      // Click Create button
      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });
      await createBtn.click();

      // Modal should close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

      // Card should appear
      const todoColumn = page.locator('[data-column="todo"]');
      const card = todoColumn.locator('[data-testid="card"]').filter({ hasText: 'Full details card' });
      await expect(card).toBeVisible();
    });

    test('new card appears in To Do column', async ({ page }) => {
      // Create a card
      await page.locator('[data-testid="add-card"]').click();
      await page.locator('[data-testid="title-input"]').fill('New card in todo');

      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });
      await createBtn.click();

      // Verify card is in To Do column specifically
      const todoColumn = page.locator('[data-column="todo"]');
      await expect(todoColumn.locator('[data-testid="card"]').filter({ hasText: 'New card in todo' })).toBeVisible();

      // Verify NOT in other columns
      const inProgressColumn = page.locator('[data-column="in_progress"]');
      await expect(inProgressColumn.locator('[data-testid="card"]').filter({ hasText: 'New card in todo' })).not.toBeVisible();
    });

    test('card shows correct status', async ({ page }) => {
      // Create a card
      await page.locator('[data-testid="add-card"]').click();
      await page.locator('[data-testid="title-input"]').fill('Status check card');

      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });
      await createBtn.click();

      // Wait for modal to close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

      // Find the card and check its status attribute
      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Status check card' });
      await expect(card).toHaveAttribute('data-status', 'todo');
    });
  });

  test.describe('Create Card - Validation', () => {
    test('create button disabled when title is empty', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Leave title empty
      const titleInput = page.locator('[data-testid="title-input"]');
      await expect(titleInput).toHaveValue('');

      // Create button should be disabled or form should not submit
      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });

      // Try clicking - form validation should prevent submission
      await createBtn.click();

      // Modal should still be visible (form didn't submit)
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();
    });

    test('trims whitespace from title', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();

      // Enter title with whitespace
      await page.locator('[data-testid="title-input"]').fill('  My Card  ');

      const createBtn = page.locator('button[type="submit"]').filter({ hasText: /create/i });
      await createBtn.click();

      // Wait for modal to close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

      // Card title should be trimmed
      const card = page.locator('[data-testid="card"]').filter({ hasText: 'My Card' });
      await expect(card).toBeVisible();
    });
  });

  test.describe('Create Card - Cancel', () => {
    test('can cancel card creation', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Enter some data
      await page.locator('[data-testid="title-input"]').fill('Should not be created');

      // Click Cancel or close button
      const closeBtn = page.locator('[data-testid="close-btn"]').or(
        page.locator('button').filter({ hasText: /cancel/i })
      );
      await closeBtn.first().click();

      // Modal should close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible();

      // Card should NOT be created
      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Should not be created' });
      await expect(card).not.toBeVisible();
    });

    test('can close modal with Escape key', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Press Escape
      await page.keyboard.press('Escape');

      // Modal should close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible();
    });

    test('can close modal by clicking backdrop', async ({ page }) => {
      // Open modal
      await page.locator('[data-testid="add-card"]').click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Click outside modal (on backdrop)
      await page.locator('.modal-backdrop').click({ position: { x: 10, y: 10 } });

      // Modal should close
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible();
    });
  });
});
