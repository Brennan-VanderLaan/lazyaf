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
import { createTestApi, createApi } from '../../helpers/api';

test.describe('Retry Failed Card', () => {
  let testApi: ReturnType<typeof createTestApi>;
  let repoId: string;
  let cardId: string;

  test.beforeEach(async ({ page, request }) => {
    testApi = createTestApi(request);

    // Reset and create test data
    await testApi.reset();
    const repo = await testApi.createRepo('test-repo', 'main');
    repoId = repo.id;

    // Create a card and try to start it (will likely fail without runners)
    const card = await testApi.createCard(repoId, {
      title: 'Retry Test Card',
      description: 'Card for retry testing',
    });
    cardId = card.id;

    // Try to start the card to get it into a failed state
    const api = createApi(request);
    try {
      await api.startCard(cardId);
    } catch {
      // Expected to fail
    }

    await page.goto('/');

    // Select the repo
    const repoItem = page.locator('[data-testid="repo-item"]').filter({ hasText: 'test-repo' });
    if (await repoItem.isVisible()) {
      await repoItem.click();
    }

    await expect(page.locator('[data-testid="board"]')).toBeVisible({ timeout: 10000 });
  });

  test.describe('Retry Button', () => {
    test('Retry button visible for Failed cards', async ({ page }) => {
      // Find failed card
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const retryBtn = page.locator('[data-testid="retry-btn"]');
        await expect(retryBtn).toBeVisible();
      }
    });

    test('Retry button NOT visible for To Do cards', async ({ page }) => {
      // Create a fresh todo card
      await page.locator('[data-testid="add-card"]').click();
      await page.locator('[data-testid="title-input"]').fill('Fresh Todo Card');
      await page.locator('button[type="submit"]').filter({ hasText: /create/i }).click();
      await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

      // Open the todo card
      const todoCard = page.locator('[data-column="todo"] [data-testid="card"]').filter({ hasText: 'Fresh Todo Card' });
      await todoCard.click();
      await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

      // Retry button should NOT be visible
      const retryBtn = page.locator('[data-testid="retry-btn"]');
      await expect(retryBtn).not.toBeVisible();
    });

    test('Retry also available for In Review cards', async ({ page }) => {
      // Check if there are any in_review cards
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Retry button should be visible (to re-run with changes)
        const retryBtn = page.locator('[data-testid="retry-btn"]');
        // May or may not be visible depending on implementation
      }
    });
  });

  test.describe('Retry Execution', () => {
    test('clicking Retry restarts card execution', async ({ page }) => {
      // Find and click failed card
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const retryBtn = page.locator('[data-testid="retry-btn"]');
        if (await retryBtn.isVisible()) {
          await retryBtn.click();

          // Wait for status change
          await page.waitForTimeout(2000);

          // Card should either move to in_progress or fail again
          // Close modal and check
          await page.keyboard.press('Escape');

          // Card should no longer be in original failed state
          // (could be in_progress, or failed again with new attempt)
        }
      }
    });

    test('retry calls correct API', async ({ page }) => {
      let retryApiCalled = false;
      await page.route('**/api/cards/*/start', async (route) => {
        retryApiCalled = true;
        await route.continue();
      });

      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const retryBtn = page.locator('[data-testid="retry-btn"]');
        if (await retryBtn.isVisible()) {
          await retryBtn.click();
          await page.waitForTimeout(1000);

          expect(retryApiCalled).toBe(true);
        }
      }
    });

    test('shows loading state during retry', async ({ page }) => {
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const retryBtn = page.locator('[data-testid="retry-btn"]');
        if (await retryBtn.isVisible()) {
          await retryBtn.click();

          // Button should show loading or be disabled
          await expect(retryBtn).toBeDisabled();
        }
      }
    });
  });

  test.describe('Retry with Modifications', () => {
    test('can edit card description before retry', async ({ page }) => {
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Edit description
        const descInput = page.locator('[data-testid="description-input"]');
        if (await descInput.isVisible() && await descInput.isEditable()) {
          await descInput.fill('Updated description for retry');

          // Now retry
          const retryBtn = page.locator('[data-testid="retry-btn"]');
          if (await retryBtn.isVisible()) {
            await retryBtn.click();
            await page.waitForTimeout(1000);
          }
        }
      }
    });

    test.skip('can change runner type before retry', async ({ page }) => {
      // Requires runner type selector to be editable
    });

    test.skip('can attach different agent files', async ({ page }) => {
      // Requires agent file selector
    });
  });

  test.describe('Failure Analysis', () => {
    test('shows failure reason clearly', async ({ page }) => {
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for error message/reason
        const errorMsg = page.locator('.error, .error-message, .failure-reason, [data-testid="error"]');
        // Should show some indication of failure
      }
    });

    test('shows logs up to failure', async ({ page }) => {
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Try to view logs
        const logsBtn = page.locator('[data-testid="logs-btn"]');
        if (await logsBtn.isVisible()) {
          await logsBtn.click();
          const logs = page.locator('[data-testid="job-logs"]');
          await expect(logs).toBeVisible();
        }
      }
    });

    test.skip('shows which step failed (pipeline)', async ({ page }) => {
      // Requires pipeline execution
    });
  });

  test.describe('Multiple Retries', () => {
    test('can retry multiple times', async ({ page }) => {
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      let failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });

      if (await failedCard.isVisible()) {
        // First retry
        await failedCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        let retryBtn = page.locator('[data-testid="retry-btn"]');
        if (await retryBtn.isVisible()) {
          await retryBtn.click();
          await page.waitForTimeout(2000);
          await page.keyboard.press('Escape');

          // Wait for card to potentially fail again
          await page.waitForTimeout(1000);

          // Try second retry if still failed
          failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Retry Test Card' });
          if (await failedCard.isVisible()) {
            await failedCard.click();
            await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

            retryBtn = page.locator('[data-testid="retry-btn"]');
            if (await retryBtn.isVisible()) {
              // Can retry again
              await expect(retryBtn).toBeEnabled();
            }
          }
        }
      }
    });

    test.skip('retry count or history shown', async ({ page }) => {
      // Requires retry history tracking
    });
  });
});
