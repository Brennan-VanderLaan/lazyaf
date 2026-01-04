/**
 * Playwright Configuration for E2E Tests
 *
 * ============================================================================
 * QUICK START - Run from frontend/ directory
 * ============================================================================
 *
 * By Project (test category):
 *   pnpm test:e2e:mocked     - Smoke tests with mocked backend (fast)
 *   pnpm test:e2e:stories    - All customer story tests
 *   pnpm test:e2e:critical   - Critical failure handling (P0)
 *   pnpm test:e2e:ui-tests   - UI completeness tests
 *   pnpm test:e2e:realtime   - Real-time sync tests
 *   pnpm test:e2e:real       - Full integration (needs backend)
 *   pnpm test:e2e:all        - Everything
 *
 * By Feature (grep patterns):
 *   pnpm test:e2e:p0         - All P0 tests (cards + critical failures)
 *   pnpm test:e2e:cards      - Card lifecycle tests
 *   pnpm test:e2e:pipeline   - Pipeline tests
 *   pnpm test:e2e:runner     - Runner visibility tests
 *   pnpm test:e2e:debug-rerun - Debug re-run tests
 *   pnpm test:e2e:playground - Agent playground tests
 *
 * Interactive modes:
 *   pnpm test:e2e:ui         - Playwright UI (best for development)
 *   pnpm test:e2e:headed     - See browser while running
 *   pnpm test:e2e:debug      - Step-through debugging
 *   pnpm test:e2e:report     - View last test report
 *
 * Run specific test file:
 *   pnpm test:e2e --grep "Create Card"
 *   pnpm test:e2e ../tdd/frontend/e2e/stories/02-card-lifecycle/create-card.spec.ts
 *
 * ============================================================================
 * ENVIRONMENT SETUP
 * ============================================================================
 *
 * For mocked tier (default):
 *   Just run the commands above - no backend needed
 *
 * For real tier:
 *   1. Start backend: cd backend && LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app
 *   2. Run tests: pnpm test:e2e:real
 *
 * ============================================================================
 */

import { defineConfig, devices } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';

// ESM-compatible __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Backend URLs
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';
const FRONTEND_URL = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5173';

export default defineConfig({
  // Test directory
  testDir: './e2e',

  // Test file pattern
  testMatch: '**/*.spec.ts',

  // Parallel execution settings
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // Workers: use PLAYWRIGHT_WORKERS env var, or 50% of CPUs in CI, or auto locally
  workers: process.env.PLAYWRIGHT_WORKERS
    ? parseInt(process.env.PLAYWRIGHT_WORKERS)
    : process.env.CI ? '50%' : undefined,

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
        storageState: undefined,
      },
    },

    // =========================================================================
    // STORIES - Customer journey tests (can run mocked or real)
    // =========================================================================
    {
      name: 'stories',
      testMatch: ['**/stories/**/*.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
        actionTimeout: 15000,
        navigationTimeout: 20000,
      },
    },

    // =========================================================================
    // CRITICAL FAILURES - Error handling tests (P0)
    // =========================================================================
    {
      name: 'critical',
      testMatch: ['**/critical-failures/**/*.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
        actionTimeout: 10000,
      },
    },

    // =========================================================================
    // UI COMPLETENESS - Quality assurance tests
    // =========================================================================
    {
      name: 'ui',
      testMatch: ['**/ui-completeness/**/*.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
      },
    },

    // =========================================================================
    // REALTIME SYNC - Multi-user broadcast tests
    // =========================================================================
    {
      name: 'realtime',
      testMatch: ['**/realtime-sync/**/*.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
        actionTimeout: 5000, // Real-time should be fast
      },
    },

    // =========================================================================
    // REAL TIER - Full integration tests against real backend
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
        actionTimeout: 10000,
        navigationTimeout: 15000,
      },
    },

    // =========================================================================
    // ALL - Run everything (for CI)
    // =========================================================================
    {
      name: 'all',
      testMatch: ['**/*.spec.ts'],
      testIgnore: ['**/setup/*.setup.ts'],
      use: {
        ...devices['Desktop Chrome'],
        actionTimeout: 15000,
        navigationTimeout: 20000,
      },
    },
  ],

  // Web server configuration
  webServer: [
    // Frontend dev server (path relative to tdd/frontend)
    {
      command: 'pnpm --dir ../../frontend dev',
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
