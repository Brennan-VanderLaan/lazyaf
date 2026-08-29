import { defineConfig } from 'vitest/config'

// Unit tests cover pure store/helper logic only — no component mounting,
// so no svelte plugin and a plain node environment.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
