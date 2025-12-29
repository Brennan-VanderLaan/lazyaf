import { writable, derived } from 'svelte/store';
import type { Runner, PoolStatus } from '../api/types';
import { runners as runnersApi } from '../api/client';

// Pool status store
function createPoolStatusStore() {
  const { subscribe, set } = writable<PoolStatus | null>(null);
  const loading = writable(false);
  const error = writable<string | null>(null);

  let pollInterval: ReturnType<typeof setInterval> | null = null;

  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const data = await runnersApi.status();
      set(data);
    } catch (e) {
      error.set(e instanceof Error ? e.message : 'Failed to load pool status');
    } finally {
      loading.set(false);
    }
  }

  function startPolling(intervalMs: number = 2000) {
    if (pollInterval) return;
    load(); // Initial load
    pollInterval = setInterval(load, intervalMs);
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },
    load,
    startPolling,
    stopPolling,
  };
}

export const poolStatus = createPoolStatusStore();

// Individual runners store
function createRunnersStore() {
  const { subscribe, set } = writable<Runner[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);

  let pollInterval: ReturnType<typeof setInterval> | null = null;

  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const data = await runnersApi.list();
      set(data);
    } catch (e) {
      error.set(e instanceof Error ? e.message : 'Failed to load runners');
    } finally {
      loading.set(false);
    }
  }

  function startPolling(intervalMs: number = 2000) {
    if (pollInterval) return;
    load(); // Initial load
    pollInterval = setInterval(load, intervalMs);
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },
    load,
    startPolling,
    stopPolling,
  };
}

export const runnersStore = createRunnersStore();

// Derived stores for convenience
export const idleRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'idle')
);

export const busyRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'busy')
);
