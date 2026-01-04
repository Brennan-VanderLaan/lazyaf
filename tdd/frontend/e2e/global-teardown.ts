/**
 * Global Teardown for E2E Tests
 *
 * Runs once after all tests complete.
 * - Cleanup operations
 * - Summary logging
 */

import { FullConfig } from '@playwright/test';

async function globalTeardown(config: FullConfig) {
  console.log('\n=== E2E Test Suite Complete ===\n');

  // Optional: Reset database after all tests
  // This is typically not needed since we reset before tests,
  // but can be enabled for cleanup
  const shouldCleanup = process.env.E2E_CLEANUP_AFTER === 'true';

  if (shouldCleanup) {
    const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

    try {
      const response = await fetch(`${BACKEND_URL}/api/test/reset`, {
        method: 'POST',
      });

      if (response.ok) {
        console.log('Database reset complete (post-test cleanup)');
      }
    } catch (error) {
      // Ignore cleanup errors
    }
  }
}

export default globalTeardown;
