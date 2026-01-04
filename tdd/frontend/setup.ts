/**
 * Vitest Global Setup
 *
 * This file runs before each test file.
 * Use it to set up global mocks and test utilities.
 */

import { vi, beforeEach, afterEach } from 'vitest';

// ============================================================================
// Global Mocks
// ============================================================================

/**
 * Mock fetch by default (tests should override as needed)
 */
beforeEach(() => {
  // Default fetch mock returns empty success
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(''),
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/**
 * Mock window.location
 */
Object.defineProperty(window, 'location', {
  value: {
    protocol: 'http:',
    host: 'localhost:5173',
    hostname: 'localhost',
    port: '5173',
    pathname: '/',
    search: '',
    hash: '',
    href: 'http://localhost:5173/',
  },
  writable: true,
});

/**
 * Mock localStorage
 */
const localStorageMock = {
  store: {} as Record<string, string>,
  getItem: vi.fn((key: string) => localStorageMock.store[key] || null),
  setItem: vi.fn((key: string, value: string) => {
    localStorageMock.store[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete localStorageMock.store[key];
  }),
  clear: vi.fn(() => {
    localStorageMock.store = {};
  }),
};

Object.defineProperty(window, 'localStorage', {
  value: localStorageMock,
});

/**
 * Mock matchMedia (for responsive components)
 */
Object.defineProperty(window, 'matchMedia', {
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

/**
 * Mock IntersectionObserver
 */
class MockIntersectionObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

Object.defineProperty(window, 'IntersectionObserver', {
  value: MockIntersectionObserver,
});

/**
 * Mock ResizeObserver
 */
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

Object.defineProperty(window, 'ResizeObserver', {
  value: MockResizeObserver,
});

// ============================================================================
// Custom Matchers
// ============================================================================

// Add custom matchers if needed
// expect.extend({
//   toHaveBeenCalledWithMessage(received, expected) {
//     // Custom matcher implementation
//   },
// });

// ============================================================================
// Test Utilities
// ============================================================================

/**
 * Helper to wait for all pending promises
 */
export async function flushPromises(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0));
}

/**
 * Helper to wait for Svelte reactivity to settle
 */
export async function tick(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0));
}

/**
 * Helper to create a mock WebSocket that auto-connects
 */
export function createConnectedMockWebSocket() {
  const { MockWebSocket } = require('./fixtures/mock-websocket');
  const ws = new MockWebSocket('ws://localhost:8000/ws');
  ws.simulateOpen();
  return ws;
}
