/**
 * Smoke Tests - App Loads Successfully
 *
 * These tests verify basic app functionality without requiring a backend.
 * They run in the MOCKED tier for fast feedback.
 *
 * Run with: pnpm test:e2e:mocked
 */

import { test, expect, Page } from '@playwright/test';
import { createBoardPage, createNavigation } from '../helpers/pages';

// Helper to collect console errors
async function withConsoleErrors(
  page: Page,
  action: () => Promise<void>
): Promise<string[]> {
  const errors: string[] = [];
  const handler = (msg: { type: () => string; text: () => string }) => {
    if (msg.type() === 'error') {
      errors.push(msg.text());
    }
  };

  page.on('console', handler);
  await action();
  page.off('console', handler);

  return errors;
}

test.describe('Smoke Tests - App Loads', () => {
  test('homepage loads without JavaScript errors', async ({ page }) => {
    const errors = await withConsoleErrors(page, async () => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');
    });

    // Filter out expected errors (network errors are expected without backend)
    const unexpectedErrors = errors.filter(
      err =>
        !err.includes('404') &&
        !err.includes('500') &&
        !err.includes('WebSocket') &&
        !err.includes('Failed to fetch') &&
        !err.includes('Failed to load resource') &&
        !err.includes('Error loading') &&
        !err.includes('ERR_CONNECTION_REFUSED') &&
        !err.includes('ECONNREFUSED')
    );

    expect(unexpectedErrors).toHaveLength(0);
  });

  test('app shows main layout structure', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // Body should render
    await expect(page.locator('body')).toBeVisible();

    // Should have some content (not a blank page)
    const content = await page.locator('body').textContent();
    expect(content?.length).toBeGreaterThan(0);
  });

  test('app handles backend being unavailable gracefully', async ({ page }) => {
    // Block all API requests
    await page.route('**/api/**', route => route.abort());
    await page.route('**/ws/**', route => route.abort());
    await page.route('**/ws', route => route.abort());

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    // Should not show white screen of death
    const bodyContent = await page.locator('body').textContent();
    expect(bodyContent?.length).toBeGreaterThan(0);

    // Should not show uncaught exception text
    await expect(page.locator('body')).not.toContainText('Uncaught');
    await expect(page.locator('body')).not.toContainText('undefined is not');
  });

  test('app renders without crashing after delay', async ({ page }) => {
    await page.goto('/');

    // Wait for any async initialization
    await page.waitForTimeout(2000);

    // Page should still be interactive
    await expect(page.locator('body')).toBeVisible();
  });
});

test.describe('Smoke Tests - No Infinite Loops', () => {
  test('no excessive network requests', async ({ page }) => {
    const requests: string[] = [];

    page.on('request', request => {
      requests.push(request.url());
    });

    await page.goto('/');
    await page.waitForTimeout(5000);

    // Count requests per endpoint
    const requestCounts = requests.reduce(
      (acc, url) => {
        try {
          const endpoint = new URL(url).pathname;
          acc[endpoint] = (acc[endpoint] || 0) + 1;
        } catch {
          // Ignore invalid URLs
        }
        return acc;
      },
      {} as Record<string, number>
    );

    // No endpoint should be called excessively
    for (const [endpoint, count] of Object.entries(requestCounts)) {
      expect(count, `${endpoint} called ${count} times - possible infinite loop`).toBeLessThan(50);
    }
  });

  test('no memory leak from WebSocket reconnection', async ({ page }) => {
    const wsAttempts: number[] = [];

    page.on('websocket', () => {
      wsAttempts.push(Date.now());
    });

    await page.goto('/');
    await page.waitForTimeout(10000);

    // Should not have excessive reconnection attempts
    expect(
      wsAttempts.length,
      'Too many WebSocket connection attempts - possible reconnection loop'
    ).toBeLessThan(20);
  });
});

test.describe('Smoke Tests - Navigation', () => {
  test('can navigate between main sections', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Page should have loaded
    await expect(page.locator('body')).toBeVisible();

    // Try to find and click navigation elements
    // Note: Actual navigation depends on app structure
    const nav = createNavigation(page);

    // If pipelines link exists, it should be clickable
    if (await nav.pipelinesLink.isVisible()) {
      await nav.pipelinesLink.click();
      await page.waitForTimeout(500);
      // URL should have changed (hash-based routing)
      expect(page.url()).toContain('pipeline');
    }
  });
});

test.describe('Smoke Tests - Basic Rendering', () => {
  test('no blank page on initial load', async ({ page }) => {
    await page.goto('/');

    // Give time for Svelte to hydrate
    await page.waitForTimeout(1000);

    // Check for any visible content
    const visibleElements = await page.locator('body > *').count();
    expect(visibleElements).toBeGreaterThan(0);
  });

  test('page title is set', async ({ page }) => {
    await page.goto('/');
    const title = await page.title();
    // Should have some title (not empty)
    expect(title.length).toBeGreaterThan(0);
  });

  test('no uncaught errors in page', async ({ page }) => {
    const pageErrors: Error[] = [];

    page.on('pageerror', error => {
      pageErrors.push(error);
    });

    await page.goto('/');
    await page.waitForTimeout(2000);

    // Filter out network-related errors (expected without backend)
    const realErrors = pageErrors.filter(
      err =>
        !err.message.includes('fetch') &&
        !err.message.includes('WebSocket') &&
        !err.message.includes('ERR_CONNECTION')
    );

    expect(realErrors).toHaveLength(0);
  });
});
