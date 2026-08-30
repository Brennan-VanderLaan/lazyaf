import { writable } from 'svelte/store';
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
} from '../api/types';
import { cardsStore } from './cards';
import { experimentsStore } from './experiments';
import { debugSessionsStore } from './debug';
import { jobsStore, type JobStatusUpdate } from './jobs';
import { pipelinesStore, activeRunsStore, liveStepLogsStore } from './pipelines';
import { reposStore } from './repos';
import { runnersStore } from './runners';

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
  | 'debug_session_status';

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
  }
}

function createWebSocketStore() {
  const status = writable<WebSocketStatus>('disconnected');
  let ws: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (ws?.readyState === WebSocket.OPEN) return;

    status.set('connecting');

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      status.set('connected');
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
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
      // Reconnect after 3 seconds
      reconnectTimeout = setTimeout(connect, 3000);
    };
  }

  function disconnect() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    if (ws) {
      ws.close();
      ws = null;
    }
    status.set('disconnected');
  }

  return {
    status: { subscribe: status.subscribe },
    connect,
    disconnect,
  };
}

export const websocketStore = createWebSocketStore();
