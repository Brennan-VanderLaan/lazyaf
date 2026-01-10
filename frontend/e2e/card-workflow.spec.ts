/**
 * E2E Tests: Card Workflow (User Story 1)
 *
 * Tests the full user workflow through the UI:
 * 1. Select/create a repository
 * 2. Create a card with title and description
 * 3. Start work on the card (drag or click)
 * 4. Watch status updates in real-time
 * 5. View diff when complete
 *
 * Prerequisites:
 *   - Backend running: cd backend && uvicorn app.main:app --reload
 *   - Frontend running: cd frontend && npm run dev
 *   - For full tests: Mock runner: docker-compose --profile testing up runner-mock
 */

import { test, expect, type Page } from '@playwright/test';

// Test configuration
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Helper: Create a test repo via API (faster than UI for setup)
async function createTestRepo(page: Page): Promise<string> {
  const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
    data: {
      name: `e2e-test-${Date.now()}`,
      default_branch: 'main',
    },
  });
  expect(response.ok()).toBeTruthy();
  const repo = await response.json();
  return repo.id;
}

// Helper: Wait for WebSocket connection
async function waitForWebSocket(page: Page) {
  // The app connects to WebSocket on load - wait for it to establish
  await page.waitForTimeout(1000);
}

// Helper: Select a repo in the sidebar
async function selectRepo(page: Page, repoName: string) {
  await page.locator('.repo-item').filter({ hasText: repoName }).click();
  await expect(page.locator('.board-header h1')).toContainText(repoName);
}


test.describe('Board Page - Basic UI', () => {
  test('shows "No Repository Selected" when no repo is selected', async ({ page }) => {
    await page.goto('/');

    // Should see the no-repo message
    await expect(page.locator('.no-repo')).toBeVisible();
    await expect(page.locator('.no-repo')).toContainText('No Repository Selected');
  });

  test('repo selector shows list of repositories', async ({ page }) => {
    // Create a repo first
    const repoId = await createTestRepo(page);

    await page.goto('/');
    await page.reload(); // Refresh to load repos

    // Should see repositories section
    await expect(page.locator('.repo-selector h2')).toContainText('Repositories');
  });
});


test.describe('Card Creation - UI Flow', () => {
  let repoId: string;
  let repoName: string;

  test.beforeEach(async ({ page }) => {
    // Create a test repo via API
    repoName = `e2e-card-test-${Date.now()}`;
    const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
      data: { name: repoName, default_branch: 'main' },
    });
    const repo = await response.json();
    repoId = repo.id;
  });

  test('can create a new card via UI', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);

    // Select the repo
    await selectRepo(page, repoName);

    // Click "New Card" button
    await page.click('button.btn-create');

    // Fill in card details in modal
    await expect(page.locator('.modal')).toBeVisible();
    await page.fill('#title', 'Test Feature');
    await page.fill('#description', 'A test feature created via E2E test');

    // Submit the form
    await page.click('button:has-text("Create Card")');

    // Modal should close
    await expect(page.locator('.modal')).not.toBeVisible();

    // Card should appear in "To Do" column
    await expect(page.locator('.column').filter({ hasText: 'To Do' })).toContainText('Test Feature');
  });

  test('can create card with mock runner type', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Open create modal
    await page.click('button.btn-create');
    await expect(page.locator('.modal')).toBeVisible();

    // Fill card details
    await page.fill('#title', 'Mock Runner Test');
    await page.fill('#description', 'Testing with mock runner');

    // Select mock runner type
    await page.selectOption('select', { label: 'Mock (Testing)' });

    // Create the card
    await page.click('button:has-text("Create Card")');

    // Verify card appears
    await expect(page.locator('.column').filter({ hasText: 'To Do' })).toContainText('Mock Runner Test');
  });

  test('card modal shows runner type selector', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    await page.click('button.btn-create');
    await expect(page.locator('.modal')).toBeVisible();

    // Check runner type options are available
    const select = page.locator('select').first();
    await expect(select).toBeVisible();

    // Verify mock option exists
    const options = await select.locator('option').allTextContents();
    expect(options).toContain('Mock (Testing)');
  });
});


test.describe('Card Status Updates - UI Flow', () => {
  let repoId: string;
  let repoName: string;

  test.beforeEach(async ({ page }) => {
    repoName = `e2e-status-test-${Date.now()}`;
    const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
      data: { name: repoName, default_branch: 'main' },
    });
    const repo = await response.json();
    repoId = repo.id;

    // Initialize repo with git data for testing (required for starting cards)
    const setupResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/test-setup`);
    expect(setupResponse.ok(), `test-setup failed: ${await setupResponse.text()}`).toBeTruthy();
  });

  test('starting a card moves it to In Progress column', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Create a card via API for faster setup
    await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
      data: {
        title: 'Start Test Card',
        description: 'Testing start functionality',
        runner_type: 'mock',
      },
    });

    // Refresh to see the card
    await page.reload();
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Click on the card to open modal
    await page.locator('.card').filter({ hasText: 'Start Test Card' }).click();

    // Click Start button in modal
    await page.click('button:has-text("Start Work")');

    // Wait for status update
    await page.waitForTimeout(500);

    // Card should move to "In Progress" column
    // Note: Without mock runner actually running, it stays in_progress
    await expect(
      page.locator('.column').filter({ hasText: 'In Progress' })
    ).toContainText('Start Test Card');
  });

  test('card shows job status when in progress', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Create and start a card via API
    const createResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
      data: {
        title: 'Job Status Test',
        description: 'Testing job status display',
        runner_type: 'mock',
      },
    });
    const card = await createResponse.json();
    await page.request.post(`${BACKEND_URL}/api/cards/${card.id}/start`);

    // Refresh to see updated state
    await page.reload();
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Click on the card
    await page.locator('.card').filter({ hasText: 'Job Status Test' }).click();

    // Should see some job-related info in the modal
    await expect(page.locator('.modal')).toBeVisible();
    // The modal should show the card is in progress
    await expect(page.locator('.modal')).toContainText('In Progress');
  });
});


test.describe('Real-time Updates via WebSocket', () => {
  let repoId: string;
  let repoName: string;

  test.beforeEach(async ({ page }) => {
    repoName = `e2e-ws-test-${Date.now()}`;
    const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
      data: { name: repoName, default_branch: 'main' },
    });
    const repo = await response.json();
    repoId = repo.id;
  });

  test('UI updates when card status changes via API', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Create a card via API
    const createResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
      data: {
        title: 'WebSocket Test Card',
        description: 'Testing WebSocket updates',
        runner_type: 'mock',
      },
    });
    const card = await createResponse.json();

    // Refresh to see the card
    await page.reload();
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Card should be in To Do
    await expect(
      page.locator('.column').filter({ hasText: 'To Do' })
    ).toContainText('WebSocket Test Card');

    // Update card status via API (simulating what runner would do)
    await page.request.patch(`${BACKEND_URL}/api/cards/${card.id}`, {
      data: { status: 'in_review' },
    });

    // Wait for WebSocket update (give it a moment)
    await page.waitForTimeout(1000);

    // Card should now be in "In Review" column (WebSocket pushed the update)
    await expect(
      page.locator('.column').filter({ hasText: 'In Review' })
    ).toContainText('WebSocket Test Card');
  });
});


test.describe('Full E2E with Mock Runner', () => {
  // These tests require the mock runner to be running
  // Skip if SKIP_RUNNER_TESTS is set
  test.skip(!!process.env.SKIP_RUNNER_TESTS, 'Skipping runner tests');

  let repoId: string;
  let repoName: string;

  test.beforeEach(async ({ page }) => {
    repoName = `e2e-runner-test-${Date.now()}`;
    // Need an ingested repo for real execution
    const response = await page.request.post(`${BACKEND_URL}/api/repos`, {
      data: { name: repoName, default_branch: 'main' },
    });
    const repo = await response.json();
    repoId = repo.id;

    // Initialize repo with git data for testing
    const setupResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/test-setup`);
    expect(setupResponse.ok(), `test-setup failed: ${await setupResponse.text()}`).toBeTruthy();
  });

  test('card reaches in_review after mock runner executes', async ({ page }) => {
    await page.goto('/');
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Create card with mock config
    const createResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
      data: {
        title: 'Full E2E Test',
        description: 'Testing complete flow with mock runner',
        runner_type: 'mock',
        step_config: {
          mock_config: {
            response_mode: 'batch',
            delay_ms: 50,
            file_operations: [
              { action: 'create', path: 'test.txt', content: 'E2E test content' }
            ],
            output_events: [
              { type: 'content', text: 'Mock execution...' },
              { type: 'complete', text: 'Done!' }
            ],
            exit_code: 0
          }
        }
      },
    });
    const card = await createResponse.json();

    // Start the card
    await page.request.post(`${BACKEND_URL}/api/cards/${card.id}/start`);

    // Refresh and select repo
    await page.reload();
    await waitForWebSocket(page);
    await selectRepo(page, repoName);

    // Wait for card to reach in_review (mock runner should complete quickly)
    await expect(
      page.locator('.column').filter({ hasText: 'In Review' })
    ).toContainText('Full E2E Test', { timeout: 30000 });
  });
});
