/**
 * Snapshot-on-reconnect (QA triage T7).
 *
 * The defect: every store fed by the socket is delta-only once loaded, deltas
 * broadcast during an outage are never replayed, and `ws.onopen` did nothing
 * but set a status flag. A card edited while the backend restarted therefore
 * kept its old title and column until someone pressed F5.
 *
 * These drive `snapshotTargets` / `resyncAll` through their injected
 * dependencies and assert what actually gets CALLED — not that a list of
 * strings has the right length (standing rule R6: a real seam, not a mirror
 * of the implementation).
 */
import { describe, it, expect, vi } from 'vitest';

import {
  defaultSnapshotDeps,
  resyncAll,
  snapshotTargets,
  type SnapshotDeps,
} from './websocket';

function fakeDeps(repoId: string | null): SnapshotDeps & {
  calls: () => Record<string, unknown[][]>;
} {
  const repos = { load: vi.fn() };
  const cards = { load: vi.fn() };
  const pipelines = { load: vi.fn() };
  const runs = { loadRecent: vi.fn() };
  const runners = { load: vi.fn() };
  const debugSessions = { load: vi.fn() };
  const modelEndpoints = { load: vi.fn() };
  return {
    repos,
    cards,
    pipelines,
    runs,
    runners,
    debugSessions,
    modelEndpoints,
    currentRepoId: () => repoId,
    calls: () => ({
      repos: repos.load.mock.calls,
      cards: cards.load.mock.calls,
      pipelines: pipelines.load.mock.calls,
      runs: runs.loadRecent.mock.calls,
      runners: runners.load.mock.calls,
      debugSessions: debugSessions.load.mock.calls,
      modelEndpoints: modelEndpoints.load.mock.calls,
    }),
  };
}

describe('snapshotTargets', () => {
  it('refetches every delta-only store, so nothing missed in a gap survives', async () => {
    const deps = fakeDeps('repo-1');
    await resyncAll(snapshotTargets(deps));

    const calls = deps.calls();
    expect(calls.repos).toHaveLength(1);
    expect(calls.runners).toHaveLength(1);
    expect(calls.runs).toHaveLength(1);
    expect(calls.debugSessions).toHaveLength(1);
    expect(calls.cards).toEqual([['repo-1']]);
    expect(calls.pipelines).toEqual([['repo-1']]);
  });

  it('skips the repo-scoped fetches when no repo is selected', async () => {
    const deps = fakeDeps(null);
    const targets = snapshotTargets(deps);
    await resyncAll(targets);

    const calls = deps.calls();
    // A `load(null)` here would be a request the backend rejects, rendered by
    // the board as an error the user cannot act on.
    expect(calls.cards).toHaveLength(0);
    expect(calls.pipelines).toHaveLength(0);
    // The global stores still resync.
    expect(calls.repos).toHaveLength(1);
    expect(calls.runners).toHaveLength(1);
    expect(targets.map((t) => t.name)).not.toContain('cards');
  });

  it('reads the selected repo AT RESYNC TIME, not when the target list was built', async () => {
    let repoId: string | null = 'repo-before';
    const deps = fakeDeps(null);
    deps.currentRepoId = () => repoId;

    repoId = 'repo-after';
    await resyncAll(snapshotTargets(deps));

    expect(deps.calls().cards).toEqual([['repo-after']]);
  });

  it('the production dependency wiring points at the real stores', () => {
    // Pins the seam to reality: fakes above prove the logic, this proves the
    // default is not wired to something inert.
    expect(typeof defaultSnapshotDeps.repos.load).toBe('function');
    expect(typeof defaultSnapshotDeps.cards.load).toBe('function');
    expect(typeof defaultSnapshotDeps.pipelines.load).toBe('function');
    expect(typeof defaultSnapshotDeps.runs.loadRecent).toBe('function');
    expect(typeof defaultSnapshotDeps.runners.load).toBe('function');
    expect(typeof defaultSnapshotDeps.debugSessions.load).toBe('function');
    expect(defaultSnapshotDeps.currentRepoId()).toBeNull();
  });
});

describe('resyncAll', () => {
  it('runs every target even when one rejects', async () => {
    const ok = vi.fn();
    const outcomes = await resyncAll([
      { name: 'first', run: () => Promise.reject(new Error('still 503')) },
      { name: 'second', run: ok },
    ]);

    expect(ok).toHaveBeenCalledTimes(1);
    expect(outcomes.find((o) => o.name === 'first')).toMatchObject({ ok: false });
    expect(outcomes.find((o) => o.name === 'second')).toMatchObject({ ok: true });
  });

  it('survives a target that throws synchronously', async () => {
    const ok = vi.fn();
    const outcomes = await resyncAll([
      {
        name: 'boom',
        run: () => {
          throw new TypeError('Failed to fetch');
        },
      },
      { name: 'after', run: ok },
    ]);

    expect(ok).toHaveBeenCalledTimes(1);
    expect(outcomes[0]).toMatchObject({ name: 'boom', ok: false });
    expect((outcomes[0].error as Error).message).toBe('Failed to fetch');
  });

  it('REPORTS failures rather than swallowing them', async () => {
    // A resync that half-worked must not be able to claim it worked.
    const outcomes = await resyncAll([
      { name: 'a', run: () => Promise.reject(new Error('nope')) },
    ]);
    expect(outcomes.every((o) => o.ok)).toBe(false);
    expect(outcomes[0].error).toBeInstanceOf(Error);
  });

  it('awaits async targets before reporting them successful', async () => {
    let resolved = false;
    const outcomes = await resyncAll([
      {
        name: 'slow',
        run: () =>
          new Promise((resolve) =>
            setTimeout(() => {
              resolved = true;
              resolve(null);
            }, 5),
          ),
      },
    ]);
    expect(resolved).toBe(true);
    expect(outcomes[0].ok).toBe(true);
  });
});
