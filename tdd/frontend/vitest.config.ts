/**
 * Vitest Configuration for Frontend Tests
 *
 * Run unit tests: pnpm test:unit
 * Run integration tests: pnpm test:integration
 * Run all with coverage: pnpm test:coverage
 */

import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'path';

export default defineConfig({
  plugins: [svelte({ hot: false })],

  resolve: {
    alias: {
      // Match SvelteKit's path aliases
      '$lib': resolve(__dirname, '../../frontend/src/lib'),
      '$app': resolve(__dirname, './mocks/app'),  // Mock SvelteKit app modules
    },
  },

  test: {
    // Test environment
    environment: 'jsdom',

    // Include patterns
    include: [
      'unit/**/*.test.ts',
      'integration/**/*.test.ts',
    ],

    // Exclude E2E (those use Playwright)
    exclude: [
      'e2e/**',
      '**/node_modules/**',
    ],

    // Global setup
    globals: true,
    setupFiles: ['./setup.ts'],

    // Coverage configuration
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      reportsDirectory: './coverage',
      include: ['../../frontend/src/**/*.ts', '../../frontend/src/**/*.svelte'],
      exclude: [
        '**/*.test.ts',
        '**/*.spec.ts',
        '**/types.ts',  // Type-only files
      ],
    },

    // Test isolation
    isolate: true,
    poolOptions: {
      threads: {
        singleThread: false,
      },
    },

    // Timeouts
    testTimeout: 10000,
    hookTimeout: 10000,

    // Reporter
    reporters: ['verbose'],
  },
});
