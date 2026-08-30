import { derived, get, writable } from 'svelte/store';
import type {
  Card,
  Pipeline,
  PipelineRun,
  StepRun,
  Repo,
  Runner,
  ExperimentStatusFrame,
  ExperimentCellFrame,
  DebugSessionInfo,
  ModelEndpointStatusFrame,
} from '../api/types';
import { cardsStore } from './cards';
import { experimentsStore } from './experiments';
import { debugSessionsStore } from './debug';
import { jobsStore, type JobStatusUpdate } from './jobs';
import { pipelinesStore, activeRunsStore, liveStepLogsStore } from './pipelines';
import { reposStore, selectedRepoId } from './repos';
import { runnersStore } from './runners';
import { endpointsStore } from './endpoints';

export type WebSocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

// -----------------------------------------------------------------------------
// Server -> client message contract.
//
// EVERY message type the backend can broadcast (see
// backend/app/services/websocket.py ConnectionManager) must appear in
// ServerMessageType AND in the handleServerMessage switch below.
// websocket.test.ts greps the backend source and fails loudly if the two
// sides drift, so add new frames in both places.
// -----------------------------------------------------------------------------

export type ServerMessageType =
  | 'card_updated'
  | 'card_deleted'
  | 'job_status'
  | 'runner_status'
  | 'pipeline_updated'
  | 'pipeline_deleted'
  | 'pipeline_run_status'
  | 'step_run_status'
  | 'step_update'
  | 'step_log'
  | 'step_log_batch'
  | 'repo_created'
  | 'repo_updated'
  | 'repo_deleted'
  | 'experiment_status'
  | 'experiment_cell_status'
  | 'debug_session_status'
  | 'model_endpoint_status';

/**
 * The full set of server message types the frontend handles. Exported for the
 * backend/frontend contract test (websocket.test.ts) — keep in lockstep with
 * ServerMessageType and the switch in handleServerMessage.
 */
export const HANDLED_MESSAGE_TYPES: readonly ServerMessageType[] = [
  'card_updated',
  'card_deleted',
  'job_status',
  'runner_status',
  'pipeline_updated',
  'pipeline_deleted',
  'pipeline_run_status',
  'step_run_status',
  'step_update',
  'step_log',
  'step_log_batch',
  'repo_created',
  'repo_updated',
  'repo_deleted',
  'experiment_status',
  'experiment_cell_status',
  'debug_session_status',
  'model_endpoint_status',
];

/** step_update payload: a bare status transition for one step of a run. */
export interface StepUpdatePayload {
  pipeline_run_id: string;
  step_index: number;
  status: string;
}

/** step_log payload: a single live log line from a running local step. */
export interface StepLogPayload {
  pipeline_run_id: string;
  step_index: number;
  line: string;
}

/**
 * step_log_batch payload: multiple live log lines coalesced into one frame
 * (the local executor batches bursty output instead of one broadcast per
 * line).
 */
export interface StepLogBatchPayload {
  pipeline_run_id: string;
  step_index: number;
  lines: string[];
}

export interface WebSocketMessage {
  type: ServerMessageType;
  payload: unknown;
}

/**
 * Some step frames were specified with a `run_id` payload key while the
 * backend's typed publish API emits `pipeline_run_id`. Accept both so a
 * backend-side rename cannot silently drop live updates.
 */
function runIdOf(payload: { pipeline_run_id?: string; run_id?: string }): string | undefined {
  return payload.pipeline_run_id ?? payload.run_id;
}

/**
 * Dispatch one server message into the client stores. Exported (rather than
 * closed over in the store) so unit tests can drive the full contract without
 * a live socket.
 */
export function handleServerMessage(message: WebSocketMessage) {
  switch (message.type) {
    case 'card_updated':
      cardsStore.updateLocal(message.payload as Card);
      break;
    case 'card_deleted':
      cardsStore.deleteLocal((message.payload as { id: string }).id);
      break;
    case 'job_status':
      jobsStore.updateFromWebSocket(message.payload as JobStatusUpdate);
      break;
    case 'runner_status':
      // 12.6: the runner panel is snapshot-then-delta, so this frame is the
      // ONLY live update path - the 2000ms polls it used to sit behind are
      // gone. The payload is one full runner projection (the same shape a
      // row of GET /api/runners carries), not a patch.
      runnersStore.applyDelta(message.payload as Runner);
      break;
    case 'pipeline_updated':
      pipelinesStore.updateLocal(message.payload as Pipeline);
      break;
    case 'pipeline_deleted':
      pipelinesStore.deleteLocal((message.payload as { id: string }).id);
      break;
    case 'pipeline_run_status':
      activeRunsStore.updateRun(message.payload as PipelineRun);
      break;
    case 'step_run_status':
      // Full StepRun snapshot (sans logs) — merge into the owning run.
      activeRunsStore.updateStepRun(message.payload as StepRun);
      break;
    case 'step_update': {
      const p = message.payload as StepUpdatePayload & { run_id?: string };
      const runId = runIdOf(p);
      if (runId !== undefined) {
        activeRunsStore.updateStepStatus(runId, p.step_index, p.status);
      }
      break;
    }
    case 'step_log': {
      const p = message.payload as StepLogPayload & { run_id?: string };
      const runId = runIdOf(p);
      if (runId !== undefined) {
        liveStepLogsStore.appendLine(runId, p.step_index, p.line);
      }
      break;
    }
    case 'step_log_batch': {
      const p = message.payload as StepLogBatchPayload & { run_id?: string };
      const runId = runIdOf(p);
      if (runId !== undefined && Array.isArray(p.lines)) {
        liveStepLogsStore.appendLines(runId, p.step_index, p.lines);
      }
      break;
    }
    case 'repo_created':
    case 'repo_updated':
      reposStore.updateLocal(message.payload as Repo);
      break;
    case 'repo_deleted':
      reposStore.deleteLocal((message.payload as { id: string }).id);
      break;
    case 'experiment_status':
      // 12.6.5: a PROGRESS DELTA, not a full experiment row — it carries no
      // matrix, no verify block and no cells. The store merges it into the
      // known row for exactly that reason.
      experimentsStore.applyStatusFrame(message.payload as ExperimentStatusFrame);
      break;
    case 'experiment_cell_status':
      experimentsStore.applyCellFrame(message.payload as ExperimentCellFrame);
      break;
    case 'debug_session_status':
      // 12.7: a FULL projection, the same body GET /api/debug/{id} serves,
      // so the store replaces wholesale rather than reconciling fields.
      // Without this case the panel only ever showed its onMount snapshot -
      // and a session parked at a breakpoint broadcasts nothing else, so a
      // pause that began after the panel mounted stayed invisible.
      debugSessionsStore.applyDelta(message.payload as DebugSessionInfo);
      break;
    case 'model_endpoint_status':
      // M14 cross-agent contract #10. The envelope is
      // `{id, endpoint}` where `endpoint` is the SAME projection
      // `GET /api/model-endpoints` returns (schemas.model_endpoint.
      // endpoint_read), so a page hydrated by the snapshot and a page
      // updated by this frame cannot show different fields.
      //
      // `endpoint: null` means DELETED, which is a different fact from
      // `enabled: false` - the store drops the former and keeps (and greys)
      // the latter, because a disabled endpoint is a deliberate operator
      // state that has to stay visible and re-enableable.
      endpointsStore.applyDelta(message.payload as ModelEndpointStatusFrame);
      break;
  }
}

// -----------------------------------------------------------------------------
// SNAPSHOT-ON-RECONNECT (QA triage T7)
//
// Every store fed by this socket is delta-only once it has loaded. A delta
// broadcast while the socket is down is not queued anywhere - it is GONE. So a
// board that survives an outage without refetching is not "mostly right", it
// is permanently wrong by an unknown amount, and only F5 fixes it. That was
// the reported behaviour: a card edited during a 30s backend restart kept its
// old title and old column forever.
//
// `stores/runners.ts` has documented the contract since 12.6 - load() is
// called "on mount AND AFTER A SOCKET RECONNECT" - and only the mount half was
// ever wired. This is the other half, for every store rather than just runners.
//
// The dependency object is a real seam (standing rule R6): the tests drive
// `snapshotTargets` with fakes and assert what actually gets called, instead of
// asserting on a list of names that could drift away from the behaviour.
// -----------------------------------------------------------------------------

/** One refetch to perform on reconnect. `name` is how a failure is reported. */
export interface ResyncTarget {
  name: string;
  run: () => unknown;
}

export interface ResyncOutcome {
  name: string;
  ok: boolean;
  error?: unknown;
}

/** The stores a reconnect has to re-snapshot. Injectable so tests can watch it. */
export interface SnapshotDeps {
  repos: { load: () => unknown };
  cards: { load: (repoId: string) => unknown };
  pipelines: { load: (repoId: string) => unknown };
  runs: { loadRecent: (limit?: number) => unknown };
  runners: { load: () => unknown };
  debugSessions: { load: () => unknown };
  modelEndpoints: { load: () => unknown };
  /** Read at resync time, not at module load: the user may have switched repos. */
  currentRepoId: () => string | null;
}

export const defaultSnapshotDeps: SnapshotDeps = {
  repos: reposStore,
  cards: cardsStore,
  pipelines: pipelinesStore,
  runs: activeRunsStore,
  runners: runnersStore,
  debugSessions: debugSessionsStore,
  modelEndpoints: endpointsStore,
  currentRepoId: () => get(selectedRepoId),
};

/**
 * What to refetch after a gap. Repo-scoped stores are included only when a
 * repo is actually selected - loading cards for `null` would be a request the
 * backend rejects and the board would render the rejection as an error.
 */
export function snapshotTargets(deps: SnapshotDeps = defaultSnapshotDeps): ResyncTarget[] {
  const targets: ResyncTarget[] = [
    { name: 'repos', run: () => deps.repos.load() },
    { name: 'runners', run: () => deps.runners.load() },
    { name: 'pipeline-runs', run: () => deps.runs.loadRecent() },
    { name: 'debug-sessions', run: () => deps.debugSessions.load() },
    { name: 'model-endpoints', run: () => deps.modelEndpoints.load() },
  ];

  const repoId = deps.currentRepoId();
  if (repoId) {
    targets.push({ name: 'cards', run: () => deps.cards.load(repoId) });
    targets.push({ name: 'pipelines', run: () => deps.pipelines.load(repoId) });
  }

  return targets;
}

/**
 * Run every snapshot refetch, independently.
 *
 * `allSettled`, not `all`: one endpoint still failing after a restart must not
 * stop the other five from recovering. Failures are RETURNED rather than
 * swallowed, so a resync that half-worked cannot report itself as a resync
 * that worked (standing rule R1).
 */
export async function resyncAll(
  targets: ResyncTarget[] = snapshotTargets(),
): Promise<ResyncOutcome[]> {
  const settled = await Promise.allSettled(
    targets.map((t) => Promise.resolve().then(() => t.run())),
  );
  return settled.map((result, i) =>
    result.status === 'fulfilled'
      ? { name: targets[i].name, ok: true }
      : { name: targets[i].name, ok: false, error: result.reason },
  );
}

/** How long to wait before dialling again after a close. */
export const RECONNECT_DELAY_MS = 3000;

function createWebSocketStore() {
  const status = writable<WebSocketStatus>('disconnected');
  /** Epoch ms of the last successful open. null until the socket has ever been up. */
  const lastConnectedAt = writable<number | null>(null);
  /** Epoch ms of the last completed snapshot refetch. "The board is in sync as of...". */
  const lastSyncedAt = writable<number | null>(null);
  /** Consecutive drops since the last successful open. Drives "attempt N" in the UI. */
  const reconnectAttempts = writable(0);
  /** True while a reconnect snapshot is in flight. */
  const resyncing = writable(false);

  let ws: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
  /**
   * Whether this socket has EVER been open. The first open needs no resync -
   * components fetch their own snapshots on mount - so resyncing there would
   * double every request on page load. Every subsequent open follows a gap.
   */
  let hasConnected = false;

  async function resync(): Promise<ResyncOutcome[]> {
    resyncing.set(true);
    try {
      const outcomes = await resyncAll();
      lastSyncedAt.set(Date.now());
      for (const outcome of outcomes) {
        if (!outcome.ok) {
          console.error(`Reconnect resync failed for ${outcome.name}:`, outcome.error);
        }
      }
      return outcomes;
    } finally {
      resyncing.set(false);
    }
  }

  function connect() {
    // CONNECTING counts: a manual retry racing the reconnect timer used to
    // open a second socket and leak the first.
    if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;

    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }

    status.set('connecting');

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      status.set('connected');
      lastConnectedAt.set(Date.now());
      reconnectAttempts.set(0);
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      if (hasConnected) {
        // A GAP just ended. Deltas broadcast while we were away were never
        // received and cannot be replayed; only a snapshot closes it.
        void resync();
      } else {
        hasConnected = true;
        lastSyncedAt.set(Date.now());
      }
    };

    ws.onmessage = (event) => {
      try {
        const message: WebSocketMessage = JSON.parse(event.data);
        handleServerMessage(message);
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onerror = () => {
      status.set('error');
    };

    ws.onclose = () => {
      status.set('disconnected');
      ws = null;
      reconnectAttempts.update((n) => n + 1);
      reconnectTimeout = setTimeout(connect, RECONNECT_DELAY_MS);
    };
  }

  function disconnect() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    if (ws) {
      // Drop the handlers first: close() fires onclose, which would otherwise
      // schedule the very reconnect this call exists to cancel.
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    status.set('disconnected');
  }

  /** "Try now" from the offline banner: dial immediately, do not wait out the timer. */
  function retryNow() {
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    connect();
  }

  return {
    status: { subscribe: status.subscribe },
    lastConnectedAt: { subscribe: lastConnectedAt.subscribe },
    lastSyncedAt: { subscribe: lastSyncedAt.subscribe },
    reconnectAttempts: { subscribe: reconnectAttempts.subscribe },
    resyncing: { subscribe: resyncing.subscribe },
    connect,
    disconnect,
    retryNow,
    resync,
  };
}

export const websocketStore = createWebSocketStore();

/**
 * The one thing the UI branches on: is what you are looking at actually live?
 *
 * `connecting` is deliberately NOT live. The first moments of a page load sit
 * in `connecting`, and claiming "live" there is the same lie as showing a
 * stale board through an outage.
 */
export const isConnected = derived(websocketStore.status, ($status) => $status === 'connected');
