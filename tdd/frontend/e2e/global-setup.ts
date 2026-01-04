/**
 * Global Setup for E2E Tests
 *
 * Runs once before all tests.
 * - Verifies backend is accessible (for real tier)
 * - Logs configuration info
 */

import { FullConfig } from '@playwright/test';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

async function globalSetup(config: FullConfig) {
  console.log('\n=== E2E Test Suite Starting ===\n');
  console.log(`BACKEND_URL env: ${process.env.BACKEND_URL}`);
  console.log(`Using BACKEND_URL: ${BACKEND_URL}`);

  // Check if we're running real tier tests
  const runningRealTier = config.projects.some(
    p => p.name === 'real' || p.name === 'real-setup'
  );

  if (runningRealTier) {
    console.log('Real tier detected - checking backend...');

    try {
      const response = await fetch(`${BACKEND_URL}/health`);
      const health = await response.json();
      console.log(`Backend health: ${JSON.stringify(health)}`);

      // Check if test mode is enabled
      const testHealthResponse = await fetch(`${BACKEND_URL}/api/test/health`);
      if (testHealthResponse.ok) {
        const testHealth = await testHealthResponse.json();
        console.log(`Test mode enabled: ${testHealth.test_mode}`);
        console.log(`Mock AI enabled: ${testHealth.mock_ai}`);
      } else {
        console.warn(
          'WARNING: Test API not available. Ensure backend is running with LAZYAF_TEST_MODE=true'
        );
      }
    } catch (error) {
      console.warn(`WARNING: Backend not accessible at ${BACKEND_URL}`);
      console.warn('Real tier tests may fail. Start backend with:');
      console.warn('  LAZYAF_TEST_MODE=true LAZYAF_MOCK_AI=true uvicorn app.main:app');
    }
  } else {
    console.log('Running mocked tier only - backend not required');
  }

  console.log('\n');
}

export default globalSetup;
