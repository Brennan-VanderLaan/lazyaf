/**
 * WebSocket contract + dispatch tests.
 *
 * The contract half is the pin that prevents silent backend/frontend drift:
 * it greps the backend source for every message type string passed to
 * ConnectionManager.broadcast() (definitions in
 * backend/app/services/websocket.py plus any raw .broadcast(...) call site
 * under backend/app) and asserts the frontend's handled set covers each one.
 * A failure names exactly which side drifted.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';

import { HANDLED_MESSAGE_TYPES, handleServerMessage } from './websocket';
import { activeRunsStore, liveStepLogsStore, stepLogKey } from './pipelines';
import type { PipelineRun, StepRun } from '../api/types';

// Raw source of every backend module (frontend/src/lib/stores -> repo root).
// Vite's import.meta.glob keeps this free of node builtins, which the app
// tsconfig excludes.
const BACKEND_SOURCES = import.meta.glob('../../../../backend/app/**/*.py', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/**
 * Every message type the backend can broadcast, with its source file.
 *
 * TWO SPELLINGS, because the backend uses both. A literal at the call site
 * (`broadcast("step_update", ...)`) is the common one; a module constant
 * (`WS_DEBUG_SESSION_STATUS = "debug_session_status"` then
 * `broadcast(WS_DEBUG_SESSION_STATUS, ...)`) is the 12.7 debug idiom. Reading
 * only literals is how `debug_session_status` reached the frontend union as a
 * silent hole: the guard below passed because the extraction never saw the
 * frame at all. Resolving constants is what makes "no backend frame goes
 * unhandled" true rather than true-for-the-frames-we-happened-to-match.
 */
function backendBroadcastTypes(): Map<string, string[]> {
  const types = new Map<string, string[]>();

  // Module-level `NAME = "value"` / `NAME: str = "value"` across every backend
  // source, so a constant defined in one module and imported into another
  // still resolves.
  const constants = new Map<string, string>();
  const constantPattern = /^([A-Z][A-Z0-9_]*)\s*(?::\s*[A-Za-z_[\]. ]+\s*)?=\s*["']([a-z0-9_]+)["']/gm;
  for (const source of Object.values(BACKEND_SOURCES)) {
    for (const match of source.matchAll(constantPattern)) {
      constants.set(match[1], match[2]);
    }
  }

  // Matches `.broadcast("type_string"` / `.broadcast('type_string'` and
  // `.broadcast(CONSTANT_NAME`, including multi-line calls
  // (a `broadcast(` whose first argument sits on the next line).
  const pattern = /\.broadcast\(\s*(?:["']([a-z0-9_]+)["']|([A-Za-z_][A-Za-z0-9_]*))/g;
  for (const [file, source] of Object.entries(BACKEND_SOURCES)) {
    for (const match of source.matchAll(pattern)) {
      const type = match[1] ?? (match[2] ? constants.get(match[2]) : undefined);
      // An unresolved identifier is a local variable holding a runtime value,
      // not a frame name; skipping it is correct, and the arity/coverage
      // assertions below still fail loudly if the extraction rots wholesale.
      if (!type) continue;
      const files = types.get(type) ?? [];
      if (!files.includes(file)) files.push(file);
      types.set(type, files);
    }
  }
  return types;
}

describe('WS message contract (backend broadcast types vs frontend union)', () => {
  const backendTypes = backendBroadcastTypes();

  it('extraction actually found the backend broadcast call sites (regex not rotted)', () => {
    // If the backend refactors broadcast() such that the regex finds nothing,
    // this test must fail rather than vacuously pass.
    expect(
      backendTypes.size,
      'No broadcast("...") literals found under backend/app - the extraction ' +
        'regex in websocket.test.ts no longer matches the backend source. ' +
        'Fix the extraction, do not delete this test.'
    ).toBeGreaterThanOrEqual(10);
    expect([...backendTypes.keys()]).toContain('pipeline_run_status');
    expect([...backendTypes.keys()]).toContain('step_update');
  });

  it('FRONTEND DRIFTED if this fails: every backend-emitted type is handled by the frontend', () => {
    for (const [type, files] of backendTypes) {
      expect(
        HANDLED_MESSAGE_TYPES,
        `Backend broadcasts "${type}" (${files.join(', ')}) but the frontend ` +
          `union/switch in src/lib/stores/websocket.ts does not handle it. ` +
          `Add it to ServerMessageType, HANDLED_MESSAGE_TYPES and handleServerMessage.`
      ).toContain(type);
    }
  });

  it('BACKEND DRIFTED if this fails: frontend does not claim types the backend never emits', () => {
    // step_log_batch is the agreed 12.2-INT batched-log frame; tolerate the
    // frontend handling it ahead of the backend emitting it (and vice versa
    // for the single-line step_log fallback).
    const tolerated = new Set(['step_log_batch', 'step_log']);
    for (const type of HANDLED_MESSAGE_TYPES) {
      if (tolerated.has(type)) continue;
      expect(
        [...backendTypes.keys()],
        `Frontend handles "${type}" but no backend/app source broadcasts it. ` +
          `Either the backend removed/renamed the frame (update websocket.ts) ` +
          `or the extraction regex missed a call site (update websocket.test.ts).`
      ).toContain(type);
    }
  });
});

// -----------------------------------------------------------------------------
// Dispatch behavior for the live-execution frames
// -----------------------------------------------------------------------------

function makeRun(overrides: Partial<PipelineRun> = {}): PipelineRun {
  return {
    id: 'run-1',
    pipeline_id: 'pipe-1',
    status: 'running',
    trigger_type: 'manual',
    trigger_ref: null,
    trigger_context: null,
    current_step: 0,
    steps_completed: 0,
    steps_total: 2,
    started_at: null,
    completed_at: null,
    created_at: '2026-01-01T00:00:00Z',
    step_runs: [],
    ...overrides,
  };
}

function makeStepRun(overrides: Partial<StepRun> = {}): StepRun {
  return {
    id: 'sr-1',
    pipeline_run_id: 'run-1',
    step_index: 0,
    step_name: 'Step 1',
    status: 'running',
    job_id: null,
    executor: 'local',
    logs: '',
    error: null,
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  activeRunsStore.clear();
  liveStepLogsStore.clear();
});

describe('handleServerMessage: step_update', () => {
  it('updates the matching step status on the run', () => {
    activeRunsStore.addRun(makeRun({ step_runs: [makeStepRun()] }));

    handleServerMessage({
      type: 'step_update',
      payload: { pipeline_run_id: 'run-1', step_index: 0, status: 'passed' },
    });

    expect(get(activeRunsStore).get('run-1')?.step_runs[0].status).toBe('passed');
  });

  it('ignores non-RunStatus executor statuses like "preparing"', () => {
    activeRunsStore.addRun(makeRun({ step_runs: [makeStepRun({ status: 'running' })] }));

    handleServerMessage({
      type: 'step_update',
      payload: { pipeline_run_id: 'run-1', step_index: 0, status: 'preparing' },
    });

    expect(get(activeRunsStore).get('run-1')?.step_runs[0].status).toBe('running');
  });

  it('accepts the alternate run_id payload key', () => {
    activeRunsStore.addRun(makeRun({ step_runs: [makeStepRun()] }));

    handleServerMessage({
      type: 'step_update',
      payload: { run_id: 'run-1', step_index: 0, status: 'failed' },
    });

    expect(get(activeRunsStore).get('run-1')?.step_runs[0].status).toBe('failed');
  });
});

describe('handleServerMessage: step_run_status', () => {
  it('inserts a new step_run sorted by index and merges over an existing one', () => {
    activeRunsStore.addRun(makeRun({ step_runs: [makeStepRun({ step_index: 1, id: 'sr-2' })] }));

    handleServerMessage({
      type: 'step_run_status',
      payload: makeStepRun({ step_index: 0, id: 'sr-1' }),
    });
    let steps = get(activeRunsStore).get('run-1')!.step_runs;
    expect(steps.map(s => s.step_index)).toEqual([0, 1]);

    // A later frame for the same index merges (WS step dicts omit logs, which
    // must survive).
    activeRunsStore.updateStepRun(makeStepRun({ step_index: 0, logs: 'kept' }));
    // Real WS step dicts simply omit the logs key (JSON has no undefined).
    const wsStepDict: Partial<StepRun> = makeStepRun({ step_index: 0, status: 'passed' });
    delete wsStepDict.logs;
    handleServerMessage({ type: 'step_run_status', payload: wsStepDict });
    steps = get(activeRunsStore).get('run-1')!.step_runs;
    expect(steps[0].status).toBe('passed');
    expect(steps[0].logs).toBe('kept');
  });
});

describe('handleServerMessage: step_log / step_log_batch', () => {
  it('step_log appends a single live line', () => {
    handleServerMessage({
      type: 'step_log',
      payload: { pipeline_run_id: 'run-1', step_index: 0, line: 'hello' },
    });
    handleServerMessage({
      type: 'step_log',
      payload: { pipeline_run_id: 'run-1', step_index: 0, line: 'world' },
    });

    expect(get(liveStepLogsStore).get(stepLogKey('run-1', 0))).toEqual(['hello', 'world']);
  });

  it('step_log_batch appends all lines in order after existing ones', () => {
    handleServerMessage({
      type: 'step_log',
      payload: { pipeline_run_id: 'run-1', step_index: 2, line: 'first' },
    });
    handleServerMessage({
      type: 'step_log_batch',
      payload: { pipeline_run_id: 'run-1', step_index: 2, lines: ['a', 'b', 'c'] },
    });

    expect(get(liveStepLogsStore).get(stepLogKey('run-1', 2))).toEqual(['first', 'a', 'b', 'c']);
  });

  it('keys lines per run and step independently', () => {
    handleServerMessage({
      type: 'step_log_batch',
      payload: { pipeline_run_id: 'run-1', step_index: 0, lines: ['r1s0'] },
    });
    handleServerMessage({
      type: 'step_log_batch',
      payload: { run_id: 'run-2', step_index: 0, lines: ['r2s0'] },
    });

    const map = get(liveStepLogsStore);
    expect(map.get(stepLogKey('run-1', 0))).toEqual(['r1s0']);
    expect(map.get(stepLogKey('run-2', 0))).toEqual(['r2s0']);
  });
});

describe('handleServerMessage: pipeline_run_status preserves step state', () => {
  it('a run frame without step_runs does not wipe known steps', () => {
    activeRunsStore.addRun(makeRun({ step_runs: [makeStepRun()] }));

    const { step_runs: _dropped, ...bare } = makeRun({ status: 'passed', steps_completed: 2 });
    handleServerMessage({ type: 'pipeline_run_status', payload: bare });

    const run = get(activeRunsStore).get('run-1')!;
    expect(run.status).toBe('passed');
    expect(run.steps_completed).toBe(2);
    expect(run.step_runs).toHaveLength(1);
  });
});
