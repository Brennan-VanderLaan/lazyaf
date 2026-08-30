/**
 * Runner store contract - Phase 12.6 (Agent E test contract 5).
 *
 * The store is snapshot-then-delta. Both halves are tested here because the
 * failure mode that shipped in the salvaged attempt was exactly the missing
 * half: the deltas worked, the snapshot was deleted, and a page reload
 * showed an empty runner panel over a live fleet. A store test that only
 * drove deltas would have gone green over it.
 *
 * `runnersApi.list` is mocked at the module boundary (R6: the seam is the
 * real HTTP client the store calls, not a hand-rolled fake store).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { Runner, RunnerState } from '../api/types';

const listMock = vi.fn();

vi.mock('../api/client', () => ({
  runners: { list: (...args: unknown[]) => listMock(...args) },
}));

// Imported AFTER the mock is registered so the store closes over it.
const { runnersStore, idleRunners, busyRunners, connectedRunners, isGone, isLive } =
  await import('./runners');

function makeRunner(overrides: Partial<Runner> = {}): Runner {
  return {
    id: 'runner-1',
    name: 'runner-1',
    runner_type: 'generic',
    status: 'idle',
    labels: { arch: 'amd64', has: ['docker'] },
    current_step_execution_id: null,
    current_step_id: null,
    protocol_version: 1,
    agent_version: '12.6',
    connected_at: '2026-08-30T00:00:00Z',
    last_heartbeat: '2026-08-30T00:00:10Z',
    created_at: '2026-08-30T00:00:00Z',
    connection: 'websocket',
    ...overrides,
  };
}

beforeEach(() => {
  listMock.mockReset();
  listMock.mockResolvedValue([]);
  runnersStore.reset();
});

describe('snapshot half', () => {
  it('load() populates the store from GET /api/runners', async () => {
    listMock.mockResolvedValue([
      makeRunner({ id: 'a' }),
      makeRunner({ id: 'b', status: 'busy' }),
    ]);

    await runnersStore.load();

    expect(get(runnersStore).map((r) => r.id)).toEqual(['a', 'b']);
    expect(get(runnersStore.loaded)).toBe(true);
  });

  it('load() replaces the previous snapshot rather than appending to it', async () => {
    listMock.mockResolvedValue([makeRunner({ id: 'a' }), makeRunner({ id: 'b' })]);
    await runnersStore.load();

    // A runner left the fleet between the two snapshots.
    listMock.mockResolvedValue([makeRunner({ id: 'b' })]);
    await runnersStore.load();

    expect(get(runnersStore).map((r) => r.id)).toEqual(['b']);
  });

  it('a failed load records the error and leaves the list untouched', async () => {
    listMock.mockResolvedValue([makeRunner({ id: 'a' })]);
    await runnersStore.load();

    listMock.mockRejectedValue(new Error('backend down'));
    await runnersStore.load();

    expect(get(runnersStore.error)).toBe('backend down');
    // Showing the last known fleet beats blanking the panel on one 500.
    expect(get(runnersStore).map((r) => r.id)).toEqual(['a']);
  });

  it('rows are ordered by id so a state change never reshuffles the panel', async () => {
    listMock.mockResolvedValue([
      makeRunner({ id: 'zeta' }),
      makeRunner({ id: 'alpha' }),
      makeRunner({ id: 'mid' }),
    ]);
    await runnersStore.load();
    expect(get(runnersStore).map((r) => r.id)).toEqual(['alpha', 'mid', 'zeta']);

    runnersStore.applyDelta(makeRunner({ id: 'zeta', status: 'busy' }));
    expect(get(runnersStore).map((r) => r.id)).toEqual(['alpha', 'mid', 'zeta']);
  });
});

describe('delta half', () => {
  it('a delta for an UNKNOWN runner is an insert', () => {
    runnersStore.applyDelta(makeRunner({ id: 'new-one' }));
    expect(get(runnersStore).map((r) => r.id)).toEqual(['new-one']);
  });

  it('a delta for a KNOWN runner is an update, not a duplicate', async () => {
    listMock.mockResolvedValue([makeRunner({ id: 'a', status: 'idle' })]);
    await runnersStore.load();

    runnersStore.applyDelta(
      makeRunner({ id: 'a', status: 'busy', current_step_execution_id: 'se-9' })
    );

    const rows = get(runnersStore);
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe('busy');
    expect(rows[0].current_step_execution_id).toBe('se-9');
  });

  it.each<RunnerState>(['disconnected', 'dead'])(
    'a %s delta removes the runner from the panel',
    async (status) => {
      listMock.mockResolvedValue([makeRunner({ id: 'a' }), makeRunner({ id: 'b' })]);
      await runnersStore.load();

      runnersStore.applyDelta(makeRunner({ id: 'a', status }));

      expect(get(runnersStore).map((r) => r.id)).toEqual(['b']);
    }
  );

  it('a disconnected delta for a runner we never saw is a no-op, not an insert', () => {
    runnersStore.applyDelta(makeRunner({ id: 'ghost', status: 'disconnected' }));
    expect(get(runnersStore)).toHaveLength(0);
  });

  it('a malformed frame (null, or missing id) is dropped', () => {
    runnersStore.applyDelta(null);
    runnersStore.applyDelta(undefined);
    runnersStore.applyDelta({ ...makeRunner(), id: '' });
    expect(get(runnersStore)).toHaveLength(0);
  });

  it('drives the full idle -> assigned -> busy -> idle lifecycle from deltas alone', () => {
    const seen: RunnerState[] = [];
    const stop = runnersStore.subscribe((rows) => {
      if (rows.length) seen.push(rows[0].status);
    });

    for (const status of ['idle', 'assigned', 'busy', 'idle'] as RunnerState[]) {
      runnersStore.applyDelta(makeRunner({ id: 'a', status }));
    }
    stop();

    expect(seen).toEqual(['idle', 'assigned', 'busy', 'idle']);
  });
});

describe('derived views', () => {
  it('idleRunners excludes a row whose connection is not a live websocket', async () => {
    // An "idle" row left behind by a crashed backend process is
    // indistinguishable from a live one in the DB alone; counting it would
    // overstate capacity to whoever reads the panel.
    listMock.mockResolvedValue([
      makeRunner({ id: 'live', status: 'idle', connection: 'websocket' }),
      makeRunner({ id: 'stale', status: 'idle', connection: 'none' }),
    ]);
    await runnersStore.load();

    expect(get(idleRunners).map((r) => r.id)).toEqual(['live']);
  });

  it('busyRunners covers assigned as well as busy', async () => {
    listMock.mockResolvedValue([
      makeRunner({ id: 'a', status: 'assigned' }),
      makeRunner({ id: 'b', status: 'busy' }),
      makeRunner({ id: 'c', status: 'idle' }),
    ]);
    await runnersStore.load();

    expect(get(busyRunners).map((r) => r.id)).toEqual(['a', 'b']);
  });

  it('connectedRunners is every live, socket-backed row', async () => {
    listMock.mockResolvedValue([
      makeRunner({ id: 'a', status: 'connecting' }),
      makeRunner({ id: 'b', status: 'busy' }),
      makeRunner({ id: 'c', status: 'idle', connection: 'none' }),
    ]);
    await runnersStore.load();

    expect(get(connectedRunners).map((r) => r.id)).toEqual(['a', 'b']);
  });
});

describe('state vocabulary (cross-agent contract #4)', () => {
  it('isGone / isLive partition every RunnerState exactly once', () => {
    const all: RunnerState[] = [
      'disconnected',
      'connecting',
      'idle',
      'assigned',
      'busy',
      'dead',
    ];
    for (const status of all) {
      expect(isGone(status) !== isLive(status)).toBe(true);
    }
  });
});
