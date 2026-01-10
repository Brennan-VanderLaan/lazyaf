import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E Test Configuration for LazyAF Frontend
 *
 * These tests verify the full user workflow through the actual UI:
 * - Browser interactions with Svelte components
 * - Real-time updates via WebSocket
 * - Integration with backend API
 *
 * Prerequisites:
 *   1. Backend running: cd backend && uvicorn app.main:app --reload
 *   2. Frontend running: cd frontend && npm run dev
 *   3. Mock runner (for full tests): docker-compose --profile testing up runner-mock
 */
export default defineConfig({
  testDir: './e2e',

  // Run tests in parallel
  fullyParallel: true,

  // Fail the build on CI if you accidentally left test.only in the source code
  forbidOnly: !!process.env.CI,

  // Retry on CI only
  retries: process.env.CI ? 2 : 0,

  // Opt out of parallel tests on CI
  workers: process.env.CI ? 1 : undefined,

  // Reporter to use
  reporter: [
    ['html', { open: 'never' }],
    ['list'],
  ],

  // Shared settings for all projects
  use: {
    // Base URL for the frontend
    baseURL: process.env.FRONTEND_URL || 'http://localhost:5173',

    // Collect trace when retrying the failed test
    trace: 'on-first-retry',

    // Screenshot on failure
    screenshot: 'only-on-failure',

    // Video on failure
    video: 'on-first-retry',
  },

  // Configure projects for major browsers
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Run your local dev server before starting the tests (optional)
  // Uncomment if you want Playwright to start the servers automatically
  // webServer: [
  //   {
  //     command: 'cd ../backend && uvicorn app.main:app --port 8000',
  //     url: 'http://localhost:8000/health',
  //     reuseExistingServer: !process.env.CI,
  //     timeout: 30000,
  //   },
  //   {
  //     command: 'npm run dev',
  //     url: 'http://localhost:5173',
  //     reuseExistingServer: !process.env.CI,
  //     timeout: 30000,
  //   },
  // ],
});
