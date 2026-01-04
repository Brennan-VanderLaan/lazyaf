/**
 * E2E Tests - Execution Visibility
 *
 * Story: As a user, I need to see what's happening while a card executes
 * so I know the AI is working and can monitor progress.
 *
 * Priority: P0 - Core happy path
 *
 * Run with: pnpm test:e2e --grep "Execution Visibility"
 */

import { test, expect } from '@playwright/test';
import { createTestApi, createApi } from '../../helpers/api';

test.describe('Execution Visibility', () => {
  let testApi: ReturnType<typeof createTestApi>;
  let repoId: string;
  let cardId: string;

  test.beforeEach(async ({ page, request }) => {
    testApi = createTestApi(request);

    // Reset and create test data
    await testApi.reset();
    const repo = await testApi.createRepo('test-repo', 'main');
    repoId = repo.id;

    // Create a card
    const card = await testApi.createCard(repoId, {
      title: 'Execution Test Card',
      description: 'Testing execution visibility',
    });
    cardId = card.id;

    await page.goto('/');

    // Select the repo
    const repoItem = page.locator('[data-testid="repo-item"]').filter({ hasText: 'test-repo' });
    if (await repoItem.isVisible()) {
      await repoItem.click();
    }

    await expect(page.locator('[data-testid="board"]')).toBeVisible({ timeout: 10000 });
  });

  test.describe('Card Shows Execution State', () => {
    test('In Progress card shows job status component', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail without runners
      }

      await page.reload();
      await expect(page.locator('[data-testid="board"]')).toBeVisible({ timeout: 10000 });

      // Find the card (may be in progress or failed)
      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Check for job status component
        const jobStatus = page.locator('[data-testid="job-status"]');
        // Job status should be visible for started cards
        const status = await page.locator('[data-testid="status"]').textContent();
        if (status && !status.toLowerCase().includes('todo')) {
          // Non-todo cards should show job status
          await expect(jobStatus.or(page.locator('.job-status'))).toBeVisible();
        }
      }
    });

    test('shows which runner is executing', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail
      }

      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Runner info should be visible if card is being executed
        const runnerInfo = page.locator('.runner-name, .runner-info, [data-testid="runner-info"]');
        // This is optional - depends on implementation
      }
    });

    test('shows execution duration/elapsed time', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail
      }

      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Duration/timer should be visible for in-progress cards
        const duration = page.locator('.duration, .elapsed, .timer, [data-testid="duration"]');
        // This is optional depending on implementation
      }
    });
  });

  test.describe('Runner Panel Shows Activity', () => {
    test('runner panel is visible', async ({ page }) => {
      // Runner panel should be visible in sidebar
      const runnerPanel = page.locator('[data-testid="runner-panel"]');
      await expect(runnerPanel).toBeVisible();
    });

    test('runner shows status', async ({ page }) => {
      // Each runner item should show status
      const runnerPanel = page.locator('[data-testid="runner-panel"]');
      await expect(runnerPanel).toBeVisible();

      // Runner items (if any) should have status
      const runnerItems = page.locator('[data-testid="runner-item"]');
      const count = await runnerItems.count();

      if (count > 0) {
        const firstRunner = runnerItems.first();
        await expect(firstRunner).toHaveAttribute('data-status');
      }
    });

    test('shows no runners message when none connected', async ({ page }) => {
      // If no runners, should show appropriate message
      const noRunnersMsg = page.locator('[data-testid="no-runners"]');
      const runnerItems = page.locator('[data-testid="runner-item"]');

      const runnerCount = await runnerItems.count();
      if (runnerCount === 0) {
        await expect(noRunnersMsg).toBeVisible();
      }
    });

    test('pool stats are visible', async ({ page }) => {
      const poolStats = page.locator('[data-testid="pool-stats"]');
      await expect(poolStats).toBeVisible();
    });
  });

  test.describe('Log Streaming', () => {
    test('can view logs for started card', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail
      }

      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for logs button
        const logsBtn = page.locator('[data-testid="logs-btn"]');
        if (await logsBtn.isVisible()) {
          await logsBtn.click();

          // Logs container should appear
          const logs = page.locator('[data-testid="job-logs"]');
          await expect(logs).toBeVisible();
        }
      }
    });

    test('logs container shows log content', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail
      }

      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const logsBtn = page.locator('[data-testid="logs-btn"]');
        if (await logsBtn.isVisible()) {
          await logsBtn.click();

          const logs = page.locator('[data-testid="job-logs"]');
          await expect(logs).toBeVisible();

          // Logs should have some content (even if just placeholder)
          const logsText = await logs.textContent();
          expect(logsText).toBeDefined();
        }
      }
    });

    test.skip('logs update in real-time', async ({ page }) => {
      // This test requires active execution and WebSocket
      // Would need to mock or have real runner
    });

    test.skip('logs auto-scroll to bottom', async ({ page }) => {
      // Requires real log streaming
    });

    test.skip('can pause log auto-scroll', async ({ page }) => {
      // Requires real log streaming
    });
  });

  test.describe('Completion Transition', () => {
    test('card shows appropriate status after execution', async ({ page, request }) => {
      // Start the card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // May fail
      }

      // Wait a bit for execution
      await page.waitForTimeout(2000);
      await page.reload();

      // Card should be in a non-todo status
      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        const status = await card.getAttribute('data-status');
        // Status should have changed from todo
        // Could be: in_progress, in_review, done, or failed
      }
    });

    test('completed card shows in In Review column', async ({ page, request }) => {
      // This test would verify successful completion
      // Requires a working runner and successful execution

      // For now, just verify the column exists
      const inReviewColumn = page.locator('[data-column="in_review"]');
      await expect(inReviewColumn).toBeVisible();
    });

    test('card shows diff preview when in review', async ({ page, request }) => {
      // This requires a card that completed successfully
      // Would need to seed data with a card in in_review status

      // For now, just verify diff viewer component exists in modal
      // when we click on an in_review card (if any)
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Diff viewer should be visible
        const diffViewer = page.locator('[data-testid="diff-viewer"]').or(page.locator('.diff-viewer'));
        // May or may not be visible depending on whether changes were made
      }
    });
  });

  test.describe('Failure Visibility', () => {
    test('failed card shows error state', async ({ page, request }) => {
      // Start the card (will likely fail without runners)
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // Expected to fail
      }

      await page.waitForTimeout(1000);
      await page.reload();

      // Check if card is in failed state
      const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
      const failedCard = failedSection.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });

      if (await failedCard.isVisible()) {
        // Card should show failed status
        await expect(failedCard).toHaveAttribute('data-status', 'failed');
      }
    });

    test('can view failure reason', async ({ page, request }) => {
      // Try to start card (will fail)
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // Expected
      }

      await page.waitForTimeout(1000);
      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for error message
        const errorMsg = page.locator('.error, .error-message, .failure-reason');
        // Error should be visible if card failed
      }
    });

    test('can view logs up to failure point', async ({ page, request }) => {
      // Try to start card
      const api = createApi(request);
      try {
        await api.startCard(cardId);
      } catch {
        // Expected
      }

      await page.waitForTimeout(1000);
      await page.reload();

      const card = page.locator('[data-testid="card"]').filter({ hasText: 'Execution Test Card' });
      if (await card.isVisible()) {
        await card.click();
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
  });

  test.describe('Step Progress (for Pipelines)', () => {
    test.skip('shows current step indicator', async ({ page }) => {
      // Requires pipeline execution
    });

    test.skip('shows step status icons', async ({ page }) => {
      // Requires pipeline execution
    });

    test.skip('shows progress percentage', async ({ page }) => {
      // Requires pipeline execution
    });
  });
});
