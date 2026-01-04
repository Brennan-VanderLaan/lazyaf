/**
 * E2E Tests - Runner Pool Status
 *
 * Story: As a user, I need to see the overall health of the runner pool
 * to know if work can be processed.
 *
 * Priority: P1 - Operational visibility
 *
 * Run with: pnpm test:e2e --grep "Pool Status"
 */

import { test, expect } from '@playwright/test';

test.describe('Runner Pool Status', () => {
  test.describe('Pool Stats Display', () => {
    test('runner panel shows pool statistics', async ({ page }) => {
      await page.goto('/');

      const poolStats = page.locator('.pool-stats');
      await expect(poolStats).toBeVisible();
    });

    test('shows connected runner count', async ({ page }) => {
      await page.goto('/');

      // Assert: "Connected" stat visible
      const connectedStat = page.locator('.stat').filter({ hasText: /connected/i });
      await expect(connectedStat).toBeVisible();
    });

    test('shows ready/idle runner count', async ({ page }) => {
      await page.goto('/');

      // Assert: "Ready" or "Idle" stat visible
      const readyStat = page.locator('.stat').filter({ hasText: /ready|idle/i });
      await expect(readyStat).toBeVisible();
    });

    test('shows busy runner count', async ({ page }) => {
      await page.goto('/');

      // Assert: "Busy" stat visible
      const busyStat = page.locator('.stat').filter({ hasText: /busy/i });
      await expect(busyStat).toBeVisible();
    });
  });

  test.describe('Stats Accuracy', () => {
    test.skip('connected count matches actual connected runners', async ({ page }) => {
      // With 3 runners connected
      // Assert: connected shows 3
    });

    test.skip('ready count matches idle runners', async ({ page }) => {
      // With 2 idle runners
      // Assert: ready shows 2
    });

    test.skip('busy count matches executing runners', async ({ page }) => {
      // With 1 runner executing
      // Assert: busy shows 1
    });

    test.skip('stats sum correctly', async ({ page }) => {
      // ready + busy <= connected
      // (some may be in transitional states)
    });
  });

  test.describe('Real-Time Updates', () => {
    test.skip('stats update when runner connects', async ({ page }) => {
      // Initial: 2 connected
      // Runner connects via WebSocket
      // Assert: connected becomes 3
    });

    test.skip('stats update when runner disconnects', async ({ page }) => {
      // Initial: 3 connected
      // Runner disconnects
      // Assert: connected becomes 2
    });

    test.skip('ready/busy update during execution', async ({ page }) => {
      // Runner goes from idle to busy
      // Assert: ready decreases, busy increases
    });
  });

  test.describe('Zero Runners State', () => {
    test('shows zero when no runners connected', async ({ page }) => {
      // Block WebSocket to ensure no runners
      await page.route('**/ws/**', route => route.abort());
      await page.goto('/');

      // Assert: connected shows 0
      const connectedValue = page.locator('.stat-value').first();
      await expect(connectedValue).toHaveText('0');
    });
  });
});
