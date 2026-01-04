/**
 * E2E Tests - Pipeline Execution Visibility
 *
 * Tests for Phase 3: Step execution status should be visible in UI.
 *
 * Run with: pnpm test:e2e
 */

import { test, expect, type Page } from '@playwright/test';

test.describe('Pipeline Execution - Run Viewer', () => {
  // These tests assume you can navigate to a pipeline run viewer

  test.skip('shows pipeline run status', async ({ page }) => {
    // Navigate to a pipeline run (adjust URL)
    // await page.goto('/pipelines/run/test-run-id');

    // Status should be visible
    const statusElement = page.locator('.run-status');
    await expect(statusElement).toBeVisible();
    await expect(statusElement).toContainText(/pending|running|passed|failed|cancelled/i);
  });

  test.skip('shows progress bar', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    const progressBar = page.locator('.progress-bar');
    await expect(progressBar).toBeVisible();

    // Progress fill should have width
    const progressFill = page.locator('.progress-fill');
    const width = await progressFill.evaluate(el =>
      getComputedStyle(el).width
    );
    expect(width).not.toBe('0px');
  });

  test.skip('shows step timeline', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    const stepItems = page.locator('.step-item');
    const count = await stepItems.count();
    expect(count).toBeGreaterThan(0);
  });
});

test.describe('Pipeline Execution - Step Status', () => {
  test.skip('step shows correct status icon', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    // Each step should have a status indicator
    const stepStatus = page.locator('.step-status').first();
    await expect(stepStatus).toBeVisible();

    // Should be one of the valid icons
    const icon = await stepStatus.textContent();
    expect(['...', '*', '✓', '✗', '⊘']).toContain(icon?.trim() || '');
  });

  test.skip('clicking step shows logs', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    // Click first step
    await page.locator('.step-item').first().click();

    // Step details should appear
    const stepDetails = page.locator('.step-details');
    await expect(stepDetails).toBeVisible();
  });

  test.skip('step logs display correctly', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    // Select a step
    await page.locator('.step-item').first().click();

    // Logs area should be present
    const logsElement = page.locator('.logs');
    await expect(logsElement).toBeVisible();
  });

  test.skip('error state shows error badge', async ({ page }) => {
    // Navigate to a failed run
    // await page.goto('/pipelines/run/failed-run-id');

    // Find failed step
    const failedStep = page.locator('.step-item').filter({
      has: page.locator('.step-status', { hasText: /✗|failed/i })
    });

    await failedStep.click();

    // Error badge should appear
    await expect(page.locator('.error-badge')).toBeVisible();
  });
});

test.describe('Pipeline Execution - Real-time Updates', () => {
  test.skip('progress updates while running', async ({ page }) => {
    // Start a pipeline run and watch progress
    // This is a complex test requiring backend coordination

    // await page.goto('/pipelines/run/running-run-id');

    // Get initial progress
    const progressText = page.locator('.progress-text');
    const initial = await progressText.textContent();

    // Wait for update (this needs a running pipeline)
    await page.waitForTimeout(5000);

    // Progress should have changed
    // const updated = await progressText.textContent();
    // expect(updated).not.toBe(initial);
  });

  test.skip('logs stream in real-time', async ({ page }) => {
    // Navigate to running step
    // await page.goto('/pipelines/run/running-run-id');
    // await page.locator('.step-item').first().click();

    // Get initial log length
    const logs = page.locator('.logs');
    const initialText = await logs.textContent();

    // Wait for more logs
    await page.waitForTimeout(3000);

    // Logs should have grown
    // const updatedText = await logs.textContent();
    // expect(updatedText?.length).toBeGreaterThan(initialText?.length || 0);
  });

  test.skip('status transitions are visible', async ({ page }) => {
    // Watch a step go through states:
    // pending -> preparing -> running -> completed

    // This requires coordinating with a pipeline execution
  });
});

test.describe('Pipeline Execution - Actions', () => {
  test.skip('can cancel running pipeline', async ({ page }) => {
    // Navigate to running pipeline
    // await page.goto('/pipelines/run/running-run-id');

    // Find cancel button
    const cancelButton = page.locator('.btn-cancel');
    await expect(cancelButton).toBeVisible();

    // Click should work (don't actually cancel in test)
    // await cancelButton.click();
    // await expect(page.locator('.modal')).toContainText(/confirm|cancel/i);
  });

  test.skip('close button works', async ({ page }) => {
    // await page.goto('/pipelines/run/test-run-id');

    await page.locator('.close-btn').click();

    // Modal should close
    await expect(page.locator('.modal')).not.toBeVisible();
  });

  test.skip('debug rerun available for failed runs', async ({ page }) => {
    // Navigate to failed run
    // await page.goto('/pipelines/run/failed-run-id');

    // Debug button should be visible
    const debugButton = page.locator('.btn-debug');
    await expect(debugButton).toBeVisible();
    await expect(debugButton).toContainText(/debug/i);
  });
});

test.describe('Pipeline Execution - Edge Cases', () => {
  test.skip('handles missing pipeline gracefully', async ({ page }) => {
    await page.goto('/pipelines/run/nonexistent-run-id');

    // Should show error state, not crash
    await expect(page.locator('body')).not.toContainText('undefined');
    // await expect(page.locator('.error')).toBeVisible();
  });

  test.skip('handles empty logs', async ({ page }) => {
    // Some steps may have no logs yet
    // await page.goto('/pipelines/run/test-run-id');
    // await page.locator('.step-item').first().click();

    // Should show "(No logs)" or similar, not crash
    const logsArea = page.locator('.step-details');
    await expect(logsArea).toBeVisible();
  });

  test.skip('handles rapid status updates', async ({ page }) => {
    // If backend sends many updates quickly, UI should remain stable
    // This is more of a stress test
  });
});
