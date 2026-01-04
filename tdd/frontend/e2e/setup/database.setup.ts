/**
 * Database Setup for Real Tier Tests
 *
 * This setup file runs before real tier tests to:
 * - Verify backend connectivity
 * - Reset the database
 * - Verify test mode is enabled
 */

import { test as setup, expect } from '@playwright/test';
import { createTestApi } from '../helpers/api';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

setup('verify backend and reset database', async ({ request }) => {
  // Create test API client
  const testApi = createTestApi(request);

  // Step 1: Verify test API is available
  setup.step('verify test API is available', async () => {
    const available = await testApi.isAvailable();
    expect(
      available,
      `Test API not available at ${BACKEND_URL}. Ensure backend is running with LAZYAF_TEST_MODE=true`
    ).toBeTruthy();
  });

  // Step 2: Reset database
  setup.step('reset database', async () => {
    const result = await testApi.reset();
    expect(result.success).toBeTruthy();
    console.log(`Database reset complete. Cleared: ${result.tables_cleared.join(', ')}`);
  });

  // Step 3: Verify empty state
  setup.step('verify empty state', async () => {
    const state = await testApi.getState();
    expect(state.repos).toBe(0);
    expect(state.cards).toBe(0);
    expect(state.pipelines).toBe(0);
    console.log('Database is in clean state');
  });
});
