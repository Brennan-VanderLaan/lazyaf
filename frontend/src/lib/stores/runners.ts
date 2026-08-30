import { derived, writable } from 'svelte/store';
import type { Runner, RunnerState } from '../api/types';
import { runners as runnersApi } from '../api/client';

/**
 * Runner store - Phase 12.6: SNAPSHOT FETCH + WEBSOCKET DELTAS.
 *
 * The 12.5 store polled `GET /api/runners` and `GET /api/runners/status`
 * every 2000ms. Both endpoints served the in-memory polling pool, which no
 * longer exists: runners enroll over `/ws/runner`, the registry owns their
 * state, and every transition already broadcasts one `runner_status` frame
 * (`RunnerRegistry.transition` -> `manager.send_runner_status`). Polling a
 * push channel is latency the UI pays for nothing.
 *
 * The pattern is snapshot-THEN-delta, and both halves are load-bearing:
 *
 *   - `load()` fetches the full list once. Without it a reload shows an
 *     empty panel until some runner happens to change state - a live fleet
 *     that is quietly idle can broadcast nothing for hours. (This is the
 *     exact regression a prior attempt shipped: it deleted the HTTP path,
 *     kept the deltas, and the panel was blank on every refresh.)
 *   - `applyDelta()` merges one `runner_status` frame. The frame payload is
 *     byte-identical to one row of the snapshot - both are produced by
 *     `RunnerRegistry._as_dict` - so there is exactly one shape to merge and
 *     no field-by-field reconciliation.
 *
 * Storage is a `Map<string, Runner>` rather than an array so a delta is an
 * O(1) upsert keyed by runner id; the public store is the derived array the
 * panel renders.
 */

/** Terminal states: the runner is gone, and the row is only a tombstone. */
const GONE_STATES: readonly RunnerState[] = ['disconnected', 'dead'];

/** States that mean this runner can still take work or is doing work. */
const LIVE_STATES: readonly RunnerState[] = ['connecting', 'idle', 'assigned', 'busy'];

export function isGone(status: RunnerState): boolean {
  return GONE_STATES.includes(status);
}

export function isLive(status: RunnerState): boolean {
  return LIVE_STATES.includes(status);
}

function sortRunners(list: Runner[]): Runner[] {
  // Stable, id-ordered: the panel must not reshuffle rows every time a
  // runner changes state, or a fleet under load becomes unreadable.
  return [...list].sort((a, b) => a.id.localeCompare(b.id));
}

function createRunnersStore() {
  const byId = new Map<string, Runner>();
  const { subscribe, set } = writable<Runner[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);
  /** True once a snapshot has landed - lets the panel tell "empty" from "not asked yet". */
  const loaded = writable(false);

  function publish() {
    set(sortRunners([...byId.values()]));
  }

  /**
   * Fetch the whole registry. Called on mount and after a socket reconnect
   * (deltas that arrived while the socket was down were never seen, so the
   * in-memory map is stale by an unknown amount and only a snapshot can fix
   * it).
   */
  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const data = await runnersApi.list();
      byId.clear();
      for (const runner of data) {
        byId.set(runner.id, runner);
      }
      loaded.set(true);
      publish();
    } catch (e) {
      error.set(e instanceof Error ? e.message : 'Failed to load runners');
    } finally {
      loading.set(false);
    }
  }

  /**
   * Merge one `runner_status` frame.
   *
   *   unknown id            -> INSERT (a runner that enrolled since the snapshot)
   *   known id              -> UPDATE (replace the row wholesale; the frame is a
   *                            full projection, not a patch)
   *   disconnected / dead   -> REMOVE (the panel lists reachable runners; a
   *                            tombstone row that still says "idle" is how an
   *                            operator dispatches work at a machine that is
   *                            not there)
   *
   * A frame with no `id` is dropped rather than inserted under `undefined`.
   */
  function applyDelta(runner: Runner | null | undefined) {
    if (!runner || !runner.id) return;
    if (isGone(runner.status)) {
      byId.delete(runner.id);
    } else {
      byId.set(runner.id, runner);
    }
    publish();
  }

  /** Test/teardown hook: drop everything without touching the network. */
  function reset() {
    byId.clear();
    loaded.set(false);
    error.set(null);
    publish();
  }

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },
    loaded: { subscribe: loaded.subscribe },
    load,
    applyDelta,
    reset,
  };
}

export const runnersStore = createRunnersStore();

// Derived views the panel renders. `connection === 'websocket'` is part of
// "available": a row left behind by a crashed backend process still says
// `idle` in the DB, and counting it would overstate capacity.
export const idleRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'idle' && r.connection === 'websocket')
);

export const busyRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.status === 'busy' || r.status === 'assigned')
);

export const connectedRunners = derived(runnersStore, ($runners) =>
  $runners.filter((r) => r.connection === 'websocket' && isLive(r.status))
);
