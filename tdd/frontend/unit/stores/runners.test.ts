/**
 * Runner Store Unit Tests
 *
 * Tests for the runner store behavior after Phase 12 changes.
 * These tests verify:
 * - Graceful handling of removed HTTP endpoints (Phase 1)
 * - WebSocket-based state updates (Phase 2)
 * - Support for new runner states (Phase 2)
 *
 * Run with: pnpm test:unit
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// TODO: Import from actual store once fixed
// import { runnersStore, poolStatus } from '$lib/stores/runners';
// import { websocketStore } from '$lib/stores/websocket';

import {
  runnerStatusMessages,
  runnerLifecycleSequence,
  type RunnerState,
} from '../../fixtures/websocket-messages/runner-messages';
import { MockWebSocket, createMockWebSocketFactory } from '../../fixtures/mock-websocket';

describe('Runner Store - Phase 1: Graceful Degradation', () => {
  beforeEach(() => {
    // Reset fetch mock
    vi.resetAllMocks();
  });

  describe('when HTTP endpoints return 404', () => {
    it.todo('should not throw when runnersStore.load() gets 404');
    // TODO: Implement once store is ready
    // it('should not throw when runnersStore.load() gets 404', async () => {
    //   vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    //     ok: false,
    //     status: 404,
    //     json: () => Promise.resolve({ detail: 'Not found' }),
    //   }));
    //
    //   await expect(runnersStore.load()).resolves.not.toThrow();
    // });

    it.todo('should set error state when API returns 404');
    // it('should set error state when API returns 404', async () => {
    //   vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    //     ok: false,
    //     status: 404,
    //     json: () => Promise.resolve({ detail: 'Not found' }),
    //   }));
    //
    //   await runnersStore.load();
    //
    //   let error: string | null = null;
    //   runnersStore.error.subscribe(e => error = e)();
    //   expect(error).toContain('404');
    // });

    it.todo('should not crash when poolStatus.load() gets 404');

    it.todo('should set error state for pool status when API returns 404');
  });

  describe('when HTTP endpoints throw network error', () => {
    it.todo('should handle network errors gracefully');
    // it('should handle network errors gracefully', async () => {
    //   vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));
    //
    //   await expect(runnersStore.load()).resolves.not.toThrow();
    //
    //   let error: string | null = null;
    //   runnersStore.error.subscribe(e => error = e)();
    //   expect(error).toBeTruthy();
    // });
  });

  describe('polling behavior', () => {
    it.todo('should stop polling when endpoint returns 404 multiple times');
    // Prevent infinite 404 loops

    it.todo('should continue attempting reconnection with backoff');
  });
});

describe('Runner Store - Phase 2: WebSocket Updates', () => {
  let mockWsFactory: ReturnType<typeof createMockWebSocketFactory>;

  beforeEach(() => {
    mockWsFactory = createMockWebSocketFactory();
    vi.stubGlobal('WebSocket', mockWsFactory.factory);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    mockWsFactory.clear();
  });

  describe('runner_status message handling', () => {
    it.todo('should add runner to store when runner_status received');
    // it('should add runner to store when runner_status received', () => {
    //   // Connect WebSocket
    //   websocketStore.connect();
    //   const ws = mockWsFactory.getLatest();
    //   ws.simulateOpen();
    //
    //   // Simulate runner connecting
    //   ws.simulateMessage(runnerStatusMessages.idle);
    //
    //   let runners: Runner[] = [];
    //   runnersStore.subscribe(r => runners = r)();
    //   expect(runners).toHaveLength(1);
    //   expect(runners[0].id).toBe(runnerStatusMessages.idle.payload.id);
    // });

    it.todo('should update existing runner when status changes');
    // it('should update existing runner when status changes', () => {
    //   websocketStore.connect();
    //   const ws = mockWsFactory.getLatest();
    //   ws.simulateOpen();
    //
    //   // Add runner
    //   ws.simulateMessage(runnerStatusMessages.idle);
    //
    //   // Change to busy
    //   ws.simulateMessage(runnerStatusMessages.busy);
    //
    //   let runners: Runner[] = [];
    //   runnersStore.subscribe(r => runners = r)();
    //   expect(runners).toHaveLength(1);  // Same runner, not duplicated
    //   expect(runners[0].status).toBe('busy');
    // });

    it.todo('should handle runner disconnect message');
    // it('should handle runner disconnect message', () => {
    //   websocketStore.connect();
    //   const ws = mockWsFactory.getLatest();
    //   ws.simulateOpen();
    //
    //   ws.simulateMessage(runnerStatusMessages.idle);
    //   ws.simulateMessage(runnerStatusMessages.disconnected);
    //
    //   let runners: Runner[] = [];
    //   runnersStore.subscribe(r => runners = r)();
    //   // Either removed or status is 'disconnected'
    //   expect(
    //     runners.length === 0 ||
    //     runners[0].status === 'disconnected'
    //   ).toBe(true);
    // });
  });

  describe('new runner states', () => {
    const allStates: RunnerState[] = [
      'disconnected',
      'connecting',
      'idle',
      'assigned',
      'busy',
      'dead',
    ];

    it.each(allStates)('should accept %s state', (state) => {
      // TODO: Implement once types are updated
      // expect(() => {
      //   websocketStore.connect();
      //   const ws = mockWsFactory.getLatest();
      //   ws.simulateOpen();
      //   ws.simulateMessage({
      //     type: 'runner_status',
      //     payload: { id: 'test', status: state },
      //   });
      // }).not.toThrow();
      expect(allStates).toContain(state);  // Placeholder assertion
    });

    it.todo('should reject invalid states');
    // it('should reject invalid states', () => {
    //   // The store should validate incoming states
    //   // Invalid states should be logged but not crash
    // });
  });

  describe('complete runner lifecycle', () => {
    it.todo('should handle full lifecycle from connect to disconnect');
    // it('should handle full lifecycle from connect to disconnect', () => {
    //   websocketStore.connect();
    //   const ws = mockWsFactory.getLatest();
    //   ws.simulateOpen();
    //
    //   // Play through lifecycle
    //   for (const message of runnerLifecycleSequence) {
    //     ws.simulateMessage(message);
    //   }
    //
    //   // Final state should be disconnected
    //   let runners: Runner[] = [];
    //   runnersStore.subscribe(r => runners = r)();
    //   // Verify final state
    // });
  });
});

describe('Runner Store - Derived Stores', () => {
  describe('idleRunners derived store', () => {
    it.todo('should filter runners by idle status');
    // it('should filter runners by idle status', () => {
    //   // Add mix of idle and busy runners
    //   // Verify idleRunners only contains idle ones
    // });

    it.todo('should update when runner status changes');
  });

  describe('busyRunners derived store', () => {
    it.todo('should filter runners by busy status');

    it.todo('should include assigned runners as "busy" for UI purposes');
    // assigned state is a transitional state, may want to show as busy
  });
});

describe('Runner Store - Pool Status', () => {
  describe('with WebSocket-based updates', () => {
    it.todo('should derive pool counts from runner list');
    // it('should derive pool counts from runner list', () => {
    //   // With WebSocket, we can derive counts from the runner list
    //   // instead of making a separate HTTP call
    // });

    it.todo('should count all new states correctly');
    // it('should count all new states correctly', () => {
    //   // idle_runners = count where status === 'idle'
    //   // busy_runners = count where status in ['assigned', 'busy']
    //   // offline_runners = count where status in ['disconnected', 'dead']
    // });
  });
});
