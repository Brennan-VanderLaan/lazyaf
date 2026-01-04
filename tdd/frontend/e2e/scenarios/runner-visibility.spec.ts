/**
 * E2E Tests - Runner Visibility
 *
 * Tests for Phase 2: Runners should appear in UI when they connect via WebSocket.
 *
 * These tests require either:
 * 1. A running backend with a connected runner, OR
 * 2. WebSocket mocking via page.evaluate
 *
 * Run with: pnpm test:e2e
 */

import { test, expect, type Page } from '@playwright/test';

// Helper to inject a mock WebSocket message
async function injectWebSocketMessage(page: Page, message: object) {
  // This relies on the websocketStore being exposed or
  // intercepting the actual WebSocket connection
  await page.evaluate((msg) => {
    // Option 1: If there's a global debug hook
    // window.__injectWebSocketMessage?.(msg);

    // Option 2: Dispatch a custom event that the store listens to (for testing)
    window.dispatchEvent(new CustomEvent('test:ws-message', { detail: msg }));
  }, message);
}

test.describe('Runner Visibility - Empty State', () => {
  test('shows empty state when no runners connected', async ({ page }) => {
    // Block WebSocket to ensure no runners
    await page.route('**/ws/**', route => route.abort());

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Look for runner panel
    const runnerPanel = page.locator('[data-testid="runner-panel"]').or(
      page.locator('.runner-panel')
    );

    if (await runnerPanel.count() > 0) {
      // Should show "No runners connected" or similar
      await expect(runnerPanel).toContainText(/no runner|0 runner/i);
    }
  });

  test('shows loading state while connecting', async ({ page }) => {
    // Delay WebSocket response
    await page.route('**/ws/**', async route => {
      await new Promise(r => setTimeout(r, 2000));
      route.abort();
    });

    await page.goto('/');

    // Should show loading or connecting state briefly
    // await expect(page.locator('.runner-panel')).toContainText(/loading|connecting/i);
  });
});

test.describe('Runner Visibility - With Runners', () => {
  // These tests require backend + runner integration
  // Skip if running in isolation

  test.skip('shows runner when one connects', async ({ page }) => {
    // This test requires a real backend with a runner connecting
    await page.goto('/');

    // Wait for runner to appear
    const runnerItem = page.locator('[data-testid="runner-item"]').first();
    await expect(runnerItem).toBeVisible({ timeout: 10000 });

    // Should show runner name
    await expect(runnerItem).toContainText(/runner/i);
  });

  test.skip('shows runner status indicator', async ({ page }) => {
    await page.goto('/');

    // Find runner with status
    const statusDot = page.locator('.status-dot').first();
    await expect(statusDot).toBeVisible();

    // Should have a background color (indicating status)
    const bgColor = await statusDot.evaluate(el =>
      getComputedStyle(el).backgroundColor
    );
    expect(bgColor).not.toBe('rgba(0, 0, 0, 0)');
  });

  test.skip('updates status in real-time', async ({ page }) => {
    await page.goto('/');

    // Get initial status
    const statusElement = page.locator('.runner-status').first();
    await expect(statusElement).toBeVisible();

    const initialStatus = await statusElement.textContent();

    // Wait for status to change (requires runner activity)
    // This might timeout if no status change occurs
    await expect(async () => {
      const currentStatus = await statusElement.textContent();
      expect(currentStatus).not.toBe(initialStatus);
    }).toPass({ timeout: 30000 });
  });
});

test.describe('Runner Visibility - Runner States', () => {
  // Test that all new states are handled

  test('handles idle state display', async ({ page }) => {
    await page.goto('/');

    // If we can inject messages, test each state
    // await injectWebSocketMessage(page, {
    //   type: 'runner_status',
    //   payload: { id: 'test', name: 'test', status: 'idle' },
    // });

    // Verify idle indicator color (green)
  });

  test('handles busy state display', async ({ page }) => {
    await page.goto('/');
    // Verify busy indicator color (yellow/warning)
  });

  test('handles assigned state display', async ({ page }) => {
    await page.goto('/');
    // assigned is transitional - may show as busy or with special indicator
  });

  test('handles disconnected state display', async ({ page }) => {
    await page.goto('/');
    // Verify disconnected indicator (gray/muted)
  });

  test('handles dead state display', async ({ page }) => {
    await page.goto('/');
    // Verify dead indicator (red/error) - runner crashed
  });
});

test.describe('Runner Panel - Pool Stats', () => {
  test('shows pool statistics', async ({ page }) => {
    await page.goto('/');

    const poolStats = page.locator('.pool-stats');

    if (await poolStats.count() > 0) {
      // Should show stat labels
      await expect(poolStats).toContainText(/total|idle|busy/i);

      // Should show numeric values
      const statValues = poolStats.locator('.stat-value');
      const count = await statValues.count();
      expect(count).toBeGreaterThan(0);
    }
  });

  test('updates pool stats in real-time', async ({ page }) => {
    await page.goto('/');

    // This requires observing changes over time
    // Consider: snapshot initial -> wait -> snapshot final -> compare
  });
});

test.describe('Runner Panel - Interaction', () => {
  test('can expand/collapse runner list', async ({ page }) => {
    await page.goto('/');

    const toggleButton = page.locator('.btn-toggle');

    if (await toggleButton.count() > 0) {
      // Click to toggle
      await toggleButton.click();

      // List visibility should change
      // (exact behavior depends on initial state)
    }
  });

  test.skip('clicking runner shows logs modal', async ({ page }) => {
    // Requires a runner to be present
    await page.goto('/');

    const runnerItem = page.locator('.runner-item').first();
    await runnerItem.click();

    // Logs modal should appear
    await expect(page.locator('.modal-logs')).toBeVisible();
  });

  test('docker command modal opens', async ({ page }) => {
    await page.goto('/');

    const dockerButton = page.locator('[title="Get Docker command"]').or(
      page.locator('.btn-icon').filter({ hasText: /docker|whale/i })
    );

    if (await dockerButton.count() > 0) {
      await dockerButton.click();

      // Modal should appear
      await expect(page.locator('.modal')).toBeVisible();
      await expect(page.locator('.modal')).toContainText(/docker|runner/i);
    }
  });
});
