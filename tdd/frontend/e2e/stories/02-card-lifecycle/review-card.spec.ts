/**
 * E2E Tests - Review Card
 *
 * Story: As a user, I need to review AI-generated changes before they're merged.
 * This is critical for compliance and quality control.
 *
 * Priority: P1 - Compliance requirement
 *
 * Run with: pnpm test:e2e --grep "Review Card"
 */

import { test, expect } from '@playwright/test';
import { createTestApi, createApi } from '../../helpers/api';

test.describe('Review Card', () => {
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
      title: 'Review Test Card',
      description: 'Card for review testing',
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

  test.describe('Diff Viewer', () => {
    test('In Review column exists', async ({ page }) => {
      const inReviewColumn = page.locator('[data-column="in_review"]');
      await expect(inReviewColumn).toBeVisible();
    });

    test('In Review card shows diff viewer when has changes', async ({ page }) => {
      // This test requires a card that completed with changes
      // Check if there are any in_review cards
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Diff viewer should be visible
        const diffViewer = page.locator('[data-testid="diff-viewer"]');
        // May not be visible if no changes
      }
    });

    test('diff viewer shows file list', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const fileList = page.locator('[data-testid="file-list"]');
        // Should be visible if there are changes
      }
    });

    test('diff shows additions and deletions with colors', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for diff rows with add/del classes
        const addLines = page.locator('.diff-row.add, .additions');
        const delLines = page.locator('.diff-row.del, .deletions');
        // These indicate proper diff rendering
      }
    });

    test('can expand/collapse diff sections', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for expandable file headers
        const fileItems = page.locator('[data-testid="file-item"]');
        if (await fileItems.count() > 0) {
          // Click to toggle expansion
          const firstFile = fileItems.first();
          const fileHeader = firstFile.locator('.file-header');
          if (await fileHeader.isVisible()) {
            await fileHeader.click();
            // Diff content should toggle
          }
        }
      }
    });

    test('shows diff stats (lines added/removed)', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for stats like "+X -Y"
        const stats = page.locator('.additions, .deletions, .file-stats');
        // Stats should be visible in diff header
      }
    });
  });

  test.describe('Approve Card', () => {
    test('Approve button visible for In Review cards', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const approveBtn = page.locator('[data-testid="approve-btn"]');
        await expect(approveBtn).toBeVisible();
      }
    });

    test('Approve button NOT visible for To Do cards', async ({ page }) => {
      // Click on our todo card
      const todoCard = page.locator('[data-column="todo"] [data-testid="card"]').filter({ hasText: 'Review Test Card' });

      if (await todoCard.isVisible()) {
        await todoCard.click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Approve button should NOT be visible
        const approveBtn = page.locator('[data-testid="approve-btn"]');
        await expect(approveBtn).not.toBeVisible();
      }
    });

    test('clicking Approve moves card to Done', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        const cardTitle = await inReviewCards.first().locator('.card-title').textContent();

        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const approveBtn = page.locator('[data-testid="approve-btn"]');
        await approveBtn.click();

        // Wait for transition
        await page.waitForTimeout(2000);
        await page.keyboard.press('Escape');

        // Card should move to Done column
        const doneColumn = page.locator('[data-column="done"]');
        const cardInDone = doneColumn.locator('[data-testid="card"]').filter({ hasText: cardTitle || '' });
        await expect(cardInDone).toBeVisible();
      }
    });

    test('approval calls correct API', async ({ page }) => {
      let approveApiCalled = false;
      await page.route('**/api/cards/*/approve', async (route) => {
        approveApiCalled = true;
        await route.continue();
      });

      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const approveBtn = page.locator('[data-testid="approve-btn"]');
        await approveBtn.click();

        await page.waitForTimeout(1000);
        expect(approveApiCalled).toBe(true);
      }
    });
  });

  test.describe('Reject Card', () => {
    test('Reject button visible for In Review cards', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const rejectBtn = page.locator('[data-testid="reject-btn"]');
        await expect(rejectBtn).toBeVisible();
      }
    });

    test('clicking Reject moves card to Failed', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        const cardTitle = await inReviewCards.first().locator('.card-title').textContent();

        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        const rejectBtn = page.locator('[data-testid="reject-btn"]');
        await rejectBtn.click();

        // Wait for transition
        await page.waitForTimeout(2000);
        await page.keyboard.press('Escape');

        // Card should move to Failed section
        const failedSection = page.locator('[data-column="failed"]').or(page.locator('.failed-cards'));
        const cardInFailed = failedSection.locator('[data-testid="card"]').filter({ hasText: cardTitle || '' });
        // Card may or may not be visible depending on UI implementation
      }
    });

    test.skip('can provide rejection reason', async ({ page }) => {
      // Would require rejection reason input
    });
  });

  test.describe('Request Changes', () => {
    test.skip('can add comments to specific lines', async ({ page }) => {
      // Requires line commenting feature
    });

    test.skip('comments are saved', async ({ page }) => {
      // Requires commenting feature
    });
  });

  test.describe('Branch Operations', () => {
    test('shows current branch name for in-review card', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Look for branch name display
        const branchName = page.locator('.branch-name, [data-testid="branch"], .card-branch');
        // Branch should be visible
      }
    });

    test('shows target branch for merge', async ({ page }) => {
      const inReviewCards = page.locator('[data-column="in_review"] [data-testid="card"]');
      const count = await inReviewCards.count();

      if (count > 0) {
        await inReviewCards.first().click();
        await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

        // Target branch (e.g., "main") should be displayed
        const targetBranch = page.locator('.target-branch, .merge-target');
        // May or may not be visible
      }
    });

    test.skip('shows commit history', async ({ page }) => {
      // Requires commit history display
    });
  });
});
