import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E configuration for the LazyAF frontend.
 *
 * This is the REAL-BACKEND tier: every spec talks to a live backend
 * (docker compose "e2e" profile) through a vite dev server. See e2e/README.md
 * for the full startup/teardown flow.
 *
 * URLs are env-var driven; the defaults match the compose e2e profile
 * (backend-e2e on :8765) and a dedicated e2e frontend port (:5174) so this
 * lane never collides with a developer's normal `npm run dev` on :5173
 * proxying to the dev backend on :8000.
 */
const FRONTEND_URL = process.env.FRONTEND_URL || 'http://localhost:5174';
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8765';
const FRONTEND_PORT = new URL(FRONTEND_URL).port || '5174';

export default defineConfig({
  testDir: './e2e',

  // WORKERS MUST STAY AT 1 FOR THIS TIER. All specs share one backend, one
  // SQLite DB, and a global /api/test/reset that also wipes in-memory
  // singletons (standing rule R6). Parallel workers reset state out from
  // under each other - attempt #1 raised workers against a shared-DB global
  // reset an hour before the branch was abandoned (post-mortem landmine 8).
  // If parallelism is ever needed, it requires per-worker namespacing first.
  workers: 1,
  fullyParallel: false,

  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,

  // Real pipeline runs (runner polling + container script execution) take
  // tens of seconds; specs use bounded expect timeouts within this budget.
  timeout: 120_000,

  reporter: [
    ['html', { open: 'never' }],
    ['list'],
  ],

  use: {
    baseURL: FRONTEND_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'on-first-retry',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Startup choice (documented in e2e/README.md):
  //  - Frontend: Playwright OWNS it, exclusively. The vite dev server is
  //    started here on the dedicated e2e port, proxying /api and /ws to
  //    BACKEND_URL. reuseExistingServer is deliberately false and the
  //    command uses --strictPort: a stray process already squatting on the
  //    e2e port (a forgotten dev server, possibly pointed at the WRONG
  //    backend) fails the run LOUDLY instead of being silently reused.
  //    Nothing else may pre-start this server - scripts/test.ps1|test.sh
  //    only manage the compose stack.
  //  - Backend: started EXTERNALLY (docker compose --profile e2e, plus the
  //    frontend/e2e/compose.test-mode.yml override for the test-mode API).
  //    Playwright's webServer has no teardown story for compose stacks, and
  //    owning half a stack's lifecycle from here would hide failures - the
  //    helpers fail loudly with the exact startup command when the backend
  //    is missing or lacks LAZYAF_TEST_MODE.
  webServer: {
    command: `npm run dev -- --port ${FRONTEND_PORT} --strictPort`,
    url: FRONTEND_URL,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { VITE_BACKEND_URL: BACKEND_URL },
  },
});
