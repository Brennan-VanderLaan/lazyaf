/**
 * E2E Tests - No Runners UX
 *
 * Story: As a user with no runners connected, I need clear guidance
 * on how to start runners so I can use the platform.
 *
 * Priority: P1 - Onboarding/UX
 *
 * Run with: pnpm test:e2e --grep "No Runners"
 */

import { test, expect } from '@playwright/test';

test.describe('No Runners UX', () => {
  test.describe('Empty State Message', () => {
    test('shows "No runners connected" when pool empty', async ({ page }) => {
      // Block WebSocket to ensure no runners
      await page.route('**/ws/**', route => route.abort());
      await page.goto('/');

      // Expand runner list
      const toggleBtn = page.locator('.btn-toggle');
      if (await toggleBtn.isVisible()) {
        await toggleBtn.click();
      }

      // Assert: empty state message
      const noRunners = page.locator('.no-runners');
      await expect(noRunners).toBeVisible();
      await expect(noRunners).toContainText(/no runner/i);
    });

    test('shows hint to get help', async ({ page }) => {
      await page.route('**/ws/**', route => route.abort());
      await page.goto('/');

      // Assert: hint about how to start runners
      const hint = page.locator('.hint');
      await expect(hint).toContainText(/\?|help|start/i);
    });
  });

  test.describe('Help Modal', () => {
    test('help button visible in runner panel', async ({ page }) => {
      await page.goto('/');

      const helpBtn = page.locator('.btn-icon[title*="runner"]').or(
        page.locator('.panel-header .btn-icon')
      );
      await expect(helpBtn).toBeVisible();
    });

    test('clicking help opens runner instructions modal', async ({ page }) => {
      await page.goto('/');

      // Click help button
      const helpBtn = page.locator('.panel-header .btn-icon');
      await helpBtn.click();

      // Assert: modal opens
      const modal = page.locator('.modal');
      await expect(modal).toBeVisible();
      await expect(modal).toContainText(/starting a runner/i);
    });

    test('modal shows Docker command', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Assert: Docker command visible
      const modal = page.locator('.modal');
      await expect(modal).toContainText(/docker run/i);
    });

    test('modal shows Python command', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Assert: Python command visible
      const modal = page.locator('.modal');
      await expect(modal).toContainText(/python/i);
    });

    test('modal shows required environment variables', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Assert: env vars documented
      const modal = page.locator('.modal');
      await expect(modal).toContainText(/LAZYAF_BACKEND_URL/i);
      await expect(modal).toContainText(/ANTHROPIC_API_KEY/i);
    });

    test('can close modal with X button', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Close modal
      await page.locator('.btn-close').click();

      // Assert: modal closed
      await expect(page.locator('.modal')).not.toBeVisible();
    });

    test('can close modal with Escape key', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Press Escape
      await page.keyboard.press('Escape');

      // Assert: modal closed
      await expect(page.locator('.modal')).not.toBeVisible();
    });

    test('can close modal by clicking backdrop', async ({ page }) => {
      await page.goto('/');
      await page.locator('.panel-header .btn-icon').click();

      // Click backdrop
      await page.locator('.modal-backdrop').click({ position: { x: 10, y: 10 } });

      // Assert: modal closed
      await expect(page.locator('.modal')).not.toBeVisible();
    });
  });

  test.describe('Error When Starting Card', () => {
    test.skip('shows clear error when starting card with no runners', async ({ page }) => {
      // No runners connected
      // Try to start a card
      // Assert: error message about no runners
      // Assert: suggests starting a runner
    });
  });
});
