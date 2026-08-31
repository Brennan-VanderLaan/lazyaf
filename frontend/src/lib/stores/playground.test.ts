import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { playgroundStore, isRunning, canStart, hasResult } from './playground';
import type { PlaygroundStatus } from '../api/types';

beforeEach(() => {
  playgroundStore.reset();
});

describe('playgroundStore.setConfig', () => {
  it('merges partial config without touching other fields', () => {
    playgroundStore.setConfig({ repoId: 'repo-1', taskOverride: 'do the thing' });

    const state = get(playgroundStore);
    expect(state.repoId).toBe('repo-1');
    expect(state.taskOverride).toBe('do the thing');
    // Untouched defaults survive
    expect(state.runnerType).toBe('claude-code');
    expect(state.status).toBe('idle');
  });
});

describe('playgroundStore.clearLogs / reset', () => {
  it('clearLogs empties logs but keeps configuration', () => {
    playgroundStore.setConfig({ branch: 'main', logs: ['line 1', 'line 2'] });
    playgroundStore.clearLogs();

    const state = get(playgroundStore);
    expect(state.logs).toEqual([]);
    expect(state.branch).toBe('main');
  });

  it('reset restores the initial state', () => {
    playgroundStore.setConfig({
      repoId: 'repo-1',
      status: 'failed' as PlaygroundStatus,
      error: 'oops',
      logs: ['x'],
    });
    playgroundStore.reset();

    const state = get(playgroundStore);
    expect(state.repoId).toBeNull();
    expect(state.status).toBe('idle');
    expect(state.error).toBeNull();
    expect(state.logs).toEqual([]);
  });
});

describe('derived: isRunning / canStart', () => {
  it('isRunning is true only for queued and running', () => {
    const expectations: Array<[PlaygroundStatus, boolean]> = [
      ['idle', false],
      ['queued', true],
      ['running', true],
      ['completed', false],
      ['failed', false],
      ['cancelled', false],
    ];
    for (const [status, expected] of expectations) {
      playgroundStore.setConfig({ status });
      expect(get(isRunning), `status=${status}`).toBe(expected);
    }
  });

  it('canStart is the complement of an in-flight session', () => {
    playgroundStore.setConfig({ status: 'idle' });
    expect(get(canStart)).toBe(true);

    playgroundStore.setConfig({ status: 'running' });
    expect(get(canStart)).toBe(false);

    playgroundStore.setConfig({ status: 'cancelled' });
    expect(get(canStart)).toBe(true);
  });
});

describe('derived: hasResult', () => {
  it('is false while running even with a diff present', () => {
    playgroundStore.setConfig({ status: 'running', diff: 'diff --git a b' });
    expect(get(hasResult)).toBe(false);
  });

  /**
   * CHANGED DELIBERATELY (QA-7, finding PG-07). This used to assert `false`,
   * which is what made the single most natural first prompt a stranger types
   * ("what does this repo do?") finish with no Changes section, no "nothing
   * changed" message and no Reset button - the page looked like nothing had
   * happened. `hasResult` now means "the run reached a terminal state", not
   * "the run produced a diff", and the page renders the empty-changes branch.
   */
  it('is true when completed with no diff, error, or changed files', () => {
    playgroundStore.setConfig({ status: 'completed', diff: null, error: null, filesChanged: [] });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when a run was cancelled', () => {
    playgroundStore.setConfig({ status: 'cancelled', diff: null, error: null, filesChanged: [] });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when completed with a diff', () => {
    playgroundStore.setConfig({ status: 'completed', diff: 'diff --git a b' });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when failed with an error', () => {
    playgroundStore.setConfig({ status: 'failed', error: 'agent exploded' });
    expect(get(hasResult)).toBe(true);
  });

  it('is true when completed with only files changed', () => {
    playgroundStore.setConfig({ status: 'completed', filesChanged: ['a.ts'] });
    expect(get(hasResult)).toBe(true);
  });
});
