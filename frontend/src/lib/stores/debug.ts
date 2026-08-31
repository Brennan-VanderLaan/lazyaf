import { derived, writable, type Readable } from 'svelte/store';
import type {
  DebugBreakpointOption,
  DebugSessionInfo,
  DebugState,
  Pipeline,
  PipelineGraphModel,
  PipelineV2,
  StepRunV2,
} from '../api/types';
import { debug as debugApi } from '../api/client';
import { graphStepOrder } from '../components/graph/order';
import { parseTimestamp } from '../utils/time';

/**
 * Debug Re-Run store - Phase 12.7.
 *
 * SNAPSHOT FETCH + WEBSOCKET DELTAS, the same shape as the runner panel and
 * for the same reason: `debug_session_status` frames only arrive when
 * something CHANGES, and a session parked at a breakpoint changes nothing for
 * up to four hours. Without the snapshot half a page reload during a pause
 * shows an empty panel over a wedged pipeline - the single most confusing
 * state this feature can produce.
 *
 * Two departures from the runner store, both deliberate:
 *
 *  - A TERMINAL SESSION IS KEPT, not evicted. `end_reason` is the whole
 *    point of R1 here ("timed out at breakpoint", "aborted by user"): the
 *    operator needs to read WHY the pause ended, and evicting the row the
 *    instant it ends would replace that sentence with an empty panel.
 *    `GET /api/debug` lists non-terminal sessions only, so a terminal row
 *    survives until reload and no further - stated, not hidden.
 *
 *  - The countdown is driven by a LOCAL clock tick (`debugNow`), not by
 *    polling. `expires_at` is authoritative and absolute; re-fetching it once
 *    a second to render a number the browser can subtract itself would be
 *    latency paid for nothing.
 */

/** States in which the session is over and only `end_reason` still matters. */
const TERMINAL_STATES: readonly DebugState[] = ['timeout', 'ended'];

/** States in which the run is stopped at a gate, waiting on a human. */
const PAUSED_STATES: readonly DebugState[] = ['waiting_at_bp', 'connected'];

export function isTerminalDebugState(status: DebugState): boolean {
  return TERMINAL_STATES.includes(status);
}

/** True while a step is held at a breakpoint (whether or not anyone attached). */
export function isPausedDebugState(status: DebugState): boolean {
  return PAUSED_STATES.includes(status);
}

/** Human label for a session state. `pending` is the subtle one. */
export function debugStateLabel(status: DebugState): string {
  switch (status) {
    case 'pending':
      return 'Running';
    case 'waiting_at_bp':
      return 'Paused at breakpoint';
    case 'connected':
      return 'Attached';
    case 'timeout':
      return 'Timed out';
    case 'ended':
      return 'Ended';
    default:
      return status;
  }
}

// -----------------------------------------------------------------------------
// Breakpoint identity - the client half of ONE resolver
// -----------------------------------------------------------------------------

/**
 * The breakpoint identity of a step run, mirroring the backend's
 * `debug_step_key(step_run) = step_run.step_id or str(step_run.step_index)`.
 *
 * Exported so the panel can match a live `StepRun` against the session's
 * breakpoint lists without re-deriving the rule inline.
 *
 * THE INDEX FALLBACK SURVIVES 12.8 and is not dead code: every step the
 * executor dispatches now carries `step_id`, but `pipeline_executor` still
 * writes one StepRun with `step_id = null` - the graph-defect row
 * (`step_name = "pipeline graph"`) it creates when a definition cannot be
 * executed at all. That row has no step to be identified by, so its key is
 * its index, and dropping the fallback would key it `"null"`.
 */
export function debugStepKey(stepRun: Pick<StepRunV2, 'step_id' | 'step_index'>): string {
  return stepRun.step_id || String(stepRun.step_index);
}

/**
 * The selectable breakpoints of a pipeline, in display order.
 *
 * The key is the graph step id, which is exactly what the executor stamps
 * onto `StepRun.step_id`. There is no second keying rule: 12.8 retired the
 * v1 array, and with it the index-keyed branch this function used to carry
 * for it. That branch existed because a v1 `PipelineStepConfig.id` is a
 * context-directory reference the executor never wrote to `StepRun.step_id`,
 * so a v1 pipeline had to be keyed positionally.
 *
 * An empty list means the pipeline has no step to break on - NOT a
 * fallback. There is deliberately nothing to fall back TO: a pipeline whose
 * `steps_graph` is missing or empty has no executable definition, and
 * inventing index keys for it would hand the backend breakpoints that no
 * gate can ever match - a breakpoint that never fires, with no error
 * anywhere. That is the exact trap the "one resolver" rule exists to close.
 *
 * `index` is display position only; the KEY is the identity.
 */
export function debugBreakpointOptions(pipeline: Pipeline | PipelineV2): DebugBreakpointOption[] {
  const graph = (pipeline as PipelineV2).steps_graph as PipelineGraphModel | null | undefined;
  if (!graph || !graph.steps) return [];

  return graphStepOrder(graph).map((stepId, position) => {
    const step = graph.steps[stepId];
    return {
      key: stepId,
      name: step?.name ?? stepId,
      type: step?.type ?? 'script',
      index: position,
    };
  });
}

// -----------------------------------------------------------------------------
// Countdown helpers
// -----------------------------------------------------------------------------

/**
 * Milliseconds left before `expiresAt`, floored at 0. null when unbounded.
 *
 * Parsed through the shared helper: `expires_at` is a backend timestamp, and
 * `new Date(naiveUtcString)` read it as browser-local - so a four-hour session
 * on a UTC-4 laptop counted down from eight, or showed 0:00 while the gate was
 * still open (QA triage T1).
 */
export function remainingMs(expiresAt: string | null, now: number = Date.now()): number | null {
  if (!expiresAt) return null;
  const deadline = parseTimestamp(expiresAt);
  if (deadline === null) return null;
  return Math.max(0, deadline - now);
}

/** `12:05` / `1:02:05`. Bare minutes:seconds under an hour. */
export function formatCountdown(ms: number): string {
  const total = Math.floor(ms / 1000);
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  const pad = (n: number) => String(n).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/**
 * A 1Hz clock, live only while something subscribes. Components read it to
 * re-render a countdown; nothing else in the app pays for the interval.
 */
export const debugNow: Readable<number> = readableClock(1000);

function readableClock(intervalMs: number): Readable<number> {
  const { subscribe } = writable<number>(Date.now(), (set) => {
    const handle = setInterval(() => set(Date.now()), intervalMs);
    return () => clearInterval(handle);
  });
  return { subscribe };
}

// -----------------------------------------------------------------------------
// Store
// -----------------------------------------------------------------------------

function createDebugSessionsStore() {
  const byId = new Map<string, DebugSessionInfo>();
  const { subscribe, set } = writable<DebugSessionInfo[]>([]);
  const loading = writable(false);
  const error = writable<string | null>(null);
  /** True once a snapshot has landed - lets a panel tell "none" from "not asked yet". */
  const loaded = writable(false);

  function publish() {
    // id-ordered: a panel must not reshuffle when a delta lands.
    set([...byId.values()].sort((a, b) => a.id.localeCompare(b.id)));
  }

  /**
   * Accept one session projection. A row without `pipeline_run_id` cannot be
   * attributed to a run, so it is REFUSED with a named error rather than
   * indexed under `undefined` - a session silently missing from the run's
   * panel is indistinguishable from no session at all, which is precisely
   * the dark failure R1 forbids.
   */
  function accept(session: DebugSessionInfo | null | undefined): boolean {
    if (!session || !session.id) return false;
    if (!session.pipeline_run_id) {
      error.set(
        `Debug session ${session.id} arrived without pipeline_run_id; it cannot be ` +
          `attributed to a run. This is a backend contract break ` +
          `(backend/app/schemas/debug.py DebugSessionInfo).`
      );
      return false;
    }
    byId.set(session.id, session);
    return true;
  }

  /** Fetch every non-terminal session. Call on mount and after a WS reconnect. */
  async function load() {
    loading.set(true);
    error.set(null);
    try {
      const sessions = await debugApi.list();
      byId.clear();
      for (const session of sessions) accept(session);
      loaded.set(true);
      publish();
    } catch (e) {
      error.set(e instanceof Error ? e.message : 'Failed to load debug sessions');
    } finally {
      loading.set(false);
    }
  }

  /**
   * Merge one `debug_session_status` frame. The payload is a full projection
   * (the same body `GET /api/debug/{id}` serves), so this is a wholesale
   * replacement - there is exactly one shape and no field reconciliation.
   */
  function applyDelta(session: DebugSessionInfo | null | undefined) {
    if (accept(session)) publish();
  }

  /** Re-read one session over HTTP (used after an action, and as WS repair). */
  async function refresh(sessionId: string): Promise<DebugSessionInfo | null> {
    try {
      const session = await debugApi.get(sessionId);
      applyDelta(session);
      return session;
    } catch (e) {
      error.set(e instanceof Error ? e.message : 'Failed to refresh debug session');
      return null;
    }
  }

  /**
   * Start a debug re-run. Returns the create response so the caller can jump
   * straight to the new run; the session row itself arrives via `refresh`
   * (and thereafter over the WS).
   */
  async function startRerun(runId: string, request: Parameters<typeof debugApi.createRerun>[1]) {
    error.set(null);
    const response = await debugApi.createRerun(runId, request);
    await refresh(response.debug_session_id);
    return response;
  }

  /**
   * Continue past the current breakpoint. `clearRemaining` drops the
   * remaining breakpoints, i.e. "run to completion". The session goes back to
   * `pending`, NOT to a terminal state - that is what makes a second
   * breakpoint in the same run possible.
   */
  async function resume(sessionId: string, clearRemaining: boolean = false) {
    error.set(null);
    const response = await debugApi.resume(sessionId, clearRemaining);
    await refresh(sessionId);
    return response;
  }

  /** End the session and cancel its run. Terminal. */
  async function abort(sessionId: string) {
    error.set(null);
    const response = await debugApi.abort(sessionId);
    await refresh(sessionId);
    return response;
  }

  /** Push `expires_at` out. The paused gate re-reads the row and re-arms. */
  async function extend(sessionId: string, additionalMinutes: number = 30) {
    error.set(null);
    const response = await debugApi.extend(sessionId, additionalMinutes);
    await refresh(sessionId);
    return response;
  }

  /** Test/teardown hook: drop everything without touching the network. */
  function reset() {
    byId.clear();
    loaded.set(false);
    error.set(null);
    loading.set(false);
    publish();
  }

  return {
    subscribe,
    loading: { subscribe: loading.subscribe },
    error: { subscribe: error.subscribe },
    loaded: { subscribe: loaded.subscribe },
    load,
    applyDelta,
    refresh,
    startRerun,
    resume,
    abort,
    extend,
    reset,
  };
}

export const debugSessionsStore = createDebugSessionsStore();

/**
 * The session gating one pipeline run, or null.
 *
 * When more than one row somehow claims the same run (the backend enforces a
 * UNIQUE constraint on `pipeline_run_id`, so this is a "cannot happen" that a
 * UI should still survive), the NON-TERMINAL one wins: a live pause outranks
 * a tombstone.
 */
export function debugSessionForRun(runId: string): Readable<DebugSessionInfo | null> {
  return derived(debugSessionsStore, ($sessions) => {
    const matches = $sessions.filter((s) => s.pipeline_run_id === runId);
    if (matches.length === 0) return null;
    return matches.find((s) => !isTerminalDebugState(s.status)) ?? matches[0];
  });
}

/** Every session currently holding a run at a gate. */
export const pausedDebugSessions = derived(debugSessionsStore, ($sessions) =>
  $sessions.filter((s) => isPausedDebugState(s.status))
);
