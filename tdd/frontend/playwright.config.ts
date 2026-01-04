/**
 * Playwright Configuration for E2E Tests
 *
 * Two-tier test architecture:
 * 1. MOCKED tier - Fast tests with mocked backend responses
 * 2. REAL tier - Comprehensive tests against real backend (minus AI calls)
 *
 * Run commands:
 *   pnpm test:e2e           - Run all E2E tests
 *   pnpm test:e2e:ui        - Run with Playwright UI
 *   pnpm test:e2e:mocked    - Run only mocked tier (fast)
 *   pnpm test:e2e:real      - Run only real tier (comprehensive)
 *
 * Environment setup for real tier:
 *   LAZYAF_TEST_MODE=true - Enable test API endpoints
 *   LAZYAF_MOCK_AI=true   - Mock AI API calls
 */

import { defineConfig, devices } from '@playwright/test';
import path from 'path';

// Backend URLs
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';
const FRONTEND_URL = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5173';

export default defineConfig({
  // Test directory
  testDir: './e2e',

  // Test file pattern
  testMatch: '**/*.spec.ts',

  // Fail fast on CI
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,

  // Reporter
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e-report' }],
    ...(process.env.CI ? [['junit', { outputFile: './e2e-results/junit.xml' }] as const] : []),
  ],

  // Shared settings for all projects
  use: {
    // Base URL for tests
    baseURL: FRONTEND_URL,

    // Collect trace on failure
    trace: 'on-first-retry',

    // Screenshot on failure
    screenshot: 'only-on-failure',

    // Video on failure
    video: 'on-first-retry',

    // Extra HTTP headers
    extraHTTPHeaders: {
      'Accept': 'application/json',
    },
  },

  // Timeout settings
  timeout: 30000,
  expect: {
    timeout: 5000,
  },

  // Projects - two-tier architecture
  projects: [
    // =========================================================================
    // MOCKED TIER - Fast tests with mocked responses
    // =========================================================================
    {
      name: 'mocked',
      testMatch: ['**/smoke/**/*.spec.ts', '**/mocked/**/*.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
        // Custom storage state for mocked tests
        storageState: undefined,
      },
      // No backend dependency - all responses mocked
    },

    // =========================================================================
    // REAL TIER - Tests against real backend (minus AI)
    // =========================================================================
    {
      name: 'real-setup',
      testMatch: '**/setup/*.setup.ts',
      use: {
        ...devices['Desktop Chrome'],
      },
    },
    {
      name: 'real',
      testMatch: ['**/workflows/**/*.spec.ts', '**/real/**/*.spec.ts'],
      dependencies: ['real-setup'],
      use: {
        ...devices['Desktop Chrome'],
        // Longer timeouts for real backend operations
        actionTimeout: 10000,
        navigationTimeout: 15000,
      },
    },

    // =========================================================================
    // SMOKE TESTS - Run on both tiers (uses mocked project by default)
    // =========================================================================
    // Smoke tests are included in 'mocked' project above
  ],

  // Web server configuration
  webServer: [
    // Frontend dev server
    {
      command: 'cd ../../frontend && pnpm dev',
      url: FRONTEND_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 120000,
    },
    // Backend server (only for real tier tests)
    // Note: Backend should be started separately with test mode enabled:
    //   LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app
    // Uncomment below to auto-start backend:
    // {
    //   command: 'cd ../../backend && LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app --reload',
    //   url: `${BACKEND_URL}/health`,
    //   reuseExistingServer: !process.env.CI,
    //   timeout: 30000,
    // },
  ],

  // Output directory for test artifacts
  outputDir: './e2e-results',

  // Global setup/teardown
  globalSetup: path.join(__dirname, 'e2e/global-setup.ts'),
  globalTeardown: path.join(__dirname, 'e2e/global-teardown.ts'),
});

/**
 * Test tier expectations:
 *
 * MOCKED TIER (< 60 seconds):
 * - All API calls intercepted and mocked
 * - WebSocket connections simulated
 * - Fast execution, no backend required
 * - Good for UI component testing
 *
 * REAL TIER (minutes):
 * - Real backend with LAZYAF_TEST_MODE=true
 * - Database reset before each test file
 * - Real API calls, real database operations
 * - AI calls mocked via LAZYAF_MOCK_AI=true
 * - WebSocket events from real backend
 * - Good for integration/workflow testing
 */
