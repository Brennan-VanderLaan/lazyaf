/**
 * E2E Tests - Repository Management
 *
 * Story: As a user, I need to add and select repositories to work with.
 * This is foundational - nothing else works without a repo selected.
 *
 * Run with: pnpm test:e2e --grep "Repository Management"
 */

import { test, expect } from '@playwright/test';

test.describe('Repository Management', () => {
  test.describe('No Repository State', () => {
    test('shows "No Repository Selected" when none active', async ({ page }) => {
      await page.goto('/');

      // Board area should show empty state
      const noRepoMessage = page.locator('.no-repo, [data-testid="no-repo"]');
      await expect(noRepoMessage).toBeVisible();
      await expect(noRepoMessage).toContainText(/no repo|select a repo/i);
    });

    test('sidebar shows repo list or empty state', async ({ page }) => {
      await page.goto('/');

      const sidebar = page.locator('.sidebar, [data-testid="sidebar"]');
      await expect(sidebar).toBeVisible();

      // Should either show repos or "Add repository" prompt
    });
  });

  test.describe('Add Repository', () => {
    test.skip('can open add repository modal', async ({ page }) => {
      await page.goto('/');

      // Find and click add repo button
      const addButton = page.locator('[data-testid="add-repo-btn"]').or(
        page.locator('button').filter({ hasText: /add repo/i })
      );
      await addButton.click();

      // Modal should appear
      await expect(page.locator('.modal')).toBeVisible();
    });

    test.skip('add repo form has required fields', async ({ page }) => {
      // Open add repo modal
      // Assert: name field exists
      // Assert: path or URL field exists
      // Assert: submit button exists
    });

    test.skip('can add a local repository via path', async ({ page }) => {
      // Open modal
      // Fill in repo path
      // Submit
      // Assert: repo appears in sidebar
    });

    test.skip('shows error for invalid repo path', async ({ page }) => {
      // Try to add non-existent path
      // Assert: error message shown
    });
  });

  test.describe('Select Repository', () => {
    test.skip('clicking repo in sidebar selects it', async ({ page }) => {
      // Assuming repos exist
      await page.goto('/');

      const repoItem = page.locator('[data-testid="repo-item"]').first();
      await repoItem.click();

      // Board should now show repo content
      await expect(page.locator('.board-header')).toBeVisible();
    });

    test.skip('selected repo shows in board header', async ({ page }) => {
      // Select a repo
      // Assert: board header shows repo name
      // Assert: board header shows default branch
    });

    test.skip('selecting different repo switches context', async ({ page }) => {
      // Select repo A
      // Assert: shows repo A cards
      // Select repo B
      // Assert: shows repo B cards (different)
    });
  });

  test.describe('Repository Info', () => {
    test.skip('shows repo details (name, branch, remote)', async ({ page }) => {
      // Select a repo
      // Find repo info component
      // Assert: shows name
      // Assert: shows default branch
      // Assert: shows remote URL if configured
    });

    test.skip('shows ingestion status', async ({ page }) => {
      // Repo should show if it's been ingested
      // Assert: ingestion indicator visible
    });
  });
});
