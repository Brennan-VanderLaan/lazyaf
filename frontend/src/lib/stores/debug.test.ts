/**
 * Debug store contract - Phase 12.7.
 *
 * Three things are pinned here because each of them is a way this feature can
 * fail SILENTLY, which is the only way it is allowed to fail loudly:
 *
 *  1. BREAKPOINT IDENTITY. A legacy step's key is its index-as-a-string, not
 *     its optional `config.id`; a graph step's key is its step id. Get this
 *     wrong and the backend accepts a key that no gate will ever match - a
 *     breakpoint that never fires, with no error anywhere.
 *  2. SNAPSHOT-THEN-DELTA. `debug_session_status` frames only arrive on a
 *     change, and a session parked at a breakpoint changes nothing for hours,
 *     so a delta-only store shows an empty panel over a wedged pipeline after
 *     any reload. Both halves are driven here.
 *  3. A SESSION WITHOUT `pipeline_run_id` IS REFUSED, LOUDLY. It cannot be
 *     attributed to a run, and indexing it under `undefined` would make it
 *     indistinguishable from "no debug session".
 *
 * `debugApi` is mocked at the module boundary (R6: the seam is the real HTTP
 * client the store calls, not a hand-rolled fake store).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import type { DebugSessionInfo, Pipeline, PipelineV2 } from '../api/types';

const listMock = vi.fn();
const getMock = vi.fn();
const createRerunMock = vi.fn();
const resumeMock = vi.fn();
const abortMock = vi.fn();
const extendMock = vi.fn();

vi.mock('../api/client', () => ({
  debug: {
    list: (...args: unknown[]) => listMock(...args),
    get: (...args: unknown[]) => getMock(...args),
    createRerun: (...args: unknown[]) => createRerunMock(...args),
    resume: (...args: unknown[]) => resumeMock(...args),
    abort: (...args: unknown[]) => abortMock(...args),
    extend: (...args: unknown[]) => extendMock(...args),
  },
}));

// Imported AFTER the mock is registered so the store closes over it.
const {
  debugSessionsStore,
  debugSessionForRun,
  debugStepKey,
  debugBreakpointOptions,
  isTerminalDebugState,
  isPausedDebugState,
  debugStateLabel,
  remainingMs,
  formatCountdown,
  pausedDebugSessions,
} = await import('./debug');

function makeSession(overrides: Partial<DebugSessionInfo> = {}): DebugSessionInfo {
  return {
    id: 'sess-1',
    pipeline_run_id: 'run-1',
    original_run_id: 'run-0',
    status: 'waiting_at_bp',
    current_step: { key: 'build', name: 'build', index: 1, type: 'script' },
    commit: { sha: 'abcdef1234567890', message: 'fix the thing', branch: 'main' },
    runtime: { host: 'local', orchestrator: 'docker', image: 'lazyaf-base:dev', image_sha: null },
    logs: '',
    join_command: 'lazyaf debug attach sess-1',
    expires_at: '2026-08-30T12:30:00Z',
    created_at: '2026-08-30T12:00:00Z',
    ended_at: null,
    connection_mode: null,
    breakpoints: ['build', 'test'],
    breakpoints_hit: ['build'],
    breakpoints_pending: ['test'],
    attach_available: true,
    attach_unavailable_reason: null,
    end_reason: null,
    ...overrides,
  };
}

function legacyPipeline(): Pipeline {
  return {
    id: 'p1',
    repo_id: 'r1',
    name: 'legacy',
    description: null,
    steps: [
      // `id` here is a v1 CONTEXT-DIRECTORY reference, NOT a step_run.step_id.
      // If the resolver ever keys off it, this fixture catches it.
      { id: 'ctx-alpha', name: 'setup', type: 'script', config: {} },
      { name: 'build', type: 'script', config: {} },
      { name: 'verify', type: 'test', config: {} },
    ],
    triggers: [],
    is_template: false,
    created_at: '',
    updated_at: '',
  } as unknown as Pipeline;
}

function graphPipeline(): PipelineV2 {
  return {
    ...legacyPipeline(),
    steps: [],
    steps_graph: {
      version: 2,
      // Deliberately NOT in execution order, so a pass-through of
      // Object.keys() would produce the wrong list.
      steps: {
        deploy: { id: 'deploy', name: 'deploy', type: 'script', config: {}, timeout: 60 },
        orphan: { id: 'orphan', name: 'orphan', type: 'script', config: {}, timeout: 60 },
        build: { id: 'build', name: 'build', type: 'docker', config: {}, timeout: 60 },
        test: { id: 'test', name: 'test', type: 'test', config: {}, timeout: 60 },
      },
      edges: [
        { id: 'e1', from_step: 'build', to_step: 'test', condition: 'on_success' },
        { id: 'e2', from_step: 'test', to_step: 'deploy', condition: 'on_success' },
      ],
      entry_points: ['build'],
    },
  } as unknown as PipelineV2;
}

beforeEach(() => {
  listMock.mockReset();
  getMock.mockReset();
  createRerunMock.mockReset();
  resumeMock.mockReset();
  abortMock.mockReset();
  extendMock.mockReset();
  listMock.mockResolvedValue([]);
  getMock.mockImplementation(async (id: string) => makeSession({ id }));
  debugSessionsStore.reset();
});

describe('breakpoint identity (client half of ONE resolver)', () => {
  it('a graph step run is keyed by its step_id', () => {
    expect(debugStepKey({ step_id: 'build', step_index: 3 })).toBe('build');
  });

  it('a legacy step run with no step_id is keyed by its index as a string', () => {
    expect(debugStepKey({ step_id: null, step_index: 3 })).toBe('3');
  });

  it('legacy pipeline options are index keys, NOT the step config id', () => {
    const options = debugBreakpointOptions(legacyPipeline());
    expect(options.map((o) => o.key)).toEqual(['0', '1', '2']);
    // The regression this exists to prevent: keying off `config.id` would
    // yield 'ctx-alpha', which no gate can ever match.
    expect(options.map((o) => o.key)).not.toContain('ctx-alpha');
    expect(options.map((o) => o.name)).toEqual(['setup', 'build', 'verify']);
  });

  it('graph pipeline options are step ids in entry-point-first traversal order', () => {
    const options = debugBreakpointOptions(graphPipeline());
    expect(options.map((o) => o.key)).toEqual(['build', 'test', 'deploy', 'orphan']);
  });

  it('a graph step unreachable from any entry point still gets a checkbox', () => {
    // Refusing to list it would be a step the user cannot break on, with no
    // message saying why.
    expect(debugBreakpointOptions(graphPipeline()).map((o) => o.key)).toContain('orphan');
  });

  it('graph options carry the step type for the icon, not a default', () => {
    const byKey = new Map(debugBreakpointOptions(graphPipeline()).map((o) => [o.key, o.type]));
    expect(byKey.get('build')).toBe('docker');
    expect(byKey.get('test')).toBe('test');
  });
});

describe('state vocabulary', () => {
  it('timeout and ended are terminal; nothing else is', () => {
    expect(isTerminalDebugState('timeout')).toBe(true);
    expect(isTerminalDebugState('ended')).toBe(true);
    expect(isTerminalDebugState('pending')).toBe(false);
    expect(isTerminalDebugState('waiting_at_bp')).toBe(false);
    expect(isTerminalDebugState('connected')).toBe(false);
  });

  it('a run is held at a gate while waiting_at_bp or connected', () => {
    expect(isPausedDebugState('waiting_at_bp')).toBe(true);
    expect(isPausedDebugState('connected')).toBe(true);
    // `pending` is the RESUMED state - executing, not at a breakpoint. A UI
    // that labelled it "paused" would misreport a running pipeline.
    expect(isPausedDebugState('pending')).toBe(false);
  });

  it('pending reads as Running, not as Pending', () => {
    expect(debugStateLabel('pending')).toBe('Running');
    expect(debugStateLabel('waiting_at_bp')).toBe('Paused at breakpoint');
  });
});

describe('countdown', () => {
  const now = Date.parse('2026-08-30T12:00:00Z');

  it('returns null for an unbounded session', () => {
    expect(remainingMs(null, now)).toBeNull();
  });

  it('floors at zero rather than counting negative', () => {
    expect(remainingMs('2026-08-30T11:59:00Z', now)).toBe(0);
  });

  it('measures against the absolute deadline', () => {
    expect(remainingMs('2026-08-30T12:05:00Z', now)).toBe(300_000);
  });

  it('reads a NAIVE backend expires_at as UTC, not as browser-local time', () => {
    // QA triage T1: the backend serialises `expires_at` with no designator,
    // and `new Date(str)` therefore read it as local. On a UTC-4 laptop a
    // four-hour debug session counted down from eight; west of UTC the other
    // way, a live gate showed 0:00. Both spellings must land on one instant.
    expect(remainingMs('2026-08-30T12:05:00', now)).toBe(300_000);
    expect(remainingMs('2026-08-30T12:05:00.123456', now)).toBe(300_123);
  });

  it('returns null - never NaN - for an unparseable deadline', () => {
    expect(remainingMs('not a date', now)).toBeNull();
  });

  it('formats under and over an hour', () => {
    expect(formatCountdown(65_000)).toBe('1:05');
    expect(formatCountdown(3_725_000)).toBe('1:02:05');
    expect(formatCountdown(0)).toBe('0:00');
  });
});

describe('snapshot-then-delta', () => {
  it('SNAPSHOT: load() populates the store with no delta ever arriving', async () => {
    listMock.mockResolvedValue([makeSession({ id: 'a' }), makeSession({ id: 'b', pipeline_run_id: 'run-2' })]);
    await debugSessionsStore.load();
    expect(get(debugSessionsStore).map((s) => s.id)).toEqual(['a', 'b']);
    expect(get(debugSessionsStore.loaded)).toBe(true);
  });

  it('load() distinguishes "no sessions" from "not asked yet"', async () => {
    expect(get(debugSessionsStore.loaded)).toBe(false);
    await debugSessionsStore.load();
    expect(get(debugSessionsStore.loaded)).toBe(true);
    expect(get(debugSessionsStore)).toEqual([]);
  });

  it('DELTA: an unknown session id inserts', () => {
    debugSessionsStore.applyDelta(makeSession({ id: 'new' }));
    expect(get(debugSessionsStore).map((s) => s.id)).toEqual(['new']);
  });

  it('DELTA: a known id replaces wholesale (the frame is a projection, not a patch)', () => {
    debugSessionsStore.applyDelta(makeSession({ id: 'a', status: 'waiting_at_bp' }));
    debugSessionsStore.applyDelta(
      makeSession({ id: 'a', status: 'connected', breakpoints_hit: ['build', 'test'] })
    );
    const rows = get(debugSessionsStore);
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe('connected');
    expect(rows[0].breakpoints_hit).toEqual(['build', 'test']);
  });

  it('a TERMINAL session is kept, so end_reason stays readable', () => {
    // Deliberately unlike the runner store, which evicts. Evicting here would
    // replace "timed out at breakpoint" with an empty panel at the exact
    // moment the operator needs the sentence.
    debugSessionsStore.applyDelta(makeSession({ id: 'a' }));
    debugSessionsStore.applyDelta(
      makeSession({ id: 'a', status: 'ended', end_reason: 'aborted by user' })
    );
    const rows = get(debugSessionsStore);
    expect(rows).toHaveLength(1);
    expect(rows[0].end_reason).toBe('aborted by user');
  });

  it('rows stay id-ordered so a delta never reshuffles the panel', () => {
    debugSessionsStore.applyDelta(makeSession({ id: 'c' }));
    debugSessionsStore.applyDelta(makeSession({ id: 'a' }));
    debugSessionsStore.applyDelta(makeSession({ id: 'b' }));
    expect(get(debugSessionsStore).map((s) => s.id)).toEqual(['a', 'b', 'c']);
  });

  it('a session without pipeline_run_id is REFUSED with a named error', () => {
    debugSessionsStore.applyDelta({ ...makeSession(), pipeline_run_id: '' } as DebugSessionInfo);
    expect(get(debugSessionsStore)).toEqual([]);
    expect(get(debugSessionsStore.error)).toMatch(/pipeline_run_id/);
    expect(get(debugSessionsStore.error)).toMatch(/schemas\/debug\.py/);
  });

  it('a null/idless frame is dropped without erroring', () => {
    debugSessionsStore.applyDelta(null);
    debugSessionsStore.applyDelta(undefined);
    expect(get(debugSessionsStore)).toEqual([]);
    expect(get(debugSessionsStore.error)).toBeNull();
  });

  it('load() surfaces a transport failure instead of silently emptying', async () => {
    listMock.mockRejectedValue(new Error('backend down'));
    await debugSessionsStore.load();
    expect(get(debugSessionsStore.error)).toBe('backend down');
    expect(get(debugSessionsStore.loaded)).toBe(false);
  });
});

describe('lookup by run', () => {
  it('finds the session gating a run', () => {
    debugSessionsStore.applyDelta(makeSession({ id: 'a', pipeline_run_id: 'run-7' }));
    expect(get(debugSessionForRun('run-7'))?.id).toBe('a');
  });

  it('returns null for a run with no session', () => {
    expect(get(debugSessionForRun('run-nope'))).toBeNull();
  });

  it('a live pause outranks a tombstone on the same run', () => {
    debugSessionsStore.applyDelta(
      makeSession({ id: 'a', pipeline_run_id: 'run-7', status: 'ended', end_reason: 'resumed' })
    );
    debugSessionsStore.applyDelta(
      makeSession({ id: 'b', pipeline_run_id: 'run-7', status: 'waiting_at_bp' })
    );
    expect(get(debugSessionForRun('run-7'))?.id).toBe('b');
  });

  it('pausedDebugSessions lists only sessions holding a gate', () => {
    debugSessionsStore.applyDelta(makeSession({ id: 'a', status: 'waiting_at_bp' }));
    debugSessionsStore.applyDelta(makeSession({ id: 'b', pipeline_run_id: 'r2', status: 'pending' }));
    debugSessionsStore.applyDelta(makeSession({ id: 'c', pipeline_run_id: 'r3', status: 'connected' }));
    expect(get(pausedDebugSessions).map((s) => s.id)).toEqual(['a', 'c']);
  });
});

describe('actions', () => {
  it('startRerun posts to the run and then reads the new session back', async () => {
    createRerunMock.mockResolvedValue({
      run_id: 'run-9',
      debug_session_id: 'sess-9',
      join_command: 'lazyaf debug attach sess-9',
    });
    getMock.mockResolvedValue(makeSession({ id: 'sess-9', pipeline_run_id: 'run-9' }));

    const response = await debugSessionsStore.startRerun('run-1', {
      breakpoints: ['0', '2'],
      use_original_commit: true,
      commit_sha: null,
      branch: null,
      timeout_seconds: 3600,
    });

    expect(createRerunMock).toHaveBeenCalledWith('run-1', {
      breakpoints: ['0', '2'],
      use_original_commit: true,
      commit_sha: null,
      branch: null,
      timeout_seconds: 3600,
    });
    expect(response.debug_session_id).toBe('sess-9');
    // The create response carries no token by design; only these three keys.
    expect(Object.keys(response).sort()).toEqual(['debug_session_id', 'join_command', 'run_id']);
    expect(get(debugSessionsStore).map((s) => s.id)).toEqual(['sess-9']);
  });

  it('resume defaults to "next breakpoint", not "run to completion"', async () => {
    resumeMock.mockResolvedValue({ status: 'pending', next_breakpoint: 'test' });
    await debugSessionsStore.resume('sess-1');
    expect(resumeMock).toHaveBeenCalledWith('sess-1', false);
  });

  it('resume(clearRemaining) drops the remaining breakpoints', async () => {
    resumeMock.mockResolvedValue({ status: 'pending', next_breakpoint: null });
    await debugSessionsStore.resume('sess-1', true);
    expect(resumeMock).toHaveBeenCalledWith('sess-1', true);
  });

  it('resume refreshes the row afterwards so the panel reflects the new state', async () => {
    resumeMock.mockResolvedValue({ status: 'pending', next_breakpoint: 'test' });
    getMock.mockResolvedValue(makeSession({ id: 'sess-1', status: 'pending' }));
    await debugSessionsStore.resume('sess-1');
    expect(getMock).toHaveBeenCalledWith('sess-1');
    expect(get(debugSessionsStore)[0].status).toBe('pending');
  });

  it('abort ends the session and the refreshed row carries end_reason', async () => {
    abortMock.mockResolvedValue({ status: 'ended', end_reason: 'aborted by user' });
    getMock.mockResolvedValue(
      makeSession({ id: 'sess-1', status: 'ended', end_reason: 'aborted by user' })
    );
    const response = await debugSessionsStore.abort('sess-1');
    expect(response.end_reason).toBe('aborted by user');
    expect(get(debugSessionsStore)[0].end_reason).toBe('aborted by user');
  });

  it('extend defaults to +30 minutes and moves expires_at', async () => {
    extendMock.mockResolvedValue({ expires_at: '2026-08-30T13:00:00Z' });
    getMock.mockResolvedValue(makeSession({ expires_at: '2026-08-30T13:00:00Z' }));
    await debugSessionsStore.extend('sess-1');
    expect(extendMock).toHaveBeenCalledWith('sess-1', 30);
    expect(get(debugSessionsStore)[0].expires_at).toBe('2026-08-30T13:00:00Z');
  });

  it('a failed action rejects to the caller rather than being swallowed', async () => {
    resumeMock.mockRejectedValue(new Error('session already ended'));
    await expect(debugSessionsStore.resume('sess-1')).rejects.toThrow('session already ended');
  });

  it('reset drops everything without touching the network', () => {
    debugSessionsStore.applyDelta(makeSession());
    debugSessionsStore.reset();
    expect(get(debugSessionsStore)).toEqual([]);
    expect(get(debugSessionsStore.loaded)).toBe(false);
    expect(listMock).not.toHaveBeenCalled();
  });
});
