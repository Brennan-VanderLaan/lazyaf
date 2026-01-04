/**
 * Runner Store - Phase 12.6 WebSocket-only Architecture
 *
 * Runners are now managed entirely via WebSocket broadcasts.
 * No HTTP polling - status updates are pushed from the backend.
 */
import { writable, derived } from 'svelte/store';
import type { Runner, RunnerStatus, RunnerStatusUpdate, PoolStatus } from '../api/types';

/**
 * Internal runner map for quick lookups.
 * Runners are indexed by ID for efficient updates.
 */
function createRunnersStore() {
  const { subscribe, set, update } = writable<Map<string, Runner>>(new Map());

  return {
    subscribe,

    /**
     * Update or add a runner from a WebSocket status update.
     * This is the primary way runners enter and update in the store.
     */
    handleStatusUpdate(payload: RunnerStatusUpdate) {
      update((runners) => {
        const existing = runners.get(payload.id);

        if (payload.status === 'disconnected' && !existing) {
          // Disconnect for unknown runner - ignore
          return runners;
        }

        if (existing) {
          // Update existing runner
          runners.set(payload.id, {
            ...existing,
            status: payload.status,
            name: payload.name ?? existing.name,
            runner_type: payload.runner_type ?? existing.runner_type,
          });
        } else {
          // New runner - create with available data
          runners.set(payload.id, {
            id: payload.id,
            name: payload.name ?? null,
            runner_type: payload.runner_type ?? 'unknown',
            status: payload.status,
            labels: null,
            current_step_execution_id: null,
            websocket_id: null,
            last_heartbeat: new Date().toISOString(),
            connected_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
          });
        }

        return new Map(runners);
      });
    },

    /**
     * Remove a runner from the store (e.g., after extended disconnection).
     */
    remove(runnerId: string) {
      update((runners) => {
        runners.delete(runnerId);
        return new Map(runners);
      });
    },

    /**
     * Clear all runners (e.g., on WebSocket disconnect).
     */
    clear() {
      set(new Map());
    },
  };
}

// The main runners store (map)
const runnersMap = createRunnersStore();

// Export handleStatusUpdate for use by websocket.ts
export const handleRunnerStatusUpdate = runnersMap.handleStatusUpdate.bind(runnersMap);
export const clearRunners = runnersMap.clear.bind(runnersMap);

// Derived store: array of runners (for iteration in components)
export const runnersStore = derived(runnersMap, ($map) => Array.from($map.values()));

// Derived store: pool status (computed from runners)
export const poolStatus = derived(runnersStore, ($runners): PoolStatus => {
  const counts: PoolStatus = {
    total_runners: $runners.length,
    idle_runners: 0,
    busy_runners: 0,
    disconnected_runners: 0,
    dead_runners: 0,
  };

  for (const runner of $runners) {
    switch (runner.status) {
      case 'idle':
        counts.idle_runners++;
        break;
      case 'busy':
      case 'assigned':
        counts.busy_runners++;
        break;
      case 'disconnected':
      case 'connecting':
        counts.disconnected_runners++;
        break;
      case 'dead':
        counts.dead_runners++;
        break;
    }
  }

  return counts;
});

// Derived stores for convenience filtering
export const idleRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'idle')
);

export const busyRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'busy' || r.status === 'assigned')
);

export const connectedRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status !== 'disconnected' && r.status !== 'dead')
);
