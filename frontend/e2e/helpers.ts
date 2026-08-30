/**
 * Shared configuration and test-mode API helpers for the Playwright e2e suite.
 *
 * URLs come from env vars so the same specs run against any stack. Defaults
 * match the docker-compose "e2e" profile (backend-e2e on :8765) plus the vite
 * dev server that playwright.config.ts starts on :5174. scripts/test.ps1 and
 * scripts/test.sh export the same values explicitly.
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8765';
export const FRONTEND_URL = process.env.FRONTEND_URL || 'http://localhost:5174';

export interface SeedResponse {
  success: boolean;
  repo: { id: string; name: string; default_branch: string; git_initialized: boolean };
  pipeline: { id: string; name: string };
  cards: { id: string; title: string; status: string }[];
}

/**
 * Wipe DB tables and in-memory singletons via the env-gated test-mode API.
 * Fails loudly (never skips) when the backend was started without
 * LAZYAF_TEST_MODE=true - a silent skip here would un-trust the whole lane.
 */
export async function resetBackend(request: APIRequestContext): Promise<void> {
  const response = await request.post(`${BACKEND_URL}/api/test/reset`);
  if (!response.ok()) {
    throw new Error(
      `POST /api/test/reset failed (${response.status()}): ${await response.text()}\n` +
      `The e2e backend must run with LAZYAF_TEST_MODE=true - start it with:\n` +
      `  docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml --profile e2e up -d backend-e2e runner-agent-e2e`
    );
  }
}

/** Create the deterministic seed fixtures (ingested repo + pipeline + cards). */
export async function seedBackend(request: APIRequestContext): Promise<SeedResponse> {
  const response = await request.post(`${BACKEND_URL}/api/test/seed`);
  if (!response.ok()) {
    throw new Error(`POST /api/test/seed failed (${response.status()}): ${await response.text()}`);
  }
  return response.json();
}

/**
 * Create a test repo via API (faster than UI for setup). The name embeds a
 * timestamp so parallel-in-time runs against a shared backend never collide.
 */
export async function createTestRepo(
  page: Page,
  prefix = 'e2e-test'
): Promise<{ id: string; name: string }> {
  const name = `${prefix}-${Date.now()}`;
  const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
    data: { name, default_branch: 'main' },
  });
  expect(response.ok()).toBeTruthy();
  const repo = await response.json();
  return { id: repo.id, name };
}

/** Select a repo in the board sidebar and wait for its board to load. */
export async function selectRepo(page: Page, repoName: string) {
  // Wait for repo list to load
  await page.waitForSelector('.repo-item', { timeout: 5000 });

  // Find and click the specific repo
  const repoItem = page.locator('.repo-item').filter({ hasText: repoName });
  await expect(repoItem).toBeVisible({ timeout: 5000 });
  await repoItem.click();

  // Wait for board to show the selected repo
  await expect(page.locator('.board-header h1')).toContainText(repoName, { timeout: 5000 });
}

/** Navigate to the pipelines page and select a repo by name. */
export async function goToPipelinesPage(page: Page, repoName: string) {
  await page.goto('/#/pipelines');

  // Wait for repo list to load
  await expect(page.locator('.repo-list')).toBeVisible({ timeout: 5000 });

  // Find and click the repo - it may be scrolled out of view
  const repoItem = page.locator('.repo-item').filter({ hasText: repoName });
  await expect(repoItem).toBeVisible({ timeout: 5000 });
  await repoItem.scrollIntoViewIfNeeded();
  await repoItem.click();

  // Wait for the "New Pipeline" button to appear (indicates repo is selected)
  await expect(page.locator('button:has-text("New Pipeline")')).toBeVisible({ timeout: 3000 });
}
