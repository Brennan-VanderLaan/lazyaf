import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { activeRunsStore, runsByStatus, hasActiveRuns } from './pipelines';
import type { PipelineRun, RunStatus } from '../api/types';

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
