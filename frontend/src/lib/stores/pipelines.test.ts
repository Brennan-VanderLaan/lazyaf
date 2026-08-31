import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { PipelineRun, RunStatus } from '../api/types';

// `pipelineRuns.list` is the seam: recent runs enter the store through the real
// HTTP client the store calls, not through a hand-poked internal map.
const listMock = vi.fn();

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  pipelineRuns: { list: (...args: unknown[]) => listMock(...args) },
}));

// Dynamic so the mock factory above is installed before the store module runs.
const { activeRunsStore, runsByStatus, hasActiveRuns } = await import('./pipelines');

function makeRun(overrides: Partial<PipelineRun> = {}): PipelineRun {
  return {
    id: 'run-1',
    pipeline_id: 'pipe-1',
    status: 'pending',
    trigger_type: 'manual',
    trigger_ref: null,
    trigger_context: null,
    current_step: 0,
    steps_completed: 0,
    steps_total: 3,
    started_at: null,
    completed_at: null,
    created_at: '2026-01-01T00:00:00Z',
    step_runs: [],
    ...overrides,
  };
}

beforeEach(() => {
  activeRunsStore.clear();
  listMock.mockReset();
});

describe('activeRunsStore', () => {
  it('addRun inserts a run retrievable by id', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1' }));
    expect(activeRunsStore.get('r1')?.id).toBe('r1');
    expect(get(activeRunsStore).size).toBe(1);
  });

  it('updateRun replaces the run with the same id instead of duplicating', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'running', steps_completed: 1 }));
    activeRunsStore.updateRun(makeRun({ id: 'r1', status: 'passed', steps_completed: 3 }));

    const map = get(activeRunsStore);
    expect(map.size).toBe(1);
    expect(map.get('r1')?.status).toBe('passed');
    expect(map.get('r1')?.steps_completed).toBe(3);
  });

  it('updateRun produces a new Map reference so derived stores re-fire', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1' }));
    const before = get(activeRunsStore);
    activeRunsStore.updateRun(makeRun({ id: 'r1', status: 'running' }));
    const after = get(activeRunsStore);
    expect(after).not.toBe(before);
  });

  it('removeRun deletes only the targeted run', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1' }));
    activeRunsStore.addRun(makeRun({ id: 'r2' }));
    activeRunsStore.removeRun('r1');

    expect(activeRunsStore.get('r1')).toBeUndefined();
    expect(activeRunsStore.get('r2')?.id).toBe('r2');
  });

  it('get returns undefined for an unknown id', () => {
    expect(activeRunsStore.get('missing')).toBeUndefined();
  });
});

describe('activeRunsStore.loadRecent', () => {
  it('drops a run the new payload no longer lists', async () => {
    activeRunsStore.addRun(makeRun({ id: 'deleted-run', status: 'passed', created_at: '2026-01-01T00:00:00Z' }));
    activeRunsStore.addRun(makeRun({ id: 'kept-run', status: 'passed', created_at: '2026-01-02T00:00:00Z' }));

    listMock.mockResolvedValue([makeRun({ id: 'kept-run', status: 'passed', created_at: '2026-01-02T00:00:00Z' })]);
    await activeRunsStore.loadRecent();

    const map = get(activeRunsStore);
    expect(map.has('deleted-run')).toBe(false);
    expect(map.get('kept-run')?.id).toBe('kept-run');
    expect(map.size).toBe(1);
  });

  it('stops the live poll once the ghost running run is gone from the payload', async () => {
    activeRunsStore.addRun(makeRun({ id: 'ghost', status: 'running' }));
    expect(get(hasActiveRuns)).toBe(true);

    listMock.mockResolvedValue([]);
    await activeRunsStore.loadRecent();

    expect(get(activeRunsStore).size).toBe(0);
    expect(get(hasActiveRuns)).toBe(false);
  });

  it('keeps a live run that fell off the end of a full page', async () => {
    activeRunsStore.addRun(makeRun({ id: 'off-page-live', status: 'running', created_at: '2026-01-01T00:00:00Z' }));
    activeRunsStore.addRun(makeRun({ id: 'off-page-done', status: 'passed', created_at: '2026-01-01T00:00:00Z' }));

    // A full page (limit === payload length) means older runs exist beyond it.
    listMock.mockResolvedValue([makeRun({ id: 'newer', status: 'passed', created_at: '2026-01-05T00:00:00Z' })]);
    await activeRunsStore.loadRecent(1);

    const map = get(activeRunsStore);
    expect(map.has('off-page-live')).toBe(true);
    expect(map.has('off-page-done')).toBe(false);
    expect(map.has('newer')).toBe(true);
  });

  it('takes the payload copy of a run it already held', async () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'running', steps_completed: 1 }));

    listMock.mockResolvedValue([makeRun({ id: 'r1', status: 'passed', steps_completed: 3 })]);
    await activeRunsStore.loadRecent();

    expect(get(activeRunsStore).get('r1')?.status).toBe('passed');
    expect(get(activeRunsStore).get('r1')?.steps_completed).toBe(3);
  });
});

describe('runsByStatus', () => {
  it('groups runs into every status bucket', () => {
    const statuses: RunStatus[] = ['pending', 'running', 'passed', 'failed', 'cancelled'];
    statuses.forEach((status, i) => {
      activeRunsStore.addRun(makeRun({ id: `r-${status}-${i}`, status }));
    });
    activeRunsStore.addRun(makeRun({ id: 'r-passed-extra', status: 'passed' }));

    const grouped = get(runsByStatus);
    expect(grouped.pending).toHaveLength(1);
    expect(grouped.running).toHaveLength(1);
    expect(grouped.passed).toHaveLength(2);
    expect(grouped.failed).toHaveLength(1);
    expect(grouped.cancelled).toHaveLength(1);
  });

  it('returns empty buckets for an empty store', () => {
    const grouped = get(runsByStatus);
    expect(grouped.pending).toEqual([]);
    expect(grouped.passed).toEqual([]);
  });
});

describe('hasActiveRuns', () => {
  it('is false when the store is empty', () => {
    expect(get(hasActiveRuns)).toBe(false);
  });

  it('is true while any run is pending or running', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'passed' }));
    expect(get(hasActiveRuns)).toBe(false);

    activeRunsStore.addRun(makeRun({ id: 'r2', status: 'running' }));
    expect(get(hasActiveRuns)).toBe(true);
  });

  it('flips back to false once the active run finishes', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'pending' }));
    expect(get(hasActiveRuns)).toBe(true);

    activeRunsStore.updateRun(makeRun({ id: 'r1', status: 'failed' }));
    expect(get(hasActiveRuns)).toBe(false);
  });
});

describe('activeRunsStore merges runs by id instead of replacing them', () => {
  function makeStep(over: Record<string, unknown> = {}) {
    return {
      id: 'sr-0',
      pipeline_run_id: 'r1',
      step_index: 0,
      step_name: 'build',
      status: 'running',
      executor: 'local',
      job_id: null,
      logs: null,
      error: null,
      started_at: '2026-01-01T00:00:00Z',
      completed_at: null,
      created_at: '2026-01-01T00:00:00Z',
      ...over,
    } as never;
  }

  /**
   * Runs arrive from four places that do not all carry the same fields. A
   * payload that omits `step_runs` used to blank the step timeline the open
   * viewer was rendering - it emptied and refilled on a 3s cycle, moving the
   * row the user was about to click.
   */
  it('loadRecent keeps step_runs a summary payload does not mention', async () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'running', step_runs: [makeStep()] }));

    const summary = makeRun({ id: 'r1', status: 'running' });
    delete (summary as unknown as Record<string, unknown>).step_runs;
    listMock.mockResolvedValue([summary]);
    await activeRunsStore.loadRecent();

    expect(get(activeRunsStore).get('r1')?.step_runs).toHaveLength(1);
  });

  it('loadRecent still takes every field the payload DOES carry', async () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'running', steps_completed: 1, step_runs: [makeStep()] }));

    listMock.mockResolvedValue([
      makeRun({ id: 'r1', status: 'passed', steps_completed: 3, step_runs: [makeStep({ status: 'passed' })] }),
    ]);
    await activeRunsStore.loadRecent();

    const run = get(activeRunsStore).get('r1');
    expect(run?.status).toBe('passed');
    expect(run?.steps_completed).toBe(3);
    expect(run?.step_runs?.[0].status).toBe('passed');
  });

  it('updateRun keeps step_runs that a pipeline_run_status frame omits', () => {
    activeRunsStore.addRun(makeRun({ id: 'r1', status: 'running', step_runs: [makeStep()] }));

    const frame = makeRun({ id: 'r1', status: 'passed' });
    delete (frame as unknown as Record<string, unknown>).step_runs;
    activeRunsStore.updateRun(frame);

    expect(get(activeRunsStore).get('r1')?.status).toBe('passed');
    expect(get(activeRunsStore).get('r1')?.step_runs).toHaveLength(1);
  });
});
